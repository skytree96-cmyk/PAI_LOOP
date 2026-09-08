from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.account_models import DepartmentAccount
from pai_loop.accounts import departments, now_utc, password_hash
from pai_loop.main import create_app


SYN_SERVER_HEADERS = {"X-PAI-LOOP-API-KEY": "SYN-internal-api-tests-only"}


def internal_server_client(app, **kwargs) -> TestClient:
    """Explicit opt-in for internal API tests, never browser/auth rejection cases."""
    configured = app.state.settings.api_key
    if not configured:
        configured = SYN_SERVER_HEADERS["X-PAI-LOOP-API-KEY"]
        app.state.settings = replace(app.state.settings, api_key=configured)
    headers = dict(kwargs.pop("headers", {}))
    headers["X-PAI-LOOP-API-KEY"] = configured
    return TestClient(app, headers=headers, **kwargs)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # TestClient concurrency exercises separate request-scoped SQLAlchemy
    # sessions. A :memory: engine uses StaticPool, so those sessions share one
    # DBAPI connection and one request can close/rollback it after the planner
    # lock is released while another request is committing. A file-backed
    # database gives each concurrent session its own connection, matching the
    # lifecycle isolation provided by production PostgreSQL.
    database_path = tmp_path / "pai-loop-test.db"
    app = create_app(
        database_url=f"sqlite:///{database_path.as_posix()}",
        seed_synthetic=False,
    )
    # Internal API fixtures use the real server-auth boundary. Browser/auth
    # rejection cases must remove this header or use a separate TestClient.
    app.state.settings = replace(app.state.settings, api_key=SYN_SERVER_HEADERS["X-PAI-LOOP-API-KEY"])
    with TestClient(app, headers=SYN_SERVER_HEADERS) as test_client:
        yield test_client


def login_department_reader(client: TestClient) -> dict:
    """Provision a paid-disabled SYN account and obtain a real browser session."""
    client.headers.pop("X-PAI-LOOP-API-KEY", None)
    client.app.state.settings = replace(client.app.state.settings, department_accounts_enabled=True)
    username, password = "SYN_REDACTED_READER", "SYN-reader-password-only"
    with client.app.state.session_factory() as session:
        if not session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == username)):
            session.add(DepartmentAccount(username=username, role="DEPARTMENT", department_id=next(iter(departments())),
                password_hash=password_hash(password), active=True, paid_analysis_allowed=False, created_at=now_utc()))
            session.commit()
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/v1/accounts/login", headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"]
    assert not response.json()["capabilities"]["request_paid_analysis"]
    return response.json()

