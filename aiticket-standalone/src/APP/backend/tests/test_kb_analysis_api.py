import json
import os
import sys
import importlib
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kb_analysis import KBAnalyzer, resolve_kb_dir


def _write_manifest(kb_root: Path, contents: dict) -> None:
    manifest_path = kb_root / "INDEX" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-03-15T00:00:00",
                "source_files": len(contents),
                "converted_files": len(contents),
                "contents": contents,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_resolve_kb_dir_falls_back_to_shared_repo_when_worktree_kb_is_sparse(tmp_path):
    repo_root = tmp_path / "aiticket"
    shared_kb_root = repo_root / "KB"
    worktree_root = repo_root / ".worktrees" / "feature-a"
    worktree_kb_root = worktree_root / "KB"

    worktree_kb_root.mkdir(parents=True, exist_ok=True)
    (worktree_kb_root / "index.json").write_text("{}", encoding="utf-8")
    _write_manifest(shared_kb_root, {})

    assert resolve_kb_dir(worktree_root) == shared_kb_root


def test_manifest_and_search_use_pipeline_manifest_shape(tmp_path):
    kb_root = tmp_path / "KB"
    converted_path = kb_root / "OUTPUT" / "converted" / "流程中心" / "流程监控能力.md"
    converted_path.parent.mkdir(parents=True, exist_ok=True)
    converted_path.write_text(
        "流程监控支持未来审批人查询，用于定位审批链路。",
        encoding="utf-8",
    )

    _write_manifest(
        kb_root,
        {
            "CNT-0001": {
                "content_id": "CNT-0001",
                "source_path": str(kb_root / "流程中心" / "流程监控能力.docx"),
                "source_rel_path": "流程中心/流程监控能力.docx",
                "ext": ".docx",
                "name": "流程监控能力",
                "summary": "流程监控支持查询未来审批人。",
                "keywords": ["流程监控", "未来审批人"],
                "converted_path": str(converted_path),
                "l1_index_id": "IDX-L1-monitor",
                "l2_index_id": "IDX-L2-monitor-docs",
                "index_id": "IDX-F-CNT-0001",
                "l1_name": "流程中心",
                "l2_name": "流程中心/产品培训课件",
            },
            "CNT-0002": {
                "content_id": "CNT-0002",
                "source_path": str(kb_root / "APP" / "ARCHITECTURE.md"),
                "source_rel_path": "APP/ARCHITECTURE.md",
                "ext": ".md",
                "name": "架构说明",
                "summary": "APP 架构说明。",
                "keywords": ["架构", "APP"],
                "converted_path": str(kb_root / "APP" / "ARCHITECTURE.md"),
                "l1_index_id": "IDX-L1-app",
                "l2_index_id": "IDX-L2-app-docs",
                "index_id": "IDX-F-CNT-0002",
                "l1_name": "APP",
                "l2_name": "APP/backend",
            },
        },
    )

    analyzer = KBAnalyzer(kb_dir=kb_root)

    manifest = analyzer.get_manifest()
    results = analyzer.search_knowledge("未来审批人", top_k=5)

    assert manifest["total_count"] == 2
    assert manifest["sources"]["apcom_docs"]["count"] == 1
    assert manifest["sources"]["app_docs"]["count"] == 1
    assert any(topic["topic_id"] == "IDX-L2-monitor-docs" for topic in manifest["topics"])
    assert results[0]["content_id"] == "CNT-0001"
    assert results[0]["citation_label"] == "流程中心/流程监控能力.docx"
    assert results[0]["source_kind"] == "apcom_docs"
    assert "IDX-L2-monitor-docs" in results[0]["topic_ids"]
    assert "流程中心/产品培训课件" in results[0]["topic_names"]
    assert results[0]["content_url"].endswith("/api/kb/content/CNT-0001")
    assert results[0]["metadata_url"].endswith("/api/kb/metadata/CNT-0001")


