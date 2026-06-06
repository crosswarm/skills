import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm_service import LLMService


def test_openai_client_uses_explicit_timeout(monkeypatch):
    captured = {}

    class FakeResponse:
        def __iter__(self):
            return iter([])

    class FakeCompletions:
        def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return FakeResponse()

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, api_key, base_url, timeout):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["timeout"] = timeout
            self.chat = FakeChat()

    monkeypatch.setattr("llm_service.OpenAI", FakeClient)

    service = LLMService()
    list(service._call_openai("test-key", "MiniMax-M2.5", "https://api.minimaxi.com/v1", "system", "query", []))

    assert captured["timeout"] == 45.0
    assert captured["create_kwargs"]["stream"] is True
