import json
import os
import sys
import zipfile
from pathlib import Path

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _write_docx(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
                "</w:document>"
            ),
        )


@pytest.fixture
def kb_fixture(tmp_path: Path):
    project_root = tmp_path / "repo"
    kb_root = project_root / "KB"
    apcom_root = tmp_path / "iuap-apcom-docs"
    data_root = project_root / "data"

    (kb_root / "INDEX").mkdir(parents=True, exist_ok=True)
    (kb_root / "OUTPUT" / "converted" / "流程中心" / "产品白皮书").mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    raw_doc_path = kb_root / "流程中心" / "产品白皮书" / "流程管理.docx"
    _write_docx(raw_doc_path, "流程监控与流程设计能力说明。支持事件回调和规则触发。")

    local_doc_path = kb_root / "OUTPUT" / "converted" / "流程中心" / "产品白皮书" / "流程管理.md"
    local_doc_path.write_text(
        "# 流程管理\n\n## 流程监控\n流程监控与流程设计能力说明。\n\n## 接口扩展\n支持事件回调和规则触发。",
        encoding="utf-8",
    )

    manifest = {
        "generated_at": "2026-03-14T15:10:20",
        "source_files": 1,
        "converted_files": 1,
        "root_index_id": "IDX-L0-ROOT",
        "contents": {
            "CNT-0001": {
                "content_id": "CNT-0001",
                "source_path": str(raw_doc_path),
                "source_rel_path": "流程中心/产品白皮书/流程管理.docx",
                "ext": ".docx",
                "name": "流程管理",
                "top_category": "流程中心",
                "second_category": "产品白皮书",
                "converted_path": str(local_doc_path),
                "text_chars": 22,
                "summary": "流程监控与流程设计能力说明。",
                "keywords": ["流程监控", "流程设计", "工作流"],
                "index_id": "IDX-F-CNT-0001",
                "l1_index_id": "IDX-L1-1",
                "l2_index_id": "IDX-L2-1",
                "l1_name": "流程中心",
                "l2_name": "流程中心/产品白皮书",
                "backlink_index_ids": ["IDX-L0-ROOT", "IDX-L1-1", "IDX-L2-1", "IDX-F-CNT-0001"],
                "related_content_ids": [],
                "related_links": [],
            }
        },
    }
    (kb_root / "INDEX" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    (apcom_root / "docs" / "2.应用支撑" / "规则引擎" / "产品概述").mkdir(parents=True, exist_ok=True)
    apcom_doc = apcom_root / "docs" / "2.应用支撑" / "规则引擎" / "产品概述" / "规则引擎产品介绍.md"
    apcom_doc.write_text(
        "# 规则引擎产品介绍\n\n规则引擎支持业务规则定义、流程条件控制和表达式能力。",
        encoding="utf-8",
    )
    (apcom_root / "index.md").write_text("# 文档索引\n\n- 2.应用支撑\n", encoding="utf-8")

    topic_file = project_root / "topic.md"
    topic_file.write_text(
        "# 主题\n\n- [TOP-WF] 工作流\n  - [TOP-WF.MONITOR] 流程监控\n- [TOP-APCOM] 应用与开发平台\n  - [TOP-APCOM.SUPPORT.RULE] 规则引擎\n",
        encoding="utf-8",
    )

    return {
        "project_root": project_root,
        "kb_root": kb_root,
        "apcom_root": apcom_root,
        "topic_file": topic_file,
        "sqlite_path": data_root / "sqlite" / "kb_chunks.db",
        "chroma_path": data_root / "chroma_kb",
        "ticket_chroma_path": data_root / "ticket_chroma",
    }


def test_loads_local_manifest_and_apcom_docs(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    manifest = service.get_manifest()

    assert manifest["sources"]["kb_local"]["count"] == 1
    assert manifest["sources"]["apcom_docs"]["count"] == 1
    assert manifest["total_count"] == 2


def test_search_returns_multi_source_results(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    results = service.search("规则引擎", top_k=10)
    assert len(results) >= 1
    assert any(item["source_kind"] == "apcom_docs" for item in results)
    assert results[0]["citation_label"].startswith("[")
    assert any(item.get("chunk_id") for item in results)
    assert any(item.get("match_type") == "chunk" for item in results)


def test_get_content_reads_repo_relative_converted_path(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    manifest = json.loads((kb_fixture["kb_root"] / "INDEX" / "manifest.json").read_text(encoding="utf-8"))
    manifest["contents"]["CNT-0001"]["source_path"] = "KB/流程中心/产品白皮书/流程管理.docx"
    manifest["contents"]["CNT-0001"]["converted_path"] = "KB/OUTPUT/converted/流程中心/产品白皮书/流程管理.md"
    (kb_fixture["kb_root"] / "INDEX" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    content = service.get_content("CNT-0001")

    assert content is not None
    assert "流程监控与流程设计能力说明" in content["raw_content"]


def test_get_content_reroots_missing_absolute_paths_to_project_root(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    manifest = json.loads((kb_fixture["kb_root"] / "INDEX" / "manifest.json").read_text(encoding="utf-8"))
    manifest["contents"]["CNT-0001"]["source_path"] = "/Volumes/MacMini/Users/cfone/Studio/aiticket/KB/流程中心/产品白皮书/流程管理.docx"
    manifest["contents"]["CNT-0001"]["converted_path"] = "/Volumes/MacMini/Users/cfone/Studio/aiticket/KB/OUTPUT/converted/流程中心/产品白皮书/流程管理.md"
    (kb_fixture["kb_root"] / "INDEX" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    content = service.get_content("CNT-0001")

    assert content is not None
    assert "接口扩展" in content["raw_content"]


def test_sync_builds_local_manifest_from_raw_source_file(tmp_path: Path):
    from kb_runtime_service import KnowledgeRuntimeService

    project_root = tmp_path / "repo"
    kb_root = project_root / "KB"
    apcom_root = tmp_path / "iuap-apcom-docs"
    data_root = project_root / "data"
    topic_file = project_root / "topic.md"

    source_file = kb_root / "流程中心" / "帮助文档" / "流程监控说明.md"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "# 流程监控说明\n\n流程监控支持未来审批查询和人工干预。",
        encoding="utf-8",
    )
    topic_file.write_text("# 主题\n\n- [TOP-WF] 工作流\n", encoding="utf-8")

    service = KnowledgeRuntimeService(
        project_root=project_root,
        kb_root=kb_root,
        apcom_root=apcom_root,
        topic_file=topic_file,
        sqlite_path=data_root / "sqlite" / "kb_chunks.db",
        chroma_path=data_root / "chroma_kb",
        ticket_chroma_path=data_root / "ticket_chroma",
    )

    result = service.sync(force_refresh=True)

    manifest = json.loads((kb_root / "INDEX" / "manifest.json").read_text(encoding="utf-8"))
    item = next(iter(manifest["contents"].values()))
    converted_path = project_root / item["converted_path"]

    assert result["ok"] is True
    assert result["local_manifest_count"] == 1
    assert item["source_path"] == "KB/流程中心/帮助文档/流程监控说明.md"
    assert item["converted_path"] == "KB/OUTPUT/converted/流程中心/帮助文档/流程监控说明.md"
    assert item["parse_status"] == "parsed"
    assert converted_path.exists()
    assert "未来审批查询" in converted_path.read_text(encoding="utf-8")


def test_search_bundle_includes_ticket_cases_and_source_groups(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    service._search_ticket_cases = lambda query, top_k=5: [
        {
            "content_id": "TICKET-MYPROJECT-1001",
            "chunk_id": "TICKET-MYPROJECT-1001",
            "source_kind": "ticket_case",
            "source_repo": "ticket_chroma",
            "name": "MYPROJECT-1001 流程监控异常",
            "summary": "历史工单记录了流程监控页面异常与处理方案。",
            "keywords": ["流程监控", "异常监控"],
            "source_rel_path": "MYPROJECT-1001",
            "source_path": "MYPROJECT-1001",
            "converted_path": "",
            "index_path": "",
            "l1_module": "工单案例",
            "l2_module": "流程中心",
            "doc_type": "JIRA工单",
            "topic_ids": ["TOP-WF.MONITOR"],
            "citation_label": "[TICKET] MYPROJECT-1001",
            "related_content_ids": [],
            "backlink_index_ids": [],
            "chunk_text": "流程监控页面异常，涉及未来审批链路查询。",
            "chunk_preview": "流程监控页面异常，涉及未来审批链路查询。",
            "match_type": "ticket",
            "score": 0.91,
        }
    ]

    bundle = service.search_bundle("规则引擎 流程监控", top_k=10)

    assert bundle["sources"]["kb_local"]["count"] >= 1
    assert bundle["sources"]["apcom_docs"]["count"] >= 1
    assert bundle["sources"]["ticket_case"]["count"] >= 1
    assert bundle["source_groups"]["ticket_case"][0]["content_id"] == "TICKET-MYPROJECT-1001"
    assert any(item["source_kind"] == "ticket_case" for item in bundle["items"])
    assert bundle["query_profile"]["query_intent"] in {"principle", "operation", "mixed"}
    assert bundle["primary_materials"]
    assert all(item["source_kind"] != "ticket_case" for item in bundle["primary_materials"])
    assert bundle["ticket_summary"]["related_count"] >= 1
    assert bundle["relevance_summary"]["high_count"] + bundle["relevance_summary"]["medium_count"] + bundle["relevance_summary"]["low_count"] == len(bundle["items"])


def test_search_bundle_expands_lian_gang_alias_terms(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    service._search_ticket_cases = lambda query, top_k=5: []
    service._load_kb_items = lambda: [
        {
            "content_id": "CNT-9001",
            "source_kind": "kb_local",
            "source_repo": "KB",
            "name": "代理审批与同一处理人自动去重",
            "summary": "支持代理审批、审批委托，以及同一处理人在流程中出现多次时自动去重。",
            "keywords": ["代理审批", "审批委托", "同一处理人自动去重"],
            "source_rel_path": "流程中心/帮助文档/代理审批.md",
            "source_path": "流程中心/帮助文档/代理审批.md",
            "converted_path": "",
            "index_path": "",
            "l1_module": "流程中心",
            "l2_module": "帮助文档",
            "doc_type": "帮助文档",
            "topic_ids": ["TOP-WF"],
            "citation_label": "[KB] 流程中心/帮助文档/代理审批.md",
            "related_content_ids": [],
            "backlink_index_ids": [],
        }
    ]
    service._manifest_cache = None

    bundle = service.search_bundle("连岗审批该怎么设置", top_k=5)

    assert bundle["items"]
    assert bundle["items"][0]["content_id"] == "CNT-9001"


def test_ticket_case_content_is_metadata_only(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service._get_ticket_case_by_issue_key = lambda issue_key: {
        "content_id": f"TICKET-{issue_key}",
        "chunk_id": f"TICKET-{issue_key}",
        "source_kind": "ticket_case",
        "source_repo": "ticket_chroma",
        "name": f"{issue_key} 连岗审批未生效",
        "summary": "客户反馈连岗审批配置后未自动审批。",
        "keywords": ["连岗审批", "代理审批"],
        "source_rel_path": issue_key,
        "source_path": issue_key,
        "converted_path": "",
        "index_path": "",
        "l1_module": "工单案例",
        "l2_module": "工作流设计",
        "doc_type": "JIRA工单",
        "topic_ids": ["TOP-WF"],
        "citation_label": f"[TICKET] {issue_key}",
        "related_content_ids": [],
        "backlink_index_ids": [],
        "chunk_text": "这里是一大段不应该直接暴露给 KB 页面的工单正文。",
        "chunk_preview": "这里是一大段不应该直接暴露给 KB 页面的工单正文。",
        "match_type": "ticket",
        "score": 1.0,
        "ticket_metadata": {"issue_key": issue_key, "module": "工作流设计"},
    }

    content = service.get_content("TICKET-MYPROJECT-1001")

    assert content is not None
    assert content["source_kind"] == "ticket_case"
    assert content["display_mode"] == "metadata_only"
    assert content["raw_content"] == ""
    assert content["metadata_url"].endswith("/api/kb/metadata/TICKET-MYPROJECT-1001")


def test_get_content_keeps_apcom_source_content(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    results = service.search("规则引擎", top_k=3)
    apcom_item = next(item for item in results if item["source_kind"] == "apcom_docs")

    content = service.get_content(apcom_item["content_id"])

    assert content is not None
    assert content["source_kind"] == "apcom_docs"
    assert "规则引擎" in content["raw_content"]
    assert "流程监控与流程设计能力说明" not in content["raw_content"]


def test_analyze_returns_evidence_sections_questions(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service._search_ticket_cases = lambda query, top_k=5: [
        {
            "content_id": "TICKET-MYPROJECT-1001",
            "source_kind": "ticket_case",
            "source_repo": "ticket_chroma",
            "name": "MYPROJECT-1001 流程监控异常",
            "summary": "历史工单记录了流程监控页面异常与处理方案。",
            "keywords": ["流程监控", "异常监控"],
            "source_rel_path": "MYPROJECT-1001",
            "source_path": "MYPROJECT-1001",
            "converted_path": "",
            "index_path": "",
            "l1_module": "工单案例",
            "l2_module": "流程中心",
            "doc_type": "JIRA工单",
            "topic_ids": ["TOP-WF.MONITOR"],
            "citation_label": "[TICKET] MYPROJECT-1001",
            "related_content_ids": [],
            "backlink_index_ids": [],
            "score": 0.9,
        }
    ]

    result = service.analyze(
        summary="需要补充流程规则和流程监控相关需求分析",
        module_hint="工作流",
        top_k=5,
    )

    assert result["matched_count"] >= 1
    assert result["evidence"]
    assert result["suggested_sections"]
    assert "open_questions" in result
    assert any("TOP-" in topic_id for topic_id in result["topic_ids"])
    assert any(item["source_kind"] == "ticket_case" for item in result["evidence"])
    assert result["category_stats"]


def test_sync_builds_hybrid_index_artifacts(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    result = service.sync(force_refresh=True)

    assert result["ok"] is True
    assert result["chunk_count"] >= 2
    assert kb_fixture["sqlite_path"].exists()
    # compact 已迁 sqlite-vec：向量并入 sqlite，不再产生独立 chroma 目录


def test_sync_builds_manifest_from_raw_kb_sources(tmp_path: Path):
    from kb_runtime_service import KnowledgeRuntimeService

    project_root = tmp_path / "repo"
    kb_root = project_root / "KB"
    data_root = project_root / "data"
    topic_file = project_root / "topic.md"
    source_file = kb_root / "流程中心" / "帮助文档" / "流程监控说明.md"

    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "# 流程监控说明\n\n流程监控支持未来审批人查询和人工干预。",
        encoding="utf-8",
    )
    topic_file.write_text("# 主题\n", encoding="utf-8")

    service = KnowledgeRuntimeService(
        project_root=project_root,
        kb_root=kb_root,
        apcom_root=tmp_path / "missing-apcom",
        topic_file=topic_file,
        sqlite_path=data_root / "sqlite" / "kb_chunks.db",
        chroma_path=data_root / "chroma_kb",
        ticket_chroma_path=data_root / "ticket_chroma",
    )

    result = service.sync(force_refresh=True)
    manifest = json.loads((kb_root / "INDEX" / "manifest.json").read_text(encoding="utf-8"))
    item = next(iter(manifest["contents"].values()))

    assert result["ok"] is True
    assert result["source_files"] == 1
    assert result["local_manifest_count"] == 1
    assert item["source_path"] == "KB/流程中心/帮助文档/流程监控说明.md"
    assert item["converted_path"] == "KB/OUTPUT/converted/流程中心/帮助文档/流程监控说明.md"
    assert item["parse_status"] == "parsed"
    assert (project_root / item["converted_path"]).exists()
    assert result["chunk_count"] >= 1


def test_draft_returns_markdown_citations_and_todo_items(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    draft = service.draft(
        summary="需要补充规则引擎和流程监控的一体化能力方案",
        module_hint="工作流",
        top_k=4,
    )

    assert "# PRD初稿" in draft["prd_markdown"]
    assert draft["citations"]
    assert draft["todo_items"]
    assert draft["evidence"]


def test_answer_question_returns_multi_source_groups(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    service._search_ticket_cases = lambda query, top_k=5: [
        {
            "content_id": "TICKET-MYPROJECT-1001",
            "chunk_id": "TICKET-MYPROJECT-1001",
            "source_kind": "ticket_case",
            "source_repo": "ticket_chroma",
            "name": "MYPROJECT-1001 流程监控异常",
            "summary": "历史工单记录了流程监控页面异常与处理方案。",
            "keywords": ["流程监控", "异常监控"],
            "source_rel_path": "MYPROJECT-1001",
            "source_path": "MYPROJECT-1001",
            "converted_path": "",
            "index_path": "",
            "l1_module": "工单案例",
            "l2_module": "流程中心",
            "doc_type": "JIRA工单",
            "topic_ids": ["TOP-WF.MONITOR"],
            "citation_label": "[TICKET] MYPROJECT-1001",
            "related_content_ids": [],
            "backlink_index_ids": [],
            "chunk_text": "流程监控页面异常，涉及未来审批链路查询。",
            "chunk_preview": "流程监控页面异常，涉及未来审批链路查询。",
            "match_type": "ticket",
            "score": 0.91,
        }
    ]

    result = service.answer_question("规则引擎与流程监控如何结合", mode="short")

    assert result["sources"]
    assert result["source_groups"]["apcom_docs"]
    assert result["source_groups"]["kb_local"]
    assert result["source_groups"]["ticket_case"]
    assert result["query_profile"]["query_intent"] == "mixed"
    assert result["primary_materials"]
    assert all(item["source_kind"] != "ticket_case" for item in result["primary_materials"])
    assert result["ticket_summary"]["related_count"] >= 1


def test_operation_query_prefers_kb_local_and_exposes_relevance_reason(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    service._search_ticket_cases = lambda query, top_k=5: [
        {
            "content_id": "TICKET-MYPROJECT-1001",
            "chunk_id": "TICKET-MYPROJECT-1001",
            "source_kind": "ticket_case",
            "source_repo": "ticket_chroma",
            "name": "MYPROJECT-1001 流程监控权限细化",
            "summary": "开启流程监控后用户权限过大，可以干预流程执行。",
            "keywords": ["流程监控", "权限", "干预"],
            "source_rel_path": "MYPROJECT-1001",
            "source_path": "MYPROJECT-1001",
            "converted_path": "",
            "index_path": "",
            "l1_module": "工单案例",
            "l2_module": "流程中心",
            "doc_type": "JIRA工单",
            "topic_ids": ["TOP-WF.MONITOR"],
            "citation_label": "[TICKET] MYPROJECT-1001",
            "related_content_ids": [],
            "backlink_index_ids": [],
            "chunk_text": "开启流程监控后用户权限过大，可以干预流程执行。",
            "chunk_preview": "开启流程监控后用户权限过大，可以干预流程执行。",
            "match_type": "ticket",
            "score": 0.91,
        }
    ]

    result = service.analyze("流程监控中如何干预流程", module_hint="流程监控", top_k=10)

    assert result["matched_count"] == len(result["evidence"])
    assert result["query_profile"]["query_intent"] == "operation"
    assert result["primary_materials"][0]["source_kind"] == "kb_local"
    assert result["primary_materials"][0]["relevance_level"] in {"high", "medium", "low"}
    assert result["primary_materials"][0]["relevance_reason"]
    assert result["ticket_summary"]["items"]


def test_answer_question_rejects_generic_llm_greeting_and_falls_back(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    class StubLLM:
        def call_llm(self, **kwargs):
            return (
                "Hello! I'm here to help with any analysis you need. "
                "Whether it's data, text, business problems, or any other topic, "
                "feel free to share the details."
            )

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
        llm_service=StubLLM(),
    )

    service.sync(force_refresh=True)
    result = service.answer_question("规则引擎与流程监控如何结合", mode="short", api_key="fake-key")

    assert result["fallback_used"] is True
    assert result["used_llm"] is False
    assert "Hello!" not in result["answer_text"]
    assert "规则引擎" in result["answer_text"] or "流程监控" in result["answer_text"]


def test_review_flags_missing_sections_and_unsupported_claims(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service.sync(force_refresh=True)
    review = service.review(
        summary="需要补充规则引擎和流程监控的一体化能力方案",
        draft_markdown="# 需求背景\n系统已经完全支持所有场景。\n",
        module_hint="工作流",
        top_k=4,
    )

    assert review["coverage_gaps"]
    assert review["unsupported_claims"]
    assert "missing_evidence" in review


def test_topic_parser_extracts_topic_ids(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    topics = service.get_topics()
    topic_ids = {item["topic_id"] for item in topics}
    assert "TOP-WF" in topic_ids
    assert "TOP-APCOM.SUPPORT.RULE" in topic_ids


def test_search_bundle_filters_to_requirement_focus_for_resubmit_comment(kb_fixture):
    from kb_runtime_service import KnowledgeRuntimeService

    service = KnowledgeRuntimeService(
        project_root=kb_fixture["project_root"],
        kb_root=kb_fixture["kb_root"],
        apcom_root=kb_fixture["apcom_root"],
        topic_file=kb_fixture["topic_file"],
        sqlite_path=kb_fixture["sqlite_path"],
        chroma_path=kb_fixture["chroma_path"],
        ticket_chroma_path=kb_fixture["ticket_chroma_path"],
    )

    service._load_kb_items = lambda: [
        {
            "content_id": "CNT-9101",
            "source_kind": "kb_local",
            "source_repo": "KB",
            "name": "审批面板重新提交附言说明",
            "summary": "支持在审批面板重新提交单据时填写附言说明，并把说明传递给后续审批人。",
            "keywords": ["重新提交", "附言", "说明", "审批面板"],
            "source_rel_path": "流程中心/帮助文档/审批面板重新提交附言说明.md",
            "source_path": "",
            "converted_path": "",
            "index_path": "",
            "l1_module": "流程中心",
            "l2_module": "帮助文档",
            "doc_type": "帮助文档",
            "topic_ids": ["TOP-WF"],
            "citation_label": "[KB] 审批面板重新提交附言说明.md",
            "related_content_ids": [],
            "backlink_index_ids": [],
        },
        {
            "content_id": "CNT-9102",
            "source_kind": "kb_local",
            "source_repo": "KB",
            "name": "权限拆分设计方案",
            "summary": "介绍流程权限拆分与角色控制，和重新提交附言场景无关。",
            "keywords": ["权限", "角色", "控制"],
            "source_rel_path": "流程中心/设计方案/权限拆分设计方案.md",
            "source_path": "",
            "converted_path": "",
            "index_path": "",
            "l1_module": "流程中心",
            "l2_module": "设计方案",
            "doc_type": "设计方案",
            "topic_ids": ["TOP-WF"],
            "citation_label": "[KB] 权限拆分设计方案.md",
            "related_content_ids": [],
            "backlink_index_ids": [],
        },
    ]
    service._manifest_cache = None
    service._search_ticket_cases = lambda query, top_k=5: [
        {
            "content_id": "TICKET-MYPROJECT-T001",
            "chunk_id": "TICKET-MYPROJECT-T001",
            "source_kind": "ticket_case",
            "source_repo": "ticket_chroma",
            "name": "MYPROJECT-T001 重新提交附言说明",
            "summary": "客户要求重新提交单据时补充附言说明。",
            "keywords": ["重新提交", "附言", "说明"],
            "source_rel_path": "MYPROJECT-T001",
            "source_path": "MYPROJECT-T001",
            "converted_path": "",
            "index_path": "",
            "l1_module": "工单案例",
            "l2_module": "审批面板",
            "doc_type": "JIRA工单",
            "topic_ids": ["TOP-WF"],
            "citation_label": "[TICKET] MYPROJECT-T001",
            "related_content_ids": [],
            "backlink_index_ids": [],
            "chunk_text": "审批面板重新提交时需要填写附言说明，并记录到审批意见中。",
            "chunk_preview": "审批面板重新提交时需要填写附言说明。",
            "match_type": "ticket",
            "score": 0.94,
        }
    ]

    bundle = service.search_bundle("重新提交单据时填写附言说明 审批面板", top_k=10)

    assert bundle["primary_materials"]
    assert bundle["primary_materials"][0]["content_id"] == "CNT-9101"
    assert all(item["content_id"] != "CNT-9102" for item in bundle["primary_materials"])
    assert bundle["ticket_summary"]["related_count"] >= 1
