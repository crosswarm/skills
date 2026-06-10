"""用户级 LLM 配置改造测试。

覆盖：
  A. auth_service CRUD —— set/get/delete user_llm_config + last_provider（加密往返、隔离 DB）。
  B. resolve_feature_llm_runtime 四路径：
     1) 用户配了该 provider           → _source=user
     2) 用户没配 + 该功能允许系统兜底  → _source=system
     3) 用户没配 + 该功能禁止系统兜底  → _blocked
     4) 后台 user_id=None：
        - 允许兜底 → _source=system
        - 禁止兜底 → _blocked

环境：
  - auth_service CRUD 用临时 sqlite（构造 AuthService(db_path=tmp)）隔离。
  - resolve_* 用 monkeypatch 替换 main.auth_service / main.load_llm_config，
    不触碰真实 DB / 文件。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ─────────────────────────── A. auth_service CRUD ───────────────────────────

def _make_service(tmp_path):
    from auth_service import AuthService
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
        session_ttl_hours=8,
    )
    admin = service.bootstrap_admin("admin", "secret-pass", display_name="管理员")
    member = service.create_user(
        "alice", "member-pass", display_name="Alice", role="member", created_by=admin["id"]
    )
    return service, member["id"]


def test_set_get_user_llm_config_roundtrip(tmp_path):
    service, uid = _make_service(tmp_path)

    # 初始为空
    assert service.get_user_llm_config(uid) == {}

    service.set_user_llm_provider(
        uid, "zhipu", api_key="sk-user-zhipu", model_name="glm-5", base_url="https://z.example/v1"
    )
    cfg = service.get_user_llm_config(uid)
    assert "zhipu" in cfg
    assert cfg["zhipu"]["api_key"] == "sk-user-zhipu"  # 解密往返
    assert cfg["zhipu"]["model_name"] == "glm-5"
    assert cfg["zhipu"]["base_url"] == "https://z.example/v1"


def test_set_user_llm_config_is_encrypted_at_rest(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "openai", api_key="sk-secret-123")

    # 直查 DB：api_key 列不得为明文
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "auth.db"))
    row = conn.execute(
        "SELECT api_key FROM user_llm_config WHERE user_id=? AND provider=?", (uid, "openai")
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] != "sk-secret-123"
    assert row[0]  # 非空（已加密）


def test_upsert_overwrites_same_provider(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "zhipu", api_key="k1", model_name="m1")
    service.set_user_llm_provider(uid, "zhipu", api_key="k2", model_name="m2")
    cfg = service.get_user_llm_config(uid)
    assert cfg["zhipu"]["api_key"] == "k2"
    assert cfg["zhipu"]["model_name"] == "m2"


def test_delete_user_llm_provider(tmp_path):
    service, uid = _make_service(tmp_path)
    service.set_user_llm_provider(uid, "zhipu", api_key="k1")
    service.set_user_llm_provider(uid, "openai", api_key="k2")
    service.delete_user_llm_provider(uid, "zhipu")
    cfg = service.get_user_llm_config(uid)
    assert "zhipu" not in cfg
    assert "openai" in cfg


def test_user_last_provider_roundtrip(tmp_path):
    service, uid = _make_service(tmp_path)
    assert service.get_user_last_provider(uid) == ""
    service.set_user_last_provider(uid, "minimax")
    assert service.get_user_last_provider(uid) == "minimax"
    service.set_user_last_provider(uid, "zhipu")
    assert service.get_user_last_provider(uid) == "zhipu"


def test_get_user_llm_config_empty_user_id(tmp_path):
    service, _ = _make_service(tmp_path)
    assert service.get_user_llm_config("") == {}
    assert service.get_user_last_provider("") == ""


# ──────────────────── B. resolve_feature_llm_runtime 四路径 ────────────────────

class _FakeAuthService:
    """最小桩：只实现 resolve_feature_llm_runtime 用到的两个方法。"""

    def __init__(self, routing, user_cfg, fallback_override=None):
        self._routing = routing
        self._user_cfg = user_cfg
        self._fallback_override = fallback_override or {}

    def get_system_setting(self, key, default=None):
        import main
        if key == main.LLM_FEATURE_ROUTING_KEY:
            return self._routing
        if key == main.LLM_FEATURE_FALLBACK_KEY:
            return self._fallback_override
        return default

    def get_user_llm_config(self, user_id):
        if not user_id:
            return {}
        return self._user_cfg.get(user_id, {})


@pytest.fixture
def main_mod():
    import main
    return main


def _patch(main, monkeypatch, *, routing, user_cfg, system_cfg, fallback_override=None):
    fake = _FakeAuthService(routing, user_cfg, fallback_override)
    monkeypatch.setattr(main, "auth_service", fake)
    monkeypatch.setattr(main, "load_llm_config", lambda: system_cfg)


def test_path1_user_configured_returns_user(main_mod, monkeypatch):
    # smart_reply 默认禁止系统兜底；用户配了 zhipu → 走用户级
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={"u1": {"zhipu": {"api_key": "sk-user", "model_name": "glm-5", "base_url": "https://u"}}},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "user"
    assert rt["api_key"] == "sk-user"
    assert rt["provider"] == "zhipu"
    assert not rt.get("_blocked")


def test_path2_no_user_fallback_on_returns_system(main_mod, monkeypatch):
    # darwin_eval 默认允许系统兜底；用户没配 → 走系统级
    _patch(
        main_mod, monkeypatch,
        routing={"darwin_eval": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system", "model_name": "glm-5"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("darwin_eval", user_id="u1")
    assert rt["_source"] == "system"
    assert rt["api_key"] == "sk-system"
    assert not rt.get("_blocked")


def test_path3_no_user_fallback_off_blocks(main_mod, monkeypatch):
    # smart_reply 默认禁止系统兜底；用户没配 → 阻断
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt.get("_blocked") is True
    assert rt["_source"] == "blocked"
    assert rt["api_key"] == ""
    assert rt["_reason"] == "feature_requires_user_llm"


def test_path4_background_no_user_fallback_on_returns_system(main_mod, monkeypatch):
    # 后台 user_id=None + 允许兜底 → 系统级
    _patch(
        main_mod, monkeypatch,
        routing={"weekly_report": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("weekly_report", user_id=None)
    assert rt["_source"] == "system"
    assert rt["api_key"] == "sk-system"


def test_path4_background_no_user_fallback_off_blocks(main_mod, monkeypatch):
    # 后台 user_id=None + 禁止兜底（smart_reply 默认 False）→ 阻断
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id=None)
    assert rt.get("_blocked") is True
    assert rt["_source"] == "blocked"


def test_admin_override_can_enable_fallback(main_mod, monkeypatch):
    # admin 把 smart_reply 兜底打开 → 用户没配也能走系统级
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
        fallback_override={"smart_reply": True},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1")
    assert rt["_source"] == "system"
    assert rt["api_key"] == "sk-system"


def test_unknown_feature_defaults_to_fallback_true(main_mod, monkeypatch):
    # 未知 feature 缺省允许兜底
    _patch(
        main_mod, monkeypatch,
        routing={"_default": "zhipu"},
        user_cfg={},
        system_cfg={"zhipu": {"api_key": "sk-system"}},
    )
    rt = main_mod.resolve_feature_llm_runtime("some_new_feature", user_id=None)
    assert rt["_source"] == "system"


def test_exclude_providers_respected(main_mod, monkeypatch):
    # 降级链 [zhipu, openai]，排除 zhipu → 用 openai 的用户凭据
    _patch(
        main_mod, monkeypatch,
        routing={"smart_reply": ["zhipu", "openai"]},
        user_cfg={"u1": {"openai": {"api_key": "sk-openai-user"}}},
        system_cfg={},
    )
    rt = main_mod.resolve_feature_llm_runtime("smart_reply", user_id="u1", exclude_providers=["zhipu"])
    assert rt["_source"] == "user"
    assert rt["provider"] == "openai"
    assert rt["api_key"] == "sk-openai-user"
