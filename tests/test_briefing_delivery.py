from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from pai_loop.account_models import DepartmentAccount
from pai_loop.briefing_cards import build_briefing_card
from pai_loop.briefing_delivery import (
    KST,
    daily_window,
    dispatch_briefings,
    enqueue_subscription_ping,
    kst_day,
    schedule_daily,
)
from pai_loop.briefing_models import TeamsBriefingDelivery
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import Notice
from pai_loop.teams_bot import TeamsDeliveryError
from pai_loop.teams_identity_models import TeamsRecipient

BASE_URL = "https://example.test"


def kst(day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(2030, 9, day, hour, minute, tzinfo=KST).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("PAI_TEAMS_BRIEFING_ENABLED", "true")
    monkeypatch.delenv("PAI_TEAMS_BRIEFING_HOUR", raising=False)
    monkeypatch.delenv("PAI_TEAMS_BRIEFING_MINUTE", raising=False)


@pytest.fixture()
def store(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'SYN-briefing.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        # An ADMIN account carries no department, so it exercises the org-wide
        # fallback next to a real department account.
        for ident, role, department in (
            ("SYN-ACC-A", "DEPARTMENT", "future-competency-solution"),
            ("SYN-ACC-B", "ADMIN", None),
        ):
            session.add(DepartmentAccount(id=ident, username="SYN_" + ident, role=role,
                                          department_id=department, password_hash="SYN-unused",
                                          active=True, created_at=kst(1)))
        session.flush()
        for key, account in (("SYN-ALICE", "SYN-ACC-A"), ("SYN-BOB", "SYN-ACC-B")):
            session.add(TeamsRecipient(id=key, account_id=account, tenant_id="SYN-TENANT",
                                       aad_object_id=key, conversation_id="SYN-conv-" + key,
                                       service_url="https://smba.trafficmanager.net/teams/",
                                       active=True, created_at=kst(1), updated_at=kst(1)))
        session.add(Notice(id="SYN-N1", notice_key="SYN-BRIEF-1", bid_notice_no="SYN-B1",
                           title="SYN 직무역량 교육과정 운영", agency="SYN 기관",
                           published_at=kst(20), deadline=kst(28, 17), status="OPEN"))
        session.commit()
    yield factory
    engine.dispose()


def _rows(factory, **where):
    with factory() as session:
        query = select(TeamsBriefingDelivery)
        for column, value in where.items():
            query = query.where(getattr(TeamsBriefingDelivery, column) == value)
        return list(session.scalars(query).all())


def test_daily_window_opens_at_kst_morning_and_closes_the_same_day():
    day = kst_day(kst(22, 12))
    start, end = daily_window(day)
    assert start.astimezone(KST).strftime("%H:%M") == "08:30"
    assert end - start == timedelta(hours=6)
    # A tick before the window or after it must never queue that day's run.
    assert not (start <= kst(22, 8, 0) < end)
    assert not (start <= kst(22, 15, 0) < end)


def test_daily_is_queued_once_per_recipient_and_only_when_the_day_is_ready(store):
    with store() as session:
        assert schedule_daily(session, now=kst(22, 9), ready=False) == 0
        session.commit()
    assert _rows(store) == []

    with store() as session:
        assert schedule_daily(session, now=kst(22, 9), ready=True) == 2
        session.commit()
    with store() as session:
        # A second tick inside the same window adds nothing.
        assert schedule_daily(session, now=kst(22, 10), ready=True) == 0
        session.commit()
    assert len(_rows(store, kind="DAILY")) == 2

    with store() as session:
        assert schedule_daily(session, now=kst(23, 9), ready=True) == 2
        session.commit()
    assert len(_rows(store, kind="DAILY")) == 4


def test_pairing_ping_is_queued_once_a_day_and_records_the_department(store):
    with store() as session:
        recipient = session.get(TeamsRecipient, "SYN-ALICE")
        assert enqueue_subscription_ping(session, recipient, now=kst(22, 14)) is not None
        session.commit()
    with store() as session:
        recipient = session.get(TeamsRecipient, "SYN-ALICE")
        # Reconnecting the same day must not queue a second ping.
        assert enqueue_subscription_ping(session, recipient, now=kst(22, 16)) is None
        session.commit()
    rows = _rows(store, kind="SUBSCRIBED")
    assert len(rows) == 1
    assert rows[0].department_id == "future-competency-solution"
    # A ping is deliberately not gated on the day's analysis being complete.
    assert rows[0].readiness_checked is False


def test_opting_out_stops_the_briefing_without_touching_interest_reminders(store):
    with store() as session:
        session.get(TeamsRecipient, "SYN-ALICE").briefing_enabled = False
        session.commit()
    sent: list[str] = []
    dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL,
                       sender=lambda recipient, card: sent.append(recipient.id) or "SYN")
    assert sent == ["SYN-BOB"]
    assert [row.recipient_id for row in _rows(store)] == ["SYN-BOB"]

    with store() as session:
        # An explicit opt-out also suppresses the pairing ping on reconnect.
        recipient = session.get(TeamsRecipient, "SYN-ALICE")
        assert enqueue_subscription_ping(session, recipient, now=kst(22, 14)) is None
        session.commit()


def test_an_untouched_recipient_is_subscribed(store):
    with store() as session:
        # NULL is "never chose", which must read as subscribed so that nobody
        # silently stops receiving the briefing when the column is added.
        assert session.get(TeamsRecipient, "SYN-ALICE").briefing_enabled is None
    result = dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL,
                                sender=lambda recipient, card: "SYN")
    assert result["queued"] == 2


