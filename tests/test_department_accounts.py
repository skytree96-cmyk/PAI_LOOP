from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from pai_loop.account_models import AccountAudit, AccountLoginBucket, AccountSession, DepartmentAccount
from pai_loop.accounts import CSRF_COOKIE, SESSION_COOKIE, departments, now_utc, password_hash, verify_password
from pai_loop.migrations import ACCOUNT_MIGRATION_ID, apply_additive_migrations, pending_migrations, schema_migrations
from pai_loop.models import BidOutcome, IngestionJob, Notice, UserDecision


# Synthetic fixture credentials only; no real registration occurs in this suite.
PASSWORD = "SYN-test-password-only-0908"
SERVER = {"X-PAI-LOOP-API-KEY": "SYN-server-bootstrap-only"}
ORIGIN = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
NOTICE = "SYN-ACCOUNT-001"
DECISIONS = f"/api/v1/operator-decisions/notices/{NOTICE}"


def _bootstrap(client, accounts):
    preview = client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": accounts})
    assert preview.status_code == 200, preview.text
    applied = client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": accounts, "dry_run": False, "preview_id": preview.json()["preview_id"]})
    assert applied.status_code == 200, applied.text
    return applied.json()


@pytest.fixture
def account_client(client):
    assert client.post("/api/v1/notices", json={"notice_key": NOTICE, "bid_notice_no": NOTICE, "revision_no": "00", "title": "SYN 부서 계정 테스트", "agency": "SYN 기관", "deadline": "2027-01-01T00:00:00Z", "status": "OPEN"}).status_code == 201
    client.app.state.settings = replace(client.app.state.settings, department_accounts_enabled=True, public_read_only=True, public_manual_analysis_enabled=True, public_manual_analysis_token="2468", api_key=SERVER["X-PAI-LOOP-API-KEY"])
    catalog = list(departments())
    accounts = [{"username": f"SYN_KMA{index + 1}", "password": PASSWORD, "role": "DEPARTMENT", "department_id": department, "active": True} for index, department in enumerate(catalog[:2])]
    accounts.append({"username": "SYN_ADMIN", "password": PASSWORD, "role": "ADMIN", "active": True})
    _bootstrap(client, accounts)
    return client


def _login(client, username="SYN_KMA1"):
    result = client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": username, "password": PASSWORD})
    assert result.status_code == 200, result.text
    return {**ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}, result.json()


def _peer(client):
    return TestClient(client.app)


def _decision(client, headers, **extra):
    return client.post(DECISIONS, headers=headers, json={"choice": "HOLD", "rationale": "SYN 분석 전 판단 근거", "expected_decision_id": None, **extra})


def _outcome(client, headers, **extra):
    return client.post("/api/v1/result-learning", headers=headers, json={"notice_key": NOTICE, "idempotency_key": "SYN-result-request-001", "expected_outcome_id": None, "status": "NO_BID", **extra})


def test_account_flag_is_disabled_by_default_and_me_is_no_store(client):
    me = client.get("/api/v1/accounts/me")
    assert me.json()["enabled"] is False
    assert me.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/runtime-profile").json()["department_accounts_enabled"] is False
    assert client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": PASSWORD}).status_code == 404
    assert client.post("/api/v1/accounts/bootstrap", json={"accounts": [{"username": "SYN_ADMIN", "password": PASSWORD, "role": "ADMIN"}]}).status_code == 401


def test_bootstrap_requires_preview_and_is_new_only(account_client):
    client = account_client
    with client.app.state.session_factory() as session:
        before = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == "SYN_KMA1"))
        saved = (before.password_hash, before.role, before.department_id, before.active, before.revision)
    changed = [{"username": "SYN_KMA1", "password": "SYN-changed-password-only", "role": "ADMIN", "active": False, "paid_analysis_allowed": True}]
    missing = client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": changed, "dry_run": False})
    assert missing.status_code == 409
    response = _bootstrap(client, changed)
    assert response["accounts"][0]["status"] == "EXISTS_UNCHANGED"
    assert "password" not in str(response).lower()
    with client.app.state.session_factory() as session:
        after = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == "SYN_KMA1"))
        assert (after.password_hash, after.role, after.department_id, after.active, after.revision) == saved
        assert not after.paid_analysis_allowed


