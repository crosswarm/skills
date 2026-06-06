from __future__ import annotations

import html
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from kb_hybrid_index import KnowledgeHybridIndex
from kb_local_builder import KBLocalBuilder


SUPPORTED_APCOM_EXTENSIONS = {".md", ".txt", ".docx", ".pptx", ".xlsx", ".csv", ".sql", ".html", ".xml"}
SKIP_DIRS = {".git", ".claude", "node_modules", "__pycache__", "assets", "images", "image", "files", "template"}
STOPWORDS = {
    "以及", "进行", "如果", "这个", "我们", "你们", "他们", "使用", "功能", "流程", "工作流",
    "the", "and", "for", "with", "from", "that", "this", "into", "are",
}

TERM_EXPANSIONS = {
    "连岗审批": ["连岗", "兼岗", "代理审批", "审批代理", "审批委托", "同一处理人自动去重"],
    "代理审批": ["审批代理", "审批委托", "代理人", "委托审批"],
    "代理人": ["代理审批", "审批代理", "审批委托", "委托审批", "代理"],
    "未来审批": ["未来审批流", "流程预测", "候选审批人", "审批链路"],
    # === 2026-04-10 新增：覆盖高频工单主题 ===
    "撤回": ["退回", "撤销", "回退", "取消审批", "流程撤回"],
    "退回": ["撤回", "驳回", "打回", "回退"],
    "流程干预": ["流程调整", "强制跳转", "流程修改", "人工干预", "流程中断"],
    "加签": ["会签", "转签", "协办", "加签审批"],
    "字段权限": ["字段可编辑", "字段只读", "必填属性", "业务活动"],
    "条件分支": ["分支条件", "条件表达式", "规则分支", "分支走向"],
    "流程卡住": ["流程挂起", "节点卡死", "流程阻塞", "审批停滞"],
    "表单": ["单据", "表单设计", "表单配置", "字段设置"],
    "权限": ["授权", "菜单权限", "功能权限"],
    "通知": ["消息通知", "邮件通知", "短信通知", "催办"],
    # === 2026-04-17 新增：补全反向映射 + 高频用户查询词 ===
    "审批人": ["审批节点", "审批环节", "处理人", "审批者"],
    "显示": ["展示", "可见", "隐藏", "不显示", "界面展示"],
    "隐藏": ["不显示", "屏蔽", "不可见", "隐藏设置"],
    "跳过": ["自动跳过", "跳过审批", "自动略过", "审批人跳过"],
    "驳回": ["退回", "打回", "拒绝", "驳回后"],
    "催办": ["催促", "提醒审批", "超时催办", "消息催办"],
    "待办": ["我的待办", "待办列表", "待办中心", "待处理"],
    "超时": ["超期", "过期", "逾期", "超时提醒"],
}

DOMAIN_PHRASES = [
    "规则引擎",
    "流程监控",
    "连岗审批",
    "代理审批",
    "审批委托",
    "未来审批",
    "未来审批流",
    "流程预测",
    "审批面板",
    "流程图",
    "同一处理人自动去重",
    # === 2026-04-10 新增 ===
    "撤回",
    "退回",
    "流程干预",
    "加签",
    "字段权限",
    "业务活动",
    "条件分支",
    "流程卡住",
    "表单设计",
    "消息通知",
]

DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
PPTX_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
XLSX_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_RELS_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}


@dataclass
class TopicNode:
    topic_id: str
    name: str
    level: int
    keywords: list[str]


