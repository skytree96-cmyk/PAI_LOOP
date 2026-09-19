from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pai_loop.demo_decisions_cli import (
    DEMO_MARKER,
    purge_demo_decisions,
    seed_demo_decisions,
)
from pai_loop.models import BidOutcome, Notice, UserDecision


NOW = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)


def _notice(session, label, *, ended=False):
    notice = Notice(
        notice_key=f"PPS-SYN_DEMO_{label}", bid_notice_no=f"SYN-DEMO-{label}",
        revision_no="00", title=f"SYN {label}", agency="SYN 기관",
        status="CLOSED" if ended else "OPEN",
        deadline=NOW - timedelta(days=5) if ended else NOW + timedelta(days=5),
    )
    session.add(notice)
    return notice


def test_seed_marks_every_row_and_leaves_work_for_each_card(client):
    with client.app.state.session_factory() as session:
        for index in range(6):
            _notice(session, f"open{index}")
        for index in range(4):
            _notice(session, f"ended{index}", ended=True)
        session.commit()

        written = seed_demo_decisions(session, open_count=6, ended_count=4, now=NOW)
        session.commit()

        assert written["in_progress"] == 4  # GO and CONDITIONAL_GO on open notices.
        assert written["no_go"] == 2
        assert written["result_missing"] == 2
        assert written["recorded"] == 2

        decisions = session.scalars(select_all(UserDecision)).all()
        assert decisions and all(row.rationale.startswith(DEMO_MARKER) for row in decisions)
        assert all(row.department_id for row in decisions)
        outcomes = session.scalars(select_all(BidOutcome)).all()
        assert outcomes and all(row.source == "DEMO_SEED" for row in outcomes)


def test_seed_skips_notices_that_already_carry_a_decision(client):
    with client.app.state.session_factory() as session:
        existing = _notice(session, "taken")
        session.flush()
        session.add(UserDecision(
            notice_id=existing.id, choice="NO_GO", rationale="실제 담당자 판단",
            department_id="finance-support", department_name="재무팀", department_revision=1,
        ))
        session.commit()

        written = seed_demo_decisions(session, open_count=5, ended_count=0, now=NOW)
        session.commit()
        assert written["in_progress"] == 0 and written["no_go"] == 0

        removed = purge_demo_decisions(session)
        session.commit()
        assert removed == {"outcomes": 0, "decisions": 0}
        remaining = session.scalars(select_all(UserDecision)).all()
        assert [row.rationale for row in remaining] == ["실제 담당자 판단"]


def select_all(model):
    from sqlalchemy import select

    return select(model)