def test_bootstrap_preview_binds_password_roles_and_is_single_use(account_client):
    client = account_client
    plan = [{"username": "SYN_EXTRA_ADMIN", "password": PASSWORD, "role": "ADMIN"}]
    preview = client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": plan}).json()
    changed = [{**plan[0], "password": "SYN-different-password-only"}]
    assert client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": changed, "dry_run": False, "preview_id": preview["preview_id"]}).status_code == 409
    request = {"accounts": plan, "dry_run": False, "preview_id": preview["preview_id"]}
    assert client.post("/api/v1/accounts/bootstrap", headers=SERVER, json=request).status_code == 200
    assert client.post("/api/v1/accounts/bootstrap", headers=SERVER, json=request).status_code == 409
    inactive = client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_EXTRA_ADMIN", "password": PASSWORD})
    assert inactive.status_code == 401


@pytest.mark.parametrize("extra", [{"department_id": "SYN-forged-dept"}, {"department_id": None}, {"department_name": "forged"}])
def test_bootstrap_allowlist_and_password_validation_do_not_echo_input(account_client, extra):
    response = account_client.post("/api/v1/accounts/bootstrap", headers=SERVER, json={"accounts": [{"username": "SYN_INVALID", "password": PASSWORD, "role": "DEPARTMENT", **extra}]})
    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert "SYN-forged-dept" not in response.text


def test_password_hash_is_salted_scrypt_and_never_returned(account_client):
    first, second = password_hash(PASSWORD), password_hash(PASSWORD)
    assert first != second and PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password("SYN-wrong", first)
    headers, me = _login(account_client)
    assert PASSWORD not in str(me) and "password_hash" not in str(me)
    assert me["capabilities"]["write_decisions"]
    assert not me["capabilities"]["request_paid_analysis"]
    assert me["capabilities"]["recompute_analysis"]
    assert not me["capabilities"]["private_evidence"]


def test_login_cookie_flags_no_session_response_and_logout_revokes(account_client):
    client = account_client
    response = client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "syn_kma1", "password": PASSWORD})
    assert response.status_code == 200
    raw = client.cookies.get(SESSION_COOKIE)
    assert raw not in response.text
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(SESSION_COOKIE + "="))
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=28800" in cookie
    headers = {**ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
    saved_cookies = dict(client.cookies)
    with client.app.state.session_factory() as session:
        row = session.scalar(select(AccountSession))
        assert row.token_hash != raw and row.csrf_hash != headers["X-CSRF-Token"]
    assert client.post("/api/v1/accounts/logout", headers=headers).status_code == 200
    assert not client.cookies.get(SESSION_COOKIE)
    replay = _peer(client)
    replay.cookies.update(saved_cookies)
    assert replay.get(DECISIONS).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://attacker.invalid"}, {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}])
def test_login_requires_same_origin(account_client, headers):
    assert account_client.post("/api/v1/accounts/login", headers=headers, json={"username": "SYN_KMA1", "password": PASSWORD}).status_code == 403


@pytest.mark.parametrize("kind", ["missing", "wrong", "cross_origin", "cross_site"])
def test_authenticated_writes_require_bound_csrf_and_origin(account_client, kind):
    headers, _ = _login(account_client)
    if kind == "missing":
        headers.pop("X-CSRF-Token")
    elif kind == "wrong":
        headers["X-CSRF-Token"] = "SYN-forged"
    elif kind == "cross_origin":
        headers["Origin"] = "http://attacker.invalid"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    assert _decision(account_client, headers).status_code == 403
    with account_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UserDecision)) == 0


def test_expiry_and_disabled_account_reject_existing_cookie(account_client):
    _login(account_client)
    with account_client.app.state.session_factory() as session:
        row = session.scalar(select(AccountSession))
        row.expires_at = now_utc() - timedelta(seconds=1)
        session.commit()
    assert account_client.get("/api/v1/accounts/me").status_code == 401
    _login(account_client)
    with account_client.app.state.session_factory() as session:
        account = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == "SYN_KMA1"))
        account.active = False
        session.commit()
    assert account_client.get(DECISIONS).status_code == 401


def test_https_session_is_secure_and_expired_logout_clears_cookie(account_client):
    response = account_client.post("https://testserver/api/v1/accounts/login", headers={"Origin": "https://testserver"}, json={"username": "SYN_KMA1", "password": PASSWORD})
    assert response.status_code == 200
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith(SESSION_COOKIE + "="))
    assert "Secure" in cookie and "HttpOnly" in cookie
    with account_client.app.state.session_factory() as session:
        row = session.scalar(select(AccountSession))
        row.expires_at = now_utc() - timedelta(seconds=1)
        session.commit()
    assert account_client.post("https://testserver/api/v1/accounts/logout", headers={"Origin": "https://testserver"}).status_code == 200
    assert not account_client.cookies.get(SESSION_COOKIE)