def test_get_content_returns_detail_fields_and_converted_markdown(tmp_path):
    kb_root = tmp_path / "KB"
    converted_path = kb_root / "OUTPUT" / "converted" / "流程中心" / "帮助文档-流程监控.md"
    converted_path.parent.mkdir(parents=True, exist_ok=True)
    converted_path.write_text(
        "## 功能描述\n流程监控支持结束审批环节和人工干预。",
        encoding="utf-8",
    )

    _write_manifest(
        kb_root,
        {
            "CNT-0005": {
                "content_id": "CNT-0005",
                "source_path": str(kb_root / "流程中心" / "帮助文档-流程监控.docx"),
                "source_rel_path": "流程中心/帮助文档-流程监控.docx",
                "ext": ".docx",
                "name": "帮助文档-流程监控",
                "summary": "流程监控帮助文档。",
                "keywords": ["流程监控", "人工干预"],
                "converted_path": str(converted_path),
                "l1_index_id": "IDX-L1-monitor",
                "l2_index_id": "IDX-L2-monitor-docs",
                "index_id": "IDX-F-CNT-0005",
                "l1_name": "流程中心",
                "l2_name": "流程中心/产品培训课件",
            }
        },
    )

    analyzer = KBAnalyzer(kb_dir=kb_root)
    item = analyzer.get_content("CNT-0005")

    assert item["citation_label"] == "流程中心/帮助文档-流程监控.docx"
    assert item["l1_module"] == "流程中心"
    assert item["l2_module"] == "流程中心/产品培训课件"
    assert item["doc_type"] == ".docx"
    assert item["source_kind"] == "apcom_docs"
    assert item["topic_names"] == ["流程中心", "流程中心/产品培训课件", "帮助文档-流程监控"]
    assert item["raw_content"].startswith("## 功能描述")


def test_get_content_supports_repo_relative_converted_path(tmp_path):
    kb_root = tmp_path / "KB"
    converted_path = kb_root / "OUTPUT" / "converted" / "流程中心" / "帮助文档-流程监控.md"
    converted_path.parent.mkdir(parents=True, exist_ok=True)
    converted_path.write_text(
        "## 功能描述\n流程监控支持未来审批人查询。",
        encoding="utf-8",
    )

    _write_manifest(
        kb_root,
        {
            "CNT-0011": {
                "content_id": "CNT-0011",
                "source_path": "KB/流程中心/帮助文档-流程监控.docx",
                "source_rel_path": "流程中心/帮助文档-流程监控.docx",
                "ext": ".docx",
                "name": "帮助文档-流程监控",
                "summary": "流程监控帮助文档。",
                "keywords": ["流程监控", "未来审批人"],
                "converted_path": "KB/OUTPUT/converted/流程中心/帮助文档-流程监控.md",
                "l1_index_id": "IDX-L1-monitor",
                "l2_index_id": "IDX-L2-monitor-docs",
                "index_id": "IDX-F-CNT-0011",
                "l1_name": "流程中心",
                "l2_name": "流程中心/产品培训课件",
            }
        },
    )

    analyzer = KBAnalyzer(kb_dir=kb_root)
    item = analyzer.get_content("CNT-0011")

    assert item is not None
    assert "未来审批人查询" in item["raw_content"]