def test_opting_out_after_queueing_cancels_the_queued_briefing(store):
    with store() as session:
        assert schedule_daily(session, now=kst(22, 9), ready=True) == 2
        session.commit()
    with store() as session:
        session.get(TeamsRecipient, "SYN-ALICE").briefing_enabled = False
        session.commit()
    sent: list[str] = []
    dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL,
                       sender=lambda recipient, card: sent.append(recipient.id) or "SYN")
    assert sent == ["SYN-BOB"]
    cancelled = [row for row in _rows(store) if row.status == "CANCELLED"]
    assert [row.error_code for row in cancelled] == ["BRIEFING_DISABLED"]


def test_inactive_recipient_is_never_pinged(store):
    with store() as session:
        recipient = session.get(TeamsRecipient, "SYN-ALICE")
        recipient.active = False
        session.flush()
        assert enqueue_subscription_ping(session, recipient, now=kst(22, 14)) is None
        session.commit()
    assert _rows(store, kind="SUBSCRIBED") == []


def test_dispatch_sends_each_queued_briefing_once(store):
    sent: list[tuple[str, dict]] = []

    def sender(recipient, card):
        sent.append((recipient.id, card))
        return "SYN-activity"

    result = dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL, sender=sender)
    assert result["queued"] == 2
    assert result["outcomes"] == {"SENT": 2}
    assert {identity for identity, _ in sent} == {"SYN-ALICE", "SYN-BOB"}

    # Nothing is left due, so a second tick is a no-op rather than a resend.
    again = dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL, sender=sender)
    assert again["processed"] == 0
    assert len(sent) == 2
    assert {row.status for row in _rows(store)} == {"SENT"}


def test_card_building_cannot_detach_the_outbox_row(store):
    """Reading the stored briefing expunges its session.

    Sharing that session with the outbox would detach the delivery row, and
    every status write after it would be silently thrown away: the message goes
    out while the row still reads CLAIMED.
    """

    def expunging_builder(session, **kwargs):
        session.expunge_all()
        return {"type": "AdaptiveCard", "body": [], "actions": []}

    result = dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL,
                                sender=lambda recipient, card: "SYN",
                                card_builder=expunging_builder)
    assert result["outcomes"] == {"SENT": 2}
    rows = _rows(store)
    assert {row.status for row in rows} == {"SENT"}
    assert all(row.sent_at is not None for row in rows)


def test_unknown_outcome_is_recorded_and_never_retried(store):
    def sender(recipient, card):
        raise TeamsDeliveryError("UNKNOWN")

    result = dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL, sender=sender)
    assert result["outcomes"] == {"UNKNOWN": 2}
    assert {row.status for row in _rows(store)} == {"UNKNOWN"}

    calls: list[str] = []

    def counting(recipient, card):
        calls.append(recipient.id)
        return "SYN-activity"

    # An uncertain send may already have arrived, so the tick must not pick it up.
    dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL, sender=counting)
    assert calls == []


def test_disabled_feature_queues_and_sends_nothing(store, monkeypatch):
    monkeypatch.setenv("PAI_TEAMS_BRIEFING_ENABLED", "false")

    def forbidden(recipient, card):
        raise AssertionError("delivery must stay off until it is enabled")

    assert dispatch_briefings(store, now=kst(22, 9), ready=True, base_url=BASE_URL, sender=forbidden) == {
        "enabled": False, "queued": 0, "processed": 0, "outcomes": {},
    }
    assert _rows(store) == []


def test_card_states_which_basis_it_was_built_on(store):
    with store() as session:
        daily = build_briefing_card(session, kind="DAILY", department_id=None,
                                    base_url=BASE_URL, now=kst(22, 9))
        ping = build_briefing_card(session, kind="SUBSCRIBED", department_id=None,
                                   base_url=BASE_URL, now=kst(22, 14))
    daily_text = str(daily)
    ping_text = str(ping)
    assert "오늘의 공고 브리핑" in daily_text and "연결 시점 기준" not in daily_text
    assert "지금 기준 공고 브리핑" in ping_text and "연결 시점 기준" in ping_text
    for card in (daily, ping):
        assert card["type"] == "AdaptiveCard"
        for action in card["actions"]:
            assert action["url"].startswith(BASE_URL + "/")


def test_unknown_department_falls_back_instead_of_failing(store):
    with store() as session:
        # An id no catalog knows must not raise and must not zero the score.
        scoped = build_briefing_card(session, kind="DAILY", department_id="SYN-NO-SUCH-DEPT",
                                     base_url=BASE_URL, now=kst(22, 9))
        unscoped = build_briefing_card(session, kind="DAILY", department_id=None,
                                       base_url=BASE_URL, now=kst(22, 9))
    assert scoped["body"] == unscoped["body"]
