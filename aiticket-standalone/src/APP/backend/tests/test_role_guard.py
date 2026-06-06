"""Tests for strict-mode role guard (Phase 0-2 封堵 fallback)."""
import importlib
import os
import sys
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_modules(*names):
    for n in names:
        if n in sys.modules:
            del sys.modules[n]
    for n in names:
        importlib.import_module(n)


# ---------------------------------------------------------------------------
# role_guard
# ---------------------------------------------------------------------------

class TestIsStrictRole:
    def test_no_env_is_strict(self, monkeypatch):
        monkeypatch.delenv("AITICKET_ROLE", raising=False)
        from role_guard import is_strict_role
        assert is_strict_role() is True  # default=deployable (strict-by-default)

    def test_mini_explicit(self, monkeypatch):
        monkeypatch.setenv("AITICKET_ROLE", "mini")
        from role_guard import is_strict_role
        assert is_strict_role() is False

    def test_qcl(self, monkeypatch):
        monkeypatch.setenv("AITICKET_ROLE", "qcl")
        from role_guard import is_strict_role
        assert is_strict_role() is True

    def test_deployable(self, monkeypatch):
        monkeypatch.setenv("AITICKET_ROLE", "deployable")
        from role_guard import is_strict_role
        assert is_strict_role() is True

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("AITICKET_ROLE", "QCL")
        from role_guard import is_strict_role
        assert is_strict_role() is True


# ---------------------------------------------------------------------------
# JiraService strict check
# ---------------------------------------------------------------------------

class TestJiraServiceStrictMode:
    def test_no_creds_strict_raises_on_request(self, monkeypatch):
        monkeypatch.setenv("AITICKET_ROLE", "qcl")
        from jira_service import JiraService
        from role_guard import NoUserContextError
        svc = JiraService()  # 构造时不 raise
        assert svc._no_default_creds is True
        with pytest.raises(NoUserContextError):
            svc._make_request("GET", "https://gfjira.yyrd.com/rest/api/2/myself")

    def test_with_session_cookies_not_blocked(self, monkeypatch):
        monkeypatch.setenv("AITICKET_ROLE", "qcl")
        from jira_service import JiraService
        svc = JiraService(session_cookies={"JSESSIONID": "abc123"})
        assert svc._no_default_creds is False

    def test_mini_no_creds_not_blocked(self, monkeypatch):
        monkeypatch.setenv("AITICKET_ROLE", "mini")
        from jira_service import JiraService
        from role_guard import NoUserContextError
        svc = JiraService()
        assert svc._no_default_creds is True
        # In mini mode, _make_request should NOT raise NoUserContextError
        # (it may raise connection errors, but not our guard)
        try:
            svc._make_request("GET", "http://127.0.0.1:1")
        except NoUserContextError:
            pytest.fail("Mini mode should not raise NoUserContextError")
        except Exception:
            pass  # connection errors are fine


# ---------------------------------------------------------------------------
# PM wallet strict check
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="PM 钱包/模块服务已在 compact 剥离（services.pm_* 不存在）")
class TestPMWalletStrictMode:
    def test_unbound_user_strict_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AITICKET_ROLE", "qcl")
        import services.pm_wallet_service as _wm
        monkeypatch.setattr(_wm, "WALLET_DIR", tmp_path / "pm_tokens")
        monkeypatch.setattr(_wm, "DEFAULT_TOKEN_PATH", tmp_path / "pm_token.json")
        from role_guard import PMNotBoundError
        with pytest.raises(PMNotBoundError):
            _wm.get_effective_cookies("someuser")

    def test_unbound_user_mini_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AITICKET_ROLE", "mini")
        import services.pm_wallet_service as _wm
        monkeypatch.setattr(_wm, "WALLET_DIR", tmp_path / "pm_tokens")
        monkeypatch.setattr(_wm, "DEFAULT_TOKEN_PATH", tmp_path / "pm_token.json")
        result = _wm.get_effective_cookies("someuser")
        assert result == {}  # no wallet, no default token → empty (mini non-strict)

    def test_none_user_strict_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AITICKET_ROLE", "qcl")
        import services.pm_wallet_service as _wm
        monkeypatch.setattr(_wm, "DEFAULT_TOKEN_PATH", tmp_path / "pm_token.json")
        from role_guard import PMNotBoundError
        with pytest.raises(PMNotBoundError):
            _wm.get_effective_cookies(None)


# ---------------------------------------------------------------------------
# PMModuleService threading.local isolation
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="PM 钱包/模块服务已在 compact 剥离（services.pm_* 不存在）")
class TestPMModuleServiceThreadingLocal:
    def test_current_pm_user_thread_isolated(self):
        import threading
        from services.pm_module_service import PMModuleService

        try:
            svc1 = PMModuleService("original_demand")
        except Exception:
            pytest.skip("pm_config.yaml not available in test environment")

        svc1.current_pm_user = "user_a"
        results = {}

        def set_in_thread(val, key):
            svc1.current_pm_user = val
            import time; time.sleep(0.05)
            results[key] = svc1.current_pm_user

        t1 = threading.Thread(target=set_in_thread, args=("thread_user_1", "t1"))
        t2 = threading.Thread(target=set_in_thread, args=("thread_user_2", "t2"))
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Each thread should see its own value
        assert results["t1"] == "thread_user_1"
        assert results["t2"] == "thread_user_2"

    def test_current_pm_user_default_none(self):
        import threading
        from services.pm_module_service import _pm_user_local

        result = {}

        def check():
            result["val"] = getattr(_pm_user_local, "pm_user", None)

        t = threading.Thread(target=check)
        t.start(); t.join()
        assert result["val"] is None  # fresh thread → no value set → None


# ---------------------------------------------------------------------------
# NoUserContextError hierarchy
# ---------------------------------------------------------------------------

class TestExceptionHierarchy:
    def test_pm_not_bound_is_no_user_context(self):
        from role_guard import NoUserContextError, PMNotBoundError
        exc = PMNotBoundError("cfone")
        assert isinstance(exc, NoUserContextError)
        assert exc.username == "cfone"
        assert exc.where == "pm_wallet"

    def test_no_user_context_attrs(self):
        from role_guard import NoUserContextError
        exc = NoUserContextError("my_where", "my_hint")
        assert exc.where == "my_where"
        assert exc.hint == "my_hint"
        assert "my_where" in str(exc)
