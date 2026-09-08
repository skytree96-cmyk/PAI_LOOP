from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from pai_loop.account_models import AccountAudit, AccountBootstrapPreview, AccountSession, DepartmentAccount
from pai_loop.accounts import SESSION_COOKIE, _hash, now_utc
from pai_loop.manual_analysis import _require_manual_operator
from test_department_accounts import PASSWORD, SERVER, ORIGIN, _bootstrap, _login, _peer, account_client

ACTIVATE = "/api/v1/accounts/initial-admin-activation"


@pytest.fixture
def inactive_admin(client):
    client.headers.pop("X-PAI-LOOP-API-KEY", None)
    client.app.state.settings = replace(client.app.state.settings, api_key=SERVER["X-PAI-LOOP-API-KEY"])
    _bootstrap(client, [{"username": "SYN_ADMIN", "password": PASSWORD, "role": "ADMIN"}])
    return client


def test_initial_admin_activation_is_bound_single_use_and_never_changes_credentials(inactive_admin):
    client = inactive_admin
    with client.app.state.session_factory() as session:
        row = session.scalar(select(DepartmentAccount))
        old = (row.id, row.username, row.password_hash, row.role, row.department_id)
        session.add(AccountSession(account_id=row.id, token_hash=_hash("SYN-old-session"), csrf_hash=_hash("SYN-old-csrf"), created_at=now_utc(), expires_at=now_utc()+timedelta(hours=1)))
        session.commit()
    body = {"username": "SYN_ADMIN"}
    assert client.post(ACTIVATE, headers={**SERVER, **ORIGIN}, json=body).status_code == 403
    assert client.post(ACTIVATE, json=body).status_code == 401
    preview = client.post(ACTIVATE, headers=SERVER, json=body).json()
    assert preview["status"] == "WOULD_ACTIVATE"
    assert preview["account"]["active"] is False
    apply = {**body, "dry_run": False, "preview_id": preview["preview_id"]}
    assert client.post(ACTIVATE, headers=SERVER, json={**apply, "username": "SYN_OTHER"}).status_code == 409
    response = client.post(ACTIVATE, headers=SERVER, json=apply)
    assert response.status_code == 200, response.text
    assert response.json()["account"]["paid_analysis_allowed"] is False
    assert PASSWORD not in response.text and "password" not in response.text
    assert client.post(ACTIVATE, headers=SERVER, json=apply).status_code == 409
    assert client.post(ACTIVATE, headers=SERVER, json=body).json()["status"] == "ALREADY_ACTIVE"
    with client.app.state.session_factory() as session:
        row = session.scalar(select(DepartmentAccount))
        assert (row.id, row.username, row.password_hash, row.role, row.department_id) == old
        assert row.active and row.revision == 2 and not row.paid_analysis_allowed
        assert session.scalar(select(AccountSession)).revoked_at is not None
        assert len(session.scalars(select(AccountAudit).where(AccountAudit.event == "INITIAL_ADMIN_ACTIVATED")).all()) == 1
    client.app.state.settings = replace(client.app.state.settings, department_accounts_enabled=True)
    headers, me = _login(client, "SYN_ADMIN")
    assert me["capabilities"]["manage_accounts"] and not me["capabilities"]["write_decisions"]
    assert not me["capabilities"]["request_paid_analysis"]
    assert client.get("/api/v1/accounts").status_code == 200


