from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.account_models import AccountSession, DepartmentAccount
from pai_loop.accounts import CSRF_COOKIE, SESSION_COOKIE, _session_hash, serial_transaction
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.followup_models import TeamsFollow, TeamsFollowDelivery
from pai_loop.models import Notice, PpsNoticeAuthority
from pai_loop.teams_bot import TeamsDeliveryError
from pai_loop.teams_followups import (
    KST, MAX_ATTEMPTS, _deliver_claim, _finish, claim_due, dispatch_due,
    notice_block_reason, reminder_schedule, router, subscribe, synchronize_schedules,
)
from pai_loop.teams_identity_models import TeamsRecipient, TeamsSessionLink


def kst(day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(2030, 9, day, hour, minute, tzinfo=KST).astimezone(timezone.utc)


@pytest.fixture()
def store(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'SYN-followups.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        account = DepartmentAccount(id="SYN-ACCOUNT", username="SYN_ADMIN", role="ADMIN",
                                    password_hash="SYN-unused", active=True, created_at=kst(1))
        session.add(account)
        session.flush()
        for key in ("SYN-ALICE", "SYN-BOB"):
            session.add(TeamsRecipient(id=key, account_id=account.id, tenant_id="SYN-TENANT",
                aad_object_id=key, conversation_id="SYN-conversation-" + key,
                service_url="https://smba.trafficmanager.net/teams/", active=True,
                created_at=kst(1), updated_at=kst(1)))
        session.add(Notice(id="SYN-NOTICE", notice_key="SYN-FOLLOW-NOTICE", bid_notice_no="SYN-BID",
                           title="SYN 관심 공고", deadline=kst(20, 17), status="OPEN"))
        session.commit()
    yield factory
    engine.dispose()


def register(factory, *, now=None, recipient="SYN-ALICE") -> str:
    with factory() as session:
        serial_transaction(session, scope="teams-followups")
        follow = subscribe(session, recipient, "SYN-ACCOUNT", session.get(Notice, "SYN-NOTICE"), now or kst(10))
        session.commit()
        return follow.id


def records(factory, generation=1):
    with factory() as session:
        return {row.event_kind: row for row in session.scalars(select(TeamsFollowDelivery).where(
            TeamsFollowDelivery.generation == generation)).all()}


def dispatch(factory, now, *, sender=None, builder=None, **kwargs):
    return dispatch_due(factory, now=now, enabled=True, sender=sender or (lambda *args: "SYN-sent"),
                        card_builder=builder or (lambda *args, **kw: {"type": "AdaptiveCard"}), **kwargs)


def test_reminders_use_kst_and_early_deadlines_precede_closing():
    schedule = reminder_schedule(kst(20, 17))
    assert schedule["D_MINUS_5"] == (kst(15), kst(16, 0))
    assert schedule["DEADLINE_DAY"] == (kst(20), kst(20, 17))
    assert reminder_schedule(kst(20, 8))["DEADLINE_DAY"][0] == kst(20, 7, 30)
    assert reminder_schedule(kst(20, 0, 10))["DEADLINE_DAY"][0] == kst(20, 0)
    assert reminder_schedule(kst(20, 0))["DEADLINE_DAY"][0] == kst(19, 23, 30)
    assert reminder_schedule(kst(20, 17).replace(tzinfo=None))["DEADLINE_DAY"][0] == kst(20)


def test_registration_idempotency_and_each_phase_sends_once(store):
    assert register(store) == register(store)
    rows = records(store)
    assert len(rows) == 3
    assert rows["REGISTERED"].scheduled_at == kst(10)
    delivered = []
    builder = lambda session, notice, kind, **kw: {"kind": kind, "title": notice.title}
    sender = lambda recipient, card: delivered.append((recipient.id, card))
    assert dispatch(store, kst(10), sender=sender, builder=builder)["outcomes"] == {"SENT": 1}
    assert dispatch(store, kst(10), sender=sender)["processed"] == 0
    with store() as session:
        session.get(Notice, "SYN-NOTICE").title = "SYN 새 분석 기준 공고"
        session.commit()
    assert dispatch(store, kst(15), sender=sender, builder=builder)["processed"] == 1
    assert dispatch(store, kst(20), sender=sender, builder=builder)["processed"] == 1
    assert [item[1]["kind"] for item in delivered] == ["REGISTERED", "D_MINUS_5", "DEADLINE_DAY"]
    assert delivered[-1][1]["title"] == "SYN 새 분석 기준 공고"
    assert {item[0] for item in delivered} == {"SYN-ALICE"}


@pytest.mark.parametrize("registered,minus_status,day_status", [
    (kst(16), "SKIPPED", "PENDING"),
    (kst(20, 8), "SKIPPED", "PENDING"),
    (kst(20, 10), "SKIPPED", "SKIPPED"),
])
def test_late_registration_does_not_backfill_old_phases(store, registered, minus_status, day_status):
    register(store, now=registered)
    rows = records(store)
    assert rows["D_MINUS_5"].status == minus_status
    assert rows["DEADLINE_DAY"].status == day_status
    assert dispatch(store, registered)["outcomes"] == {"SENT": 1}


def test_recovery_sends_only_within_phase_day_window(store):
    register(store)
    dispatch(store, kst(10))
    assert dispatch(store, kst(16))["processed"] == 0
    assert records(store)["D_MINUS_5"].error_code == "MISSED_WINDOW"
    assert dispatch(store, kst(20, 16))["outcomes"] == {"SENT": 1}
    assert dispatch(store, kst(21))["processed"] == 0


def test_extension_replans_pending_and_keeps_sent_history(store):
    register(store)
    dispatch(store, kst(10))
    dispatch(store, kst(15))
    with store() as session:
        session.get(Notice, "SYN-NOTICE").deadline = kst(25, 17)
        session.commit()
    assert dispatch(store, kst(20))["processed"] == 0
    rows = records(store)
    assert rows["D_MINUS_5"].status == "SENT"
    assert rows["D_MINUS_5"].deadline_snapshot == kst(20, 17)
    assert rows["DEADLINE_DAY"].scheduled_at == kst(25)
    assert dispatch(store, kst(25))["outcomes"] == {"SENT": 1}


def test_extension_reactivates_a_phase_previously_skipped_for_late_registration(store):
    register(store, now=kst(18))
    with store() as session:
        session.get(Notice, "SYN-NOTICE").deadline = kst(25, 17)
        session.commit()
    synchronize_schedules(store, kst(18))
    assert records(store)["D_MINUS_5"].scheduled_at == kst(20)
    assert records(store)["D_MINUS_5"].status == "PENDING"


@pytest.mark.parametrize("status", ["CANCELLED", "CLOSED", "SUPERSEDED"])
def test_inactive_notices_cancel_all_unsent_notifications(store, status):
    register(store)
    with store() as session:
        session.get(Notice, "SYN-NOTICE").status = status
        session.commit()
    assert dispatch(store, kst(10))["processed"] == 0
    assert {row.status for row in records(store).values()} == {"CANCELLED"}


@pytest.mark.parametrize("disposition,revision,deadline,expected", [
    ("CANCELLED", "00", kst(20, 17), "NOTICE_INACTIVE"),
    ("QUARANTINED", "00", kst(20, 17), "NOTICE_INACTIVE"),
    ("VALID", "01", kst(20, 17), "NOTICE_SUPERSEDED"),
    ("VALID", "00", kst(21, 17), "NOTICE_SUPERSEDED"),
    ("VALID", "00", kst(20, 17), None),
])
def test_authority_cancellation_and_reissue_are_checked(store, disposition, revision, deadline, expected):
    with store() as session:
        notice = session.get(Notice, "SYN-NOTICE")
        notice.notice_key = "PPS-SYN-FOLLOW"
        session.add(PpsNoticeAuthority(bid_notice_no=notice.bid_notice_no, disposition=disposition,
            revision_no=revision, deadline=deadline, authority_sha256="0" * 64))
        session.commit()
        assert notice_block_reason(session, notice, kst(10)) == expected


@pytest.mark.parametrize("target", ["account", "recipient"])
def test_disabled_owner_or_recipient_suppresses_messages(store, target):
    register(store)
    with store() as session:
        row = session.get(DepartmentAccount if target == "account" else TeamsRecipient,
                          "SYN-ACCOUNT" if target == "account" else "SYN-ALICE")
        row.active = False
        session.commit()
    assert dispatch(store, kst(10))["processed"] == 0
    assert all(row.status == "CANCELLED" for row in records(store).values())


def test_concurrent_registration_and_claim_are_exact(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: register(store), range(4)))
    assert len(set(ids)) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: claim_due(store, kst(10)), range(4)))
    assert len([item for item in claims if item is not None]) == 1
    assert records(store)["REGISTERED"].status == "CLAIMED"


