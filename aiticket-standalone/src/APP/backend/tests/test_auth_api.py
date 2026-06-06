import importlib
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("APP_AUTH_SECRET_PATH", str(tmp_path / "auth.key"))

    if "main" in sys.modules:
        del sys.modules["main"]

    import main

    return importlib.reload(main)


def test_bootstrap_login_and_admin_role_enforcement(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    client = TestClient(main.app)

    bootstrap_status = client.get("/api/auth/bootstrap-status")
    assert bootstrap_status.status_code == 200
    assert bootstrap_status.json()["bootstrap_required"] is True

    bootstrap_response = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "secret-pass",
            "display_name": "管理员",
        },
    )
    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["user"]["role"] == "admin"

    login_response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret-pass"},
    )
    assert login_response.status_code == 200
    assert login_response.json()["user"]["username"] == "admin"

    me_response = client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["user"]["role"] == "admin"

    create_user_response = client.post(
        "/api/admin/users",
        json={
            "username": "alice",
            "password": "member-pass",
            "display_name": "Alice",
            "role": "member",
        },
    )
    assert create_user_response.status_code == 200
    assert create_user_response.json()["user"]["role"] == "member"

    client.post("/api/auth/logout")
    member_login_response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "member-pass"},
    )
    assert member_login_response.status_code == 200

    forbidden_response = client.get("/api/admin/users")
    assert forbidden_response.status_code == 403
    assert forbidden_response.json()["detail"] == "Admin access required"


def test_member_can_save_and_read_masked_jira_binding(monkeypatch, tmp_path):
    main = load_main(monkeypatch, tmp_path)
    client = TestClient(main.app)

    client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "secret-pass",
            "display_name": "管理员",
        },
    )
    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "secret-pass"},
    )
    client.post(
        "/api/admin/users",
        json={
            "username": "alice",
            "password": "member-pass",
            "display_name": "Alice",
            "role": "member",
        },
    )
    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "member-pass"},
    )

    save_response = client.put(
        "/api/settings/jira-binding",
        json={
            "jira_username": "alice.jira",
            "jira_api_token": "jira-secret-token",
            "jira_base_url": "https://jira.example.com",
        },
    )
    assert save_response.status_code == 200
    assert save_response.json()["binding"]["has_token"] is True

    get_response = client.get("/api/settings/jira-binding")
    assert get_response.status_code == 200
    assert get_response.json()["binding"]["jira_username"] == "alice.jira"
    assert "jira-secret-token" not in get_response.text