def test_login_validation_never_echoes_password(account_client):
    response = account_client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": PASSWORD * 20})
    assert response.status_code == 422
    assert PASSWORD not in response.text and "input" not in response.text


def test_session_csrf_from_another_login_is_rejected(account_client):
    headers, _ = _login(account_client)
    other = _peer(account_client)
    other_headers, _ = _login(other)
    account_client.cookies.set(CSRF_COOKIE, other_headers["X-CSRF-Token"], domain="testserver.local", path="/")
    assert _decision(account_client, {**headers, "X-CSRF-Token": other_headers["X-CSRF-Token"]}).status_code == 403


def test_login_throttle_is_persistent_bounded_and_username_scoped(account_client):
    client = account_client
    for _ in range(5):
        assert client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": "SYN-wrong"}).status_code == 401
    peer = _peer(client)
    limited = peer.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "syn_kma1", "password": PASSWORD})
    assert limited.status_code == 429 and limited.headers["retry-after"] == "900"
    assert _login(peer, "SYN_KMA2")[1]["authenticated"]
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountLoginBucket)) == 3
        assert not any("SYN_KMA" in row.key for row in session.scalars(select(AccountLoginBucket)).all())
        for row in session.scalars(select(AccountLoginBucket)).all():
            row.expires_at = now_utc() - timedelta(seconds=1)
        session.commit()
    assert _login(client)[1]["authenticated"]


def test_all_24_departments_can_login_from_one_address_without_spending_failure_budget(account_client):
    client = account_client
    catalog = list(departments())
    assert len(catalog) == 24
    _bootstrap(client, [
        {"username": f"SYN_KMA{index + 1}", "password": PASSWORD, "role": "DEPARTMENT", "department_id": department, "active": True}
        for index, department in enumerate(catalog) if index >= 2
    ])
    for index, department in enumerate(catalog):
        with closing(_peer(client)) as peer:
            _, result = _login(peer, f"SYN_KMA{index + 1}")
            assert result["account"]["department_id"] == department
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountLoginBucket)) == 0
        assert session.scalar(select(func.count()).select_from(AccountAudit).where(AccountAudit.event == "LOGIN_SUCCEEDED")) == 24


def test_successful_login_preserves_prior_failure_counts_and_expiries(account_client):
    client = account_client
    bad = {"username": "syn_kma1", "password": "SYN-wrong"}
    for _ in range(4):
        assert client.post("/api/v1/accounts/login", headers=ORIGIN, json=bad).status_code == 401
    with client.app.state.session_factory() as session:
        before = {row.key: (row.attempts, row.expires_at) for row in session.scalars(select(AccountLoginBucket)).all()}
        assert len(before) == 3 and all(attempts == 4 for attempts, _ in before.values())
    _login(client)
    with client.app.state.session_factory() as session:
        after = {row.key: (row.attempts, row.expires_at) for row in session.scalars(select(AccountLoginBucket)).all()}
        assert after == before
    assert client.post("/api/v1/accounts/login", headers=ORIGIN, json=bad).status_code == 401
    blocked = client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": PASSWORD})
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "900"
    with client.app.state.session_factory() as session:
        assert all(row.attempts == 5 for row in session.scalars(select(AccountLoginBucket)).all())


@pytest.mark.parametrize("scope,limit", [("ip", 20), ("global", 100)])
def test_failed_login_address_and_global_limits_remain_in_force(account_client, scope, limit):
    client = account_client
    for index in range(limit):
        # A new username avoids the five-failure username limit. Global coverage
        # also changes the actual client address, never a forwarded header.
        address = f"192.0.2.{index + 1}" if scope == "global" else "192.0.2.1"
        with closing(TestClient(client.app, client=(address, 50000))) as peer:
            response = peer.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": f"SYN_UNKNOWN_{index}", "password": "SYN-wrong"})
            assert response.status_code == 401
    with closing(TestClient(client.app, client=("192.0.2.200" if scope == "global" else "192.0.2.1", 50000))) as peer:
        blocked = peer.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": PASSWORD})
        assert blocked.status_code == 429 and blocked.headers["retry-after"] == "900"
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountAudit).where(AccountAudit.event == "LOGIN_FAILED")) == limit
        assert max(row.attempts for row in session.scalars(select(AccountLoginBucket)).all()) == limit
        assert session.scalar(select(func.count()).select_from(AccountSession)) == 0