@pytest.mark.parametrize("change", ["revision", "paid", "expired", "registration-preview", "other-admin"])
def test_activation_fails_closed_for_changed_or_wrong_preview(inactive_admin, change):
    client = inactive_admin
    preview = client.post(ACTIVATE, headers=SERVER, json={"username": "SYN_ADMIN"}).json()
    if change == "registration-preview":
        preview = client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": [{"username": "SYN_ADMIN", "password": PASSWORD, "role": "ADMIN"}]}).json()
    elif change == "other-admin":
        _bootstrap(client, [{"username": "SYN_SECOND", "password": PASSWORD, "role": "ADMIN", "active": True}])
    else:
        with client.app.state.session_factory() as session:
            row = session.scalar(select(DepartmentAccount))
            if change == "revision": row.revision += 1
            elif change == "paid": row.paid_analysis_allowed = True
            else: session.get(AccountBootstrapPreview, preview["preview_id"]).expires_at = now_utc()-timedelta(seconds=1)
            session.commit()
    result = client.post(ACTIVATE, headers=SERVER, json={"username": "SYN_ADMIN", "dry_run": False, "preview_id": preview["preview_id"]})
    assert result.status_code == 409
    with client.app.state.session_factory() as session:
        assert session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == "SYN_ADMIN")).active is False


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize("enabled", [False, True])
def test_retired_pin_never_authorizes_writes_and_server_key_stays_server_only(account_client, environment, enabled):
    client = account_client
    client.app.state.settings = replace(client.app.state.settings, environment=environment, department_accounts_enabled=enabled)
    pin = {"Origin": "https://testserver", "X-PAI-Manual-Token": "2468"}
    for path, body in [("/api/v1/operator-decisions/notices/SYN-ACCOUNT-001", {"choice": "HOLD", "rationale": "SYN", "expected_decision_id": None}), ("/api/v1/result-learning", {"notice_key": "SYN-ACCOUNT-001", "status": "NO_BID", "idempotency_key": "SYN-old-pin-test", "expected_outcome_id": None})]:
        assert client.post("https://testserver"+path, headers=pin, json=body).status_code == 401
        assert client.post("https://testserver"+path, headers={**SERVER, "Origin": "https://testserver"}, json=body).status_code == 403
    assert client.get("/api/v1/operator-decisions/notices/SYN-ACCOUNT-001", headers=SERVER).status_code == 200
    for path in ("/api/v1/performance", "/api/v1/notices"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=SERVER).status_code == 200
    if environment == "production" and not enabled:
        assert client.get("/api/v1/runtime-profile").status_code == 401
        runtime = client.get("/api/v1/runtime-profile", headers=SERVER).json()
        assert not runtime["manual_analysis_enabled"] and not runtime["operator_decisions_enabled"]


def test_pre_cutover_session_hash_is_invalid_but_fresh_cookie_works(account_client):
    client = account_client
    with client.app.state.session_factory() as session:
        account = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == "SYN_KMA1"))
        session.add(AccountSession(account_id=account.id, token_hash=_hash("SYN-precutover-cookie"), csrf_hash=_hash("SYN-precutover-csrf"), created_at=now_utc(), expires_at=now_utc()+timedelta(hours=1)))
        session.commit()
    client.cookies.set(SESSION_COOKIE, "SYN-precutover-cookie")
    assert client.get("/api/v1/accounts/me").status_code == 401
    _login(client)
    assert client.get("/api/v1/accounts/me").json()["authenticated"]


@pytest.mark.parametrize("path", ["/api/v1/company-awards/search", "/api/v1/pps-discovery/search", "/api/v1/pps-discovery/save", "/api/v1/prespec-discovery/search", "/api/v1/prespec-discovery/save", "/api/v1/pre-specifications/SYN-one/analysis"])
def test_external_routes_require_explicit_paid_cookie_and_csrf(account_client, path):
    client = account_client
    headers, _ = _login(client)
    def check(headers):
        raw = {**headers, "Cookie": "; ".join(f"{key}={value}" for key, value in client.cookies.items())}
        request = Request({"type": "http", "method": "POST", "scheme": "http", "path": path, "server": ("testserver", 80), "headers": [(key.lower().encode(), value.encode()) for key, value in {"Host": "testserver", **raw}.items()], "app": client.app})
        _require_manual_operator(request)
        return request.state.department_identity
    with pytest.raises(HTTPException) as failure: check(headers)
    assert failure.value.status_code == 403
    with client.app.state.session_factory() as session:
        session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == "SYN_KMA1")).paid_analysis_allowed = True
        session.commit()
    assert check(headers).paid_analysis_allowed
    with pytest.raises(HTTPException) as failure: check(ORIGIN)
    assert failure.value.status_code == 403


def test_account_can_read_sanitized_performance_but_cannot_edit_or_import(account_client):
    client = account_client
    headers, _ = _login(client)
    assert client.get("/api/v1/performance-records", headers=headers).status_code == 200
    from pai_loop.performance_records import _operator_access
    request = Request({"type": "http", "method": "POST", "scheme": "http", "path": "/api/v1/performance-records", "server": ("testserver", 80), "headers": [(key.lower().encode(), value.encode()) for key, value in {"Host": "testserver", **headers, "Cookie": "; ".join(f"{key}={value}" for key, value in client.cookies.items())}.items()], "app": client.app})
    with pytest.raises(HTTPException) as failure: _operator_access(request, mutation=True)
    assert failure.value.status_code == 403
    assert _peer(client).get("/api/v1/performance-records").status_code == 401
