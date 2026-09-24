"""Scheduling and delivery of the personal daily briefing.

Two kinds share one outbox. ``DAILY`` is the 08:30 KST run and is enqueued only
for a KST day whose ingestion and analysis are confirmed complete, so a partial
morning never goes out as the settled view. ``SUBSCRIBED`` is the ping sent once
a person pairs: it is not a summary of a finished day, so it is not gated, and
its card says it is a snapshot.

Delivery reuses the follow outbox's boundary: a claimed row is sent at most
once, and a send whose outcome is unknown is never retried automatically.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, time as day_time, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .briefing_cards import build_briefing_card
from .briefing_models import TeamsBriefingDelivery
from .teams_bot import TeamsBotSettings, TeamsDeliveryError, send_personal_card
from .teams_identity_models import TeamsRecipient

KST = timezone(timedelta(hours=9), "Asia/Seoul")
KINDS = ("SUBSCRIBED", "DAILY")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 180
DEFAULT_DAILY_HOUR = 8
DEFAULT_DAILY_MINUTE = 30
# A missed morning is not backfilled into the evening: after this the day's run
# is left alone and the next day's briefing is the next thing that goes out.
DAILY_WINDOW_HOURS = 6


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def briefings_enabled() -> bool:
    return os.getenv("PAI_TEAMS_BRIEFING_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _daily_time() -> day_time:
    def _read(name: str, fallback: int, upper: int) -> int:
        try:
            value = int(os.getenv(name, "").strip())
        except (TypeError, ValueError):
            return fallback
        return value if 0 <= value <= upper else fallback

    return day_time(
        hour=_read("PAI_TEAMS_BRIEFING_HOUR", DEFAULT_DAILY_HOUR, 23),
        minute=_read("PAI_TEAMS_BRIEFING_MINUTE", DEFAULT_DAILY_MINUTE, 59),
    )


def kst_day(moment: datetime) -> date:
    return moment.astimezone(KST).date()


def daily_window(day: date) -> tuple[datetime, datetime]:
    """When the DAILY briefing for one KST day may be sent."""
    start = datetime.combine(day, _daily_time(), KST)
    return start.astimezone(timezone.utc), (
        start + timedelta(hours=DAILY_WINDOW_HOURS)
    ).astimezone(timezone.utc)


def subscribed(recipient: TeamsRecipient) -> bool:
    """Pairing is the opt-in, so only an explicit False unsubscribes."""
    return bool(recipient.active) and recipient.briefing_enabled is not False


def _active_recipients(session: Session) -> list[TeamsRecipient]:
    return [
        recipient
        for recipient in session.scalars(
            select(TeamsRecipient).where(TeamsRecipient.active.is_(True))
        ).all()
        if subscribed(recipient)
    ]


def _department_of(session: Session, recipient: TeamsRecipient) -> str | None:
    """The department the briefing is scoped to, or None for an org-wide view."""
    if not recipient.account_id:
        return None
    from .account_models import DepartmentAccount

    account = session.get(DepartmentAccount, recipient.account_id)
    return getattr(account, "department_id", None) if account else None


def _enqueue(
    session: Session,
    recipient: TeamsRecipient,
    *,
    kind: str,
    day: date,
    now: datetime,
    readiness_checked: bool,
) -> TeamsBriefingDelivery | None:
    existing = session.scalar(
        select(TeamsBriefingDelivery).where(
            TeamsBriefingDelivery.recipient_id == recipient.id,
            TeamsBriefingDelivery.kst_date == day,
            TeamsBriefingDelivery.kind == kind,
        )
    )
    if existing is not None:
        return None
    row = TeamsBriefingDelivery(
        recipient_id=recipient.id,
        account_id=recipient.account_id,
        department_id=_department_of(session, recipient),
        kind=kind,
        kst_date=day,
        status="PENDING",
        readiness_checked=readiness_checked,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row


def enqueue_subscription_ping(
    session: Session, recipient: TeamsRecipient, *, now: datetime | None = None
) -> TeamsBriefingDelivery | None:
    """Queue the one ping that confirms a pairing by showing what it delivers.

    Called from the pairing path inside its own transaction; the caller commits.
    A same-day reconnect finds the existing row and adds nothing.
    """

    now = now or now_utc()
    if not briefings_enabled() or not subscribed(recipient):
        return None
    return _enqueue(
        session,
        recipient,
        kind="SUBSCRIBED",
        day=kst_day(now),
        now=now,
        readiness_checked=False,
    )


def schedule_daily(
    session: Session, *, now: datetime | None = None, ready: bool = False
) -> int:
    """Queue today's DAILY briefing for every active recipient, once."""
    now = now or now_utc()
    day = kst_day(now)
    start, end = daily_window(day)
    if not ready or not (start <= now < end):
        return 0
    queued = 0
    for recipient in _active_recipients(session):
        if _enqueue(
            session,
            recipient,
            kind="DAILY",
            day=day,
            now=now,
            readiness_checked=True,
        ):
            queued += 1
    return queued


