import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth_service import AuthService


def test_auth_service_bootstrap_authenticate_and_session_roundtrip(tmp_path):
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
        session_ttl_hours=8,
    )

    assert service.has_users() is False

    admin = service.bootstrap_admin("admin", "secret-pass", display_name="管理员")
    authenticated = service.authenticate("admin", "secret-pass")
    session_token = service.create_session(admin["id"])
    current_user = service.get_user_by_session(session_token)

    assert service.has_users() is True
    assert authenticated["username"] == "admin"
    assert current_user["id"] == admin["id"]
    assert current_user["role"] == "admin"


def test_auth_service_encrypts_and_roundtrips_jira_binding(tmp_path):
    service = AuthService(
        db_path=str(tmp_path / "auth.db"),
        secret_path=str(tmp_path / "auth.key"),
        session_ttl_hours=8,
    )
    admin = service.bootstrap_admin("admin", "secret-pass", display_name="管理员")
    member = service.create_user("alice", "member-pass", display_name="Alice", role="member", created_by=admin["id"])

    service.upsert_jira_binding(
        member["id"],
        jira_username="alice.jira",
        jira_api_token="jira-secret-token",
        jira_base_url="https://jira.example.com",
    )

    summary = service.get_jira_binding_summary(member["id"])
    credentials = service.get_jira_binding_credentials(member["id"])

    assert summary["jira_username"] == "alice.jira"
    assert summary["has_token"] is True
    assert "jira-secret-token" not in str(summary)
    assert credentials["jira_username"] == "alice.jira"
    assert credentials["jira_api_token"] == "jira-secret-token"
    assert credentials["jira_base_url"] == "https://jira.example.com"
