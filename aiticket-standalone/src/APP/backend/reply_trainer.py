"""
智能回复训练器 — 通过反馈循环让 LLM 模仿用户的回复风格。

架构：
- 使用 KnowledgeHybridIndex (SQLite + Chroma) 存储回复范例
- 每次生成回复时检索相似场景的用户历史回复作为 few-shot 示例
- 定期从反馈数据中提炼风格规则，写入 system prompt
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from kb_hybrid_index import KnowledgeHybridIndex

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
TRAINER_DATA_DIR = BASE_DIR / "data" / "reply_trainer"
TRAINER_DATA_DIR.mkdir(parents=True, exist_ok=True)

FEEDBACK_STATS_FILE = BASE_DIR / "data" / "reply_feedback.json"
STYLE_RULES_FILE = BASE_DIR / "data" / "reply_style_rules.md"

# ── 可进化参数（evolution_core 通过 registry/reply.yaml 管理这些常量）──────────
SEARCH_EXAMPLES_MODIFIED_BOOST = 1.3    # modified 范例得分加权倍数
SEARCH_EXAMPLES_LOW_SCORE_FILTER = 0.3  # 范例检索低分阈值（低于此分过滤）


class ReplyTrainer:
    """回复训练器：收集反馈 → 存储范例 → 检索范例 → 提炼风格规则"""

    def __init__(self):
        self.kb = KnowledgeHybridIndex(
            sqlite_path=TRAINER_DATA_DIR / "reply_examples.db",
            chroma_path=TRAINER_DATA_DIR / "chroma",
            collection_name="reply_examples",
        )
        self._stats = self._load_stats()
        print(f"[ReplyTrainer] 初始化完成, 范例数: {self.kb.count_chunks()}, "
              f"采纳率: {self._stats.get('adoption_rate', 'N/A')}")

    # ── 反馈采集 ──

    def record_feedback(self, issue_key: str, ticket_summary: str,
                        ticket_desc: str, ai_original: str, user_final: str,
                        adopted: bool, reply_method: str = "",
                        issue_type: str = "",
                        is_style_owner: bool = True,
                        module_l2: str = "",
                        user_id: str = "",
                        adoption_tier: str = "") -> int:
        """
        记录一次回复反馈，存入 KB 并更新统计。

        Args:
            is_style_owner: True=风格学习目标用户的回复（用于风格提炼），
                           False=其他人的回复（仅作答案参考）
        """
        content_id = f"reply:{issue_key}"
        embedding_text = f"{ticket_summary} {ticket_desc}"
        # 从工单号前缀推断项目 key（MYPROJECT-61542 → MYPROJECT），防止跨项目样例污染
        project_key = issue_key.split("-")[0].upper() if "-" in issue_key else "_global"

        # 构建存储内容：用户最终回复 + 元数据标记
        content_parts = [user_final]
        if not adopted and ai_original:
            content_parts.append(f"\n\n---\n[AI原始版本]\n{ai_original}")

        # doc_type 区分来源: style_owner（风格+答案）vs answer_only（仅答案）
        doc_type = "style_owner" if is_style_owner else "answer_only"

        item = {
            "content_id": content_id,
            "source_kind": "reply_example",
            "name": issue_key,
            "summary": ticket_summary[:500],
            "source_rel_path": doc_type,
            "citation_label": "adopted" if adopted else "modified",
            "l1_module": reply_method,
            "l2_module": issue_type,
            "module_l2": module_l2,
            "project_key": project_key,
            "doc_type": doc_type,
            "user_id": user_id,
            "keywords": [issue_key, reply_method, issue_type],
        }

        chunk_count = self.kb.add_item(item, embedding_text + "\n\n" + "\n".join(content_parts))

        # 追加到同步日志（供跨机器增量同步使用）
        try:
            import time as _time
            log_path = TRAINER_DATA_DIR / "feedback_log.jsonl"
            log_entry = {
                "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                "issue_key": issue_key,
                "ticket_summary": ticket_summary[:500],
                "ticket_desc": ticket_desc[:1000],
                "ai_original": ai_original[:2000] if ai_original else "",
                "user_final": user_final[:2000] if user_final else "",
                "adopted": adopted,
                "reply_method": reply_method,
                "issue_type": issue_type,
                "is_style_owner": is_style_owner,
                "project_key": project_key,
                "module_l2": module_l2,
                "user_id": user_id,
            }
            with open(log_path, "a", encoding="utf-8") as _f:
                _f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # 更新统计 — 区分冷启动导入 vs 真实反馈
        is_live = bool(ai_original)  # 有 AI 原始版本 = 真实 AI 反馈; 空 = 冷启动导入
        if is_live:
            # 过滤历史导入污染：ai_original 和 user_final 完全一致的条目不计入 live_total
            is_identical = bool(ai_original and user_final and ai_original.strip() == user_final.strip())
            if is_identical:
                self._stats["live_skipped"] = self._stats.get("live_skipped", 0) + 1
            else:
                self._stats["live_total"] = self._stats.get("live_total", 0) + 1
                self._stats["live_adopted"] = self._stats.get("live_adopted", 0) + (1 if adopted else 0)
                self._stats["live_modified"] = self._stats.get("live_modified", 0) + (0 if adopted else 1)
                live_total = self._stats["live_total"]
                self._stats["live_adoption_rate"] = f"{self._stats['live_adopted'] / live_total * 100:.1f}%" if live_total > 0 else "N/A"
        else:
            self._stats["imported"] = self._stats.get("imported", 0) + 1

        # 保留兼容旧统计
        self._stats["total"] = self._stats.get("total", 0) + 1
        self._stats["adopted"] = self._stats.get("adopted", 0) + (1 if adopted else 0)
        self._stats["modified"] = self._stats.get("modified", 0) + (0 if adopted else 1)
        total = self._stats["total"]
        self._stats["adoption_rate"] = f"{self._stats['adopted'] / total * 100:.1f}%" if total > 0 else "N/A"
        self._stats["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # by_project 分桶统计（防跨项目混淆）
        bp = self._stats.setdefault("by_project", {})
        bp.setdefault(project_key, {"total": 0, "adopted": 0, "modified": 0})
        bp[project_key]["total"] += 1
        bp[project_key]["adopted"] += 1 if adopted else 0
        bp[project_key]["modified"] += 0 if adopted else 1

        # by_module 分桶统计
        if module_l2:
            bm = self._stats.setdefault("by_module", {})
            bm.setdefault(module_l2, {"total": 0, "adopted": 0, "modified": 0})
            bm[module_l2]["total"] += 1
            bm[module_l2]["adopted"] += 1 if adopted else 0
            bm[module_l2]["modified"] += 0 if adopted else 1

        self._save_stats()

        tag = "live" if is_live else "imported"
        print(f"[ReplyTrainer] 记录反馈: {issue_key}, 采纳={adopted}, 类型={tag}, 真实反馈={self._stats.get('live_total', 0)}")

        # 自动采集知识到 KB（后台异步，不阻塞）
        try:
            from kb_auto_import import get_auto_import
            _auto_import = get_auto_import()
            if _auto_import and (adopted or (user_final and user_final != (ai_original or ''))):
                _auto_import.extract_and_save(
                    user_final or '',
                    source_context={'type': 'reply', 'ref_id': issue_key or '', 'issue_type': issue_type or ''},
                )
        except Exception:
            pass

        # 差异分析：区分风格修改 vs 产品知识纠正（后台异步，不阻塞）
        if not adopted and ai_original and user_final:
            try:
                from reply_diff_analyzer import analyze_async as _diff_analyze_async
                _diff_analyze_async(
                    ai_original=ai_original,
                    user_final=user_final,
                    issue_key=issue_key or '',
                    ticket_summary=ticket_summary or '',
                    issue_type=issue_type or '',
                )
            except Exception:
                pass

        return chunk_count

    # ── 范例检索 ──

    def search_examples(self, query: str, top_k: int = 5, module: str = "", project_key: str = "") -> List[Dict]:
        """根据工单内容检索最相似场景的用户历史回复。module 非空时优先返回同模块样例，不足时全局补齐。
        Fix 5: 多取一些候选, 给 modified (用户修改过的金样本) 加权 30%, 过滤低分。
        """
        results = self.kb.search(query, top_k=top_k * 2, source_kind="reply_example")

        examples = []
        for hit in results:
            score = hit.get("score", 0)
            if score < SEARCH_EXAMPLES_LOW_SCORE_FILTER:
                continue

            # 从 chunk_text 中提取用户回复（去掉 AI 原始版本部分）
            text = hit.get("chunk_text", "")
            user_reply = text.split("\n\n---\n[AI原始版本]")[0].strip()
            # 去掉开头的 embedding_text 部分（summary + desc）
            # chunk_text 格式: "summary desc\n\n用户回复"
            parts = user_reply.split("\n\n", 1)
            if len(parts) > 1:
                user_reply = parts[1]

            is_modified = hit.get("citation_label") != "adopted"
            examples.append({
                "issue_key": hit.get("name", ""),
                "summary": hit.get("summary", ""),
                "reply": user_reply,
                "adopted": not is_modified,
                "is_modified": is_modified,
                "is_style_owner": hit.get("doc_type") == "style_owner",
                "reply_method": hit.get("l1_module", ""),
                "issue_type": hit.get("l2_module", ""),
                "module_l2": hit.get("module_l2", ""),
                "project_key": hit.get("project_key", "_global"),
                "score": hit.get("score", 0) * (SEARCH_EXAMPLES_MODIFIED_BOOST if is_modified else 1.0),
            })

        # 按加权分数排序, 取 top_k
        examples.sort(key=lambda x: -x["score"])

        # 项目隔离：project_key 非空时，严格过滤为同项目样例（防跨项目风格污染）
        # 规则：同项目优先；不足 top_k 时用 _global 样例补齐；绝不混入其他具名项目
        if project_key and project_key != "_global":
            same_proj = [ex for ex in examples if ex.get("project_key") in (project_key, "_global")]
            other_proj = [ex for ex in examples if ex.get("project_key") not in (project_key, "_global")]
            if same_proj:
                examples = same_proj
                if other_proj:
                    print(f"[ReplyTrainer] 项目隔离: 过滤掉 {len(other_proj)} 条其他项目样例，保留 {len(same_proj)} 条 {project_key}+_global")
            # 若 same_proj 为空（尚无本项目数据），退回全局但打日志
            elif other_proj:
                print(f"[ReplyTrainer] 项目隔离: 无 {project_key} 样例，降级使用全局 {len(examples)} 条")

        # 模块优先：module 非空时，同模块样例优先排前；不足 3 条时用全局补齐
        if module:
            module_examples = [ex for ex in examples if ex.get("module_l2") == module]
            global_examples = [ex for ex in examples if ex.get("module_l2") != module]
            merged = module_examples + global_examples
            if len(module_examples) > 0:
                print(f"[ReplyTrainer] 模块优先: module='{module}' 命中 {len(module_examples)} 条，全局补充 {max(0, top_k - len(module_examples))} 条")
            examples = merged

        _mod_count = sum(1 for ex in examples[:top_k] if ex.get("is_modified"))
        _adp_count = sum(1 for ex in examples[:top_k] if not ex.get("is_modified"))
        print(f"[ReplyTrainer] 搜索范例: {_mod_count}条modified, {_adp_count}条adopted (总候选{len(examples)})")
        return examples[:top_k]

    # ── 风格规则提炼 ──

    def evolve_style_rules(self, llm_call_fn) -> str:
        """
        分析所有反馈数据，调用 LLM 提炼风格规则。

        Args:
            llm_call_fn: LLM 调用函数，签名 fn(prompt: str) -> str
        Returns:
            生成的风格规则文本
        """
        # 直接从 SQLite 加载全量数据（search API 对空查询无效）
        all_chunks = self._load_all_chunks(limit=500)
        if len(all_chunks) < 3:
            return "范例不足（需要至少 3 条），暂无法提炼风格规则。"

        # 分类: 风格用户的回复 vs 其他人的回复 vs AI修改的回复
        owner_examples = []
        other_examples = []
        modified_examples = []
        for ex in all_chunks:
            entry = {"summary": ex.get("summary", ""), "text": ex.get("chunk_text", "")[:500]}
            if ex.get("citation_label") == "modified":
                modified_examples.append(entry)
            elif ex.get("doc_type") == "style_owner":
                owner_examples.append(entry)
            else:
                other_examples.append(entry)

        # 修改样本是最重要的学习信号 — 全部展示给 LLM (不限 10 条上限)
        # 风格用户样本取 15 条, 其他成员取 5 条 (作为参考)
        mod_limit = max(len(modified_examples), 30)  # 修改样本全量展示, 上限 30
        owner_limit = 15
        other_limit = 5

        # 构建分析 prompt
        prompt = f"""你是一个回复风格分析专家。以下是一个技术支持团队的工单回复数据。