def test_get_content_prefers_current_repo_when_absolute_project_path_still_exists(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    kb_root = repo_root / "KB"
    converted_path = kb_root / "OUTPUT" / "converted" / "流程中心" / "帮助文档-流程监控.md"
    converted_path.parent.mkdir(parents=True, exist_ok=True)
    converted_path.write_text(
        "## 功能描述\n流程监控支持未来审批人查询。",
        encoding="utf-8",
    )

    _write_manifest(
        kb_root,
        {
            "CNT-0012": {
                "content_id": "CNT-0012",
                "source_path": "/Volumes/MacMini/Users/cfone/Studio/aiticket/KB/流程中心/帮助文档-流程监控.docx",
                "source_rel_path": "流程中心/帮助文档-流程监控.docx",
                "ext": ".docx",
                "name": "帮助文档-流程监控",
                "summary": "流程监控帮助文档。",
                "keywords": ["流程监控", "未来审批人"],
                "converted_path": "/Volumes/MacMini/Users/cfone/Studio/aiticket/KB/OUTPUT/converted/流程中心/帮助文档-流程监控.md",
                "l1_index_id": "IDX-L1-monitor",
                "l2_index_id": "IDX-L2-monitor-docs",
                "index_id": "IDX-F-CNT-0012",
                "l1_name": "流程中心",
                "l2_name": "流程中心/产品培训课件",
            }
        },
    )

    old_absolute_path = "/Volumes/MacMini/Users/cfone/Studio/aiticket/KB/OUTPUT/converted/流程中心/帮助文档-流程监控.md"
    real_exists = Path.exists
    real_read_text = Path.read_text

    def fake_exists(path_obj: Path) -> bool:
        if str(path_obj) == old_absolute_path:
            return True
        return real_exists(path_obj)

    def fake_read_text(path_obj: Path, *args, **kwargs) -> str:
        if str(path_obj) == old_absolute_path:
            return "旧仓库副本内容"
        return real_read_text(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    analyzer = KBAnalyzer(kb_dir=kb_root)
    item = analyzer.get_content("CNT-0012")

    assert item is not None
    assert "未来审批人查询" in item["raw_content"]


def test_get_metadata_returns_structured_manifest_item(tmp_path):
    kb_root = tmp_path / "KB"
    _write_manifest(
        kb_root,
        {
            "CNT-0009": {
                "content_id": "CNT-0009",
                "source_path": str(kb_root / "流程中心" / "流程设计器.docx"),
                "source_rel_path": "流程中心/流程设计器.docx",
                "ext": ".docx",
                "name": "流程设计器",
                "summary": "流程设计器说明。",
                "keywords": ["流程设计器", "流程"],
                "converted_path": str(kb_root / "OUTPUT" / "converted" / "流程中心" / "流程设计器.md"),
                "l1_index_id": "IDX-L1-monitor",
                "l2_index_id": "IDX-L2-design",
                "index_id": "IDX-F-CNT-0009",
                "l1_name": "流程中心",
                "l2_name": "流程中心/设计器",
            }
        },
    )

    analyzer = KBAnalyzer(kb_dir=kb_root)
    metadata = analyzer.get_metadata("CNT-0009")

    assert metadata["content_id"] == "CNT-0009"
    assert metadata["topic_names"] == ["流程中心", "流程中心/设计器", "流程设计器"]
    assert metadata["metadata_url"].endswith("/api/kb/metadata/CNT-0009")


def test_answer_question_short_mode_uses_llm_and_truncates_to_300_chars(tmp_path):
    kb_root = tmp_path / "KB"
    _write_manifest(
        kb_root,
        {
            "CNT-0003": {
                "content_id": "CNT-0003",
                "source_path": str(kb_root / "流程中心" / "流程监控能力.docx"),
                "source_rel_path": "流程中心/流程监控能力.docx",
                "ext": ".docx",
                "name": "流程监控能力",
                "summary": "流程监控帮助说明。",
                "keywords": ["流程监控", "未来审批人"],
                "converted_path": str(kb_root / "OUTPUT" / "converted" / "流程中心" / "流程监控能力.md"),
                "l1_index_id": "IDX-L1-monitor",
                "l2_index_id": "IDX-L2-monitor-docs",
                "index_id": "IDX-F-CNT-0003",
                "l1_name": "流程中心",
                "l2_name": "流程中心/产品培训课件",
            }
        },
    )
    long_answer = "A" * 420

    class StubLLM:
        def call_llm(self, **kwargs):
            return long_answer

    analyzer = KBAnalyzer(llm_service=StubLLM(), kb_dir=kb_root)
    result = analyzer.answer_question("如何查询未来审批人", mode="short", api_key="test-key", provider="openai")

    assert result["used_llm"] is True
    assert result["fallback_used"] is False
    assert len(result["answer_text"]) <= 300
    assert result["answer_html"].startswith("<p>")


def test_answer_question_long_mode_falls_back_to_rich_text_over_500_chars(tmp_path):
    kb_root = tmp_path / "KB"
    converted_path = kb_root / "OUTPUT" / "converted" / "流程中心" / "帮助文档-流程监控.md"
    converted_path.parent.mkdir(parents=True, exist_ok=True)
    converted_path.write_text(
        "流程监控支持未来审批人查询。管理员可以通过流程监控页面观察实例状态、异常日志、干预日志，并结合流程预测、人工调整和结束审批环节等能力定位审批链路问题。"
        * 8,
        encoding="utf-8",
    )
    _write_manifest(
        kb_root,
        {
            "CNT-0010": {
                "content_id": "CNT-0010",
                "source_path": str(kb_root / "流程中心" / "帮助文档-流程监控.docx"),
                "source_rel_path": "流程中心/帮助文档-流程监控.docx",
                "ext": ".docx",
                "name": "帮助文档-流程监控",
                "summary": "流程监控帮助文档。",
                "keywords": ["流程监控", "未来审批人", "人工调整"],
                "converted_path": str(converted_path),
                "l1_index_id": "IDX-L1-monitor",
                "l2_index_id": "IDX-L2-monitor-docs",
                "index_id": "IDX-F-CNT-0010",
                "l1_name": "流程中心",
                "l2_name": "流程中心/产品培训课件",
            }
        },
    )

    analyzer = KBAnalyzer(kb_dir=kb_root)
    result = analyzer.answer_question("流程监控如何查询未来审批人", mode="long")

    assert result["used_llm"] is False
    assert result["fallback_used"] is True
    assert len(result["answer_text"]) >= 500
    assert "<h2>" in result["answer_html"]
    assert result["sources"][0]["content_id"] == "CNT-0010"


def test_answer_question_no_hit_returns_explicit_empty_message(tmp_path):
    kb_root = tmp_path / "KB"
    _write_manifest(kb_root, {})

    analyzer = KBAnalyzer(kb_dir=kb_root)
    result = analyzer.answer_question("完全不存在的问题", mode="short")

    assert result["answer_text"] == "未检索到相关知识库内容"
    assert result["sources"] == []
    assert result["fallback_used"] is True


def test_answer_question_llm_prompt_contains_document_content(monkeypatch):
    """LLM prompt必须包含文档正文，不能只有元数据JSON"""
    from unittest.mock import MagicMock, patch

    analyzer = KBAnalyzer.__new__(KBAnalyzer)
    analyzer.kb_dir = Path("/tmp/test_kb")
    analyzer.llm_service = MagicMock()

    fake_results = [{"content_id": "doc1", "name": "测试文档", "summary": "摘要", "keywords": [], "relevance": 10}]

    def mock_get_content(content_id):
        return {"raw_content": "这是文档的真实正文内容，包含具体操作步骤和说明。", "name": "测试文档"}

    captured_prompt = []

    def capture_llm(prompt, **kwargs):
        captured_prompt.append(prompt)
        return "基于文档内容的回答"

    with patch.object(analyzer, "search_knowledge", return_value=fake_results), patch.object(
        analyzer, "get_content", side_effect=mock_get_content
    ), patch.object(analyzer.llm_service, "call_llm", side_effect=capture_llm):
        analyzer.answer_question("测试查询", mode="long", api_key="test")

    assert captured_prompt, "LLM应该被调用"
    prompt = captured_prompt[0]
    assert "这是文档的真实正文内容" in prompt, f"LLM prompt中没有文档正文内容: {prompt[:200]}"
    assert "content_id" not in prompt or "raw_content" in prompt, "不应以raw JSON格式传递"


def test_fallback_long_answer_no_hardcoded_content(monkeypatch):
    """fallback不能包含与查询无关的硬编码领域叙述"""
    from unittest.mock import patch

    analyzer = KBAnalyzer.__new__(KBAnalyzer)
    analyzer.kb_dir = Path("/tmp/test_kb")
    analyzer.llm_service = None

    fake_results = [{"content_id": "doc1", "name": "用户权限配置手册", "summary": "权限配置说明", "keywords": []}]

    def mock_get_content(content_id):
        return {"raw_content": "用户权限配置需要在后台管理系统中操作。", "name": "用户权限配置手册"}

    with patch.object(analyzer, "get_content", side_effect=mock_get_content):
        result = analyzer._build_fallback_long_answer("权限配置", fake_results)

    assert "流程监控场景下的核心能力" not in result, "包含与查询无关的硬编码流程监控叙述"
    assert "未来审批人结论容易受分支条件" not in result, "包含硬编码审批链路叙述"
    assert "用户权限配置" in result or "权限" in result, "应包含与文档相关的内容"


def test_main_kb_api_routes_delegate_to_runtime_and_analyzer(monkeypatch):
    monkeypatch.setenv("USE_CHROMA_DEFAULT_EMBEDDING", "true")
    monkeypatch.setenv("ALLOW_EMBEDDING_DOWNLOAD", "false")
    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    class StubRuntimeService:
        def get_manifest(self):
            return {"total_count": 1}

        def get_content(self, content_id: str):
            return {"content_id": content_id}

        def get_metadata(self, content_id: str):
            return {"content_id": content_id, "kind": "metadata"}

        def sync(self, force_refresh: bool = False):
            return {"status": "success", "total_count": 1, "force_refresh": force_refresh}

        def search_bundle(self, query: str, top_k: int = 20, source_kind: str | None = None):
            return {
                "items": [{"content_id": "CNT-0001", "query": query, "top_k": top_k, "source_kind": source_kind}],
                "sources": {"kb_local": {"count": 1}, "apcom_docs": {"count": 0}, "ticket_case": {"count": 0}},
                "source_groups": {"kb_local": [{"content_id": "CNT-0001"}], "apcom_docs": [], "ticket_case": []},
            }

        def answer_question(self, query: str, mode: str = "short", **kwargs):
            return {"query": query, "mode": mode}

    monkeypatch.setattr(main, "kb_runtime_service", StubRuntimeService())

    assert main.get_kb_manifest()["total_count"] == 1
    assert main.get_kb_content("CNT-0001")["content_id"] == "CNT-0001"
    assert main.get_kb_metadata("CNT-0001")["kind"] == "metadata"
    assert main.sync_kb()["force_refresh"] is True
    assert main.search_kb("流程", 5)["items"][0]["top_k"] == 5
    assert main.ask_kb_question(main.KBQuestionRequest(query="流程监控", mode="long"))["mode"] == "long"


def test_main_sync_kb_wraps_runtime_errors(monkeypatch):
    monkeypatch.setenv("USE_CHROMA_DEFAULT_EMBEDDING", "true")
    monkeypatch.setenv("ALLOW_EMBEDDING_DOWNLOAD", "false")
    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    class FailingRuntimeService:
        def sync(self, force_refresh: bool = False):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "kb_runtime_service", FailingRuntimeService())

    with pytest.raises(main.HTTPException) as exc_info:
        main.sync_kb()

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "KB sync failed: boom"