def test_stale_presend_claim_is_safe_to_retry_but_sending_is_unknown(store):
    register(store)
    claim = claim_due(store, kst(10))
    synchronize_schedules(store, kst(10) + timedelta(minutes=4))
    assert records(store)["REGISTERED"].status == "RETRY"
    assert _deliver_claim(store, claim, kst(10) + timedelta(minutes=4), lambda *args: pytest.fail("stale owner sent"),
                          lambda *args, **kw: {}, "") == "LOST_LEASE"
    new_claim = claim_due(store, kst(10) + timedelta(minutes=4))
    with store() as session:
        row = session.get(TeamsFollowDelivery, new_claim[0])
        row.status = "SENDING"
        session.commit()
    synchronize_schedules(store, kst(10) + timedelta(minutes=8))
    assert records(store)["REGISTERED"].status == "UNKNOWN"
    assert dispatch(store, kst(10) + timedelta(minutes=9))["processed"] == 0
    assert _finish(store, new_claim[0], new_claim[1], "SENT", kst(10)) == "LOST_LEASE"


@pytest.mark.parametrize("outcome,expected", [("FAILED", "FAILED"), ("UNKNOWN", "UNKNOWN")])
def test_terminal_and_ambiguous_delivery_never_autoretry(store, outcome, expected):
    register(store)
    def sender(*args):
        raise TeamsDeliveryError(outcome)
    assert dispatch(store, kst(10), sender=sender)["outcomes"] == {expected: 1}
    assert dispatch(store, kst(11), sender=sender)["processed"] == 0