目标：提炼「风格用户」的个人回复风格，**特别是从用户修改 AI 回复的模式中学习 AI 应该避免什么和应该怎么写**。

## ★ 用户修改过的 AI 回复 (最重要的学习材料!) — {len(modified_examples)} 条
"""
        # 修改样本放在最前面 + 全量展示, 因为这是最高价值的学习信号
        if modified_examples:
            prompt += "（每条包含用户最终版本和 [AI原始版本] 对比, 请仔细分析每一处修改的意图）\n"
            for i, ex in enumerate(modified_examples[:mod_limit], 1):
                prompt += f"\n### 修改 {i}: {ex['summary']}\n{ex['text']}\n"
        else:
            prompt += "(暂无修改数据, 后续积累后再分析)\n"

        prompt += f"\n## 风格用户的回复（需要学习此人的表达方式）— {len(owner_examples)} 条\n"
        for i, ex in enumerate(owner_examples[:owner_limit], 1):
            prompt += f"\n### 风格 {i}: {ex['summary']}\n{ex['text']}\n"

        if other_examples:
            prompt += f"\n## 团队其他成员的回复（参考答案质量，不学风格）— {len(other_examples)} 条\n"
            for i, ex in enumerate(other_examples[:other_limit], 1):
                prompt += f"\n### 参考 {i}: {ex['summary']}\n{ex['text']}\n"

        # 使用真实反馈统计 (区分导入 vs 真实)
        live_total = self._stats.get('live_total', 0)
        live_adopted = self._stats.get('live_adopted', 0)
        live_modified = self._stats.get('live_modified', 0)
        imported = self._stats.get('imported', 0)

        prompt += f"""
