"""Seed reviewable department decisions and outcomes for a demo environment.

The result screens cannot be exercised while no department has recorded a
decision: every pipeline card and every selection ratio reads zero. This
command writes explicitly labelled demo rows so the screens can be reviewed,
and refuses to run against a database that already holds real operator work
unless the caller states that intent.

Every row it writes is marked ``DEMO_SEED`` in its rationale and source, so a
later cleanup can find them exactly. It never invents evaluations, never
touches notices, and never calls an external service.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, load_only, raiseload

from .database import build_engine, build_session_factory
from .department_ranking import load_department_keyword_profiles
from .models import BidOutcome, Notice, UserDecision


DEMO_MARKER = "DEMO_SEED"
DEMO_SOURCE = "DEMO_SEED"
_DEMO_RATIONALE = {
    "GO": f"{DEMO_MARKER}: 화면 검토용 참여 결정입니다. 실제 판단이 아닙니다.",
    "CONDITIONAL_GO": f"{DEMO_MARKER}: 화면 검토용 조건부 참여 결정입니다. 실제 판단이 아닙니다.",
    "NO_GO": f"{DEMO_MARKER}: 화면 검토용 불참 결정입니다. 실제 판단이 아닙니다.",
}
_LOSS_REASON = f"{DEMO_MARKER}: 가격 경쟁력 부족으로 가정한 화면 검토용 사유입니다."


def _demo_decision_count(session: Session) -> int:
    return session.scalar(
        select(func.count(UserDecision.id)).where(
            UserDecision.rationale.like(f"{DEMO_MARKER}%")
        )
    ) or 0


def _real_decision_count(session: Session) -> int:
    return session.scalar(
        select(func.count(UserDecision.id)).where(
            ~UserDecision.rationale.like(f"{DEMO_MARKER}%")
        )
    ) or 0


def _candidate_notices(session: Session, *, now: datetime) -> tuple[list[Notice], list[Notice]]:
    """Return open and ended notices that carry no decision yet."""

    decided = set(session.scalars(select(UserDecision.notice_id).distinct()).all())
    rows = list(
        session.scalars(
            select(Notice)
            .options(load_only(Notice.id, Notice.notice_key, Notice.title, Notice.deadline, Notice.status, raiseload=True))
            .order_by(Notice.deadline.desc())
        ).all()
    )
    open_rows = [
        notice for notice in rows
        if notice.id not in decided
        and notice.status.upper() == "OPEN"
        and notice.deadline.replace(tzinfo=notice.deadline.tzinfo or timezone.utc) >= now
    ]
    ended_rows = [
        notice for notice in rows
        if notice.id not in decided
        and (
            notice.status.upper() != "OPEN"
            or notice.deadline.replace(tzinfo=notice.deadline.tzinfo or timezone.utc) < now
        )
    ]
    return open_rows, ended_rows


def _department_cycle(limit: int) -> list[dict[str, Any]]:
    departments = load_department_keyword_profiles()["departments"]
    return [departments[index % len(departments)] for index in range(limit)]


def seed_demo_decisions(
    session: Session,
    *,
    open_count: int,
    ended_count: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Write demo decisions plus recorded outcomes for the ended sample."""

    now = now or datetime.now(timezone.utc)
    open_rows, ended_rows = _candidate_notices(session, now=now)
    open_rows = open_rows[:open_count]
    ended_rows = ended_rows[:ended_count]
    departments = _department_cycle(len(open_rows) + len(ended_rows))
    written = {"in_progress": 0, "result_missing": 0, "recorded": 0, "no_go": 0}

    for index, notice in enumerate(open_rows):
        department = departments[index]
        # Two of every three become GO so the in-progress card is populated
        # while the waiting card keeps real examples of an undecided notice.
        choice = "NO_GO" if index % 3 == 2 else ("CONDITIONAL_GO" if index % 3 == 1 else "GO")
        session.add(UserDecision(
            notice_id=notice.id, choice=choice, actor_label=f"{DEMO_MARKER} 담당자",
            rationale=_DEMO_RATIONALE[choice], department_id=department["id"],
            department_name=department["name"], department_revision=1,
            created_at=now - timedelta(days=index % 5),
        ))
        written["no_go" if choice == "NO_GO" else "in_progress"] += 1

    for offset, notice in enumerate(ended_rows):
        index = len(open_rows) + offset
        department = departments[index]
        session.add(UserDecision(
            notice_id=notice.id, choice="GO", actor_label=f"{DEMO_MARKER} 담당자",
            rationale=_DEMO_RATIONALE["GO"], department_id=department["id"],
            department_name=department["name"], department_revision=1,
            created_at=now - timedelta(days=20 + offset),
        ))
        # Half of the ended sample stays without an outcome so the result-entry
        # card has work; the other half shows a recorded win and loss.
        if offset % 2 == 0:
            written["result_missing"] += 1
            continue
        won = offset % 4 == 1
        session.add(BidOutcome(
            notice_id=notice.id,
            outcome_key=f"{DEMO_MARKER}-{notice.notice_key}"[:180],
            status="WON" if won else "LOST",
            department_id=department["id"], department_name=department["name"],
            department_revision=1,
            submitted_bid_amount=84_000_000.0, winning_bid_amount=84_000_000.0 if won else 79_500_000.0,
            technical_score=88.0, price_score=9.2, total_score=97.2 if won else 91.4,
            rank=1 if won else 3,
            winner_name=None if won else f"{DEMO_MARKER} 타사",
            loss_reason=None if won else _LOSS_REASON,
            source=DEMO_SOURCE, source_reference=f"{DEMO_MARKER}: 화면 검토용 기록입니다.",
            evidence_json={"marker": DEMO_MARKER},
            occurred_at=now - timedelta(days=10 + offset),
        ))
        written["recorded"] += 1
    return written


