#!/usr/bin/env python3
"""
KB 知识合并器 — 月度知识合并与可信度重评估工具。

功能：
1. consolidate_topic(topic_name): 语义聚类合并同一话题的冗余 chunks
2. reassess_credibility_all(): 全量重新计算所有文档的 credibility
3. archive_deprecated(): credibility < 0.3 的条目标记为 deprecated
4. generate_health_report(): 生成 KB 健康报告（Markdown）
5. main(): CLI 入口（--full / --topic X / --report-only）

运行: python scripts/kb_consolidator.py --full
"""

import argparse
import hashlib
import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# -- 路径设置 --
SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# -- no_proxy 防止 requests 走代理超时 --
os.environ.setdefault("no_proxy", "localhost,127.0.0.1,0.0.0.0,::1")
os.environ["NO_PROXY"] = os.environ["no_proxy"]

# -- 日志 --
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kb_consolidator")

# -- 常量 --
SQLITE_PATH = PROJECT_ROOT / "data" / "sqlite" / "kb_chunks.db"
CHROMA_PATH = PROJECT_ROOT / "data" / "chroma_kb"
LOG_FILE = BACKEND_DIR / "data" / "kb_consolidation_log.jsonl"
LLM_CONFIG_PATH = BACKEND_DIR / "llm_config.json"

# source_trust 权重表
SOURCE_TRUST = {
    "human_verified":    1.0,   # 2+人工回复验证 = 最高
    "kb_local":          0.9,
    "feedback":          0.8,
    "trainer_high":      0.65,
    "kb_auto_enriched":  0.5,
    # 兜底映射：source_kind → trust
    "apcom_docs":        0.85,
    "kb_compiled":       0.8,
    "user_contributed":  0.75,
    "reply_example":     0.6,
    "ticket_case":       0.55,
}

# 时间衰减半衰期（天）
TIME_DECAY_HALF_LIFE_DAYS = 365


def _load_llm_config() -> dict:
    """读取 llm_config.json，返回当前生效 provider 的配置"""
    try:
        with open(LLM_CONFIG_PATH, encoding="utf-8") as f:
            full = json.load(f)
        provider = full.get("last_provider", "")
        if not provider or provider == "none":
            return {}
        pcfg = full.get(provider, {})
        return {
            "provider": provider,
            "api_key": pcfg.get("api_key", ""),
            "model_name": pcfg.get("model_name", ""),
            "base_url": pcfg.get("base_url", ""),
        }
    except Exception:
        return {}


def _call_llm(prompt: str, llm_config: dict = None) -> str:
    """调用 LLM（复用 llm_service.py 的 call_llm）"""
    try:
        from llm_service import LLMService
        svc = LLMService()
        cfg = llm_config or _load_llm_config()
        if not cfg.get("api_key"):
            logger.warning("无 LLM API Key 配置，无法执行 LLM 调用")
            return ""
        result = svc.call_llm(
            prompt=prompt,
            api_key=cfg.get("api_key"),
            provider=cfg.get("provider", "gemini"),
            model_name=cfg.get("model_name", ""),
            base_url=cfg.get("base_url", ""),
        )
        return (result or "").strip()
    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        return ""


