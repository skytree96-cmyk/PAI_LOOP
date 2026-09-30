from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from pai_loop.demo_outcome_feedback_cli import (
    DEMO_OUTCOME_KEY_PREFIX,
    candidate_notices,
    main,
    purge_demo_outcomes,
    seed_demo_outcomes,
)
from pai_loop.models import BidOutcome, Notice, PpsNoticeAuthority, UserDecision
from pai_loop.result_learning import _workflow, is_demo_outcome


NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)


def _notice(session, label, *, days, status="CLOSED", amount=100_000_000.0):
    notice = Notice(
        notice_key=f"PPS-SYN-DEMO-{label}", bid_notice_no=f"SYN-DEMO-{label}", revision_no="000",
        title=f"SYN 시연 {label}", agency="SYN 기관", status=status,
        deadline=NOW - timedelta(days=days), estimated_amount=amount,
    )
    session.add(notice)
    session.flush()
    return notice


def _prepare(session):
    fresh = [_notice(session, f"FRESH{index}", days=index + 1) for index in range(3)]
    decided = _notice(session, "DECIDED", days=1)
    session.add(UserDecision(notice_id=decided.id, choice="GO", rationale="실제 담당자 판단",
                             department_id="SYN-DEPT", department_name="SYN", department_revision=1))
    recorded = _notice(session, "RECORDED", days=1)
    session.add(BidOutcome(notice_id=recorded.id, outcome_key="real-record", status="LOST",
                           source="MANUAL_UI", department_id="SYN-DEPT", department_revision=1,
                           evidence_json={}))
    _notice(session, "OPEN", days=-3, status="OPEN")
    _notice(session, "CANCELLED", days=2, status="CANCELLED")
    authority_cancelled = _notice(session, "AUTHORITY", days=2)
    session.add(PpsNoticeAuthority(bid_notice_no=authority_cancelled.bid_notice_no, revision_no="000",
                                   disposition="CANCELLED", authority_sha256="0" * 64))
    session.commit()
    return fresh


def test_seed_uses_only_untouched_ended_notices_and_marks_every_row(client):
    with client.app.state.session_factory() as session:
        fresh = _prepare(session)
        written = seed_demo_outcomes(session, count=10, now=NOW)
        session.commit()

        assert [notice.notice_key for notice, _ in written] == [row.notice_key for row in fresh]
        rows = [row for _, row in written]
        assert [row.status for row in rows] == ["WON", "LOST", "WON"]
        assert all(is_demo_outcome(row) and row.outcome_key.startswith(DEMO_OUTCOME_KEY_PREFIX) for row in rows)
        assert all(row.source == "PPS_AUTO_FEEDBACK" and row.department_id is None for row in rows)
        # A demo win reads as a verified provider result; a demo loss keeps the
        # provider-without-participation state that asks for confirmation.
        assert [_workflow(row)["record_status"] for row in rows] == ["VALIDATED", "DRAFT", "VALIDATED"]
        assert rows[0].winner_name == "사단법인 한국능률협회" and rows[0].rank == 1
        assert rows[1].winner_name.startswith("경쟁사") and rows[1].rank is None
        assert 87_000_000 <= rows[0].winning_bid_amount <= 95_000_000
        assert all(row.occurred_at <= NOW for row in rows)
        assert candidate_notices(session, count=10, now=NOW) == []


def test_explicit_notice_keys_must_be_untouched_and_ended(client):
    with client.app.state.session_factory() as session:
        _prepare(session)
        with pytest.raises(ValueError, match="판단·결과가 없는 종료 공고"):
            seed_demo_outcomes(session, count=5, now=NOW, notice_keys=["PPS-SYN-DEMO-DECIDED"])
        with pytest.raises(ValueError, match="찾을 수 없습니다"):
            seed_demo_outcomes(session, count=5, now=NOW, notice_keys=["PPS-SYN-DEMO-MISSING"])
        written = seed_demo_outcomes(session, count=5, now=NOW, notice_keys=["PPS-SYN-DEMO-FRESH2"])
        assert [notice.notice_key for notice, _ in written] == ["PPS-SYN-DEMO-FRESH2"]


def test_purge_removes_demo_rows_and_reviews_based_on_them_only(client):
    with client.app.state.session_factory() as session:
        _prepare(session)
        written = seed_demo_outcomes(session, count=2, now=NOW)
        session.flush()
        basis = written[0][1]
        session.add(BidOutcome(
            notice_id=basis.notice_id, outcome_key="department-review", status="WON", source="MANUAL_UI",
            department_id="SYN-DEPT", department_revision=1,
            evidence_json={"_workflow": {"record_status": "VALIDATED", "basis_outcome": {"id": basis.id}}},
        ))
        session.commit()

        assert purge_demo_outcomes(session) == {"outcomes": 2, "reviews": 1}
        session.commit()
        remaining = session.scalars(select(BidOutcome)).all()
        assert [row.outcome_key for row in remaining] == ["real-record"]


def test_cli_plans_without_writing_and_refuses_a_second_apply(client, monkeypatch, capsys):
    url = client.app.state.engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("PAI_LOOP_DATABASE_URL", url)
    with client.app.state.session_factory() as session:
        _prepare(session)

    assert main(["plan", "--count", "2"]) == 0
    assert "쓰기 없음" in capsys.readouterr().out
    with client.app.state.session_factory() as session:
        assert session.scalars(select(BidOutcome).where(BidOutcome.source == "PPS_AUTO_FEEDBACK")).all() == []

    assert main(["apply", "--count", "2"]) == 0
    assert "추가: 시연 결과 2건" in capsys.readouterr().out
    assert main(["apply", "--count", "2"]) == 2
    assert "purge 후 다시" in capsys.readouterr().err
    assert main(["purge"]) == 0
    assert "시연 결과 2건" in capsys.readouterr().out
    monkeypatch.delenv("PAI_LOOP_DATABASE_URL")
    assert main(["plan"]) == 2