def test_unclassified_sender_error_is_unknown(store):
    register(store)
    def sender(*args):
        raise TimeoutError("SYN internal detail must not persist")
    assert dispatch(store, kst(10), sender=sender)["outcomes"] == {"UNKNOWN": 1}
    assert records(store)["REGISTERED"].error_code == "SEND_OUTCOME_UNKNOWN"


def test_retries_are_bounded_and_do_not_ignore_retry_after(store):
    register(store)
    attempts = []
    def sender(*args):
        attempts.append(1)
        raise TeamsDeliveryError("RETRY", status_code=429, retry_after_seconds=120)
    current = kst(10)
    assert dispatch(store, current, sender=sender)["outcomes"] == {"RETRY": 1}
    assert records(store)["REGISTERED"].scheduled_at == current + timedelta(seconds=120)
    assert dispatch(store, current + timedelta(seconds=119), sender=sender)["processed"] == 0
    for _ in range(MAX_ATTEMPTS - 1):
        current += timedelta(seconds=120)
        dispatch(store, current, sender=sender)
    assert len(attempts) == MAX_ATTEMPTS
    assert records(store)["REGISTERED"].status == "FAILED"
    assert dispatch(store, current + timedelta(hours=1), sender=sender)["processed"] == 0


def test_render_failure_retries_without_provider_calls_and_is_bounded(store):
    register(store)
    def bad_builder(*args, **kwargs):
        raise ValueError("SYN private builder detail")
    for offset in range(MAX_ATTEMPTS):
        dispatch(store, kst(10) + timedelta(hours=offset), builder=bad_builder,
                 sender=lambda *args: pytest.fail("card failure must not send"))
    assert records(store)["REGISTERED"].status == "FAILED"
    assert records(store)["REGISTERED"].attempts == MAX_ATTEMPTS


def test_changed_deadline_or_unsubscribe_invalidates_claim_before_send(store):
    follow_id = register(store)
    claim = claim_due(store, kst(10))
    with store() as session:
        session.get(TeamsFollow, follow_id).active = False
        session.commit()
    assert _deliver_claim(store, claim, kst(10), lambda *args: pytest.fail("unsubscribed sent"),
                          lambda *args, **kw: {}, "") == "CANCELLED"
    assert records(store)["REGISTERED"].status == "CANCELLED"


def test_worker_disabled_by_default(store, monkeypatch):
    register(store)
    monkeypatch.delenv("PAI_TEAMS_FOLLOWUPS_ENABLED", raising=False)
    assert dispatch_due(store, now=kst(10))["enabled"] is False
    assert records(store)["REGISTERED"].status == "PENDING"


