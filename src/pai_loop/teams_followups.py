"""Personal Teams follows, KST reminders, and a crash-safe delivery outbox.

No LLM, network request, or browser timer is needed to register an interest.
Workers render current stored analysis only after claiming a due delivery.
The durable SENDING boundary deliberately turns uncertain sends into UNKNOWN:
Bot Framework does not provide an idempotency key for outgoing activities.
"""
from __future__ import annotations

import argparse
import os
import time
import uuid
from datetime import datetime, time as day_time, timedelta, timezone
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .account_models import DepartmentAccount
from .accounts import authenticated_account, serial_transaction
from .followup_models import TeamsFollow, TeamsFollowDelivery
from .models import Notice, PpsNoticeAuthority

KST = timezone(timedelta(hours=9), "Asia/Seoul")
EVENT_KINDS = ("REGISTERED", "D_MINUS_5", "DEADLINE_DAY")
MUTABLE_STATES = ("PENDING", "RETRY", "CLAIMED", "SKIPPED", "CANCELLED")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 180
router = APIRouter(prefix="/api/v1/teams/follows", tags=["personal Teams follows"])


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def followups_enabled() -> bool:
    return os.getenv("PAI_TEAMS_FOLLOWUPS_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def reminder_schedule(deadline: datetime) -> dict[str, tuple[datetime, datetime]]:
    """Give each phase a local-day delivery window, always before the deadline.

    Early deadlines use 30 minutes before closing, clamped to local midnight.
    A midnight deadline necessarily has its last reminder the preceding night.
    Missed D-5 windows are never backfilled into another local calendar day.
    """
    deadline = utc(deadline)
    local = deadline.astimezone(KST)
    midnight = datetime.combine(local.date(), day_time(), KST)
    morning = midnight + timedelta(hours=9)
    if deadline <= morning:
        morning = max(midnight, local - timedelta(minutes=30))
        if morning >= deadline:
            morning = local - timedelta(minutes=30)
    minus_five = midnight - timedelta(days=5) + timedelta(hours=9)
    return {
        "D_MINUS_5": (utc(minus_five), min(deadline, utc(minus_five.replace(hour=0) + timedelta(days=1)))),
        "DEADLINE_DAY": (utc(morning), deadline),
    }


def notice_block_reason(session: Session, notice: Notice, now: datetime) -> str | None:
    if notice.status.upper() != "OPEN":
        return "NOTICE_INACTIVE"
    if utc(notice.deadline) <= utc(now):
        return "NOTICE_CLOSED"
    if notice.notice_key.upper().startswith("PPS-"):
        authority = session.get(PpsNoticeAuthority, notice.bid_notice_no)
        if authority:
            if authority.disposition.upper() != "VALID":
                return "NOTICE_INACTIVE"
            if str(authority.revision_no) != str(notice.revision_no):
                return "NOTICE_SUPERSEDED"
            if authority.deadline is not None and utc(authority.deadline) != utc(notice.deadline):
                return "NOTICE_SUPERSEDED"
    return None


def _notice(session: Session, key: str) -> Notice:
    notice = session.scalar(select(Notice).where(Notice.notice_key == key))
    if notice is None:
        notice = session.get(Notice, key)
    if notice is None:
        raise HTTPException(404, "공고를 찾을 수 없습니다.")
    return notice


def _cancel_pending(session: Session, follow: TeamsFollow, reason: str, now: datetime) -> None:
    session.execute(update(TeamsFollowDelivery).where(
        TeamsFollowDelivery.follow_id == follow.id,
        TeamsFollowDelivery.generation == follow.generation,
        TeamsFollowDelivery.status.in_(MUTABLE_STATES),
    ).values(status="CANCELLED", error_code=reason, lease_token=None, lease_until=None, updated_at=now))


def reconcile_follow(session: Session, follow: TeamsFollow, notice: Notice, now: datetime) -> None:
    """Replan unsent phases; never rewrite an accepted/ambiguous delivery."""
    from .teams_identity_models import TeamsRecipient

    now = utc(now)
    recipient = session.get(TeamsRecipient, follow.recipient_id)
    account = session.get(DepartmentAccount, follow.account_id)
    reason = ("UNFOLLOWED" if not follow.active else
              "RECIPIENT_INACTIVE" if not recipient or not recipient.active else
              "ACCOUNT_INACTIVE" if not account or not account.active else
              notice_block_reason(session, notice, now))
    if reason:
        _cancel_pending(session, follow, reason, now)
        return
    deadline = utc(notice.deadline)
    changed = utc(follow.deadline_snapshot) != deadline
    follow.deadline_snapshot = deadline
    if changed:
        follow.updated_at = now
    planned = {"REGISTERED": (utc(follow.subscribed_at), deadline), **reminder_schedule(deadline)}
    rows = {row.event_kind: row for row in session.scalars(select(TeamsFollowDelivery).where(
        TeamsFollowDelivery.follow_id == follow.id,
        TeamsFollowDelivery.generation == follow.generation,
    )).all()}
    for kind, (due, expires) in planned.items():
        row = rows.get(kind)
        if row is not None and row.status not in MUTABLE_STATES:
            continue
        past_registration = kind != "REGISTERED" and due < utc(follow.subscribed_at)
        missed_window = expires <= now
        reason = "REGISTERED_AFTER_SCHEDULE" if past_registration else "MISSED_WINDOW" if missed_window else None
        state = "SKIPPED" if reason else "PENDING"
        if row is None:
            session.add(TeamsFollowDelivery(
                follow_id=follow.id, generation=follow.generation, event_kind=kind,
                status=state, scheduled_at=due, expires_at=expires, deadline_snapshot=deadline,
                attempts=0, error_code=reason, created_at=now, updated_at=now,
            ))
        elif changed or row.status in {"CANCELLED", "SKIPPED"} or missed_window:
            row.status, row.error_code = state, reason
            row.scheduled_at, row.expires_at, row.deadline_snapshot = due, expires, deadline
            row.lease_token, row.lease_until, row.updated_at = None, None, now


def subscribe(session: Session, recipient_id: str, account_id: str, notice: Notice, now: datetime) -> TeamsFollow:
    """Call under serial_transaction so concurrent clicks are idempotent."""
    now = utc(now)
    if notice_block_reason(session, notice, now):
        raise HTTPException(409, "마감·취소·대체된 공고에는 알림을 등록할 수 없습니다.")
    follow = session.scalar(select(TeamsFollow).where(
        TeamsFollow.recipient_id == recipient_id, TeamsFollow.notice_id == notice.id,
    ))
    if follow is None:
        follow = TeamsFollow(recipient_id=recipient_id, account_id=account_id, notice_id=notice.id,
                             active=True, generation=1, subscribed_at=now, deadline_snapshot=utc(notice.deadline),
                             created_at=now, updated_at=now)
        session.add(follow)
        session.flush()
    elif not follow.active:
        follow.active, follow.generation = True, follow.generation + 1
        follow.subscribed_at, follow.updated_at, follow.account_id = now, now, account_id
    reconcile_follow(session, follow, notice, now)
    session.flush()
    return follow


def _payload(session: Session, follow: TeamsFollow, notice: Notice) -> dict:
    deliveries = list(session.scalars(select(TeamsFollowDelivery).where(
        TeamsFollowDelivery.follow_id == follow.id, TeamsFollowDelivery.generation == follow.generation,
    )).all())
    deliveries.sort(key=lambda row: EVENT_KINDS.index(row.event_kind))
    pending = [row.scheduled_at for row in deliveries if row.status in {"PENDING", "RETRY", "CLAIMED"}]
    return {
        "notice_id": notice.id, "notice_key": notice.notice_key, "notice_title": notice.title,
        "deadline": utc(notice.deadline), "active": follow.active, "subscribed_at": follow.subscribed_at,
        "next_delivery_at": min(pending) if follow.active and pending else None,
        "deliveries": [{"event_kind": row.event_kind, "status": row.status,
                        "scheduled_at": row.scheduled_at, "sent_at": row.sent_at,
                        "error_code": row.error_code} for row in deliveries],
    }


@router.get("")
def list_follows(request: Request) -> dict:
    from .teams_bot import recipient_for_request
    authenticated_account(request)
    with request.app.state.session_factory() as session:
        recipient = recipient_for_request(request, session)
        rows = session.execute(select(TeamsFollow, Notice).join(Notice, TeamsFollow.notice_id == Notice.id).where(
            TeamsFollow.recipient_id == recipient.id, TeamsFollow.active.is_(True),
        ).order_by(Notice.deadline)).all()
        return {"items": [_payload(session, follow, notice) for follow, notice in rows],
                "timezone": "Asia/Seoul", "enabled": followups_enabled()}


@router.post("/{notice_id}")
def register_follow(notice_id: str, request: Request) -> dict:
    from .teams_bot import recipient_for_request
    identity = authenticated_account(request, mutation=True)
    with request.app.state.session_factory() as session:
        serial_transaction(session, scope="teams-followups")
        recipient = recipient_for_request(request, session)
        notice = _notice(session, notice_id)
        follow = subscribe(session, recipient.id, identity.id, notice, now_utc())
        result = _payload(session, follow, notice)
        session.commit()
        return result


@router.delete("/{notice_id}")
def unregister_follow(notice_id: str, request: Request) -> dict:
    from .teams_bot import recipient_for_request
    authenticated_account(request, mutation=True)
    with request.app.state.session_factory() as session:
        serial_transaction(session, scope="teams-followups")
        recipient = recipient_for_request(request, session)
        notice = _notice(session, notice_id)
        follow = session.scalar(select(TeamsFollow).where(
            TeamsFollow.recipient_id == recipient.id, TeamsFollow.notice_id == notice.id,
        ))
        if follow:
            now = now_utc()
            follow.active, follow.updated_at = False, now
            _cancel_pending(session, follow, "UNFOLLOWED", now)
        session.commit()
        return {"active": False, "notice_id": notice.id, "notice_key": notice.notice_key}


@router.get("/{notice_id}/preview")
def preview_follow(notice_id: str, request: Request) -> dict:
    from .teams_bot import recipient_for_request
    from .teams_cards import build_notice_card
    authenticated_account(request)
    with request.app.state.session_factory() as session:
        recipient_for_request(request, session)
        notice = _notice(session, notice_id)
        return {"card": build_notice_card(session, notice, "REGISTERED", now=now_utc(),
                                         base_url=os.getenv("PAI_TEAMS_PUBLIC_BASE_URL", ""))}


def synchronize_schedules(session_factory, now: datetime) -> None:
    """Recover only pre-send leases; an interrupted SENDING is never resent."""
    with session_factory() as session:
        serial_transaction(session, scope="teams-followups")
        session.execute(update(TeamsFollowDelivery).where(
            TeamsFollowDelivery.status == "CLAIMED", TeamsFollowDelivery.lease_until <= now,
        ).values(status="RETRY", error_code="CLAIM_EXPIRED", lease_token=None, lease_until=None, updated_at=now))
        session.execute(update(TeamsFollowDelivery).where(
            TeamsFollowDelivery.status == "SENDING", TeamsFollowDelivery.lease_until <= now,
        ).values(status="UNKNOWN", error_code="SEND_OUTCOME_UNKNOWN", lease_token=None, lease_until=None, updated_at=now))
        for follow, notice in session.execute(select(TeamsFollow, Notice).join(
            Notice, TeamsFollow.notice_id == Notice.id,
        ).where(TeamsFollow.active.is_(True))).all():
            reconcile_follow(session, follow, notice, now)
        session.commit()


def claim_due(session_factory, now: datetime) -> tuple[str, str] | None:
    """Commit a single compare-and-swap lease before rendering or networking."""
    with session_factory() as session:
        serial_transaction(session, scope="teams-followups")
        candidate = session.scalar(select(TeamsFollowDelivery.id).where(
            TeamsFollowDelivery.status.in_(("PENDING", "RETRY")),
            TeamsFollowDelivery.scheduled_at <= now, TeamsFollowDelivery.expires_at > now,
            TeamsFollowDelivery.attempts < MAX_ATTEMPTS,
        ).order_by(TeamsFollowDelivery.scheduled_at, TeamsFollowDelivery.id).limit(1))
        if not candidate:
            session.commit()
            return None
        token = str(uuid.uuid4())
        result = session.execute(update(TeamsFollowDelivery).where(
            TeamsFollowDelivery.id == candidate, TeamsFollowDelivery.status.in_(("PENDING", "RETRY")),
            TeamsFollowDelivery.scheduled_at <= now, TeamsFollowDelivery.expires_at > now,
            TeamsFollowDelivery.attempts < MAX_ATTEMPTS,
        ).values(status="CLAIMED", lease_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS), updated_at=now))
        session.commit()
        return (candidate, token) if result.rowcount == 1 else None


