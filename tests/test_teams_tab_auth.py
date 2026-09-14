from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import login_department_reader
from pai_loop.migrations import TEAMS_FOLLOWUPS_MIGRATION_ID, apply_additive_migrations, pending_migrations


def test_https_teams_cookie_opt_in_keeps_csrf_and_origin_checks(client, monkeypatch):
    monkeypatch.setenv("PAI_TEAMS_TAB_AUTH_ENABLED", "true")
    login_department_reader(client)
    with TestClient(client.app, base_url="https://testserver") as secure:
        logged_in = secure.post("/api/v1/accounts/login", headers={"Origin": "https://testserver"}, json={
            "username": "SYN_REDACTED_READER", "password": "SYN-reader-password-only"})
        assert logged_in.status_code == 200
        cookie = logged_in.headers["set-cookie"]
        assert "SameSite=none" in cookie and "Secure" in cookie and "HttpOnly" in cookie
        assert secure.post("/api/v1/teams/link-code", headers={"Origin": "https://testserver"}).status_code == 403
        assert secure.post("/api/v1/teams/link-code", headers={"Origin": "https://evil.test",
                           "x-csrf-token": logged_in.json()["csrf_token"]}).status_code == 403
        assert secure.get("/api/v1/teams/connection").status_code == 200


def test_http_retains_strict_cookie_even_with_teams_flag(client, monkeypatch):
    monkeypatch.setenv("PAI_TEAMS_TAB_AUTH_ENABLED", "true")
    login_department_reader(client)
    response = client.post("/api/v1/accounts/login", headers={"Origin": "http://testserver"}, json={
        "username": "SYN_REDACTED_READER", "password": "SYN-reader-password-only"})
    assert "SameSite=strict" in response.headers["set-cookie"]


def test_tab_config_is_data_free_and_opt_in_while_callback_auth_is_enforced(client, monkeypatch):
    client.headers.pop("X-PAI-LOOP-API-KEY", None)
    assert client.get("/teams-config.html").status_code == 401
    monkeypatch.setenv("PAI_TEAMS_TAB_AUTH_ENABLED", "true")
    assert client.get("/teams-config.html").status_code == 200
    assert client.get("/api/v1/teams/connection").status_code == 401
    assert client.post("/api/v1/teams/messages", json={}).status_code in {401, 503}
    assert client.post("/api/v1/teams/messages/anything", json={}).status_code == 401


def test_teams_additive_migration_is_registered_and_idempotent(client):
    from sqlalchemy import inspect
    engine = client.app.state.engine
    assert not pending_migrations(engine)
    assert apply_additive_migrations(engine) == []
    tables = set(inspect(engine).get_table_names())
    assert {"teams_recipients", "teams_session_links", "teams_link_codes", "teams_follows", "teams_follow_deliveries"} <= tables
    from pai_loop.migrations import schema_migrations
    with engine.connect() as connection:
        assert connection.scalar(select(schema_migrations.c.migration_id).where(
            schema_migrations.c.migration_id == TEAMS_FOLLOWUPS_MIGRATION_ID))


def test_enabled_worker_checks_bot_configuration_before_startup(monkeypatch, tmp_path):
    from pai_loop import main
    monkeypatch.setenv("PAI_TEAMS_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(main.TeamsBotSettings, "from_env", lambda: SimpleNamespace(enabled=False))
    app = main.create_app(database_url=f"sqlite:///{(tmp_path / 'SYN-worker.db').as_posix()}", seed_synthetic=False)
    with pytest.raises(ValueError, match="valid bot"):
        with TestClient(app):
            pass


def test_enabled_worker_runs_without_browser_and_stops_with_app(monkeypatch, tmp_path):
    from pai_loop import main
    ran = threading.Event()
    monkeypatch.setenv("PAI_TEAMS_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(main.TeamsBotSettings, "from_env", lambda: SimpleNamespace(enabled=True))
    monkeypatch.setattr(main, "dispatch_due", lambda _factory: ran.set())
    app = main.create_app(database_url=f"sqlite:///{(tmp_path / 'SYN-worker.db').as_posix()}", seed_synthetic=False)
    with TestClient(app):
        assert ran.wait(5), "durable worker should tick without a user browser request"
