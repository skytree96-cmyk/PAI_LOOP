"""Seed and purge presentation rows shaped like PPS automatic result feedback.

The daily PPS feedback has produced no automatic rows yet, so the result
screens cannot show that flow. This command attaches clearly labelled demo
results to real ended notices that carry no decision or result, so no work
queue of a department changes. Every row keeps ``evidence_json.demo_seed``,
which the screens render as a 시연 badge and ``purge`` uses to remove exactly
those rows plus any department review that was based on one of them.

It never calls PPS or a model and never touches notices or decisions.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only, raiseload

from .database import build_engine, build_session_factory
from .integrations.company_awards import DEFAULT_COMPANY_NAME
from .models import BidOutcome, Notice, UserDecision
from .notice_freshness import authoritative_pps_cancelled_notice_keys
from .result_learning import DEMO_SEED_MARKER, is_demo_outcome


DEMO_OUTCOME_KEY_PREFIX = "demo-seed:pps-auto-feedback:"
DEMO_SOURCE_REFERENCE = "시연 예시 데이터"
_COMPETITORS = ("경쟁사 A", "경쟁사 B", "경쟁사 C", "경쟁사 D")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _digest(notice_key: str) -> str:
    return hashlib.sha256(notice_key.encode("utf-8")).hexdigest()


def _is_ended(notice: Notice, now: datetime) -> bool:
    return notice.status.upper() != "CANCELLED" and _utc(notice.deadline) < now


def candidate_notices(
    session: Session,
    *,
    count: int,
    now: datetime,
    notice_keys: list[str] | None = None,
) -> list[Notice]:
    """Ended PPS notices without any decision or result, newest deadline first."""

    busy = set(session.scalars(select(UserDecision.notice_id).distinct()).all())
    busy |= set(session.scalars(select(BidOutcome.notice_id).distinct()).all())
    statement = select(Notice).options(load_only(
        Notice.id, Notice.notice_key, Notice.bid_notice_no, Notice.revision_no, Notice.title,
        Notice.agency, Notice.deadline, Notice.status, Notice.estimated_amount, raiseload=True,
    ), raiseload("*"))
    if notice_keys:
        rows = list(session.scalars(statement.where(Notice.notice_key.in_(notice_keys))).all())
        by_key = {row.notice_key: row for row in rows}
        missing = [key for key in notice_keys if key not in by_key]
        if missing:
            raise ValueError(f"공고를 찾을 수 없습니다: {', '.join(missing)}")
        rows = [by_key[key] for key in notice_keys]
        unusable = [row.notice_key for row in rows if row.id in busy or not _is_ended(row, now)]
        if unusable:
            raise ValueError(f"판단·결과가 없는 종료 공고만 지정할 수 있습니다: {', '.join(unusable)}")
    else:
        rows = [
            row for row in session.scalars(
                statement.where(Notice.notice_key.like("PPS-%")).order_by(Notice.deadline.desc())
            ).all()
            if row.id not in busy and _is_ended(row, now)
        ]
    cancelled = authoritative_pps_cancelled_notice_keys(session, rows)
    return [row for row in rows if row.notice_key not in cancelled][:count]


def _demo_values(notice: Notice, index: int, *, now: datetime) -> dict[str, Any]:
    digest = _digest(notice.notice_key)
    won = index % 2 == 0
    rate = 0.87 + (int(digest[:8], 16) % 800) / 10_000
    amount = round(notice.estimated_amount * rate, -3) if notice.estimated_amount else None
    occurred_at = min(
        _utc(notice.deadline) + timedelta(days=3 + int(digest[8:10], 16) % 5),
        now - timedelta(hours=1),
    )
    return {
        "outcome_key": f"{DEMO_OUTCOME_KEY_PREFIX}{digest[:40]}",
        "status": "WON" if won else "LOST",
        "winning_bid_amount": amount,
        "winning_bid_rate": round(rate * 100, 3) if amount is not None else None,
        "rank": 1 if won else None,
        "winner_name": DEFAULT_COMPANY_NAME if won else _COMPETITORS[(index // 2) % len(_COMPETITORS)],
        # A demo loss keeps the provider-without-participation state, so it
        # renders as waiting for the department to confirm its participation.
        "reason_code": "PPS_EXACT_WINNER_MATCH" if won else "PPS_OTHER_WINNER_REVIEW",
        "source": "PPS_AUTO_FEEDBACK",
        "source_reference": DEMO_SOURCE_REFERENCE,
        "occurred_at": occurred_at,
        "observed_at": now - timedelta(minutes=index),
        "evidence_json": {
            "schema_version": "pai-loop-demo-outcome-feedback-1",
            "demo_seed": {"marker": DEMO_SEED_MARKER, "purpose": "presentation", "seeded_at": now.isoformat()},
            "provider": "PPS_DATA_GO_KR_1230000",
            "exact_match": {
                "verified": True,
                "bid_notice_no": notice.bid_notice_no,
                "revision_no": notice.revision_no,
            },
            "company_match_basis": DEMO_SEED_MARKER,
        },
    }


def existing_demo_outcomes(session: Session) -> list[BidOutcome]:
    rows = session.scalars(
        select(BidOutcome).where(BidOutcome.outcome_key.like(f"{DEMO_OUTCOME_KEY_PREFIX}%"))
    ).all()
    return [row for row in rows if is_demo_outcome(row)]


def seed_demo_outcomes(
    session: Session,
    *,
    count: int,
    now: datetime | None = None,
    notice_keys: list[str] | None = None,
) -> list[tuple[Notice, BidOutcome]]:
    now = now or datetime.now(timezone.utc)
    written: list[tuple[Notice, BidOutcome]] = []
    for index, notice in enumerate(candidate_notices(session, count=count, now=now, notice_keys=notice_keys)):
        row = BidOutcome(notice_id=notice.id, **_demo_values(notice, index, now=now))
        session.add(row)
        written.append((notice, row))
    return written


def purge_demo_outcomes(session: Session) -> dict[str, int]:
    demo = existing_demo_outcomes(session)
    demo_ids = {row.id for row in demo}
    reviews = []
    if demo_ids:
        for row in session.scalars(
            select(BidOutcome).where(BidOutcome.notice_id.in_({item.notice_id for item in demo}))
        ).all():
            workflow = (row.evidence_json or {}).get("_workflow") if isinstance(row.evidence_json, dict) else None
            basis = workflow.get("basis_outcome") if isinstance(workflow, dict) else None
            if row.id not in demo_ids and isinstance(basis, dict) and basis.get("id") in demo_ids:
                reviews.append(row)
    for row in [*reviews, *demo]:
        session.delete(row)
    return {"outcomes": len(demo), "reviews": len(reviews)}


def _describe(notice: Notice, status: str) -> str:
    label = "낙찰(확인 완료)" if status == "WON" else "미낙찰(참여 확인 필요)"
    return f"- {notice.notice_key} · {label} · {notice.title[:60]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="발표용 나라장터 자동 반영 시연 결과")
    parser.add_argument("action", choices=("plan", "apply", "purge"), nargs="?", default="plan")
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--notice-key", action="append", default=[], dest="notice_keys")
    args = parser.parse_args(argv)
    if not 1 <= args.count <= 30:
        parser.error("--count must be between 1 and 30")

    database_url = os.environ.get("PAI_LOOP_DATABASE_URL", "").strip()
    if not database_url:
        print("PAI_LOOP_DATABASE_URL is required", file=sys.stderr)
        return 2
    session_factory = build_session_factory(build_engine(database_url))
    with session_factory() as session:
        if args.action == "purge":
            removed = purge_demo_outcomes(session)
            session.commit()
            print(f"삭제: 시연 결과 {removed['outcomes']}건 · 이를 바탕으로 만든 부서 검토본 {removed['reviews']}건")
            return 0
        existing = existing_demo_outcomes(session)
        if existing and args.action == "apply":
            print(f"이미 시연 결과 {len(existing)}건이 있습니다. purge 후 다시 실행하세요.", file=sys.stderr)
            return 2
        now = datetime.now(timezone.utc)
        try:
            if args.action == "plan":
                chosen = candidate_notices(session, count=args.count, now=now, notice_keys=args.notice_keys or None)
                for index, notice in enumerate(chosen):
                    print(_describe(notice, _demo_values(notice, index, now=now)["status"]))
                print(f"계획: {len(chosen)}건 (기존 시연 결과 {len(existing)}건, 쓰기 없음)")
                return 0
            written = seed_demo_outcomes(session, count=args.count, now=now, notice_keys=args.notice_keys or None)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        session.commit()
        for notice, row in written:
            print(_describe(notice, row.status))
        print(f"추가: 시연 결과 {len(written)}건")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