def test_concurrent_success_and_failures_preserve_the_username_limit(account_client):
    client = account_client
    for _ in range(3):
        assert client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": "SYN-wrong"}).status_code == 401

    def attempt(password):
        with closing(_peer(client)) as peer:
            return peer.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": password}).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(attempt, [PASSWORD, "SYN-wrong", "SYN-wrong", "SYN-wrong"]))
    assert statuses[0] in {200, 429}
    assert statuses[1:].count(401) == 2 and statuses[1:].count(429) == 1
    with client.app.state.session_factory() as session:
        assert all(row.attempts == 5 for row in session.scalars(select(AccountLoginBucket)).all())
    assert attempt(PASSWORD) == 429


def test_persistent_login_bucket_capacity_fails_closed_and_recovers_after_expiry(account_client):
    with account_client.app.state.session_factory() as session:
        session.add_all([AccountLoginBucket(key=f"{index:064x}", attempts=0, expires_at=now_utc() + timedelta(minutes=10)) for index in range(1024)])
        session.commit()
    response = account_client.post("/api/v1/accounts/login", headers=ORIGIN, json={"username": "SYN_KMA1", "password": PASSWORD})
    assert response.status_code == 429
    with account_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountLoginBucket)) == 1024
        for row in session.scalars(select(AccountLoginBucket)).all():
            row.expires_at = now_utc() - timedelta(seconds=1)
        session.commit()
    assert _login(account_client)[1]["authenticated"]


def test_pin_cannot_bypass_account_mode_even_in_development(account_client):
    assert _decision(account_client, {**ORIGIN, "X-PAI-Manual-Token": "2468"}).status_code == 401
    assert _outcome(account_client, {**ORIGIN, "X-PAI-Manual-Token": "2468"}).status_code == 401


def test_department_identity_is_server_assigned_and_other_departments_are_readable(account_client):
    first_headers, first_me = _login(account_client)
    saved = _decision(account_client, first_headers, actor_label="SYN_FORGED_ADMIN", department_id="SYN-forged").json()
    assert saved["actor_label"] == first_me["account"]["department_name"]
    assert saved["account_id"] == first_me["account"]["id"]
    assert saved["department_id"] == first_me["account"]["department_id"]
    assert saved["analysis_state_snapshot"] == "NOT_EVALUATED"
    other = _peer(account_client)
    other_headers, other_me = _login(other, "SYN_KMA2")
    assert other.get(DECISIONS).json()[0]["id"] == saved["id"]
    rejected = _decision(other, other_headers, expected_decision_id=saved["id"])
    assert rejected.status_code == 409
    second = _decision(other, other_headers)
    assert second.status_code == 201
    assert second.json()["department_id"] == other_me["account"]["department_id"]
    assert len(account_client.get(DECISIONS).json()) == 2
    assert account_client.get(f"/api/v1/notices/{NOTICE}").json()["decisions"] == []


def test_missing_own_decision_version_is_rejected_and_stale_tab_conflicts(account_client):
    headers, _ = _login(account_client)
    missing = account_client.post(DECISIONS, headers=headers, json={"choice": "HOLD", "rationale": "SYN reason"})
    assert missing.status_code == 422
    first = _decision(account_client, headers)
    assert first.status_code == 201
    assert _decision(account_client, headers).status_code == 409
    revised = _decision(account_client, headers, expected_decision_id=first.json()["id"], choice="GO")
    assert revised.status_code == 201
    assert len(account_client.get(DECISIONS).json()) == 2


def test_same_department_concurrent_first_decisions_have_one_winner(account_client):
    headers, _ = _login(account_client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _decision(account_client, headers), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]


def test_department_revision_keeps_cas_correct_across_clock_skew(account_client):
    headers, _ = _login(account_client)
    first = _decision(account_client, headers).json()
    with account_client.app.state.session_factory() as session:
        row = session.get(UserDecision, first["id"])
        row.created_at = now_utc() + timedelta(days=1)
        session.commit()
    second = _decision(account_client, headers, expected_decision_id=first["id"])
    assert second.status_code == 201 and second.json()["department_revision"] == 2
    third = _decision(account_client, headers, expected_decision_id=second.json()["id"])
    assert third.status_code == 201 and third.json()["department_revision"] == 3