def _finish(session_factory, delivery_id: str, token: str, outcome: str, now: datetime,
            *, error_code: str | None = None, retry_after_seconds: int | None = None) -> str:
    with session_factory() as session:
        serial_transaction(session, scope="teams-followups")
        row = session.get(TeamsFollowDelivery, delivery_id)
        if row is None or row.status not in {"CLAIMED", "SENDING"} or row.lease_token != token:
            session.commit()
            return "LOST_LEASE"
        if outcome == "RETRY":
            delay = min(3600, max(30, retry_after_seconds or 30 * 2 ** max(0, row.attempts - 1)))
            retry_at = now + timedelta(seconds=delay)
            if row.attempts >= MAX_ATTEMPTS or retry_at >= row.expires_at:
                outcome, error_code = "FAILED", "RETRY_EXHAUSTED"
            else:
                row.scheduled_at = retry_at
        row.status, row.error_code, row.updated_at = outcome, error_code, now
        row.lease_token, row.lease_until = None, None
        if outcome == "SENT":
            row.sent_at = now
        session.commit()
        return outcome


def _deliver_claim(session_factory, claim: tuple[str, str], now: datetime,
                   sender: Callable, card_builder: Callable, base_url: str) -> str:
    from .teams_bot import TeamsDeliveryError
    from .teams_identity_models import TeamsRecipient

    delivery_id, token = claim
    # Build from current state while still before the durable send boundary.
    try:
        with session_factory() as session:
            serial_transaction(session, scope="teams-followups")
            row = session.get(TeamsFollowDelivery, delivery_id)
            if not row or row.status != "CLAIMED" or row.lease_token != token or row.lease_until <= now:
                session.commit()
                return "LOST_LEASE"
            follow = session.get(TeamsFollow, row.follow_id)
            notice = session.get(Notice, follow.notice_id) if follow else None
            if not follow or not notice or row.generation != follow.generation:
                row.status, row.error_code = "CANCELLED", "SUBSCRIPTION_CHANGED"
                session.commit()
                return "CANCELLED"
            reconcile_follow(session, follow, notice, now)
            session.flush()
            session.refresh(row)
            if row.status != "CLAIMED" or row.lease_token != token:
                session.commit()
                return row.status
            recipient = session.get(TeamsRecipient, follow.recipient_id)
            card = card_builder(session, notice, row.event_kind, now=now, base_url=base_url)
            row.status, row.attempts = "SENDING", row.attempts + 1
            row.lease_until, row.updated_at = now + timedelta(seconds=LEASE_SECONDS), now
            session.commit()
            session.expunge(recipient)
    except Exception:
        # Rendering happened before any provider call. Bound its retry count too.
        with session_factory() as session:
            session.execute(update(TeamsFollowDelivery).where(
                TeamsFollowDelivery.id == delivery_id, TeamsFollowDelivery.lease_token == token,
                TeamsFollowDelivery.status == "CLAIMED",
            ).values(attempts=TeamsFollowDelivery.attempts + 1))
            session.commit()
        return _finish(session_factory, delivery_id, token, "RETRY", now, error_code="CARD_BUILD_FAILED")
    try:
        sender(recipient, card)
    except TeamsDeliveryError as exc:
        outcome = getattr(exc, "outcome", "UNKNOWN")
        if outcome not in {"RETRY", "FAILED", "UNKNOWN"}:
            outcome = "UNKNOWN"
        return _finish(session_factory, delivery_id, token, outcome, now,
                       error_code={"RETRY": "PROVIDER_RETRY", "FAILED": "PROVIDER_REJECTED", "UNKNOWN": "SEND_OUTCOME_UNKNOWN"}[outcome],
                       retry_after_seconds=getattr(exc, "retry_after_seconds", None))
    except Exception:
        return _finish(session_factory, delivery_id, token, "UNKNOWN", now, error_code="SEND_OUTCOME_UNKNOWN")
    return _finish(session_factory, delivery_id, token, "SENT", now)