## 统计 (区分真实反馈 vs 冷启动导入)
- 冷启动导入 (历史回复): {imported} 条
- 真实 AI 反馈 (训练器开通后): {live_total} 条
  - 直接采纳: {live_adopted}
  - 修改后采纳: {live_modified}
  - 真实采纳率: {self._stats.get('live_adoption_rate', 'N/A')}

## 任务
请分析以上数据，提炼出这位用户的回复风格规则，输出为 Markdown 格式，包含以下维度：

1. **语气与口吻**: 正式/随意？直接/委婉？用什么样的开头和结尾？
2. **内容结构**: 回复的典型段落结构是什么？是否使用编号列表？
3. **解决方案风格**: 偏向详细步骤还是简要说明？是否给出具体操作路径？
4. **术语偏好**: 使用哪些特定术语？避免哪些表达？
5. **长度偏好**: 典型回复长度范围？

如果有修改数据，还要分析：
6. **修改模式**: 用户通常修改什么？说明 AI 应该避免什么。

输出规则时，每条规则要可执行（"做XXX"而非"注意XXX"）。"""

        rules = llm_call_fn(prompt)

        # 写入文件
        header = f"# 回复风格规则\n\n> 自动生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        header += f"> 基于 {self._stats.get('total', 0)} 条反馈数据分析\n\n"
        full_content = header + rules

        with open(STYLE_RULES_FILE, "w", encoding="utf-8") as f:
            f.write(full_content)

        print(f"[ReplyTrainer] 风格规则已更新: {STYLE_RULES_FILE}")
        return full_content

    def get_style_rules(self) -> str:
        """读取当前风格规则。"""
        if STYLE_RULES_FILE.exists():
            return STYLE_RULES_FILE.read_text(encoding="utf-8")
        return ""

    # ── 统计 ──

    def get_stats(self) -> Dict:
        """返回训练数据统计。"""
        return {
            **self._stats,
            "example_count": self.kb.count_chunks(),
            "style_rules_exists": STYLE_RULES_FILE.exists(),
        }

    # ── 批量导入 ──

    def bulk_import(self, items: List[Dict]) -> int:
        """
        批量导入历史回复数据 (冷启动用)。
        每个 item 需要: issue_key, summary, reply
        可选: description, reply_method, issue_type, is_owner (bool)

        注意: 导入的数据 ai_original="" (无 AI 原始版本), 在统计中会被标记为
        imported (而非 live_adopted), 不影响真实采纳率计算。
        """
        count = 0
        for item in items:
            chunks = self.record_feedback(
                issue_key=item["issue_key"],
                ticket_summary=item.get("summary", ""),
                ticket_desc=item.get("description", ""),
                ai_original="",  # 空 → record_feedback 会归类为 imported
                user_final=item["reply"],
                adopted=True,
                reply_method=item.get("reply_method", ""),
                issue_type=item.get("issue_type", ""),
                is_style_owner=item.get("is_owner", True),
            )
            count += chunks
        return count

    # ── 内部方法 ──

    def _load_all_chunks(self, limit: int = 500) -> List[Dict]:
        """直接从 SQLite 加载范例 chunk 数据（绕过搜索 API）。"""
        try:
            cursor = self.kb.conn.execute(
                """SELECT chunk_id, content_id, chunk_text, chunk_preview,
                          source_kind, name, summary, doc_type, citation_label
                   FROM chunks
                   WHERE source_kind = 'reply_example'
                   ORDER BY RANDOM()
                   LIMIT ?""",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"[ReplyTrainer] 加载全量 chunks 失败: {e}")
            return []

    def _load_stats(self) -> Dict:
        if FEEDBACK_STATS_FILE.exists():
            try:
                with open(FEEDBACK_STATS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"total": 0, "adopted": 0, "modified": 0, "adoption_rate": "N/A"}

    def _save_stats(self):
        with open(FEEDBACK_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._stats, f, ensure_ascii=False, indent=2)