def test_admin_can_read_but_never_proxy_department_writes_or_paid_analysis(account_client):
    headers, me = _login(account_client, "SYN_ADMIN")
    assert me["capabilities"]["manage_accounts"]
    assert not me["capabilities"]["write_decisions"]
    assert not me["capabilities"]["write_results"]
    assert not me["capabilities"]["request_paid_analysis"]
    assert account_client.get(DECISIONS).status_code == 200
    assert account_client.get("/api/v1/result-learning").status_code == 200
    assert _decision(account_client, headers, actor_label=list(departments().values())[0]).status_code == 403
    assert _outcome(account_client, headers).status_code == 403
    assert account_client.post("/api/v1/accounts/bootstrap", headers={**headers, **SERVER}, json={"accounts": [{"username": "SYN_ADMIN2", "password": PASSWORD, "role": "ADMIN"}]}).status_code == 403


def test_department_cannot_administer_accounts_and_admin_revision_revokes_sessions(account_client):
    headers, me = _login(account_client)
    assert account_client.get("/api/v1/accounts").status_code == 403
    assert account_client.get("/api/v1/accounts/audit/list").status_code == 403
    admin = _peer(account_client)
    admin_headers, _ = _login(admin, "SYN_ADMIN")
    account_id = me["account"]["id"]
    changed = admin.patch(f"/api/v1/accounts/{account_id}", headers=admin_headers, json={"expected_revision": 1, "paid_analysis_allowed": True})
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    assert account_client.get(DECISIONS).status_code == 401


    assert admin.patch(f"/api/v1/accounts/{account_id}", headers=admin_headers, json={"expected_revision": 1, "active": False}).status_code == 409
    assert admin.patch(f"/api/v1/accounts/{account_id}", headers=admin_headers, json={"expected_revision": 2, "role": "ADMIN"}).status_code == 422
    new_headers, new_me = _login(account_client)
    assert new_me["capabilities"]["request_paid_analysis"]
    audit = admin.get("/api/v1/accounts/audit/list")
    sessions = admin.get("/api/v1/accounts/sessions/list")
    assert "ACCOUNT_UPDATED_SESSIONS_REVOKED" in audit.text
    assert "token_hash" not in sessions.text and "csrf_hash" not in sessions.text
    assert account_client.cookies.get(SESSION_COOKIE) not in sessions.text
    session_id = next(row["id"] for row in sessions.json()["sessions"] if row["account_id"] == account_id and not row["revoked_at"])
    assert admin.post(f"/api/v1/accounts/sessions/{session_id}/revoke", headers=admin_headers).status_code == 200
    assert account_client.get(DECISIONS).status_code == 401


def test_admin_cannot_disable_last_active_admin(account_client):
    headers, me = _login(account_client, "SYN_ADMIN")
    response = account_client.patch(f"/api/v1/accounts/{me['account']['id']}", headers=headers, json={"expected_revision": 1, "active": False})
    assert response.status_code == 409
    assert account_client.get("/api/v1/accounts").status_code == 200


@pytest.mark.parametrize("path", [f"/api/v1/notices/{NOTICE}/decisions", "/api/v1/notices/analysis/batch", "/api/v1/ingestion/replay"])
def test_server_key_browser_path_cannot_bypass_department_policy(account_client, path):
    headers, _ = _login(account_client)
    assert account_client.post(path, headers={**headers, **SERVER}, json={"choice": "HOLD", "rationale": "SYN"}).status_code == 403


def test_server_to_server_contract_and_private_evidence_boundary(account_client):
    server = _peer(account_client)
    response = server.post(f"/api/v1/notices/{NOTICE}/decisions", headers=SERVER, json={"choice": "HOLD", "rationale": "SYN server record"})
    assert response.status_code == 201
    assert response.json()["department_id"] is None
    headers, _ = _login(account_client)
    assert account_client.get("/api/v1/performance-records", headers=headers).status_code in {401, 403}
    assert account_client.post("/api/v1/notices/analysis/batch", headers=headers, json={"notice_keys": [NOTICE]}).status_code == 401
    assert account_client.get(f"/api/v1/notices/{NOTICE}/decisions").status_code == 401


