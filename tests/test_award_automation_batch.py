"""Active PPS-only batching, shared quota/deadline, and lifecycle regressions."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from pai_loop import api, award_automation as module
from pai_loop.award_automation_models import AwardRefreshAttempt, AwardRefreshState
from pai_loop.models import AwardHistoryItem, IngestionJob, Notice, PpsNoticeAuthority, new_id
from test_award_automation import BASE, NOW, fake_collector, post, setup


def test_ten_notice_batch_aggregates_actual_counts(client, setup, monkeypatch):
    _clock, add = setup
    for index in range(12):
        add(str(index))
    captured = fake_collector(client, monkeypatch, calls=36, records=2)
    post(client, "plan")
    result = post(client, "run", {"max_notices": 10})
    assert result["attempted"] == result["complete"] == 10
    assert result["api_calls"] == result["api_calls_24h"] == 360
    assert result["records"] == 20 and result["pending"] == 2
    assert result["notice_key"] == captured[-1][0] and result["ai_calls"] == 0
    with client.app.state.session_factory() as session:
        assert len(list(session.scalars(select(AwardRefreshAttempt)))) == 10


@pytest.mark.parametrize("remaining,per_notice,expected_attempts", [(70, 150, 1), (49, 150, 0), (49, 40, 1)])
def test_remaining_budget_is_reserved_without_wasting_full_cap(client, setup, monkeypatch, remaining, per_notice, expected_attempts):
    _clock, add = setup
    add()
    captured = fake_collector(client, monkeypatch, calls=min(36, per_notice))
    with client.app.state.session_factory() as session:
        session.add(IngestionJob(source="PPS_OUTCOME", mode="LIVE", status="COMPLETED", window_json={},
            request_json={}, api_calls=1000-remaining, created_at=NOW))
        session.commit()
    post(client, "plan")
    result = post(client, "run", {"max_notices": 10, "per_notice_api_budget": per_notice})
    assert result["attempted"] == expected_attempts
    if expected_attempts:
        assert captured[0][1]["max_api_calls"] == min(remaining, per_notice)
        with client.app.state.session_factory() as session:
            assert session.scalar(select(AwardRefreshAttempt)).reserved_calls == min(remaining, per_notice)
    else:
        assert result["status"] == "DAILY_BUDGET_REACHED" and not captured


def test_batch_stops_before_next_notice_with_less_than_sixty_seconds(client, setup, monkeypatch):
    _clock, add = setup
    add("A")
    add("B")
    monotonic = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: monotonic[0]))
    captured = fake_collector(client, monkeypatch, calls=36)
    original = module.refresh_award_history
    deadlines = []
    def collect(*args):
        deadlines.append(args[2].state.award_automation_deadline)
        result = original(*args)
        monotonic[0] += 430
        return result
    monkeypatch.setattr(module, "refresh_award_history", collect)
    post(client, "plan")
    result = post(client, "run", {"max_notices": 10})
    assert deadlines == [480] and len(captured) == result["attempted"] == 1
    assert result["pending"] == 1


@pytest.mark.parametrize("statuses,expected", [(["COMPLETED", "PARTIAL"], "PARTIAL"), (["FAILED", "PARTIAL"], "FAILED")])
def test_batch_reports_worst_outcome_and_counts_all_attempts(client, setup, monkeypatch, statuses, expected):
    _clock, add = setup
    add("A")
    add("B")
    post(client, "plan")
    def collect(key, payload, request, session):
        status = statuses.pop(0)
        job = IngestionJob(source="PPS_AWARD", mode="LIVE", status=status, window_json={},
            request_json={"automation_attempt_id": request.state.award_automation_attempt_id},
            api_calls=36, matched=1, notice_keys=[key], created_at=NOW)
        session.add(job)
        session.commit()
        if status == "FAILED":
            raise RuntimeError("SYN failure")
        return SimpleNamespace(job_id=job.id, status=status, api_calls=36, records=1)
    monkeypatch.setattr(module, "refresh_award_history", collect)
    result = post(client, "run", {"max_notices": 10})
    assert result["status"] == expected and result["attempted"] == 2
    assert result["api_calls"] == result["api_calls_24h"] == 72


def test_expired_or_closed_entries_are_skipped_without_hiding_later_open_candidate(client, setup, monkeypatch):
    _clock, add = setup
    old_id = add("A")
    expired_id = add("B")
    add("C", age=1)
    post(client, "plan")
    with client.app.state.session_factory() as session:
        session.get(Notice, old_id).status = "CLOSED"
        session.get(Notice, expired_id).deadline = NOW - timedelta(seconds=1)
        session.add(AwardHistoryItem(target_notice_id=old_id, external_identity="SYN-STORED", bid_notice_no="SYN-HISTORY",
            title="SYN stored", winner_name="SYN winner", similarity_score=40))
        session.commit()
    assert client.get(f"{BASE}/status").json()["eligible"] == 1
    captured = fake_collector(client, monkeypatch)
    result = post(client, "run")
    assert result["attempted"] == 1 and result["skipped"] == 2
    assert captured[0][0] == "SYN-QUEUE-C"
    with client.app.state.session_factory() as session:
        assert session.get(AwardRefreshState, expired_id).reason == "INACTIVE_NOTICE"
        assert session.scalar(select(AwardHistoryItem)).external_identity == "SYN-STORED"


def test_authoritative_cancellation_and_extension_reactivate_only_current_revision(client, setup, monkeypatch):
    _clock, add = setup
    old_id, current_id = add("OLD"), add("CURRENT")
    # API representative helpers use the same synthetic source adapter as the
    # automation fixture; no identifier in these tests names a real notice.
    monkeypatch.setattr(api, "_source_kind", module._source_kind)
    with client.app.state.session_factory() as session:
        old, current = session.get(Notice, old_id), session.get(Notice, current_id)
        old.bid_notice_no = current.bid_notice_no = "SYN-LOGICAL"
        old.revision_no, current.revision_no = "00", "01"
        session.add(PpsNoticeAuthority(bid_notice_no="SYN-LOGICAL", revision_no="01", disposition="CANCELLED",
            event_kind="취소공고", required_fields_complete=True, deadline=NOW+timedelta(days=20),
            provider_changed_at=NOW, authority_sha256="a"*64))
        session.commit()
    assert post(client, "plan")["skipped"] == 2
    with client.app.state.session_factory() as session:
        authority = session.get(PpsNoticeAuthority, "SYN-LOGICAL")
        authority.disposition, authority.event_kind = "VALID", "연장공고"
        authority.provider_changed_at = NOW+timedelta(minutes=1)
        session.commit()
    plan = post(client, "plan")
    assert plan["pending"] == plan["eligible"] == plan["requeued"] == 1
    captured = fake_collector(client, monkeypatch)
    assert post(client, "run")["notice_key"] == "SYN-QUEUE-CURRENT"
    assert len(captured) == 1


def test_legacy_reissued_expired_revision_cannot_resurrect_old_open_revision(client, setup):
    _clock, add = setup
    old_id, new_id_value = add("OLD"), add("NEW")
    with client.app.state.session_factory() as session:
        old, new = session.get(Notice, old_id), session.get(Notice, new_id_value)
        old.bid_notice_no = new.bid_notice_no = "SYN-REISSUED"
        old.revision_no, new.revision_no = "00", "01"
        new.deadline = NOW-timedelta(seconds=1)
        session.commit()
    assert post(client, "plan")["skipped"] == 2
    assert post(client, "run")["attempted"] == 0


def test_midnight_preserves_running_and_cross_day_completed_reservations(client, setup):
    clock, add = setup
    notice_id = add()
    post(client, "plan")
    midnight = NOW.replace(hour=15, minute=0, second=0, microsecond=0)
    clock[0] = midnight + timedelta(minutes=1)
    with client.app.state.session_factory() as session:
        session.add(IngestionJob(source="PPS_OUTCOME", mode="LIVE", status="COMPLETED", window_json={}, request_json={},
            api_calls=12, created_at=midnight-timedelta(minutes=2), completed_at=midnight+timedelta(seconds=5)))
        session.add(IngestionJob(source="PPS_AWARD", mode="LIVE", status="COMPLETED", window_json={}, request_json={},
            api_calls=900, created_at=midnight-timedelta(minutes=10), completed_at=midnight-timedelta(minutes=1)))
        session.add(AwardRefreshAttempt(id=new_id(), notice_id=notice_id, status="RUNNING", reserved_calls=150,
            started_at=midnight-timedelta(minutes=3)))
        session.commit()
    result = client.get(f"{BASE}/status").json()
    assert result["api_calls_24h"] == 12 and result["budget_reserved_24h"] == 150