@pytest.fixture()
def browsers(store, monkeypatch):
    import pai_loop.teams_followups as module
    import pai_loop.accounts as accounts
    monkeypatch.setattr(module, "now_utc", lambda: kst(10))
    monkeypatch.setattr(accounts, "now_utc", lambda: kst(10))
    app = FastAPI()
    app.state.session_factory = store
    app.state.settings = SimpleNamespace(department_accounts_enabled=True, environment="test")
    app.include_router(router)
    clients = []
    for recipient in ("SYN-ALICE", "SYN-BOB", None):
        token = "SYN-browser-" + (recipient or "UNLINKED")
        csrf = token + "-csrf"
        with store() as session:
            session.add(AccountSession(id=token, account_id="SYN-ACCOUNT", token_hash=_session_hash(token),
                        csrf_hash=_session_hash(csrf), created_at=kst(1), expires_at=kst(30)))
            session.flush()
            if recipient:
                session.add(TeamsSessionLink(session_id=token, account_id="SYN-ACCOUNT", recipient_id=recipient,
                                            created_at=kst(1)))
            session.commit()
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE, token)
        client.cookies.set(CSRF_COOKIE, csrf)
        client.headers.update({"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin", "X-CSRF-Token": csrf})
        clients.append(client)
    yield clients
    for client in clients:
        client.close()


def test_real_session_identity_owns_own_interests_even_with_shared_account(store, browsers):
    alice, bob, unlinked = browsers
    path = "/api/v1/teams/follows/SYN-FOLLOW-NOTICE"
    assert alice.post(path).status_code == 200
    assert alice.post(path).status_code == 200
    assert len(alice.get("/api/v1/teams/follows").json()["items"]) == 1
    assert bob.get("/api/v1/teams/follows").json()["items"] == []
    assert bob.delete(path).status_code == 200
    assert len(alice.get("/api/v1/teams/follows").json()["items"]) == 1
    assert unlinked.post(path).status_code == 409
    assert unlinked.get("/api/v1/teams/follows").status_code == 409
    assert bob.post(path).status_code == 200
    sent = []
    dispatch(store, kst(10), sender=lambda recipient, card: sent.append(recipient.id))
    assert sorted(sent) == ["SYN-ALICE", "SYN-BOB"]


def test_unsubscribe_preserves_audit_and_resubscribe_starts_new_generation(store, browsers):
    alice = browsers[0]
    path = "/api/v1/teams/follows/SYN-NOTICE"
    assert alice.post(path).status_code == 200
    dispatch(store, kst(10))
    assert alice.delete(path).json()["active"] is False
    assert alice.get("/api/v1/teams/follows").json()["items"] == []
    assert records(store)["REGISTERED"].status == "SENT"
    assert records(store)["D_MINUS_5"].status == "CANCELLED"
    assert alice.post(path).status_code == 200
    assert len(records(store, 2)) == 3
    assert dispatch(store, kst(10))["outcomes"] == {"SENT": 1}
    assert records(store)["REGISTERED"].status == "SENT"


def test_follow_routes_reject_csrf_missing_login_cross_origin_and_closed_notice(store, browsers):
    alice = browsers[0]
    path = "/api/v1/teams/follows/SYN-FOLLOW-NOTICE"
    assert alice.post(path, headers={"X-CSRF-Token": ""}).status_code == 403
    assert alice.delete(path, headers={"X-CSRF-Token": ""}).status_code == 403
    assert alice.post(path, headers={"Origin": "https://example.org"}).status_code == 403
    assert alice.post(path, headers={"X-PAI-LOOP-API-KEY": "SYN-server-key"}).status_code == 403
    assert alice.post("/api/v1/teams/follows/SYN-missing").status_code == 404
    with store() as session:
        session.get(Notice, "SYN-NOTICE").deadline = kst(9)
        session.commit()
    assert alice.post(path).status_code == 409
    alice.cookies.clear()
    assert alice.get("/api/v1/teams/follows").status_code == 401


def test_cli_rejects_missing_bot_before_opening_a_database(monkeypatch):
    import pai_loop.teams_followups as module
    from pai_loop.config import Settings
    monkeypatch.setattr("sys.argv", ["pai-loop-teams-followups", "--once"])
    monkeypatch.setenv("PAI_TEAMS_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings()))
    monkeypatch.delenv("PAI_TEAMS_BOT_APP_ID", raising=False)
    monkeypatch.setattr("pai_loop.database.build_engine", lambda *args: pytest.fail("misconfigured CLI opened DB"))
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_cli_validates_production_security_before_opening_database(monkeypatch):
    import pai_loop.teams_followups as module
    from pai_loop.config import Settings
    monkeypatch.setattr("sys.argv", ["pai-loop-teams-followups", "--once"])
    monkeypatch.setenv("PAI_TEAMS_FOLLOWUPS_ENABLED", "true")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(environment="production")))
    monkeypatch.setattr("pai_loop.database.build_engine", lambda *args: pytest.fail("unsafe CLI opened DB"))
    with pytest.raises(RuntimeError):
        module.main()