def test_results_are_department_owned_and_cross_department_reads_preserve_all(account_client):
    headers, me = _login(account_client)
    first = _outcome(account_client, headers)
    assert first.status_code == 201, first.text
    row = first.json()["outcome"]
    assert row["department_id"] == me["account"]["department_id"]
    other = _peer(account_client)
    other_headers, _ = _login(other, "SYN_KMA2")
    assert other.patch(f"/api/v1/result-learning/{row['id']}", headers=other_headers, json={"expected_updated_at": row["updated_at"], "operator_note": "SYN forged update"}).status_code == 403
    second = _outcome(other, other_headers)
    assert second.status_code == 201 and second.json()["outcome"]["id"] != row["id"]
    listing = other.get("/api/v1/result-learning?scope=ALL").json()["records"][0]
    assert len(listing["outcomes"]) == 2
    updated = account_client.patch(f"/api/v1/result-learning/{row['id']}", headers=headers, json={"expected_updated_at": row["updated_at"], "operator_note": "SYN own update"})
    assert updated.status_code == 200
    assert updated.json()["outcome"]["revision"] == 2
    assert account_client.patch(f"/api/v1/result-learning/{row['id']}", headers=headers, json={"expected_updated_at": row["updated_at"], "operator_note": "SYN stale"}).status_code == 409
    server = _peer(account_client)
    assert server.patch(f"/api/v1/result-learning/{row['id']}", headers=SERVER, json={"expected_updated_at": updated.json()["outcome"]["updated_at"], "operator_note": "SYN server proxy"}).status_code == 403


def test_legacy_results_are_explicitly_unassigned_and_cannot_be_claimed(account_client):
    server = _peer(account_client)
    saved = _outcome(server, SERVER).json()["outcome"]
    assert saved["department_id"] is None
    headers, _ = _login(account_client)
    assert account_client.patch(f"/api/v1/result-learning/{saved['id']}", headers=headers, json={"expected_updated_at": saved["updated_at"], "operator_note": "SYN claim"}).status_code == 403
    own = _outcome(account_client, headers, basis_outcome_id=saved["id"])
    assert own.status_code == 201
    assert own.json()["outcome"]["id"] != saved["id"]


def test_department_auto_bid_rate_preserves_owner_versions_and_audit(account_client):
    headers, identity = _login(account_client)
    calculation = {"mode": "AUTO", "basis_kind": "BASE_AMOUNT", "basis_amount": 200, "basis_reference": "SYN 기준가격 1쪽"}
    values = {"status": "SUBMITTED", "submitted_bid_amount": 100, "submitted_rate_calculation": calculation}
    first = _outcome(account_client, headers, **values)
    assert first.status_code == 201, first.text
    row = first.json()["outcome"]
    assert row["submitted_bid_rate"] == 50
    assert _outcome(account_client, headers, **values).json()["created"] is False
    stale_create = _outcome(account_client, headers, **values, idempotency_key="SYN-rate-other-request")
    assert stale_create.status_code == 409
    path = f"/api/v1/result-learning/{row['id']}"
    other = _peer(account_client)
    other_headers, _ = _login(other, "SYN_KMA2")
    change = {"expected_updated_at": row["updated_at"], "submitted_bid_amount": 120}
    assert other.patch(path, headers=other_headers, json=change).status_code == 403
    assert _peer(account_client).patch(path, headers=SERVER, json=change).status_code == 403
    updated = account_client.patch(path, headers=headers, json=change)
    assert updated.status_code == 200, updated.text
    assert updated.json()["outcome"]["submitted_bid_rate"] == 60
    assert updated.json()["outcome"]["department_id"] == row["department_id"]
    assert updated.json()["outcome"]["revision"] == 2
    assert account_client.patch(path, headers=headers, json=change).status_code == 409
    with account_client.app.state.session_factory() as session:
        item = session.get(BidOutcome, row["id"])
        history = item.evidence_json["_submitted_bid_rate"]["history"]
        assert len(history) == 2
        assert all(entry["actor_id"] == identity["account"]["id"] for entry in history)
        assert history[-1]["before"]["submitted_bid_rate"] == 50
        assert history[-1]["after"]["submitted_bid_rate"] == 60


def test_result_create_idempotence_and_department_concurrency(account_client):
    headers, _ = _login(account_client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda index: _outcome(account_client, headers, idempotency_key=f"SYN-concurrent-{index}"), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 201)
    retry = _outcome(account_client, headers, idempotency_key=f"SYN-concurrent-{winner}")
    assert retry.status_code == 201 and not retry.json()["created"]


