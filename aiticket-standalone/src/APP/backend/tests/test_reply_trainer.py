"""回复训练器单元测试"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_trainer_init_and_stats():
    """测试训练器初始化和统计"""
    from reply_trainer import ReplyTrainer
    trainer = ReplyTrainer()
    stats = trainer.get_stats()
    assert "total" in stats
    assert "adoption_rate" in stats
    assert "example_count" in stats
    print(f"✓ 初始化成功, 范例={stats['example_count']}")


def test_record_and_search():
    """测试记录反馈后能检索到"""
    from kb_hybrid_index import KnowledgeHybridIndex

    # 用临时目录隔离测试
    tmpdir = tempfile.mkdtemp(prefix="test_trainer_")
    try:
        kb = KnowledgeHybridIndex(
            sqlite_path=os.path.join(tmpdir, "test.db"),
            chroma_path=os.path.join(tmpdir, "chroma"),
            collection_name="test_reply",
        )

        # 添加一条
        item = {
            "content_id": "reply:TEST-001",
            "source_kind": "reply_example",
            "name": "TEST-001",
            "summary": "工作流审批节点配置问题",
            "source_rel_path": "style_owner",
            "citation_label": "adopted",
            "l1_module": "指导解决",
            "l2_module": "应用操作",
            "doc_type": "style_owner",
            "keywords": ["TEST-001"],
        }
        text = "工作流审批节点配置问题\n\n您好！请检查审批节点的审批模式是否为并行，谢谢"
        count = kb.add_item(item, text)
        assert count > 0, f"add_item 返回 {count}"

        # 检索
        results = kb.search("审批节点 配置", top_k=3, source_kind="reply_example")
        assert len(results) > 0, "检索结果为空"
        assert results[0]["name"] == "TEST-001"
        print(f"✓ 记录+检索成功, 返回 {len(results)} 条")

        # upsert 测试
        item["summary"] = "更新后的摘要"
        text2 = "工作流审批节点配置问题\n\n您好！更新后的回复内容"
        count2 = kb.add_item(item, text2)
        assert count2 > 0
        print(f"✓ upsert 成功")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_suggest_fields_from_examples():
    """测试从范例中投票推荐字段值"""
    from board_service_chroma import BoardService
    from llm_service import LLMService

    bs = BoardService(LLMService())

    # 模拟范例数据
    examples = [
        {"reply_method": "指导解决", "issue_type": "应用操作", "score": 0.9},
        {"reply_method": "指导解决", "issue_type": "需求问题", "score": 0.7},
        {"reply_method": "方案解决", "issue_type": "应用操作", "score": 0.5},
    ]
    ai_analysis = {"problem_analysis": "", "solution_suggestion": "", "issue_title": "测试工单"}

    result = bs._suggest_reply_fields(ai_analysis, examples)
    assert result["reply_method"]["value"] == "指导解决", f"期望指导解决，实际={result['reply_method']}"
    assert result["issue_type"]["value"] == "应用操作", f"期望应用操作，实际={result['issue_type']}"
    print(f"✓ 范例投票: 回复方式={result['reply_method']['value']}, 问题类型={result['issue_type']['value']}")


def test_suggest_fields_keyword_fallback():
    """测试无范例时关键词匹配回退"""
    from board_service_chroma import BoardService
    from llm_service import LLMService

    bs = BoardService(LLMService())

    ai_analysis = {
        "problem_analysis": "产品存在BUG，导致数据异常",
        "solution_suggestion": "需要修复补丁",
        "issue_title": "数据错误"
    }

    result = bs._suggest_reply_fields(ai_analysis, [])
    assert result["reply_method"]["value"] == "方案解决"
    assert result["issue_type"]["value"] == "产品错误"
    print(f"✓ 关键词回退: 回复方式={result['reply_method']['value']}, 问题类型={result['issue_type']['value']}")


if __name__ == "__main__":
    test_trainer_init_and_stats()
    test_record_and_search()
    test_suggest_fields_from_examples()
    test_suggest_fields_keyword_fallback()
    print("\n全部测试通过 ✓")
