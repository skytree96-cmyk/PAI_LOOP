"""SYN-only password boundaries and administrator reset security regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest
from sqlalchemy import select

from pai_loop.account_models import AccountAudit, AccountSession, DepartmentAccount
from pai_loop.accounts import verify_password
from test_department_accounts import (
    ORIGIN, PASSWORD, SERVER, _bootstrap, _login, _peer, account_client,
)


# Synthetic boundary values only; never credentials supplied by a user.
SHORT_PASSWORD = "Q7!"
OTHER_SHORT_PASSWORD = "R8!"
ACCOUNTS = "/api/v1/accounts"


def _account(client, username="SYN_KMA1"):
    with client.app.state.session_factory() as session:
        row = session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == username))
        return row.id, row.revision, row.password_hash


def _password_login(client, password, username="SYN_KMA1"):
    return client.post(f"{ACCOUNTS}/login", headers=ORIGIN,
                       json={"username": username, "password": password})


def test_bootstrap_two_rejected_three_created_and_repeat_preserves_hash(account_client):
    client = account_client
    candidate = {"username": "SYN_SHORT_ADMIN", "password": "Q7", "role": "ADMIN", "active": True}
    rejected = client.post(f"{ACCOUNTS}/bootstrap", headers=SERVER, json={"accounts": [candidate]})
    assert rejected.status_code == 422
    assert "Q7" not in rejected.text and '"input"' not in rejected.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(DepartmentAccount).where(DepartmentAccount.username == candidate["username"])) is None
    candidate["password"] = SHORT_PASSWORD
    created = _bootstrap(client, [candidate])
    assert created["accounts"][0]["status"] == "CREATED"
    saved = _account(client, candidate["username"])
    assert verify_password(SHORT_PASSWORD, saved[2])
    repeated = _bootstrap(client, [{**candidate, "password": PASSWORD}])
    assert repeated["accounts"][0]["status"] == "EXISTS_UNCHANGED"
    assert _account(client, candidate["username"]) == saved
    assert all(value not in str([created, repeated]) for value in (SHORT_PASSWORD, PASSWORD, saved[2]))
    with closing(_peer(client)) as peer:
        assert _password_login(peer, SHORT_PASSWORD, candidate["username"]).status_code == 200


@pytest.mark.parametrize("invalid_password", ["Q7", "S" * 257])
def test_reset_two_rejected_without_mutation_or_session_revocation(account_client, invalid_password):
    client = account_client
    _login(client)
    before = _account(client)
    with closing(_peer(client)) as admin:
        headers, _ = _login(admin, "SYN_ADMIN")
        rejected = admin.patch(f"{ACCOUNTS}/{before[0]}", headers=headers,
                               json={"expected_revision": before[1], "password": invalid_password})
        assert rejected.status_code == 422
        assert invalid_password not in rejected.text and '"input"' not in rejected.text
    assert _account(client) == before
    assert client.get(f"{ACCOUNTS}/me").status_code == 200
    with client.app.state.session_factory() as session:
        assert session.scalars(select(AccountAudit).where(
            AccountAudit.target_id == before[0], AccountAudit.event == "ACCOUNT_UPDATED_SESSIONS_REVOKED",
        )).all() == []


@pytest.mark.parametrize("new_password", [SHORT_PASSWORD, PASSWORD, "S" * 256])
def test_reset_accepts_three_and_long_passwords_and_revokes_all_old_sessions(account_client, new_password):
    client = account_client
    _login(client)
    account_id, revision, previous_hash = _account(client)
    with closing(_peer(client)) as second, closing(_peer(client)) as admin:
        _login(second)
        admin_headers, _ = _login(admin, "SYN_ADMIN")
        updated = admin.patch(f"{ACCOUNTS}/{account_id}", headers=admin_headers,
                              json={"expected_revision": revision, "password": new_password})
        assert updated.status_code == 200 and updated.json()["revision"] == revision + 1
        assert client.get(f"{ACCOUNTS}/me").status_code == 401
        assert second.get(f"{ACCOUNTS}/me").status_code == 401
        current = _account(client)
        assert current[2] != previous_hash and verify_password(new_password, current[2])
        with client.app.state.session_factory() as session:
            sessions = session.scalars(select(AccountSession).where(AccountSession.account_id == account_id)).all()
            assert len(sessions) == 2 and all(row.revoked_at is not None for row in sessions)
        public = [updated.text, admin.get(ACCOUNTS).text,
                  admin.get(f"{ACCOUNTS}/audit/list").text, admin.get(f"{ACCOUNTS}/sessions/list").text]
        assert all(secret not in "\n".join(public) for secret in (new_password, PASSWORD, previous_hash, current[2]))
    with closing(_peer(client)) as fresh:
        if new_password != PASSWORD:
            assert _password_login(fresh, PASSWORD).status_code == 401
        logged_in = _password_login(fresh, new_password)
        assert logged_in.status_code == 200 and logged_in.json()["authenticated"]
        assert new_password not in logged_in.text


@pytest.mark.parametrize("mode, expected", [
    ("anonymous", 401), ("department", 403), ("no_csrf", 403),
    ("wrong_csrf", 403), ("cross_origin", 403), ("server_key", 403),
    ("department_server_key", 403),
])
def test_reset_keeps_admin_session_csrf_and_origin_boundaries(account_client, mode, expected):
    client = account_client
    before = _account(client)
    with closing(_peer(client)) as actor:
        headers = dict(ORIGIN)
        if mode in {"department", "department_server_key"}:
            headers, _ = _login(actor)
        elif mode not in {"anonymous", "server_key"}:
            headers, _ = _login(actor, "SYN_ADMIN")
        if mode == "no_csrf": headers.pop("X-CSRF-Token")
        elif mode == "wrong_csrf": headers["X-CSRF-Token"] = "SYN-forged-csrf"
        elif mode == "cross_origin": headers.update(Origin="https://syn-untrusted.invalid", **{"Sec-Fetch-Site": "cross-site"})
        elif mode in {"server_key", "department_server_key"}: headers.update(SERVER)
        rejected = actor.patch(f"{ACCOUNTS}/{before[0]}", headers=headers,
                               json={"expected_revision": before[1], "password": SHORT_PASSWORD})
        assert rejected.status_code == expected
        assert SHORT_PASSWORD not in rejected.text
    assert _account(client) == before


def test_concurrent_resets_have_one_revision_winner_and_stale_reset_preserves_it(account_client):
    client = account_client
    account_id, revision, _ = _account(client)
    with closing(_peer(client)) as admin:
        headers, _ = _login(admin, "SYN_ADMIN")
        barrier = Barrier(2)
        def reset(password):
            barrier.wait()
            return admin.patch(f"{ACCOUNTS}/{account_id}", headers=headers,
                               json={"expected_revision": revision, "password": password})
        candidates = [SHORT_PASSWORD, OTHER_SHORT_PASSWORD]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(reset, candidates))
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = candidates[next(index for index, response in enumerate(responses) if response.status_code == 200)]
        current = _account(client)
        assert current[1] == revision + 1 and verify_password(winner, current[2])
        stale = admin.patch(f"{ACCOUNTS}/{account_id}", headers=headers,
                            json={"expected_revision": revision, "password": PASSWORD})
        assert stale.status_code == 409 and _account(client) == current
    with client.app.state.session_factory() as session:
        events = session.scalars(select(AccountAudit).where(
            AccountAudit.target_id == account_id, AccountAudit.event == "ACCOUNT_UPDATED_SESSIONS_REVOKED",
        )).all()
        assert len(events) == 1


def test_admin_self_reset_revokes_current_session_and_requires_new_password(account_client):
    client = account_client
    headers, identity = _login(client, "SYN_ADMIN")
    account_id = identity["account"]["id"]
    response = client.patch(f"{ACCOUNTS}/{account_id}", headers=headers,
                            json={"expected_revision": 1, "password": SHORT_PASSWORD})
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert client.get(f"{ACCOUNTS}/me").status_code == 401
    assert client.get(ACCOUNTS).status_code == 401
    assert _password_login(client, PASSWORD, "SYN_ADMIN").status_code == 401
    assert _password_login(client, SHORT_PASSWORD, "SYN_ADMIN").status_code == 200
    assert client.get(ACCOUNTS).status_code == 200


def test_password_validation_and_audit_never_echo_secret_canary(account_client, caplog):
    client = account_client
    canary = "SYN-password-redaction-canary-" * 10
    account_id, revision, previous_hash = _account(client)
    with closing(_peer(client)) as admin:
        headers, _ = _login(admin, "SYN_ADMIN")
        rejected = admin.patch(f"{ACCOUNTS}/{account_id}", headers=headers,
                               json={"expected_revision": revision, "password": canary})
        assert rejected.status_code == 422
        audit = admin.get(f"{ACCOUNTS}/audit/list")
    boot = client.post(f"{ACCOUNTS}/bootstrap", headers=SERVER,
                       json={"accounts": [{"username": "SYN_CANARY", "password": canary, "role": "ADMIN"}]})
    login = _password_login(client, canary)
    assert boot.status_code == login.status_code == 422
    visible = "\n".join([rejected.text, boot.text, login.text, audit.text, caplog.text])
    assert "SYN-password-redaction-canary" not in visible and previous_hash not in visible
    assert all('"input"' not in response.text for response in (rejected, boot, login))
    assert _account(client) == (account_id, revision, previous_hash)


def test_login_keeps_one_character_schema_boundary(account_client):
    client = account_client
    empty = _password_login(client, "")
    one = _password_login(client, "Q")
    assert empty.status_code == 422
    assert one.status_code == 401


def test_bootstrap_exact_upper_password_boundary(account_client):
    candidate = {"username": "SYN_MAX_ADMIN", "password": "S" * 257, "role": "ADMIN"}
    rejected = account_client.post(f"{ACCOUNTS}/bootstrap", headers=SERVER, json={"accounts": [candidate]})
    assert rejected.status_code == 422 and candidate["password"] not in rejected.text
    candidate["password"] = "S" * 256
    created = _bootstrap(account_client, [candidate])
    assert created["accounts"][0]["status"] == "CREATED"
    assert verify_password(candidate["password"], _account(account_client, candidate["username"])[2])