def test_same_department_result_update_has_one_concurrent_winner(account_client):
    headers, _ = _login(account_client)
    row = _outcome(account_client, headers).json()["outcome"]
    def save(index):
        return account_client.patch(f"/api/v1/result-learning/{row['id']}", headers=headers, json={"expected_updated_at": row["updated_at"], "operator_note": f"SYN concurrent {index}"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_batch_decision_read_is_bounded_authenticated_and_admin_read_only(account_client):
    headers, _ = _login(account_client)
    _decision(account_client, headers)
    admin = _peer(account_client)
    admin_headers, _ = _login(admin, "SYN_ADMIN")
    endpoint = "/api/v1/operator-decisions/batch-read"
    assert admin.post(endpoint, headers=ORIGIN, json={"notice_keys": [NOTICE]}).status_code == 403
    response = admin.post(endpoint, headers=admin_headers, json={"notice_keys": [NOTICE, "SYN-MISSING"]})
    assert response.status_code == 200
    assert len(response.json()["decisions_by_notice"][NOTICE]) == 1
    assert response.json()["missing_notice_keys"] == ["SYN-MISSING"]
    assert admin.post(endpoint, headers=admin_headers, json={"notice_keys": [NOTICE] * 201}).status_code == 422


@pytest.mark.parametrize("username", ["SYN_KMA1", "SYN_ADMIN"])
def test_paid_analysis_requires_explicit_grant_and_forged_recompute_cannot_extract(account_client, monkeypatch, username):
    headers, _ = _login(account_client, username)
    monkeypatch.setattr("pai_loop.manual_analysis._source_kind", lambda notice: "PPS")
    response = account_client.post(f"/api/v1/notices/{NOTICE}/analysis/request", headers=headers, json={"run_extraction": True})
    assert response.status_code == 403, response.text
    forged = account_client.post(f"/api/v1/notices/{NOTICE}/analysis/request", headers=headers, json={"recompute_current": True})
    assert forged.status_code == 409
    with account_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngestionJob)) == 0


def test_free_recompute_is_allowed_without_paid_grant_and_records_server_actor(account_client, monkeypatch):
    headers, me = _login(account_client)
    monkeypatch.setattr("pai_loop.manual_analysis._source_kind", lambda notice: "PPS")
    monkeypatch.setattr("pai_loop.manual_analysis._has_complete_current_attachment_audit", lambda *args: True)
    monkeypatch.setattr("pai_loop.manual_analysis.latest_current_evaluation", lambda notice: SimpleNamespace(id="SYN-current-evaluation"))
    monkeypatch.setattr("pai_loop.manual_analysis._reason", lambda notice: SimpleNamespace(state="ANALYZED", reason_code="ANALYZED", reason="SYN complete", attempted=True))
    executions = []
    monkeypatch.setattr("pai_loop.manual_analysis._execute_reserved_manual_job", lambda *args: executions.append(args))
    response = account_client.post(f"/api/v1/notices/{NOTICE}/analysis/request", headers=headers, json={"recompute_current": True})
    assert response.status_code == 200, response.text
    assert len(executions) == 1 and executions[0][3] is True
    with account_client.app.state.session_factory() as session:
        job = session.scalar(select(IngestionJob))
        assert job.request_json["evaluation_only"] is True
        assert job.request_json["account_id"] == me["account"]["id"]
        assert session.scalar(select(AccountAudit.event).where(AccountAudit.event == "FREE_ANALYSIS_RESERVED"))
    profile = account_client.get("/api/v1/runtime-profile").json()
    assert profile["department_accounts_enabled"]
    assert profile["manual_analysis_policy"]["hourly_limit"] == 0


@pytest.mark.parametrize("quota, expected", [(0, 200), (1, 429)])
def test_paid_grant_and_existing_quota_control_actual_reservation(account_client, monkeypatch, quota, expected):
    headers, me = _login(account_client)
    account_client.app.state.settings = replace(account_client.app.state.settings, public_manual_analysis_hourly_limit=quota, openai_api_key="SYN-unusable-provider-key")
    with account_client.app.state.session_factory() as session:
        account = session.get(DepartmentAccount, me["account"]["id"])
        account.paid_analysis_allowed = True
        session.add(IngestionJob(source="MANUAL_ANALYSIS", mode="LIVE", status="COMPLETED", window_json={}, request_json={}, notice_keys=["SYN-OTHER-ACCOUNT-NOTICE"]))
        session.commit()
    monkeypatch.setattr("pai_loop.manual_analysis._source_kind", lambda notice: "PPS")
    executions = []
    monkeypatch.setattr("pai_loop.manual_analysis._execute_reserved_manual_job", lambda *args: executions.append(args))
    response = account_client.post(f"/api/v1/notices/{NOTICE}/analysis/request", headers=headers, json={"run_extraction": True})
    assert response.status_code == expected, response.text
    assert len(executions) == (1 if expected == 200 else 0)
    if expected == 200:
        assert executions[0][3] is False
        with account_client.app.state.session_factory() as session:
            assert session.scalar(select(AccountAudit.actor_account_id).where(AccountAudit.event == "PAID_ANALYSIS_RESERVED")) == me["account"]["id"]


