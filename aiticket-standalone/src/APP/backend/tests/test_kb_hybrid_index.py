from pathlib import Path
from threading import Thread

from kb_hybrid_index import KnowledgeHybridIndex


def test_get_chunks_for_content_can_be_called_from_another_thread(tmp_path: Path):
    index = KnowledgeHybridIndex(
        sqlite_path=tmp_path / "sqlite" / "kb_chunks.db",
        chroma_path=tmp_path / "chroma",
        collection_name="kb_hybrid_index_thread_test",
    )

    items = [
        {
            "content_id": "CNT-0001",
            "source_kind": "kb_local",
            "name": "流程监控",
            "summary": "流程监控摘要",
            "source_rel_path": "流程中心/帮助文档-流程监控.docx",
            "citation_label": "[KB] 流程中心/帮助文档-流程监控.docx",
            "l1_module": "流程中心",
            "l2_module": "产品培训课件",
            "doc_type": ".docx",
            "keywords": ["流程监控", "未来审批人"],
        }
    ]

    index.rebuild(items, lambda _: "流程监控支持未来审批人查询。" * 10)

    result: dict[str, object] = {}

    def worker() -> None:
        result["chunks"] = index.get_chunks_for_content("CNT-0001")

    thread = Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert "chunks" in result
    assert result["chunks"]
    assert result["chunks"][0]["chunk_id"].startswith("CNT-0001::chunk-")