class KnowledgeRuntimeService:
    def __init__(
        self,
        project_root: Path | None = None,
        kb_root: Path | None = None,
        apcom_root: Path | None = None,
        topic_file: Path | None = None,
        sqlite_path: Path | None = None,
        chroma_path: Path | None = None,
        ticket_chroma_path: Path | None = None,
        llm_service: Any | None = None,
    ) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        # kb_root: 优先使用显式参数，其次读 deployment.yaml kb.root_dir，最后默认 project_root/KB
        if kb_root is not None:
            self.kb_root = Path(kb_root).resolve()
        else:
            try:
                from config.loader import cfg as _cfg
                _cfg_kb_root = _cfg("kb", "root_dir")
                if _cfg_kb_root and _cfg_kb_root != "/data/kb":
                    self.kb_root = Path(_cfg_kb_root).resolve()
                else:
                    self.kb_root = (self.project_root / "KB").resolve()
            except Exception:
                self.kb_root = (self.project_root / "KB").resolve()
        # apcom_root: 优先使用显式参数；硬编码 /Volumes 路径已移除，默认 None（优雅降级）
        self.apcom_root = (
            Path(apcom_root).resolve() if apcom_root else None
        )
        self.topic_file = (topic_file or (self.project_root / "APP" / "backend" / "data" / "topic.md")).resolve()
        self.sqlite_path = (sqlite_path or (self.project_root / "data" / "sqlite" / "kb_chunks.db")).resolve()
        self.chroma_path = (chroma_path or (self.project_root / "data" / "chroma_kb")).resolve()
        self.ticket_chroma_path = (ticket_chroma_path or (self.project_root / "APP" / "backend" / "chroma_db")).resolve()
        self.apcom_cache_path = self.kb_root / "OUTPUT" / "apcom_docs" / "manifest.json"
        self.hybrid_index = KnowledgeHybridIndex(self.sqlite_path, self.chroma_path)
        self.local_builder = KBLocalBuilder(project_root=self.project_root, kb_root=self.kb_root, topic_file=self.topic_file)
        self.llm_service = llm_service
        self._topics: list[TopicNode] | None = None
        self._manifest_cache: dict[str, Any] | None = None

    def get_topics(self) -> list[dict[str, Any]]:
        return [self._topic_to_dict(topic) for topic in self._load_topics()]

    def get_manifest(self, force_refresh: bool = False) -> dict[str, Any]:
        if self._manifest_cache is not None and not force_refresh:
            return self._manifest_cache

        _MANIFEST_ITEM_KEYS = {"content_id", "source_kind", "name", "summary", "source_rel_path", "l1_module", "l2_module", "citation_label"}
        kb_items = self._load_kb_items()
        apcom_items = self._load_apcom_items(force_refresh=force_refresh)
        all_items = kb_items + apcom_items
        slim_items = [{k: v for k, v in item.items() if k in _MANIFEST_ITEM_KEYS} for item in all_items]
        self._manifest_cache = {
            "generated_at": self._load_kb_generated_at(),
            "total_count": len(slim_items),
            "sources": {
                "kb_local": {"count": len(kb_items)},
                "apcom_docs": {"count": len(apcom_items)},
                "ticket_case": {"count": self._ticket_count()},
            },
            "topics": self.get_topics(),
            "items": slim_items,
        }
        return self._manifest_cache

    def search(
        self,
        query: str,
        top_k: int = 20,
        source_kind: str | None = None,
        category: str | None = None,
        module_boost: list[str] = [],
    ) -> list[dict[str, Any]]:
        return self.search_bundle(query=query, top_k=top_k, source_kind=source_kind, category=category, module_boost=module_boost)["items"]

    @staticmethod
    def _score_evidence_quality(item: dict) -> dict:
        """为单条 KB/工单结果计算证据质量指标，用于 Gate 4 具体度评估。"""
        import re as _re
        text = (item.get("chunk_text") or item.get("raw_content") or
                item.get("content") or item.get("summary") or "")
        word_count = max(len(text.split()), 1)
        # 步骤密度：步骤标记词频
        step_hits = len(_re.findall(
            r'(\d+\s*[\.、。]\s*|\b步骤\b|\b点击\b|\b选择\b|\b进入\b|\b操作\b|→|①|②|③|第[一二三四五六七八九十]步)',
            text
        ))
        step_density = round(min(step_hits / word_count * 10, 1.0), 3)

        # 来源层级：官方文档 > 编译KB/本地KB > 工单案例
        source_kind = item.get("source_kind", "")
        source_tier = {"apcom_docs": 3, "kb_compiled": 2, "kb_local": 2, "ticket_case": 1}.get(source_kind, 1)

        # 完整性：文本是否被截断（超过 1480 字符视为截断）
        completeness = 0.0 if len(text) >= 1480 else 1.0

        return {
            "step_density": step_density,
            "source_tier": source_tier,
            "completeness": completeness,
        }

    def search_bundle(
        self,
        query: str,
        top_k: int = 20,
        source_kind: str | None = None,
        category: str | None = None,
        project_key: str | None = None,
        module_boost: list[str] = [],
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            empty = self._empty_source_groups()
            query_profile = self._build_query_profile("")
            return {
                "query": "",
                "items": [],
                "sources": {key: {"count": 0} for key in empty},
                "source_groups": empty,
                "query_profile": query_profile,
                "primary_materials": [],
                "primary_material_count": 0,
                "ticket_summary": self._build_ticket_result_summary([]),
                "relevance_summary": self._build_relevance_summary([]),
            }

        doc_results = self._search_documents(query=query, top_k=top_k, source_kind=source_kind, category=category, project_key=project_key)
        ticket_results = []
        if source_kind in (None, "", "ticket_case"):
            ticket_results = self._search_ticket_cases(query, top_k=max(3, top_k // 2))

        combined = self._merge_results(doc_results + ticket_results)
        limited_items = self._balance_results(combined, top_k=top_k, source_kind=source_kind)
        query_profile = self._build_query_profile(query)
        ranked_items = self._rank_results_for_query(limited_items, query, query_profile)
        # 注入证据质量指标（Gate 4 具体度评估用）
        for _item in ranked_items:
            _eq = self._score_evidence_quality(_item)
            _item["step_density"] = _eq["step_density"]
            _item["source_tier"] = _eq["source_tier"]
            _item["completeness"] = _eq["completeness"]
        if module_boost:
            for item in ranked_items:
                if item.get("l1_module") in module_boost or item.get("l2_module") in module_boost:
                    item["score"] = item.get("score", 0) + 0.08
            ranked_items.sort(key=lambda x: x.get("score", 0), reverse=True)
        source_groups = self._build_source_groups(ranked_items)
        primary_materials = self._build_primary_materials(ranked_items)
        return {
            "query": query,
            "items": ranked_items,
            "sources": {key: {"count": len(value)} for key, value in source_groups.items()},
            "source_groups": source_groups,
            "query_profile": query_profile,
            "primary_materials": primary_materials,
            "primary_material_count": len(primary_materials),
            "ticket_summary": self._build_ticket_result_summary(ranked_items),
            "relevance_summary": self._build_relevance_summary(ranked_items),
        }

    def _search_documents(
        self,
        query: str,
        top_k: int,
        source_kind: str | None = None,
        category: str | None = None,
        project_key: str | None = None,
    ) -> list[dict[str, Any]]:
        expanded_queries = self._expand_query_variants(query)
        hybrid_results = []
        for expanded_query in expanded_queries[:4]:
            hybrid_results.extend(self.hybrid_index.search(expanded_query, top_k=top_k, source_kind=source_kind, category=category, project_key=project_key))
        hybrid_results = self._merge_results(hybrid_results)
        if hybrid_results:
            for item in hybrid_results:
                item["matched_topic_ids"] = self._match_topic_ids(self._item_haystack(item) + " " + item.get("chunk_text", ""))
                item["topic_names"] = self._build_topic_names(item.get("topic_ids") or item.get("matched_topic_ids") or [])
            hybrid_results = [item for item in hybrid_results if self._passes_relevance_gate(item, query)]

        tokens = self._search_tokens(" ".join(expanded_queries))
        results: list[tuple[float, dict[str, Any]]] = []
        for item in self.get_manifest()["items"]:
            if source_kind and item["source_kind"] != source_kind:
                continue
            if category and category not in {item.get("l1_module"), item.get("l2_module"), item.get("top_category")}:
                continue
            score = self._score_item(item, query, tokens)
            if score <= 0:
                continue
            enriched = dict(item)
            enriched["score"] = round(score, 4)
            enriched["matched_topic_ids"] = self._match_topic_ids(self._item_haystack(item))
            enriched["match_type"] = "document"
            enriched["chunk_id"] = enriched.get("chunk_id") or f"{enriched.get('content_id', 'DOC')}-document"
            enriched["topic_names"] = self._build_topic_names(enriched.get("topic_ids") or enriched.get("matched_topic_ids") or [])
            results.append((score, enriched))

        results.sort(key=lambda x: (-x[0], x[1]["name"]))
        manifest_results = [item for _, item in results[:top_k * 2] if self._passes_relevance_gate(item, query)]
        merged = self._merge_results(hybrid_results + manifest_results)
        if not any(item.get("match_type") == "chunk" for item in merged):
            chunk_fallbacks: list[dict[str, Any]] = []
            for item in manifest_results[: min(3, len(manifest_results))]:
                chunks = self.hybrid_index.get_chunks_for_content(item.get("content_id", ""), limit=1)
                if not chunks:
                    continue
                chunk = chunks[0]
                chunk_fallbacks.append(
                    {
                        **item,
                        "chunk_id": chunk.get("chunk_id") or f"{item.get('content_id', 'DOC')}-chunk-fallback",
                        "chunk_index": chunk.get("chunk_index", 0),
                        "chunk_preview": chunk.get("chunk_preview", ""),
                        "chunk_text": chunk.get("chunk_text", ""),
                        "match_type": "chunk",
                        "score": round(item.get("score", 0.0) + 0.05, 4),
                    }
                )
            merged = self._merge_results(chunk_fallbacks + merged)
        return merged[:top_k]

    def get_content(self, content_id: str) -> dict[str, Any] | None:
        for item in self.get_manifest()["items"]:
            if item["content_id"] == content_id:
                enriched = dict(item)
                enriched["chunks"] = self.hybrid_index.get_chunks_for_content(content_id)
                enriched["topic_names"] = self._build_topic_names(enriched.get("topic_ids", []))
                raw_content = self._load_item_text(item).strip()
                enriched["raw_content"] = raw_content
                enriched["content_url"] = f"/api/kb/content/{content_id}"
                enriched["metadata_url"] = f"/api/kb/metadata/{content_id}"
                return enriched
        if content_id.startswith("TICKET-"):
            issue_key = content_id.removeprefix("TICKET-")
            ticket_item = self._get_ticket_case_by_issue_key(issue_key)
            if ticket_item is None:
                return None
            return {
                **ticket_item,
                "display_mode": "metadata_only",
                "chunks": [],
                "raw_content": "",
                "content_url": f"/api/kb/content/{content_id}",
                "metadata_url": f"/api/kb/metadata/{content_id}",
                "topic_names": self._build_topic_names(ticket_item.get("topic_ids", [])),
                "ticket_metadata": {
                    "issue_key": issue_key,
                    "module": ticket_item.get("l2_module", ""),
                    "team": ticket_item.get("l1_module", ""),
                    "summary": ticket_item.get("summary", ""),
                    "labels": ticket_item.get("keywords", []),
                    "source_kind": "ticket_case",
                },
            }
        return None

    def get_metadata(self, content_id: str) -> dict[str, Any] | None:
        item = self.get_content(content_id)
        if item is None:
            return None
        metadata = dict(item)
        metadata.pop("raw_content", None)
        metadata.pop("chunks", None)
        return metadata

    def analyze(
        self,
        summary: str,
        module_hint: str = "",
        top_k: int = 10,
        llm_config: dict[str, Any] | None = None,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        query = " ".join(part for part in [summary, module_hint] if part).strip()
        search_bundle = self.search_bundle(query, top_k=top_k, project_key=project_key)
        evidence = search_bundle["items"]
        topic_ids = self._match_topic_ids(query)
        suggested_sections = self._build_sections(summary, module_hint, evidence)
        open_questions = self._build_questions(summary, evidence)
        category_stats = self._build_category_stats(evidence)
        topic_names = self._build_topic_names(topic_ids)

        return {
            "query_summary": summary,
            "module_hint": module_hint,
            "matched_count": len(evidence),
            "topic_ids": topic_ids,
            "topic_names": topic_names,
            "evidence": evidence,
            "sources": search_bundle["sources"],
            "source_groups": search_bundle["source_groups"],
            "query_profile": search_bundle["query_profile"],
            "primary_materials": search_bundle["primary_materials"],
            "primary_material_count": search_bundle["primary_material_count"],
            "ticket_summary": search_bundle["ticket_summary"],
            "relevance_summary": search_bundle["relevance_summary"],
            "suggested_sections": suggested_sections,
            "open_questions": open_questions,
            "category_stats": category_stats,
            "llm_enhanced": bool(llm_config and llm_config.get("apiKey")),
        }

    def sync(self, force_refresh: bool = False) -> dict[str, Any]:
        # sync 前把受保护条目备份到 JSON（兜底恢复手段，rebuild 已内置保护逻辑）
        preserved_count = self._backup_preserved_kinds()

        local_build = self.local_builder.build()
        apcom_items = self._build_apcom_manifest()
        self._manifest_cache = None
        self._topics = None  # 重置主题缓存，确保 sync 后能读取最新 topic.md
        # 刷新 manifest cache（供 get_manifest() API 使用）
        manifest = self.get_manifest(force_refresh=True or force_refresh)
        # rebuild 使用完整 items（含 source_path/converted_path），不能用 slim_items
        # 因为 _MANIFEST_ITEM_KEYS 过滤会剥离路径字段，导致 _load_item_text() 无法读文件
        kb_full_items = self._load_kb_items()
        apcom_full_items = self._load_apcom_items()
        full_items = kb_full_items + apcom_full_items
        rebuild_result = self.hybrid_index.rebuild(full_items, self._load_item_text)
        # rebuild 返回 dict（Layer2-1），向后兼容：若旧版返回 int 则包装
        if isinstance(rebuild_result, int):
            rebuild_result = {"chunk_count": rebuild_result, "skipped_unchanged": 0}
        chunk_count = rebuild_result.get("chunk_count", 0)
        skipped_unchanged = rebuild_result.get("skipped_unchanged", 0)

        # 验证受保护数据是否仍在（rebuild 内部的 _dump_preserved_kinds 可能丢失部分条目）
        post_compiled = self.hybrid_index.count_by_source_kind('kb_compiled')
        post_user = self.hybrid_index.count_by_source_kind('user_contributed')
        post_total = post_compiled + post_user
        if preserved_count > 0 and post_total < preserved_count:
            print(f"[KBService] ⚠️ sync 后受保护条目从 {preserved_count} 降至 {post_total}，从 JSON 备份恢复...")
            restored = self._restore_from_backup()
            print(f"[KBService] 恢复完成: {restored} 条（补回 {preserved_count - post_total} 条差额）")

        return {
            "ok": True,
            "sources": manifest["sources"],
            "local_manifest_count": local_build["content_count"],
            "source_files": local_build["source_files"],
            "converted_files": local_build["converted_files"],
            "apcom_manifest_count": len(apcom_items),
            "chunk_count": chunk_count,
            "skipped_unchanged": skipped_unchanged,
            "preserved_count": preserved_count,
        }

    def _backup_preserved_kinds(self) -> int:
        """sync 前把受保护 source_kind 导出到 JSON 备份（最多保留 3 份）。返回备份条目数。"""
        import json as _json, time as _time
        from kb_hybrid_index import _PRESERVED_SOURCE_KINDS
        try:
            rows = self.hybrid_index.list_by_source_kinds(_PRESERVED_SOURCE_KINDS)
            if not rows:
                return 0
            backup_path = self.project_root / "data" / "sqlite" / "kb_compiled_backup.json"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            existing: list = []
            if backup_path.exists():
                try:
                    existing = _json.loads(backup_path.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append({"ts": int(_time.time()), "items": rows})
            # 只保留最近 3 份
            existing = existing[-3:]
            backup_path.write_text(_json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[KBService] 已备份 {len(rows)} 条受保护数据 → {backup_path}")
            return len(rows)
        except Exception as e:
            print(f"[KBService] 备份受保护数据失败: {e}")
            return 0

    def _restore_from_backup(self) -> int:
        """从最新的 JSON 备份恢复受保护条目。返回恢复条目数。"""
        import json as _json
        backup_path = self.project_root / "data" / "sqlite" / "kb_compiled_backup.json"
        if not backup_path.exists():
            print("[KBService] 无备份文件，无法恢复")
            return 0
        try:
            snapshots = _json.loads(backup_path.read_text(encoding="utf-8"))
            if not snapshots:
                return 0
            latest = snapshots[-1]
            items = latest.get("items", [])
            restored = 0
            for item in items:
                content = item.pop("content", "")
                if content.strip():
                    try:
                        self.hybrid_index.add_item(item, content)
                        restored += 1
                    except Exception as e:
                        print(f"[KBService] 恢复条目失败 {item.get('content_id')}: {e}")
            print(f"[KBService] 从备份恢复 {restored}/{len(items)} 条")
            return restored
        except Exception as e:
            print(f"[KBService] 恢复失败: {e}")
            return 0

    def draft(
        self,
        summary: str,
        module_hint: str = "",
        top_k: int = 10,
        llm_config: dict[str, Any] | None = None,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        analysis = self.analyze(summary=summary, module_hint=module_hint, top_k=top_k, llm_config=llm_config, project_key=project_key)
        citations = list(dict.fromkeys(item["citation_label"] for item in analysis["evidence"] if item.get("citation_label")))
        todo_items = analysis["open_questions"] or ["需补充接口边界、验收标准和上线约束。"]
        markdown = self._build_rule_based_prd(summary, module_hint, analysis, citations, todo_items)
        return {
            "prd_markdown": markdown,
            "citations": citations,
            "todo_items": todo_items,
            "evidence": analysis["evidence"],
            "sources": analysis["sources"],
            "source_groups": analysis["source_groups"],
            "query_profile": analysis["query_profile"],
            "primary_materials": analysis["primary_materials"],
            "primary_material_count": analysis["primary_material_count"],
            "ticket_summary": analysis["ticket_summary"],
            "relevance_summary": analysis["relevance_summary"],
            "llm_enhanced": bool(llm_config and llm_config.get("apiKey")),
        }

    def review(
        self,
        summary: str,
        draft_markdown: str,
        module_hint: str = "",
        top_k: int = 10,
        project_key: str | None = None,
    ) -> dict[str, Any]:
        analysis = self.analyze(summary=summary, module_hint=module_hint, top_k=top_k, project_key=project_key)
        coverage_gaps = [section for section in analysis["suggested_sections"] if section not in draft_markdown]
        unsupported_claims = []
        for line in draft_markdown.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if any(keyword in stripped for keyword in ("完全支持", "所有场景", "100%", "已全部实现")):
                unsupported_claims.append(stripped)
        known_citations = [item["citation_label"] for item in analysis["evidence"] if item.get("citation_label")]
        missing_evidence = known_citations[:5] if not any(label in draft_markdown for label in known_citations) else []
        return {
            "coverage_gaps": coverage_gaps,
            "unsupported_claims": unsupported_claims,
            "missing_evidence": missing_evidence,
            "suggested_sections": analysis["suggested_sections"],
            "evidence": analysis["evidence"],
            "sources": analysis["sources"],
            "source_groups": analysis["source_groups"],
            "query_profile": analysis["query_profile"],
            "primary_materials": analysis["primary_materials"],
            "primary_material_count": analysis["primary_material_count"],
            "ticket_summary": analysis["ticket_summary"],
            "relevance_summary": analysis["relevance_summary"],
        }

    def answer_question(
        self,
        query: str,
        mode: str = "short",
        api_key: str = "",
        provider: str = "gemini",
        model_name: str = "",
        base_url: str = "",
        project_key: str | None = None,
    ) -> dict[str, Any]:
        # 优先查 kb_compiled 综合解析条目
        compiled_hits = []
        try:
            compiled_hits = self.hybrid_index.search(query, top_k=2, source_kind='kb_compiled', project_key=project_key)
        except Exception:
            pass

        # 如果有综合解析条目，减少原始chunks数量
        if compiled_hits:
            raw_k = 4
        else:
            raw_k = 8

        search_bundle = self.search_bundle(query, top_k=raw_k, project_key=project_key)
        items = compiled_hits + (search_bundle["items"][:raw_k])
        results = items
        if not results:
            empty_text = "未检索到相关知识库内容"
            return {
                "answer_text": empty_text,
                "answer_html": self._render_answer_html(empty_text),
                "used_llm": False,
                "fallback_used": True,
                "sources": [],
                "references": [],
                "source_groups": search_bundle["source_groups"],
                "source_counts": search_bundle["sources"],
                "query_profile": search_bundle["query_profile"],
                "primary_materials": search_bundle["primary_materials"],
                "primary_material_count": search_bundle["primary_material_count"],
                "ticket_summary": search_bundle["ticket_summary"],
                "relevance_summary": search_bundle["relevance_summary"],
                "topics": [],
            }

        used_llm = False
        fallback_used = False
        if api_key:
            try:
                llm = self.llm_service
                if llm is None:
                    from llm_service import LLMService

                    llm = LLMService()
                prompt = self._build_qa_prompt(query, mode, results[:4])
                answer_text = llm.call_llm(
                    prompt=prompt,
                    api_key=api_key,
                    provider=provider,
                    model_name=model_name,
                    base_url=base_url,
                ).strip()
                if self._is_valid_qa_answer(answer_text, query, results):
                    if mode == "short":
                        answer_text = self._truncate_text(answer_text, 300)
                    used_llm = True
                else:
                    answer_text = ""
                    fallback_used = True
            except Exception:
                answer_text = ""
        else:
            answer_text = ""

        if not answer_text:
            fallback_used = True
            answer_text = self._build_fallback_short_answer(query, results) if mode == "short" else self._build_fallback_long_answer(query, results)
            if not answer_text:
                answer_text = "未能生成直接答案，以下为相关资料"

        topics: list[str] = []
        for result in results[:5]:
            for topic_name in result.get("topic_names", []) or self._build_topic_names(result.get("topic_ids", [])):
                if topic_name not in topics:
                    topics.append(topic_name)

        top_refs = [
            {
                "name": item.get("name", ""),
                "source_rel_path": item.get("source_rel_path", item.get("source", "")),
                "citation_label": item.get("citation_label", item.get("name", "")),
            }
            for item in results[:3]
        ]

        return {
            "answer_text": answer_text,
            "answer_html": self._render_answer_html(answer_text),
            "used_llm": used_llm,
            "fallback_used": fallback_used or not used_llm,
            "sources": results[:8],
            "references": top_refs,
            "source_groups": search_bundle["source_groups"],
            "source_counts": search_bundle["sources"],
            "query_profile": search_bundle["query_profile"],
            "primary_materials": search_bundle["primary_materials"],
            "primary_material_count": search_bundle["primary_material_count"],
            "ticket_summary": search_bundle["ticket_summary"],
            "relevance_summary": search_bundle["relevance_summary"],
            "topics": topics,
        }

    def _load_kb_generated_at(self) -> str:
        manifest_path = self.kb_root / "INDEX" / "manifest.json"
        if not manifest_path.exists():
            return ""
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8")).get("generated_at", "")
        except Exception:
            return ""

    def _empty_source_groups(self) -> dict[str, list[dict[str, Any]]]:
        return {"kb_local": [], "apcom_docs": [], "ticket_case": []}

    def _build_source_groups(self, items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        groups = self._empty_source_groups()
        for item in items:
            groups.setdefault(item.get("source_kind", "kb_local"), []).append(item)
        return groups

    def _build_query_profile(self, query: str) -> dict[str, Any]:
        query_intent = self._classify_query_intent(query)
        source_weights = self._source_weights_for_intent(query_intent)
        return {
            "query_intent": query_intent,
            "source_weights": source_weights,
            "source_weight_strategy": self._describe_source_strategy(query_intent),
        }

    def _classify_query_intent(self, query: str) -> str:
        text = (query or "").lower()
        operation_keywords = ("如何", "怎么", "怎样", "查看", "查询", "设置", "配置", "干预", "操作", "排查", "开启", "关闭")
        principle_keywords = ("原理", "机制", "架构", "规则", "规则引擎", "结合", "设计", "区别", "差异", "为什么")
        operation_hits = sum(1 for keyword in operation_keywords if keyword in text)
        principle_hits = sum(1 for keyword in principle_keywords if keyword in text)
        if operation_hits and principle_hits:
            return "mixed"
        if operation_hits:
            return "operation"
        if principle_hits:
            return "principle"
        return "mixed"

    def _source_weights_for_intent(self, query_intent: str) -> dict[str, float]:
        if query_intent == "operation":
            return {"kb_local": 1.22, "apcom_docs": 0.96, "ticket_case": 0.55}
        if query_intent == "principle":
            return {"kb_local": 0.98, "apcom_docs": 1.2, "ticket_case": 0.55}
        return {"kb_local": 1.08, "apcom_docs": 1.08, "ticket_case": 0.58}

    def _describe_source_strategy(self, query_intent: str) -> str:
        if query_intent == "operation":
            return "操作题优先使用 kb_local，apcom_docs 补充原理说明，ticket_case 仅作侧证。"
        if query_intent == "principle":
            return "原理题优先使用 apcom_docs，kb_local 补充落地说明，ticket_case 仅作侧证。"
        return "混合题对 kb_local 与 apcom_docs 近似加权，ticket_case 仅作侧证。"

    def _rank_results_for_query(
        self,
        items: list[dict[str, Any]],
        query: str,
        query_profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        query_tokens = self._search_tokens(" ".join(self._expand_query_variants(query)))
        source_weights = query_profile.get("source_weights", {})
        ranked: list[dict[str, Any]] = []
        for item in items:
            enriched = dict(item)
            base_score = self._build_base_relevance_score(item, query, query_tokens)
            source_weight = float(source_weights.get(item.get("source_kind", ""), 1.0))
            weighted_score = round(base_score * source_weight, 1)
            relevance_level = self._relevance_level_for_score(base_score)
            enriched["base_relevance_score"] = base_score
            enriched["weighted_rank_score"] = weighted_score
            enriched["relevance_level"] = relevance_level
            enriched["relevance_reason"] = self._build_relevance_reason(item, query_tokens, relevance_level)
            ranked.append(enriched)
        ranked.sort(
            key=lambda item: (
                -float(item.get("weighted_rank_score", 0.0) or 0.0),
                item.get("source_kind") == "ticket_case",
                -int(item.get("base_relevance_score", 0) or 0),
                item.get("name", ""),
            )
        )
        return ranked

    def _build_base_relevance_score(self, item: dict[str, Any], query: str, query_tokens: list[str]) -> int:
        haystack = self._item_haystack(item)
        if item.get("chunk_text"):
            haystack = f"{haystack} {(item.get('chunk_text') or '').lower()}"
        matched_terms = [term for term in query_tokens if term in haystack]
        score = float(item.get("score", 0.0) or 0.0) * 20
        if query and query.lower() in haystack:
            score += 18
        score += len(set(matched_terms)) * 16
        if any(term in (item.get("name", "") or "").lower() for term in matched_terms):
            score += 8
        if any(term in (item.get("summary", "") or "").lower() for term in matched_terms):
            score += 5
        if item.get("source_kind") == "ticket_case":
            score -= 12
        bounded = max(0, min(int(round(score)), 100))
        if matched_terms and bounded < 35:
            return 35
        return bounded

    def _relevance_level_for_score(self, score: int) -> str:
        if score >= 75:
            return "high"
        if score >= 45:
            return "medium"
        return "low"

    def _build_relevance_reason(self, item: dict[str, Any], query_tokens: list[str], relevance_level: str) -> str:
        haystack = self._item_haystack(item)
        if item.get("chunk_text"):
            haystack = f"{haystack} {(item.get('chunk_text') or '').lower()}"
        matched_terms = [term for term in query_tokens if term in haystack][:3]
        source_kind = item.get("source_kind", "")
        if matched_terms:
            joined_terms = "、".join(matched_terms)
            if source_kind == "ticket_case":
                return f"工单侧证命中 {joined_terms}，适合补充案例与风险。"
            if relevance_level == "high":
                return f"标题或摘要直接覆盖 {joined_terms}，与问题高度相关。"
            if relevance_level == "medium":
                return f"正文中命中 {joined_terms}，可作为补充参考。"
            return f"仅部分命中 {joined_terms}，相关度较弱。"
        if source_kind == "ticket_case":
            return "命中历史工单，但更适合作为侧证而非主资料。"
        return "与问题存在弱关联，建议与更高相关资料交叉验证。"

    def _build_primary_materials(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for item in items:
            if item.get("source_kind") == "ticket_case":
                continue
            key = item.get("content_id") or item.get("name", "")
            existing = selected.get(key)
            if existing is None or float(item.get("weighted_rank_score", 0.0) or 0.0) > float(existing.get("weighted_rank_score", 0.0) or 0.0):
                selected[key] = item
        primary_materials = list(selected.values())
        primary_materials.sort(
            key=lambda item: (
                -float(item.get("weighted_rank_score", 0.0) or 0.0),
                -int(item.get("base_relevance_score", 0) or 0),
                item.get("name", ""),
            )
        )
        return primary_materials[:6]

    def _build_ticket_result_summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        tickets = [item for item in items if item.get("source_kind") == "ticket_case"]
        issue_keys: list[str] = []
        for item in tickets:
            issue_key = (item.get("source_rel_path") or "").strip()
            if not issue_key:
                content_id = str(item.get("content_id", ""))
                if content_id.startswith("TICKET-"):
                    issue_key = content_id.removeprefix("TICKET-")
            if issue_key and issue_key not in issue_keys:
                issue_keys.append(issue_key)
        return {
            "related_count": len(tickets),
            "top_issue_keys": issue_keys[:5],
            "items": tickets[:5],
        }

    def _build_relevance_summary(self, items: list[dict[str, Any]]) -> dict[str, int]:
        counts = Counter(item.get("relevance_level", "low") for item in items)
        return {
            "high_count": int(counts.get("high", 0)),
            "medium_count": int(counts.get("medium", 0)),
            "low_count": int(counts.get("low", 0)),
        }

    def _merge_results(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            identity = item.get("chunk_id") if item.get("match_type") == "chunk" and item.get("chunk_id") else item.get("content_id", "")
            key = (item.get("source_kind", ""), identity)
            existing = deduped.get(key)
            if existing is None or item.get("score", 0.0) > existing.get("score", 0.0):
                deduped[key] = item
        merged = list(deduped.values())
        merged.sort(key=lambda item: (-item.get("score", 0.0), item.get("name", "")))
        return merged

    def _balance_results(self, items: list[dict[str, Any]], top_k: int, source_kind: str | None = None) -> list[dict[str, Any]]:
        if source_kind:
            return items[:top_k]

        by_source = self._build_source_groups(items)
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def result_key(item: dict[str, Any]) -> tuple[str, str]:
            identity = item.get("chunk_id") if item.get("match_type") == "chunk" and item.get("chunk_id") else item.get("content_id", "")
            return (item.get("source_kind", ""), identity)

        # Each source gets a guaranteed slot if it has hits.
        for source in ("kb_local", "apcom_docs", "ticket_case"):
            if not by_source.get(source):
                continue
            item = by_source[source][0]
            key = result_key(item)
            if key not in seen:
                selected.append(item)
                seen.add(key)
            if len(selected) >= top_k:
                return selected[:top_k]

        for item in items:
            key = result_key(item)
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            if len(selected) >= top_k:
                break
        return selected[:top_k]

    def _load_kb_items(self) -> list[dict[str, Any]]:
        manifest_path = self.kb_root / "INDEX" / "manifest.json"
        if not manifest_path.exists():
            return []
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = []
        for content_id, raw in payload.get("contents", {}).items():
            _src_rel = raw.get("source_rel_path", "")
            # Use source_path (KB/公式/...) so domain SQL LIKE '%/公式/%' matches correctly
            _src_path = raw.get("source_path", _src_rel)
            _converted_path = raw.get("converted_path", "")
            # Layer2-1: 读取 converted_path 的 mtime 用于增量去重
            _mtime: str | None = None
            if _converted_path:
                try:
                    import os as _os
                    _mtime = str(int(_os.path.getmtime(_converted_path)))
                except Exception:
                    pass
            item = {
                "content_id": content_id,
                "source_kind": "kb_local",
                "name": raw.get("name", content_id),
                "summary": raw.get("summary", ""),
                "source_rel_path": _src_path,
                # 保留完整路径供 _load_item_text() 使用（不能通过 _MANIFEST_ITEM_KEYS slim 掉）
                "source_path": _src_path,
                "converted_path": _converted_path,
                "l1_module": raw.get("top_category", ""),
                "l2_module": raw.get("second_category", ""),
                "citation_label": f"[KB] {_src_rel or content_id}",
                "project_key": "_global",
                "source_mtime": _mtime,
            }
            items.append(item)
        return items

    def _load_apcom_items(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        if self.apcom_cache_path.exists() and not force_refresh:
            try:
                payload = json.loads(self.apcom_cache_path.read_text(encoding="utf-8"))
                return payload.get("items", [])
            except Exception:
                pass
        return self._build_apcom_manifest()

    def _build_apcom_manifest(self) -> list[dict[str, Any]]:
        if self.apcom_root is None:
            return []
        docs_root = self.apcom_root / "docs"
        items: list[dict[str, Any]] = []
        if not docs_root.exists():
            return items

        counter = 1
        for path in sorted(docs_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.apcom_root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if path.suffix.lower() not in SUPPORTED_APCOM_EXTENSIONS:
                continue

            text = self._extract_text(path)
            if not text.strip():
                continue

            l1_module, l2_module, doc_type = self._derive_apcom_metadata(rel)
            content_id = f"APCOM-{counter:04d}"
            counter += 1
            summary = self._build_summary(text)
            keywords = self._extract_keywords(text)
            haystack = " ".join([path.stem, summary, str(rel)])
            item = {
                "content_id": content_id,
                "source_kind": "apcom_docs",
                "source_repo": "iuap-apcom-docs",
                "name": self._extract_title(text, path),
                "summary": summary,
                "keywords": keywords,
                "source_rel_path": rel.as_posix(),
                "source_path": str(path),
                "converted_path": str(path),
                "index_path": "",
                "l1_module": l1_module,
                "l2_module": l2_module,
                "doc_type": doc_type,
                "topic_ids": self._match_topic_ids(haystack),
                "citation_label": f"[APCOM] {rel.as_posix()}",
                "related_content_ids": [],
                "backlink_index_ids": [],
                "project_key": "_global",
            }
            items.append(item)

        self.apcom_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.apcom_cache_path.write_text(
            json.dumps({"source_root": str(self.apcom_root), "count": len(items), "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return items

    def _derive_apcom_metadata(self, rel: Path) -> tuple[str, str, str]:
        parts = rel.parts
        l1 = parts[1] if len(parts) > 1 else "未分类"
        l2 = parts[2] if len(parts) > 2 else "未分类"
        doc_type = ""
        candidates = parts[3:-1] if len(parts) > 4 else parts[2:-1]
        for part in candidates:
            if any(key in part for key in ("产品", "概述", "常见问题", "方案", "培训", "JIRA", "工单", "架构", "开发", "设计", "需求", "接口")):
                doc_type = self._strip_numeric_prefix(part)
                break
        if not doc_type:
            doc_type = self._strip_numeric_prefix(l2)
        return self._strip_numeric_prefix(l1), self._strip_numeric_prefix(l2), doc_type

    def _extract_title(self, text: str, path: Path) -> str:
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:120]
        return path.stem

    def _search_tokens(self, query: str) -> list[str]:
        normalized = (query or "").lower()
        # 注意："能" 已从停用词中移除（2026-04-10 修复）——
        # "功能"、"能力" 在工单场景中是高频有意义词，移除"能"会打断分词边界
        # 导致"功能不允许撤回" → "功 不允许撤回"，"撤回" 无法独立提取
        normalized = re.sub(r"(如何|怎么|怎样|设置|配置|查看|查询|以及|并且|并|和|与|及|该|的|了|吗|呢|吧|要|请问|需要|可以|进行|一下)", " ", normalized)
        tokens = [token for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", normalized) if token]

        # 对长中文token(>=4字)做二元切分，增加召回率
        # 例如 "流程扩展" -> 保留原词 + "流程" + "扩展"
        extra_tokens = []
        for token in tokens:
            if len(token) >= 4 and all('\u4e00' <= c <= '\u9fff' for c in token):
                for i in range(0, len(token) - 1, 2):
                    sub = token[i:i+2]
                    if sub not in tokens:
                        extra_tokens.append(sub)
        tokens.extend(extra_tokens)

        for phrase in DOMAIN_PHRASES:
            phrase_lower = phrase.lower()
            if phrase_lower in (query or "").lower() and phrase_lower not in tokens:
                tokens.append(phrase_lower)
        deduped: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            deduped.append(token)
        return deduped

    def _expand_query_variants(self, query: str) -> list[str]:
        query = (query or "").strip()
        variants = [query]

        # 1. 词典扩展
        for seed, expansions in TERM_EXPANSIONS.items():
            if seed in query:
                variants.extend(expansions)
                variants.extend(f"{query} {term}" for term in expansions[:3])
        lowered = query.lower()
        if "连岗" in lowered:
            variants.extend(["代理审批", "审批委托", "同一处理人自动去重"])

        # 2. 去虚词提取核心术语 (提高FTS命中率)
        stop_words = {"该", "怎么", "如何", "什么", "是", "的", "了", "吗", "呢", "吧",
                      "在", "有", "能", "可以", "要", "请问", "使用", "操作", "配置",
                      "这个", "那个", "一下", "一个", "进行", "需要"}
        import re as _re
        # 先去虚词再提取
        cleaned = _re.sub(r'(该|怎么|如何|什么|是|的|了|吗|请问|需要|可以|进行)', ' ', query)
        cn_tokens = _re.findall(r'[\u4e00-\u9fff]{2,}', cleaned)
        core_tokens = [t for t in cn_tokens if t not in stop_words]
        if core_tokens:
            variants.append(" ".join(core_tokens))
            for t in core_tokens:
                if len(t) >= 2 and t != query:
                    variants.append(t)
                # 长词二元切分: "流程扩展" -> "流程", "扩展"
                if len(t) >= 4 and all('\u4e00' <= c <= '\u9fff' for c in t):
                    for i in range(0, len(t) - 1, 2):
                        sub = t[i:i+2]
                        if sub not in stop_words and sub != query:
                            variants.append(sub)

        deduped = []
        seen = set()
        for item in variants:
            item = (item or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped[:12]

    def _passes_relevance_gate(self, item: dict[str, Any], query: str) -> bool:
        expanded_terms = self._search_tokens(" ".join(self._expand_query_variants(query)))
        haystack = self._item_haystack(item)
        if item.get("chunk_text"):
            haystack += " " + (item.get("chunk_text") or "").lower()
        if not expanded_terms:
            return True
        hits = [term for term in expanded_terms if term in haystack]
        # chunk级别结果(FTS/向量命中)门槛低, document级别门槛高
        min_hits = 1 if item.get("match_type") == "chunk" else min(2, len(expanded_terms))
        return len(set(hits)) >= min_hits

    def _score_item(self, item: dict[str, Any], query: str, tokens: list[str]) -> float:
        haystack = self._item_haystack(item)
        query_lower = query.lower()
        score = 0.0

        if query_lower in haystack:
            score += 4.0
        for token in tokens:
            if token in item.get("name", "").lower():
                score += 2.2
            if token in item.get("summary", "").lower():
                score += 1.8
            if token in " ".join(item.get("keywords", [])).lower():
                score += 1.5
            if token in item.get("source_rel_path", "").lower():
                score += 1.0
        if item["source_kind"] == "apcom_docs":
            score += 0.6
        elif item["source_kind"] == "kb_local":
            score += 0.4
        # 惩罚仅靠通用词(如"使用")命中的文档
        import re as _re
        stop_words = {"使用", "操作", "配置", "如何", "怎么", "问题", "设置", "方案", "说明"}
        core_tokens = [t for t in tokens if t not in stop_words and len(t) >= 2]
        core_hits = sum(1 for t in core_tokens if t in haystack)
        if core_tokens and core_hits == 0:
            score *= 0.3  # 核心词完全没命中，大幅降分

        # ── KB 自动成长系统加权（2026-04-12）──
        # 热度加权（被智能回复引用越多排越前）
        heat = item.get('heat_score', 0)
        if isinstance(heat, (int, float)) and heat > 0:
            score += heat * 0.3

        # 可信度加权
        cred = item.get('credibility', 0.7)
        if isinstance(cred, (int, float)):
            if cred < 0.5:
                score *= 0.5   # 低可信度大幅降权
            elif cred >= 0.9:
                score *= 1.2   # 高可信度加权

        # 多人验证加权（2+人工回复验证 = 最高优先级）
        validation_src = item.get('validation_sources', '[]')
        if isinstance(validation_src, str):
            import json as _json_v
            try:
                validation_src = _json_v.loads(validation_src)
            except Exception:
                validation_src = []
        if isinstance(validation_src, list) and len(validation_src) >= 2:
            score *= 1.3   # 2+人工验证加 30%

        return score

    def _item_haystack(self, item: dict[str, Any]) -> str:
        return " ".join(
            [
                item.get("name", ""),
                item.get("summary", ""),
                " ".join(item.get("keywords", [])),
                item.get("source_rel_path", ""),
                item.get("l1_module", ""),
                item.get("l2_module", ""),
                item.get("doc_type", ""),
            ]
        ).lower()

    def _load_topics(self) -> list[TopicNode]:
        if self._topics is not None:
            return self._topics
        if not self.topic_file.exists():
            self._topics = []
            return self._topics

        topics: list[TopicNode] = []
        for raw in self.topic_file.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^(\s*)-\s+\[(TOP-[^\]]+)\]\s+(.+?)\s*$", raw)
            if not match:
                continue
            indent, topic_id, name = match.groups()
            level = len(indent) // 4 + 1
            keywords = [name.strip()] + [token for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", name)]
            topics.append(TopicNode(topic_id=topic_id, name=name.strip(), level=level, keywords=keywords))
        self._topics = topics
        return topics

    def _match_topic_ids(self, text: str) -> list[str]:
        text_lower = (text or "").lower()
        matched: list[str] = []
        for topic in self._load_topics():
            if any(keyword.lower() in text_lower for keyword in topic.keywords if keyword):
                matched.append(topic.topic_id)
        return matched[:8]

    def _build_topic_names(self, topic_ids: list[str]) -> list[str]:
        topic_map = {topic.topic_id: topic.name for topic in self._load_topics()}
        return [topic_map[topic_id] for topic_id in topic_ids if topic_id in topic_map]

    def _build_sections(self, summary: str, module_hint: str, evidence: list[dict[str, Any]]) -> list[str]:
        sections = ["需求背景", "问题定义", "能力边界", "待确认项"]
        if evidence:
            doc_types = {item.get("doc_type", "") for item in evidence}
            if any("架构" in doc_type or "开发" in doc_type for doc_type in doc_types):
                sections.insert(2, "技术方案")
            if any("方案" in doc_type or "最佳实践" in doc_type for doc_type in doc_types):
                sections.insert(2, "方案参考")
            if any("JIRA" in doc_type or "工单" in doc_type for doc_type in doc_types):
                sections.insert(1, "历史案例")
        if module_hint:
            sections.insert(1, f"{module_hint}能力映射")
        return list(dict.fromkeys(sections))

    def _build_questions(self, summary: str, evidence: list[dict[str, Any]]) -> list[str]:
        questions = []
        evidence_text = " ".join(self._item_haystack(item) for item in evidence)
        if "接口" not in evidence_text:
            questions.append("缺少明确接口/集成方式证据，需确认接口边界和调用方。")
        if "方案" not in evidence_text and "最佳实践" not in evidence_text:
            questions.append("缺少方案或最佳实践类证据，需确认推荐落地方式。")
        if not evidence:
            questions.append("当前未命中知识库证据，需补充更明确的模块提示或关键词。")
        if len(summary.strip()) < 20:
            questions.append("需求摘要过短，建议补充业务背景、场景和目标。")
        return questions[:5]

    def _build_category_stats(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts = Counter(item.get("l1_module") or item.get("source_kind") for item in evidence)
        return [{"category": key, "count": value} for key, value in counts.most_common(6)]

    def _ticket_count(self) -> int:
        try:
            from vector_store import VectorStore

            store = VectorStore(persist_directory=str(self.ticket_chroma_path), allow_download=False)
            return int(store._safe_collection_count(store.issues_collection))
        except Exception:
            return 0

    def _search_ticket_cases(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        try:
            from vector_store import VectorStore

            store = VectorStore(persist_directory=str(self.ticket_chroma_path), allow_download=False)
            results = []
            for expanded_query in self._expand_query_variants(query)[:4]:
                results.extend(store.search_similar_issues(expanded_query, top_k=top_k, min_score=0.0))
        except Exception:
            return []

        mapped = []
        for item in results:
            metadata = item.get("metadata", {})
            text = item.get("document", "")
            _ik = item.get("issue_key", "")
            _proj = _ik.split("-")[0] if "-" in _ik else "_global"
            mapped.append(
                {
                    "content_id": f"TICKET-{_ik}",
                    "chunk_id": f"TICKET-{_ik}",
                    "source_kind": "ticket_case",
                    "source_repo": "ticket_chroma",
                    "name": f"{_ik} {item.get('summary', '')}".strip(),
                    "summary": self._build_summary(text or item.get("summary", "")),
                    "keywords": self._extract_keywords(text or item.get("summary", "")),
                    "source_rel_path": _ik,
                    "source_path": _ik,
                    "converted_path": "",
                    "index_path": "",
                    "l1_module": metadata.get("team", "工单案例"),
                    "l2_module": metadata.get("module", ""),
                    "doc_type": "JIRA工单",
                    "topic_ids": self._match_topic_ids(text or item.get("summary", "")),
                    "citation_label": f"[TICKET] {_ik}",
                    "related_content_ids": [],
                    "backlink_index_ids": [],
                    "chunk_text": text,
                    "chunk_preview": (text or "")[:240],
                    "match_type": "ticket",
                    "score": round(float(item.get("score", 0.0)), 4),
                    "project_key": _proj,
                }
            )
        return [item for item in self._merge_results(mapped) if self._passes_relevance_gate(item, query)]

    def _get_ticket_case_by_issue_key(self, issue_key: str) -> dict[str, Any] | None:
        try:
            from vector_store import VectorStore

            store = VectorStore(persist_directory=str(self.ticket_chroma_path), allow_download=False)
            item = store.get_issue_by_key(issue_key)
        except Exception:
            return None

        if not item:
            return None
        metadata = item.get("metadata", {})
        text = item.get("document", "")
        topic_ids = self._match_topic_ids(f"{metadata.get('summary', '')} {text}")
        _proj = issue_key.split("-")[0] if "-" in issue_key else "_global"
        return {
            "content_id": f"TICKET-{issue_key}",
            "chunk_id": f"TICKET-{issue_key}",
            "source_kind": "ticket_case",
            "source_repo": "ticket_chroma",
            "name": f"{issue_key} {metadata.get('summary', '')}".strip(),
            "summary": self._build_summary(text or metadata.get("summary", "")),
            "keywords": self._extract_keywords(text or metadata.get("summary", "")),
            "source_rel_path": issue_key,
            "source_path": issue_key,
            "converted_path": "",
            "index_path": "",
            "l1_module": metadata.get("team", "工单案例"),
            "l2_module": metadata.get("module", ""),
            "doc_type": "JIRA工单",
            "topic_ids": topic_ids,
            "citation_label": f"[TICKET] {issue_key}",
            "related_content_ids": [],
            "backlink_index_ids": [],
            "chunk_text": text,
            "chunk_preview": text[:240],
            "match_type": "ticket",
            "score": 1.0,
            "project_key": _proj,
        }

    def _load_item_text(self, item: dict[str, Any]) -> str:
        # 优先使用完整路径（本地 builder 保留的 converted_path / source_path）
        source_path = self._resolve_project_path(item.get("converted_path") or item.get("source_path") or "")
        if source_path.exists():
            return self._extract_text(source_path)
        fallback_path = self._resolve_project_path(item.get("source_path") or "")
        if fallback_path.exists():
            return self._extract_text(fallback_path)
        # slim_items 经过 _MANIFEST_ITEM_KEYS 过滤后只剩 source_rel_path，
        # 必须作为最后 fallback，否则 rebuild 时所有条目都因为空文本被跳过
        rel_path = self._resolve_project_path(item.get("source_rel_path") or "")
        if rel_path.exists():
            return self._extract_text(rel_path)
        return ""

    def _resolve_project_path(self, raw_path: str) -> Path:
        if not raw_path:
            return Path("")

        path = Path(raw_path)
        if not path.is_absolute():
            if raw_path.startswith(("KB/", "APP/", "data/")):
                return self.project_root / raw_path
            return self.kb_root / raw_path

        normalized = raw_path.replace("\\", "/")
        rerooted_path: Path | None = None
        for root_name in ("KB", "APP", "data"):
            marker = f"/{root_name}/"
            if marker in normalized:
                suffix = normalized.split(marker, 1)[1]
                rerooted_path = self.project_root / root_name / suffix
                if rerooted_path.exists():
                    return rerooted_path
                break

        if path.exists():
            return path
        if rerooted_path is not None:
            return rerooted_path
        return path

    def _build_rule_based_prd(
        self,
        summary: str,
        module_hint: str,
        analysis: dict[str, Any],
        citations: list[str],
        todo_items: list[str],
    ) -> str:
        lines = ["# PRD初稿", ""]
        for section in analysis["suggested_sections"]:
            lines.append(f"## {section}")
            if section == "需求背景":
                lines.append(summary.strip() or "需补充业务背景说明。")
            elif "映射" in section:
                lines.append(f"聚焦模块：{module_hint or '待补充'}。结合知识证据整理能力边界与依赖关系。")
            elif section == "历史案例":
                tickets = [item for item in analysis["evidence"] if item["source_kind"] == "ticket_case"][:3]
                if tickets:
                    lines.extend([f"- {item['name']}：{item['summary']}" for item in tickets])
                else:
                    lines.append("- 当前未命中明确历史工单案例。")
            elif section in {"方案参考", "技术方案", "问题定义", "能力边界", "待确认项"}:
                lines.extend(self._section_body(section, analysis, todo_items))
            else:
                lines.append("待补充。")
            if citations:
                lines.append(f"引用: {citations[min(len(citations) - 1, 0)]}")
            lines.append("")

        lines.append("## 证据清单")
        lines.extend([f"- {citation}" for citation in citations[:10]])
        lines.append("")
        lines.append("## 待确认项")
        lines.extend([f"- {item}" for item in todo_items])
        return "\n".join(lines).strip() + "\n"

    def _section_body(self, section: str, analysis: dict[str, Any], todo_items: list[str]) -> list[str]:
        evidence = analysis["evidence"][:3]
        if section == "问题定义":
            return [f"- 目标问题：{analysis['query_summary']}"]
        if section == "方案参考":
            return [f"- {item['name']}：{item['summary']}" for item in evidence] or ["- 暂无明确方案参考。"]
        if section == "技术方案":
            return [f"- 结合 {item['source_kind']} 证据，优先梳理接口、扩展点和约束条件。" for item in evidence] or ["- 暂无明确技术方案证据。"]
        if section == "能力边界":
            return [f"- {item['name']}：{item['summary']}" for item in evidence] or ["- 需进一步确认能力边界。"]
        if section == "待确认项":
            return [f"- {item}" for item in todo_items]
        return ["- 待补充。"]

    def _render_answer_html(self, answer_text: str) -> str:
        """将Markdown文本转为HTML。优先用markdown库，fallback简单转换。"""
        text = (answer_text or "").strip()
        if not text:
            return "<p></p>"
        try:
            import markdown
            return markdown.markdown(text, extensions=['fenced_code', 'tables', 'nl2br'])
        except ImportError:
            # fallback: 简单markdown→HTML
            import re as _re
            text = html.escape(text)
            text = _re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=_re.MULTILINE)
            text = _re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=_re.MULTILINE)
            text = _re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=_re.MULTILINE)
            text = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
            text = _re.sub(r'`(.+?)`', r'<code>\1</code>', text)
            text = _re.sub(r'^- (.+)$', r'<li>\1</li>', text, flags=_re.MULTILINE)
            text = text.replace('\n\n', '</p><p>').replace('\n', '<br>')
            return f"<p>{text}</p>"

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _build_fallback_short_answer(self, query: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return "未检索到相关知识库内容"

        top_result = results[0]
        source_label = {
            "kb_local": "本地知识库",
            "apcom_docs": "git文档仓",
            "ticket_case": "历史工单",
        }.get(top_result.get("source_kind", ""), "知识源")
        base_text = (
            f"{source_label}显示“{top_result.get('name', '相关资料')}”与“{query}”最相关。"
            f"建议优先参考：{top_result.get('summary', '')}"
        )
        if len(results) > 1:
            base_text += f" 同时可交叉查看 {results[1].get('name', '其他资料')}。"
        return self._truncate_text(base_text, 300)

    def _build_fallback_long_answer(self, query: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return "## 问题理解\n未检索到相关知识库内容。\n\n## 处理建议\n建议尝试更换关键词，或补充更明确的业务场景后再次搜索。"

        source_texts = []
        for result in results[:4]:
            detail = self.get_content(result["content_id"]) or result
            raw_content = detail.get("raw_content", "") or detail.get("chunk_text", "")
            doc_name = result.get("name", "未命名文档")
            summary = result.get("summary", "")
            source_texts.append(f"资料《{doc_name}》\n摘要：{summary}\n{raw_content[:900]}")

        combined_sources = "\n\n".join(source_texts)
        top_name = results[0].get("name", "知识库文档")
        top_summary = results[0].get("summary", "")
        return (
            f"## 问题理解\n针对“{query}”，当前命中了 {len(results[:4])} 条来自知识库、git 文档仓和历史工单的相关资料，"
            f"最相关条目为《{top_name}》。\n\n"
            f"## 知识库结论\n"
            f"{top_summary if top_summary else '请参考下方引用资料获取详细信息。'}\n\n"
            f"## 处理建议\n"
            f"建议先依据资料中已有能力、历史案例和上下游约束梳理处理路径，再结合业务现场进一步确认。\n\n"
            f"## 参考资料\n{combined_sources}"
        )

    def _build_qa_prompt(self, query: str, mode: str, results: list[dict[str, Any]]) -> str:
        context_parts = []
        for index, result in enumerate(results, start=1):
            detail = self.get_content(result["content_id"]) or result
            raw = (detail.get("raw_content", "") or detail.get("chunk_text", "")).strip()
            doc_name = result.get("name", f"资料{index}")
            summary = result.get("summary", "")
            source_kind = result.get("source_kind", "")
            context_parts.append(
                f"【资料{index} | {source_kind}】{doc_name}\n"
                f"摘要：{summary}\n"
                f"正文：{raw[:2000] if raw else '（无正文）'}"
            )
        context_text = "\n\n---\n\n".join(context_parts)
        if mode == "short":
            mode_hint = (
                "用300字以内简洁回答。先给结论，再补细节。"
                "像资深顾问和同事沟通一样，不要用模板化的开场白。"
            )
        else:
            mode_hint = (
                "用500字以上详细回答，分节说明。"
                "结构: 结论 → 具体操作步骤 → 注意事项。"
                "引用具体资料名称作为依据。"
            )
        return (
            "你是流程中心产品的资深顾问。请基于下方资料直接回答用户问题。\n\n"
            "要求:\n"
            "- 简洁清晰，像同事间沟通一样，不要客套和模板化开头\n"
            "- 先给结论，再补充操作步骤或细节\n"
            "- 引用具体资料名称作为依据（如「参见《xxx》」）\n"
            "- 相关数据和配置路径要准确完整\n"
            "- 如果资料不完全覆盖问题，说「以下是相关参考」并给出最接近的内容\n"
            "- 禁止编造资料中未提到的信息\n\n"
            f"用户问题：{query}\n\n"
            f"输出要求：{mode_hint}\n\n"
            f"参考资料：\n{context_text}"
        )

    def _is_valid_qa_answer(self, answer_text: str, query: str, results: list[dict[str, Any]]) -> bool:
        text = re.sub(r"\s+", " ", (answer_text or "")).strip()
        if not text or len(text) < 24:
            return False

        generic_patterns = (
            r"\bhello\b",
            r"\bhi\b",
            r"i'?m here to help",
            r"feel free to share",
            r"what would you like to analyze today",
            r"很高兴为你",
            r"请告诉我你想分析什么",
            r"我可以帮你分析",
        )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in generic_patterns):
            return False

        search_terms = set(self._search_tokens(query))
        evidence_terms: set[str] = set()
        for item in results[:4]:
            evidence_terms.update(self._search_tokens(item.get("name", "")))
            evidence_terms.update(self._search_tokens(item.get("summary", "")))
            evidence_terms.update(self._search_tokens(" ".join(item.get("keywords", [])[:6])))
            evidence_terms.update(self._search_tokens(" ".join(item.get("topic_names", [])[:4])))
            evidence_terms.update(self._search_tokens(item.get("l1_module", "")))
            evidence_terms.update(self._search_tokens(item.get("l2_module", "")))

        informative_terms = {term for term in search_terms | evidence_terms if len(term) >= 2}
        if not informative_terms:
            return True

        lowered = text.lower()
        hits = 0
        for term in informative_terms:
            if term.lower() in lowered:
                hits += 1
                if hits >= 1:
                    return True
        return False

    def _extract_text(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in {".md", ".txt", ".csv", ".sql", ".html", ".xml"}:
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return path.read_text(encoding="utf-8", errors="ignore")
        if ext == ".docx":
            return self._extract_docx_text(path)
        if ext == ".pptx":
            return self._extract_pptx_text(path)
        if ext == ".xlsx":
            return self._extract_xlsx_text(path)
        return ""

    def _extract_docx_text(self, path: Path) -> str:
        try:
            with zipfile.ZipFile(path) as zf:
                root = ET.fromstring(zf.read("word/document.xml"))
                texts = [node.text for node in root.findall(".//w:t", DOCX_NS) if node.text]
                return "\n".join(texts)
        except Exception:
            return ""

    def _extract_pptx_text(self, path: Path) -> str:
        texts: list[str] = []
        try:
            with zipfile.ZipFile(path) as zf:
                for name in sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")):
                    root = ET.fromstring(zf.read(name))
                    texts.extend([node.text for node in root.findall(".//a:t", PPTX_NS) if node.text])
        except Exception:
            return ""
        return "\n".join(texts)

    def _extract_xlsx_text(self, path: Path) -> str:
        lines: list[str] = []
        try:
            with zipfile.ZipFile(path) as zf:
                shared = self._read_shared_strings(zf)
                workbook = ET.fromstring(zf.read("xl/workbook.xml"))
                rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
                rel_map = {rel.attrib.get("Id"): rel.attrib.get("Target", "") for rel in rels.findall(".//r:Relationship", XLSX_RELS_NS)}
                rel_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                for sheet in workbook.findall(".//s:sheet", XLSX_NS):
                    rel_id = sheet.attrib.get(rel_attr)
                    target = rel_map.get(rel_id, "")
                    if not target:
                        continue
                    root = ET.fromstring(zf.read(f"xl/{target}"))
                    for row in root.findall(".//s:row", XLSX_NS):
                        row_values: list[str] = []
                        for cell in row.findall("./s:c", XLSX_NS):
                            row_values.append(self._extract_cell_value(cell, shared))
                        text = "\t".join(v for v in row_values if v).strip()
                        if text:
                            lines.append(text)
        except Exception:
            return ""
        return "\n".join(lines)

    def _read_shared_strings(self, zf: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        except Exception:
            return []
        values: list[str] = []
        for si in root.findall(".//s:si", XLSX_NS):
            values.append("".join(node.text for node in si.findall(".//s:t", XLSX_NS) if node.text))
        return values

    def _extract_cell_value(self, cell: ET.Element, shared: list[str]) -> str:
        cell_type = cell.attrib.get("t", "")
        if cell_type == "s":
            v = cell.find("./s:v", XLSX_NS)
            if v is not None and v.text and v.text.isdigit():
                idx = int(v.text)
                if 0 <= idx < len(shared):
                    return shared[idx]
            return ""
        if cell_type == "inlineStr":
            t = cell.find("./s:is/s:t", XLSX_NS)
            return t.text.strip() if t is not None and t.text else ""
        v = cell.find("./s:v", XLSX_NS)
        return v.text.strip() if v is not None and v.text else ""

    def _build_summary(self, text: str, max_len: int = 180) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        summary = " ".join(lines[:3])
        return summary if len(summary) <= max_len else summary[: max_len - 1] + "…"

    def _extract_keywords(self, text: str, limit: int = 8) -> list[str]:
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", text)
        normalized = [token.lower() for token in tokens if token.lower() not in STOPWORDS]
        counts = Counter(normalized)
        return [token for token, _ in counts.most_common(limit)]

    def _strip_numeric_prefix(self, text: str) -> str:
        return re.sub(r"^\d+[.\-、]*", "", text).strip()

    def _topic_to_dict(self, topic: TopicNode) -> dict[str, Any]:
        return {
            "topic_id": topic.topic_id,
            "name": topic.name,
            "level": topic.level,
            "keywords": topic.keywords,
        }
