import json
import os
import sys
import importlib

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("APP_AUTH_SECRET_PATH", str(tmp_path / "auth.key"))
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    return importlib.reload(main)


def bootstrap_and_login(client):
    client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "secret-pass", "display_name": "管理员"},
    )


def test_query_stream_includes_kb_summary(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)

    class StubSearchEngine:
        def search(self, query: str):
            return {
                "results": [
                    {
                        "key": "MYPROJECT-1",
                        "display_summary": "流程监控问题",
                        "content": "解决方案内容",
                        "score": 0.9,
                    }
                ]
            }

    class StubRuntimeService:
        def answer_question(self, **kwargs):
            return {
                "answer_text": "未检索到相关知识库内容",
                "sources": [],
                "source_groups": {"kb_local": [], "apcom_docs": [], "ticket_case": []},
            }

    monkeypatch.setattr(main, "search_engine", StubSearchEngine())
    monkeypatch.setattr(main, "kb_runtime_service", StubRuntimeService())

    client = TestClient(main.app)
    bootstrap_and_login(client)
    response = client.post(
        "/query",
        json={
            "query": "流程监控如何查询未来审批人",
            "api_key": None,
            "images": [],
            "model_provider": "openai",
            "model_name": "",
            "base_url": "",
        },
    )

    assert response.status_code == 200
    chunks = [chunk for chunk in response.text.split("\n---\n") if chunk.strip()]
    payloads = [json.loads(chunk) for chunk in chunks]

    assert payloads[0]["search_results"]["results"][0]["key"] == "MYPROJECT-1"
    assert payloads[1]["kb_summary"]["answer_text"] == "未检索到相关知识库内容"