def dispatch_due(session_factory, *, now: datetime | None = None, enabled: bool | None = None,
                 base_url: str | None = None, limit: int = 50, sender: Callable | None = None,
                 card_builder: Callable | None = None) -> dict:
    """One bounded synchronous tick; callers may run this in asyncio.to_thread."""
    if not (followups_enabled() if enabled is None else enabled):
        return {"enabled": False, "processed": 0, "outcomes": {}}
    from .teams_bot import send_personal_card
    from .teams_cards import build_notice_card
    fixed_now = utc(now) if now is not None else None
    tick_now = fixed_now or now_utc()
    synchronize_schedules(session_factory, tick_now)
    outcomes: dict[str, int] = {}
    for _ in range(max(0, min(limit, 200))):
        current = fixed_now or now_utc()
        claim = claim_due(session_factory, current)
        if claim is None:
            break
        result = _deliver_claim(session_factory, claim, current, sender or send_personal_card,
                                card_builder or build_notice_card,
                                base_url if base_url is not None else os.getenv("PAI_TEAMS_PUBLIC_BASE_URL", ""))
        outcomes[result] = outcomes.get(result, 0) + 1
    return {"enabled": True, "processed": sum(outcomes.values()), "outcomes": outcomes}


def main() -> None:
    parser = argparse.ArgumentParser(description="Dispatch durable personal Teams interest reminders")
    parser.add_argument("--once", action="store_true", help="Run one bounded tick and exit")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds (minimum 5)")
    args = parser.parse_args()
    if not followups_enabled():
        parser.error("PAI_TEAMS_FOLLOWUPS_ENABLED must be true; delivery is disabled by default")
    from .config import Settings
    from .database import build_engine, build_session_factory
    from .teams_bot import TeamsBotSettings
    settings = Settings.from_env()
    settings.validate_security()
    if not TeamsBotSettings.from_env().enabled:
        parser.error("Configure the personal Teams bot and HTTPS public origin before enabling delivery")
    engine = build_engine(settings.database_url)
    factory = build_session_factory(engine)
    try:
        while True:
            dispatch_due(factory)
            if args.once:
                return
            time.sleep(max(5, args.interval))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