def purge_demo_decisions(session: Session) -> dict[str, int]:
    outcomes = session.scalars(
        select(BidOutcome).where(BidOutcome.source == DEMO_SOURCE)
    ).all()
    decisions = session.scalars(
        select(UserDecision).where(UserDecision.rationale.like(f"{DEMO_MARKER}%"))
    ).all()
    for row in outcomes:
        session.delete(row)
    for row in decisions:
        session.delete(row)
    return {"outcomes": len(outcomes), "decisions": len(decisions)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="결과 화면 검토용 부서 판단·결과 더미데이터")
    parser.add_argument("--open-count", type=int, default=12)
    parser.add_argument("--ended-count", type=int, default=8)
    parser.add_argument("--allow-existing-real-decisions", action="store_true",
                        help="실제 담당자 판단이 있는 DB에도 더미데이터를 추가합니다.")
    parser.add_argument("--purge", action="store_true", help="이전에 넣은 더미데이터만 삭제합니다.")
    args = parser.parse_args(argv)

    database_url = os.environ.get("PAI_LOOP_DATABASE_URL", "").strip()
    if not database_url:
        print("PAI_LOOP_DATABASE_URL is required", file=sys.stderr)
        return 2
    engine = build_engine(database_url)
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        if args.purge:
            removed = purge_demo_decisions(session)
            session.commit()
            print(f"삭제: 판단 {removed['decisions']}건 · 결과 {removed['outcomes']}건")
            return 0
        real = _real_decision_count(session)
        if real and not args.allow_existing_real_decisions:
            print(
                f"실제 담당자 판단 {real}건이 있는 데이터베이스입니다. "
                "의도한 경우에만 --allow-existing-real-decisions 로 다시 실행하세요.",
                file=sys.stderr,
            )
            return 2
        written = seed_demo_decisions(
            session, open_count=args.open_count, ended_count=args.ended_count
        )
        session.commit()
        print(
            f"추가: 진행 건 {written['in_progress']}건 · 불참 {written['no_go']}건 · "
            f"결과 미입력 {written['result_missing']}건 · 결과 기록 {written['recorded']}건 "
            f"(기존 더미 {_demo_decision_count(session)}건 포함)"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