class KBConsolidator:
    """KB 知识合并器"""

    def __init__(self, sqlite_path: Path = SQLITE_PATH):
        self.sqlite_path = Path(sqlite_path)
        if not self.sqlite_path.exists():
            raise FileNotFoundError(f"KB 数据库不存在: {self.sqlite_path}")
        self.conn = sqlite3.connect(str(self.sqlite_path), check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema()
        self.stats = {
            "topics_processed": 0,
            "chunks_merged": 0,
            "docs_reassessed": 0,
            "docs_archived": 0,
            "errors": [],
        }

    def _ensure_schema(self):
        """确保扩展字段存在（Phase 1 schema 扩展的安全补充）"""
        existing_doc_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        existing_chunk_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(chunks)").fetchall()
        }
        # documents 表扩展
        for col, col_type, default in [
            ("credibility", "REAL", "1.0"),
            ("topic", "TEXT", "''"),
            ("merged_from", "TEXT", "''"),
            ("last_validated_at", "TEXT", "''"),
            ("heat_score", "REAL", "0.0"),
            ("created_at", "TEXT", "''"),
        ]:
            if col not in existing_doc_cols:
                self.conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {col_type} DEFAULT {default}")
                logger.info(f"documents 表新增列: {col}")

        # chunks 表扩展
        for col, col_type, default in [
            ("credibility", "REAL", "1.0"),
            ("topic", "TEXT", "''"),
            ("merged_from", "TEXT", "''"),
            ("created_at", "TEXT", "''"),
        ]:
            if col not in existing_chunk_cols:
                self.conn.execute(f"ALTER TABLE chunks ADD COLUMN {col} {col_type} DEFAULT {default}")
                logger.info(f"chunks 表新增列: {col}")

        # 初始化 created_at（首次跑时为空的记录设为当前时间）
        now_str = datetime.now().isoformat()
        self.conn.execute(
            "UPDATE documents SET created_at = ? WHERE created_at IS NULL OR created_at = ''",
            (now_str,),
        )
        self.conn.execute(
            "UPDATE chunks SET created_at = ? WHERE created_at IS NULL OR created_at = ''",
            (now_str,),
        )
        self.conn.commit()

    # ──────────────────────────────────────────────
    # 1. consolidate_topic
    # ──────────────────────────────────────────────
    def consolidate_topic(self, topic_name: str) -> dict:
        """
        对指定 topic 下所有 chunks 进行语义聚类合并。
        返回: {"topic": str, "original_chunks": int, "groups": int, "merged": int}
        """
        logger.info(f"[consolidate] 开始合并话题: {topic_name}")

        # 获取该 topic 相关的 chunks（通过 name/summary/chunk_text 模糊匹配）
        chunks = self._get_topic_chunks(topic_name)
        if len(chunks) < 2:
            logger.info(f"[consolidate] 话题 '{topic_name}' 只有 {len(chunks)} 条 chunk，跳过合并")
            return {"topic": topic_name, "original_chunks": len(chunks), "groups": 0, "merged": 0}

        # 用 LLM 做语义聚类
        groups = self._semantic_cluster(chunks, topic_name)
        if not groups:
            logger.info(f"[consolidate] 话题 '{topic_name}' 聚类结果为空，跳过")
            return {"topic": topic_name, "original_chunks": len(chunks), "groups": 0, "merged": 0}

        # 合并每组 chunks
        merged_count = 0
        for group in groups:
            if len(group) < 2:
                continue
            try:
                self._merge_chunk_group(group, topic_name)
                merged_count += len(group)
            except Exception as e:
                logger.error(f"[consolidate] 合并组失败: {e}")
                self.stats["errors"].append(f"merge_group: {topic_name}: {e}")

        self.stats["topics_processed"] += 1
        self.stats["chunks_merged"] += merged_count

        result = {
            "topic": topic_name,
            "original_chunks": len(chunks),
            "groups": len([g for g in groups if len(g) >= 2]),
            "merged": merged_count,
        }
        logger.info(f"[consolidate] 话题 '{topic_name}' 完成: {result}")
        return result

    def _get_topic_chunks(self, topic_name: str) -> list[dict]:
        """获取与话题相关的所有 chunks"""
        # 先尝试 topic 字段精确匹配
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE topic = ? AND doc_type != 'archived' ORDER BY chunk_index",
            (topic_name,),
        ).fetchall()

        if not rows:
            # 回退到名称模糊匹配
            pattern = f"%{topic_name}%"
            rows = self.conn.execute(
                """SELECT * FROM chunks
                   WHERE (name LIKE ? OR summary LIKE ?)
                   AND doc_type != 'archived'
                   ORDER BY content_id, chunk_index
                   LIMIT 100""",
                (pattern, pattern),
            ).fetchall()

        return [dict(row) for row in rows]

    def _semantic_cluster(self, chunks: list[dict], topic_name: str) -> list[list[dict]]:
        """用 LLM 判断哪些 chunks 表达同一知识点，返回分组"""
        if len(chunks) > 30:
            # 过多时只取前 30 条避免 token 溢出
            chunks = chunks[:30]

        # 构建 LLM prompt
        chunk_summaries = []
        for i, chunk in enumerate(chunks):
            preview = (chunk.get("chunk_text") or chunk.get("chunk_preview") or "")[:200]
            chunk_summaries.append(f"[{i}] {preview}")

        prompt = f"""你是知识库管理专家。以下是关于「{topic_name}」的 {len(chunks)} 条知识碎片。
请判断哪些碎片表达的是完全相同的知识点（内容重复或高度相似），将它们分组。

碎片列表：
{"chr(10)".join(chunk_summaries)}

请用 JSON 数组返回分组结果。每个分组是一个包含碎片编号的数组。
只返回有 2 条及以上的组（单条不需要合并的不要列出）。
如果没有需要合并的碎片，返回空数组 []。

示例输出格式：
[[0, 3, 7], [1, 5]]

注意：只返回纯 JSON，不要其他内容。"""

        response = _call_llm(prompt)
        if not response:
            return []

        # 解析 JSON
        try:
            # 提取 JSON 部分（LLM 可能包裹在 markdown code block 中）
            json_str = response
            if "```" in json_str:
                import re
                m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
                if m:
                    json_str = m.group(1)
            groups_indices = json.loads(json_str.strip())
            if not isinstance(groups_indices, list):
                return []

            # 将索引转为 chunk 字典
            result = []
            for group_idx in groups_indices:
                if not isinstance(group_idx, list) or len(group_idx) < 2:
                    continue
                group = []
                for idx in group_idx:
                    if isinstance(idx, int) and 0 <= idx < len(chunks):
                        group.append(chunks[idx])
                if len(group) >= 2:
                    result.append(group)
            return result

        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[consolidate] LLM 返回非法 JSON: {e}\n{response[:200]}")
            return []

    def _merge_chunk_group(self, group: list[dict], topic_name: str):
        """合并一组 chunks 为一条精炼条目"""
        # 拼接所有 chunk_text
        combined_text = "\n\n---\n\n".join(
            (c.get("chunk_text") or c.get("chunk_preview") or "") for c in group
        )
        chunk_ids = [c["chunk_id"] for c in group]

        # 用 LLM 精炼合并
        prompt = f"""请将以下关于「{topic_name}」的多条重复/相似知识碎片合并为一条精炼的知识条目。
保留所有有价值的信息，去掉重复内容。输出纯文本，不加标题。

碎片内容：
{combined_text[:4000]}"""

        merged_text = _call_llm(prompt)
        if not merged_text or len(merged_text) < 20:
            logger.warning(f"[merge] LLM 精炼结果过短，使用第一条 chunk 作为保留项")
            merged_text = group[0].get("chunk_text") or group[0].get("chunk_preview") or ""

        # 保留第一条 chunk，更新其内容，删除其余
        keeper = group[0]
        keeper_id = keeper["chunk_id"]
        content_id = keeper["content_id"]
        merged_from_json = json.dumps(chunk_ids, ensure_ascii=False)

        # 更新 keeper
        self.conn.execute(
            """UPDATE chunks SET chunk_text = ?, chunk_preview = ?, merged_from = ?, topic = ?
               WHERE chunk_id = ?""",
            (merged_text, merged_text[:240], merged_from_json, topic_name, keeper_id),
        )

        # 更新 FTS（先删后插）
        self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (keeper_id,))
        self.conn.execute(
            """INSERT INTO chunks_fts (chunk_id, name, summary, keywords, source_rel_path, chunk_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (keeper_id, keeper.get("name", ""), keeper.get("summary", ""),
             topic_name, keeper.get("source_rel_path", ""), merged_text),
        )

        # 删除被合并的其他 chunks
        to_delete = [c["chunk_id"] for c in group[1:]]
        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            self.conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", to_delete)
            self.conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", to_delete)

        self.conn.commit()
        logger.info(f"[merge] 合并 {len(group)} 条 → keeper={keeper_id}, merged_from={chunk_ids}")

    # ──────────────────────────────────────────────
    # 2. reassess_credibility_all
    # ──────────────────────────────────────────────
    def reassess_credibility_all(self) -> dict:
        """
        遍历所有 documents，重新计算 credibility:
        credibility = source_trust * time_decay * citation_factor * (1 - contradiction_penalty)
        """
        logger.info("[credibility] 开始全量可信度重评估")
        rows = self.conn.execute(
            "SELECT content_id, source_kind, doc_type, last_validated_at, created_at FROM documents"
        ).fetchall()

        updated = 0
        for row in rows:
            content_id = row["content_id"]
            source_kind = row["source_kind"] or ""
            doc_type = row["doc_type"] or ""

            # 1) source_trust
            source_trust = self._compute_source_trust(source_kind, doc_type)

            # 2) time_decay
            ref_time = row["last_validated_at"] or row["created_at"] or ""
            time_decay = self._compute_time_decay(ref_time)

            # 3) citation_factor (基于 chunks 数量和引用情况)
            citation_factor = self._compute_citation_factor(content_id)

            # 4) contradiction_penalty (简化版：暂无冲突检测机制时为 0)
            contradiction_penalty = 0.0

            credibility = round(
                source_trust * time_decay * citation_factor * (1 - contradiction_penalty),
                4,
            )
            credibility = max(0.0, min(1.0, credibility))

            self.conn.execute(
                "UPDATE documents SET credibility = ? WHERE content_id = ?",
                (credibility, content_id),
            )
            updated += 1

        self.conn.commit()
        self.stats["docs_reassessed"] = updated
        logger.info(f"[credibility] 重评估完成: {updated} 篇文档")
        return {"reassessed": updated}

    def _compute_source_trust(self, source_kind: str, doc_type: str) -> float:
        """根据 source_kind 和 doc_type 确定 source_trust"""
        # 优先匹配 doc_type 特殊标签
        if doc_type == "human_verified":
            return SOURCE_TRUST.get("human_verified", 1.0)
        if doc_type == "trainer_high":
            return SOURCE_TRUST.get("trainer_high", 0.65)
        if doc_type == "feedback":
            return SOURCE_TRUST.get("feedback", 0.8)

        # 再匹配 source_kind
        return SOURCE_TRUST.get(source_kind, 0.7)

    def _compute_time_decay(self, ref_time_str: str) -> float:
        """基于参考时间计算时间衰减因子 (0~1)"""
        if not ref_time_str:
            return 0.7  # 缺失时间的默认衰减

        try:
            # 尝试解析多种格式
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    ref_time = datetime.strptime(ref_time_str[:26], fmt)
                    break
                except ValueError:
                    continue
            else:
                return 0.7

            days_elapsed = (datetime.now() - ref_time).days
            if days_elapsed < 0:
                days_elapsed = 0

            # 指数衰减: decay = 0.5 ^ (days / half_life)
            decay = math.pow(0.5, days_elapsed / TIME_DECAY_HALF_LIFE_DAYS)
            return round(max(0.1, decay), 4)

        except Exception:
            return 0.7

    def _compute_citation_factor(self, content_id: str) -> float:
        """基于 chunk 数量和引用次数计算引用因子"""
        chunk_count = self.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE content_id = ?", (content_id,)
        ).fetchone()[0]

        # 有内容的文档基础分 0.8，每多 2 个 chunk +0.05，上限 1.0
        if chunk_count == 0:
            return 0.5
        return min(1.0, 0.8 + chunk_count * 0.025)

    # ──────────────────────────────────────────────
    # 3. archive_deprecated
    # ──────────────────────────────────────────────
    def archive_deprecated(self, threshold: float = 0.3) -> dict:
        """
        credibility < threshold 的条目标记为 deprecated（不删除，设 doc_type='archived'）
        """
        logger.info(f"[archive] 开始归档低可信度条目 (threshold={threshold})")

        rows = self.conn.execute(
            """SELECT content_id, name, credibility FROM documents
               WHERE credibility < ? AND (doc_type IS NULL OR doc_type != 'archived')""",
            (threshold,),
        ).fetchall()

        archived = 0
        for row in rows:
            self.conn.execute(
                "UPDATE documents SET doc_type = 'archived' WHERE content_id = ?",
                (row["content_id"],),
            )
            self.conn.execute(
                "UPDATE chunks SET doc_type = 'archived' WHERE content_id = ?",
                (row["content_id"],),
            )
            logger.info(f"[archive] 归档: {row['name']} (credibility={row['credibility']:.4f})")
            archived += 1

        self.conn.commit()
        self.stats["docs_archived"] = archived
        logger.info(f"[archive] 归档完成: {archived} 篇")
        return {"archived": archived, "threshold": threshold}

    # ──────────────────────────────────────────────
    # 4. generate_health_report
    # ──────────────────────────────────────────────
    def generate_health_report(self) -> str:
        """生成 KB 健康报告 (Markdown 格式)"""
        logger.info("[report] 生成健康报告")
        now = datetime.now()
        month_ago = (now - timedelta(days=30)).isoformat()

        # 总量统计
        total_docs = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        total_chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        # 分类统计
        verified_count = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE doc_type = 'human_verified'"
        ).fetchone()[0]
        archived_count = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE doc_type = 'archived'"
        ).fetchone()[0]
        normal_count = total_docs - verified_count - archived_count

        # 来源分布
        source_dist = self.conn.execute(
            "SELECT source_kind, COUNT(*) as cnt FROM documents GROUP BY source_kind ORDER BY cnt DESC"
        ).fetchall()

        # 本月新增
        new_this_month = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE created_at > ?", (month_ago,)
        ).fetchone()[0]

        # 本月合并数（从 stats 获取）
        merged_this_run = self.stats.get("chunks_merged", 0)
        archived_this_run = self.stats.get("docs_archived", 0)

        # 热门话题 TOP10（按 heat_score 排序）
        hot_topics = self.conn.execute(
            """SELECT name, heat_score, credibility FROM documents
               WHERE doc_type != 'archived' AND heat_score > 0
               ORDER BY heat_score DESC LIMIT 10"""
        ).fetchall()

        # 低可信度预警
        low_cred = self.conn.execute(
            """SELECT name, credibility FROM documents
               WHERE credibility < 0.5 AND doc_type != 'archived'
               ORDER BY credibility ASC LIMIT 10"""
        ).fetchall()

        # 可信度分布
        cred_high = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE credibility >= 0.8 AND doc_type != 'archived'"
        ).fetchone()[0]
        cred_mid = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE credibility >= 0.5 AND credibility < 0.8 AND doc_type != 'archived'"
        ).fetchone()[0]
        cred_low = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE credibility >= 0.3 AND credibility < 0.5 AND doc_type != 'archived'"
        ).fetchone()[0]
        cred_danger = self.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE credibility < 0.3 AND doc_type != 'archived'"
        ).fetchone()[0]

        # 构建报告
        lines = [
            f"## KB 知识库健康报告 — {now.strftime('%Y-%m-%d %H:%M')}",
            "",
            "### 总量统计",
            f"- 文档总数: **{total_docs}**",
            f"- 碎片总数: **{total_chunks}**",
            f"- verified: {verified_count} | normal: {normal_count} | archived: {archived_count}",
            "",
            "### 本月变动",
            f"- 本月新增: {new_this_month}",
            f"- 本次合并碎片: {merged_this_run}",
            f"- 本次归档: {archived_this_run}",
            "",
            "### 来源分布",
        ]
        for row in source_dist:
            lines.append(f"- {row['source_kind']}: {row['cnt']}")

        lines += [
            "",
            "### 可信度分布",
            f"- 高 (>=0.8): {cred_high}",
            f"- 中 (0.5~0.8): {cred_mid}",
            f"- 低 (0.3~0.5): {cred_low}",
            f"- 危险 (<0.3): {cred_danger}",
        ]

        if hot_topics:
            lines += ["", "### 热门话题 TOP10"]
            for i, row in enumerate(hot_topics, 1):
                lines.append(f"{i}. {row['name']} (heat={row['heat_score']:.1f}, cred={row['credibility']:.2f})")

        if low_cred:
            lines += ["", "### 低可信度预警"]
            for row in low_cred:
                lines.append(f"- {row['name']} (credibility={row['credibility']:.4f})")

        if self.stats.get("errors"):
            lines += ["", "### 错误日志"]
            for err in self.stats["errors"][:10]:
                lines.append(f"- {err}")

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────
    def get_all_topics(self) -> list[str]:
        """获取所有话题名称（从 documents.name 中提取 '综合解析：XXX' 模式）"""
        rows = self.conn.execute(
            """SELECT DISTINCT name FROM documents
               WHERE name LIKE '综合解析：%' AND doc_type != 'archived'"""
        ).fetchall()
        topics = []
        for row in rows:
            name = row["name"]
            if name.startswith("综合解析："):
                topics.append(name[len("综合解析："):])
        if not topics:
            # 回退：从 topic 字段获取
            topic_rows = self.conn.execute(
                "SELECT DISTINCT topic FROM documents WHERE topic IS NOT NULL AND topic != ''"
            ).fetchall()
            topics = [row["topic"] for row in topic_rows]
        return topics

    def run_full(self) -> str:
        """全量模式：遍历所有 topic -> consolidate -> reassess -> archive -> report"""
        logger.info("=" * 60)
        logger.info("KB 知识合并器 — 全量模式启动")
        logger.info("=" * 60)

        start_time = time.time()

        # 1. 获取所有 topics
        topics = self.get_all_topics()
        logger.info(f"发现 {len(topics)} 个话题")

        # 2. 逐话题合并
        consolidation_results = []
        for topic in topics:
            try:
                result = self.consolidate_topic(topic)
                consolidation_results.append(result)
            except Exception as e:
                logger.error(f"话题 '{topic}' 合并失败: {e}")
                self.stats["errors"].append(f"consolidate:{topic}: {e}")

        # 3. 全量可信度重评估
        reassess_result = self.reassess_credibility_all()

        # 4. 归档低可信度条目
        archive_result = self.archive_deprecated()

        # 5. 生成报告
        report = self.generate_health_report()

        elapsed = round(time.time() - start_time, 1)

        # 6. 记录日志
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "mode": "full",
            "elapsed_seconds": elapsed,
            "topics_count": len(topics),
            "consolidation": consolidation_results,
            "reassess": reassess_result,
            "archive": archive_result,
            "stats": self.stats,
        }
        self._write_log(log_entry)

        logger.info(f"全量模式完成，耗时 {elapsed}s")
        return report

    def _write_log(self, entry: dict):
        """追加写入 JSONL 日志"""
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.error(f"写入日志失败: {e}")

    def close(self):
        """关闭数据库连接"""
        try:
            self.conn.close()
        except Exception:
            pass


# ──────────────────────────────────────────────
# 5. main() 入口
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="KB 知识合并器 — 月度知识合并与可信度重评估")
    parser.add_argument("--full", action="store_true", help="全量合并+评估+归档+报告")
    parser.add_argument("--topic", type=str, help="单话题合并")
    parser.add_argument("--report-only", action="store_true", help="仅生成健康报告")
    parser.add_argument("--db", type=str, default=str(SQLITE_PATH), help="KB 数据库路径")
    parser.add_argument("--feishu", action="store_true", help="将报告推送到飞书群")
    args = parser.parse_args()

    consolidator = KBConsolidator(sqlite_path=Path(args.db))

    try:
        if args.report_only:
            report = consolidator.generate_health_report()
            print(report)
            if args.feishu:
                _send_feishu(report)

        elif args.topic:
            result = consolidator.consolidate_topic(args.topic)
            consolidator.reassess_credibility_all()
            report = consolidator.generate_health_report()
            print(report)

            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "mode": "single_topic",
                "topic": args.topic,
                "result": result,
                "stats": consolidator.stats,
            }
            consolidator._write_log(log_entry)

            if args.feishu:
                _send_feishu(report)

        elif args.full:
            report = consolidator.run_full()
            print(report)
            if args.feishu:
                _send_feishu(report)

        else:
            parser.print_help()

    finally:
        consolidator.close()


def _send_feishu(report: str):
    """将报告推送到飞书群"""
    try:
        from services.feishu_notifier import FeishuNotifier
        notifier = FeishuNotifier()
        # 截断过长的报告（飞书消息限制）
        if len(report) > 3000:
            report = report[:2900] + "\n\n...(报告已截断，完整版请查看服务器日志)"
        success = notifier.send_message(report)
        if success:
            logger.info("报告已推送到飞书群")
        else:
            logger.warning("飞书推送失败")
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")


if __name__ == "__main__":
    main()