def _claim(session: Session, now: datetime) -> TeamsBriefingDelivery | None:
    token = str(uuid.uuid4())
    row = session.scalar(
        select(TeamsBriefingDelivery)
        .where(
            TeamsBriefingDelivery.status.in_(("PENDING", "RETRY")),
            TeamsBriefingDelivery.attempts < MAX_ATTEMPTS,
        )
        .order_by(TeamsBriefingDelivery.created_at)
        .limit(1)
    )
    if row is None:
        return None
    claimed = session.execute(
        update(TeamsBriefingDelivery)
        .where(
            TeamsBriefingDelivery.id == row.id,
            TeamsBriefingDelivery.status.in_(("PENDING", "RETRY")),
        )
        .values(
            status="CLAIMED",
            lease_token=token,
            lease_until=now + timedelta(seconds=LEASE_SECONDS),
            updated_at=now,
        )
    )
    if claimed.rowcount != 1:
        return None
    session.commit()
    return session.get(TeamsBriefingDelivery, row.id)


def _deliver(
    session: Session,
    row: TeamsBriefingDelivery,
    *,
    session_factory: Callable[[], Session],
    base_url: str,
    sender: Callable[..., str],
    card_builder: Callable[..., dict],
    now: datetime,
) -> str:
    recipient = session.get(TeamsRecipient, row.recipient_id)
    # Re-checked at send time: a person may unsubscribe after this was queued.
    if recipient is None or not subscribed(recipient):
        row.status = "CANCELLED"
        row.error_code = (
            "RECIPIENT_INACTIVE" if recipient is None or not recipient.active
            else "BRIEFING_DISABLED"
        )
        row.updated_at = now
        session.commit()
        return "CANCELLED"
    if row.kind == "DAILY":
        _, end = daily_window(row.kst_date)
        if now >= end:
            row.status = "SKIPPED"
            row.error_code = "DAILY_WINDOW_CLOSED"
            row.updated_at = now
            session.commit()
            return "SKIPPED"
    # Build the card in a session of its own. Reading the stored briefing
    # expunges everything its session holds, which would silently detach this
    # outbox row and throw away every status write that follows.
    kind, department_id = row.kind, row.department_id
    with session_factory() as card_session:
        card = card_builder(
            card_session,
            kind=kind,
            department_id=department_id,
            base_url=base_url,
            now=now,
        )
    row.status = "SENDING"
    row.attempts += 1
    row.updated_at = now
    session.commit()
    try:
        sender(recipient, card)
    except TeamsDeliveryError as error:
        # UNKNOWN means the activity may already have arrived; never resend it.
        row.status = (
            "RETRY"
            if error.outcome == "RETRY" and row.attempts < MAX_ATTEMPTS
            else error.outcome
        )
        row.error_code = str(error.status_code or error.outcome)[:64]
        row.updated_at = now
        session.commit()
        return row.status
    row.status = "SENT"
    row.error_code = None
    row.sent_at = now
    row.updated_at = row.sent_at
    session.commit()
    return "SENT"


def dispatch_briefings(
    session_factory: Callable[[], Session],
    *,
    now: datetime | None = None,
    ready: bool | None = None,
    base_url: str | None = None,
    sender: Callable[..., str] | None = None,
    card_builder: Callable[..., dict] | None = None,
    limit: int = 20,
) -> dict:
    """One bounded tick: schedule today's run, then send what is due.

    ``now`` is injectable so a caller, and every test, decides which KST day and
    delivery window this tick belongs to instead of inheriting the wall clock.
    """

    if not briefings_enabled():
        return {"enabled": False, "queued": 0, "processed": 0, "outcomes": {}}
    now = now or now_utc()
    outcomes: dict[str, int] = {}
    with session_factory() as session:
        queued = schedule_daily(
            session, now=now, ready=_readiness(session) if ready is None else ready
        )
        session.commit()
    origin = (
        base_url
        if base_url is not None
        else os.getenv("PAI_TEAMS_PUBLIC_BASE_URL", "")
    )
    for _ in range(max(1, limit)):
        with session_factory() as session:
            row = _claim(session, now)
            if row is None:
                break
            result = _deliver(
                session,
                row,
                session_factory=session_factory,
                base_url=origin,
                sender=sender or (lambda r, c: send_personal_card(r, c)),
                card_builder=card_builder or build_briefing_card,
                now=now,
            )
        outcomes[result] = outcomes.get(result, 0) + 1
    return {
        "enabled": True,
        "queued": queued,
        "processed": sum(outcomes.values()),
        "outcomes": outcomes,
    }


def _readiness(session: Session) -> bool:
    """Today's ingestion and analysis are confirmed complete."""
    from .teams_readiness import teams_daily_readiness

    try:
        return teams_daily_readiness(session).ready
    except Exception:  # pragma: no cover - defensive operational boundary
        return False


def enabled_or_reason() -> tuple[bool, str]:
    if not briefings_enabled():
        return False, "PAI_TEAMS_BRIEFING_ENABLED must be true"
    if not TeamsBotSettings.from_env().enabled:
        return False, "Configure the personal Teams bot and HTTPS public origin"
    return True, ""