def test_account_decision_keeps_stale_evaluation_and_reason_guards(account_client):
    server = _peer(account_client)
    assert server.post("/api/v1/ingestion/replay", headers=SERVER).status_code == 200
    key = "SYN-REVIEW-001"
    stale = account_client.get(f"/api/v1/notices/{key}").json()["latest_evaluation"]["id"]
    current = server.post(f"/api/v1/notices/{key}/evaluate", headers=SERVER, json={"ruleset_version": "SYN-account-current"}).json()["id"]
    headers, _ = _login(account_client)
    payload = {"choice": "HOLD", "rationale": "SYN rationale", "expected_decision_id": None, "evaluation_id": stale}
    endpoint = f"/api/v1/operator-decisions/notices/{key}"
    assert account_client.post(endpoint, headers=headers, json=payload).status_code == 409
    assert account_client.post(endpoint, headers=headers, json={**payload, "evaluation_id": current, "rationale": "   "}).status_code == 422
    response = account_client.post(endpoint, headers=headers, json={**payload, "evaluation_id": current})
    assert response.status_code == 201
    assert response.json()["analysis_snapshot"]["evaluation"]["id"] == current


def test_account_cancelled_notice_rejects_decision_and_result_changes(account_client):
    headers, _ = _login(account_client)
    previous = _outcome(account_client, headers).json()["outcome"]
    with account_client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE))
        notice.status = "CANCELLED"
        session.commit()
    assert _decision(account_client, headers).status_code == 409
    assert _outcome(account_client, headers, expected_outcome_id=previous["id"], idempotency_key="SYN-cancelled-new").status_code == 409
    assert account_client.patch(f"/api/v1/result-learning/{previous['id']}", headers=headers, json={"expected_updated_at": previous["updated_at"], "operator_note": "SYN forbidden"}).status_code == 409


def test_account_migration_preserves_legacy_rows_and_is_idempotent(client):
    assert client.post("/api/v1/ingestion/replay").status_code == 200
    before = client.get("/api/v1/notices/SYN-REVIEW-001").json()
    evaluation = before["latest_evaluation"]["id"]
    decision = client.post("/api/v1/notices/SYN-REVIEW-001/decisions", json={"evaluation_id": evaluation, "choice": "HOLD", "actor_label": list(departments().values())[0], "rationale": "SYN legacy row"}).json()
    outcome = client.post("/api/v1/notices/SYN-REVIEW-001/outcomes", json={"status": "NO_BID", "decision_id": decision["id"]}).json()
    engine = client.app.state.engine
    with engine.begin() as connection:
        connection.execute(schema_migrations.delete().where(schema_migrations.c.migration_id == ACCOUNT_MIGRATION_ID))
        for table in ("user_decisions", "bid_outcomes"):
            index = "uq_decision_notice_department_revision" if table == "user_decisions" else "uq_outcome_notice_department_revision"
            connection.exec_driver_sql(f"DROP INDEX {index}")
            for column in ("account_id", "department_id", "department_name", "department_revision"):
                connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
    assert ACCOUNT_MIGRATION_ID in pending_migrations(engine)
    assert apply_additive_migrations(engine) == [ACCOUNT_MIGRATION_ID]
    assert apply_additive_migrations(engine) == []
    assert pending_migrations(engine) == []
    with client.app.state.session_factory() as session:
        row = session.get(UserDecision, decision["id"])
        assert row.actor_label == decision["actor_label"] and row.department_id is None
        assert row.account_id is None and row.evaluation_id == evaluation
        linked = session.get(BidOutcome, outcome["id"])
        assert linked.decision_id == row.id and linked.department_id is None
    assert all(column["nullable"] for column in inspect(engine).get_columns("user_decisions") if column["name"] in {"account_id", "department_id", "department_name"})
