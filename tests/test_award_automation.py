from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select

from pai_loop import award_automation as module
from pai_loop.award_automation_models import AwardRefreshAttempt, AwardRefreshState
from pai_loop.models import Evaluation, IngestionJob, Notice, NoticeVersion, UserDecision, new_id

BASE = "/api/v1/operations/award-refresh"
NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def add_scope_version(session, notice, *, version_no=1,
                      demand_agency_name="SYN 수요기관", demand_agency_code="SYN-DEMAND"):
    session.add(NoticeVersion(notice=notice, version_no=version_no, file_sha256="a" * 64,
        source_payload={
            "kind": "PPS_NOTICE_METADATA",
            "notice_identity": {"bid_notice_no": notice.bid_notice_no, "revision_no": notice.revision_no},
            "notice_metadata": {
                "demand_agency_name": demand_agency_name,
                "demand_agency_code": demand_agency_code,
                "announcing_agency_name": "SYN 공고기관",
                "announcing_agency_code": "SYN-ANNOUNCE",
            },
        }))


@pytest.fixture
def setup(client, monkeypatch):
    client.app.state.settings = replace(client.app.state.settings, pps_api_key="SYN-fake-never-network")
    clock = [NOW]
    monkeypatch.setattr(module, "_now", lambda: clock[0])
    # Fixture IDs remain SYN throughout. Only this fixture family is treated as
    # a real-source candidate so the production synthetic exclusion stays exact.
    original = module._source_kind
    monkeypatch.setattr(module, "_source_kind", lambda notice: "PPS" if notice.notice_key.startswith("SYN-QUEUE-") else original(notice))

    def add(key="A", *, category="용역", status="OPEN", age=0, title="가상 교육 컨설팅",
            demand_agency_name="SYN 수요기관", demand_agency_code="SYN-DEMAND"):
        with client.app.state.session_factory() as session:
            notice = Notice(notice_key=f"SYN-QUEUE-{key}", bid_notice_no=f"SYN-{key}", title=title,
                category=category, status=status, agency="가상 기관", deadline=NOW + timedelta(days=120-age),
                published_at=NOW - timedelta(days=age), created_at=NOW - timedelta(days=age))
            session.add(notice)
            session.flush()
            add_scope_version(session, notice, demand_agency_name=demand_agency_name,
                              demand_agency_code=demand_agency_code)
            session.commit()
            return notice.id

    return clock, add


def fake_collector(client, monkeypatch, *, status="COMPLETED", records=2, calls=40, wait=None):
    captured = []

    def collect(notice_key, payload, request, session):
        captured.append((notice_key, payload.model_dump()))
        if wait is not None:
            wait[0].set()
            assert wait[1].wait(5)
        job = IngestionJob(source="PPS_AWARD", mode="LIVE", status=status,
            window_json={"from": "2024-01-01", "to": "2026-09-13"}, keyword="가상 교육",
            request_json={"automation_attempt_id": request.state.award_automation_attempt_id},
            notice_keys=[notice_key], api_calls=calls, matched=records, created_at=module._now(), completed_at=module._now())
        session.add(job)
        session.commit()
        return SimpleNamespace(job_id=job.id, api_calls=calls, records=records, status=status)

    monkeypatch.setattr(module, "refresh_award_history", collect)
    return captured


def post(client, path, body=None):
    result = client.post(f"{BASE}/{path}", json=body or {})
    assert result.status_code == 200, result.text
    return result.json()


def test_plan_never_materializes_unrelated_extraction_version_payloads(client, setup):
    _clock, add = setup
    notice_id = add()
    with client.app.state.session_factory() as session:
        session.add(NoticeVersion(
            notice_id=notice_id, version_no=2, file_sha256="b" * 64,
            source_payload={"kind": "OPENAI_REQUIREMENT_EXTRACTION", "result": "SYN-LARGE" * 20_000},
        ))
        session.commit()
    loaded = []

    def inspect_version(version, _context):
        loaded.append(version.source_payload.get("kind"))

    event.listen(NoticeVersion, "load", inspect_version)
    try:
        assert post(client, "plan")["pending"] == 1
    finally:
        event.remove(NoticeVersion, "load", inspect_version)
    assert loaded and set(loaded) == {"PPS_NOTICE_METADATA"}


def test_plan_all_lifecycles_sources_and_idempotency(client, setup):
    _clock, add = setup
    add("OPEN")
    add("CLOSED", status="CLOSED", age=15)
    add("CANCELLED", status="CANCELLED", age=20)
    add("GOODS", category="물품")
    add("UNKNOWN", category=None)
    synthetic_id = add("SYNTHETIC")
    with client.app.state.session_factory() as session:
        session.get(Notice, synthetic_id).notice_key = "SYN-EXCLUDED"
        session.commit()
    first = post(client, "plan")
    assert (first["total"], first["enrolled"], first["pending"], first["unsupported"], first["skipped"]) == (6, 6, 1, 2, 3)
    second = post(client, "plan")
    assert second["enrolled"] == second["requeued"] == second["unplanned"] == 0
    assert second["eligible"] == 1


def test_run_only_awards_and_durable_counts(client, setup, monkeypatch):
    _clock, add = setup
    add()
    captured = fake_collector(client, monkeypatch)
    post(client, "plan")
    result = post(client, "run")
    assert result["status"] == "COMPLETED" and result["complete"] == 1
    assert result["api_calls_24h"] == 40 and result["budget_reserved_24h"] == 0
    assert result["ai_calls"] == 0 and result["attempted"] == 1
    assert captured[0][1]["years"] == 3
    assert captured[0][1]["include_opening_results"] is True
    assert captured[0][1]["max_opening_result_notices"] == 30
    assert captured[0][1]["opening_result_max_pages"] == 3
    assert captured[0][1]["max_api_calls"] == 150
    with client.app.state.session_factory() as session:
        state = session.scalar(select(AwardRefreshState))
        assert state.attempts == 1 and state.last_job_id == result["job_id"]
        assert session.scalar(select(func.count()).select_from(Evaluation)) == 0
        assert session.scalar(select(func.count()).select_from(UserDecision)) == 0
    assert post(client, "run")["status"] == "IDLE"


def test_zero_results_stay_fresh_then_stale_plan_requeues(client, setup, monkeypatch):
    clock, add = setup
    add()
    captured = fake_collector(client, monkeypatch, records=0)
    post(client, "plan")
    assert post(client, "run")["no_results"] == 1
    assert post(client, "plan")["requeued"] == 0
    assert post(client, "run")["attempted"] == 0
    clock[0] += timedelta(days=31)
    assert post(client, "plan")["requeued"] == 1
    assert post(client, "run")["attempted"] == 1
    assert len(captured) == 2


def test_partial_backoff_and_dead_letter_do_not_busy_retry(client, setup, monkeypatch):
    clock, add = setup
    add()
    captured = fake_collector(client, monkeypatch, status="PARTIAL", records=0, calls=150)
    post(client, "plan")
    for hours in (0, 6, 12):
        clock[0] += timedelta(hours=hours)
        assert post(client, "run")["status"] == "PARTIAL"
        assert post(client, "plan")["requeued"] == 0
        assert post(client, "run")["status"] == "IDLE"
    clock[0] += timedelta(days=40)
    assert post(client, "plan")["partial"] == 1
    assert post(client, "run")["status"] == "IDLE"
    assert len(captured) == 3


def test_kst_day_budget_counts_external_jobs_and_resets_at_midnight(client, setup, monkeypatch):
    clock, add = setup
    add()
    captured = fake_collector(client, monkeypatch)
    with client.app.state.session_factory() as session:
        session.add(IngestionJob(source="PPS_AWARD", mode="LIVE", status="COMPLETED", window_json={},
            request_json={}, api_calls=960, created_at=NOW - timedelta(hours=2)))
        session.commit()
    post(client, "plan")
    result = post(client, "run")
    assert result["status"] == "DAILY_BUDGET_REACHED" and result["attempted"] == 0
    assert not captured
    clock[0] += timedelta(hours=4)
    assert post(client, "run")["status"] == "COMPLETED"


def test_unknown_failure_retains_reservation_and_backoff(client, setup, monkeypatch):
    _clock, add = setup
    add()
    def fail(*args):
        raise RuntimeError("SYN secret-like provider prose must never be emitted")
    monkeypatch.setattr(module, "refresh_award_history", fail)
    post(client, "plan")
    result = post(client, "run")
    assert result["status"] == "FAILED" and result["budget_reserved_24h"] == 150
    assert "secret" not in str(result)
    assert post(client, "run")["status"] == "IDLE"


def test_known_failed_audit_uses_actual_calls(client, setup, monkeypatch):
    _clock, add = setup
    add()
    def fail(notice_key, payload, request, session):
        session.add(IngestionJob(source="PPS_AWARD", mode="LIVE", status="FAILED", window_json={},
            request_json={"automation_attempt_id": request.state.award_automation_attempt_id},
            api_calls=7, notice_keys=[notice_key], created_at=NOW))
        session.commit()
        raise HTTPException(502, "SYN private provider error")
    monkeypatch.setattr(module, "refresh_award_history", fail)
    post(client, "plan")
    result = post(client, "run")
    assert result["api_calls"] == result["api_calls_24h"] == 7
    assert result["budget_reserved_24h"] == 0 and result["job_id"]


def test_concurrent_runner_has_one_durable_lease(client, setup, monkeypatch):
    _clock, add = setup
    add("A")
    add("B")
    entered, release = threading.Event(), threading.Event()
    captured = fake_collector(client, monkeypatch, wait=(entered, release))
    post(client, "plan")
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(post, client, "run")
        assert entered.wait(5)
        try:
            assert post(client, "run")["status"] == "BUSY"
            assert post(client, "plan")["running"] == 1
        finally:
            release.set()
        assert future.result(timeout=5)["status"] == "COMPLETED"
    assert len(captured) == 1


@pytest.mark.parametrize("job_status", [None, "COMPLETED", "PARTIAL"])
def test_expired_lease_recovers_exact_audit_or_reserves_unknown(client, setup, job_status):
    _clock, add = setup
    notice_id = add()
    post(client, "plan")
    token = new_id()
    with client.app.state.session_factory() as session:
        state = session.get(AwardRefreshState, notice_id)
        state.status, state.lease_token, state.leased_until = "RUNNING", token, NOW - timedelta(seconds=1)
        state.attempts, state.cycle_attempts = 1, 1
        session.add(AwardRefreshAttempt(id=token, notice_id=notice_id, status="RUNNING",
            started_at=NOW - timedelta(minutes=20), reserved_calls=150))
        if job_status:
            session.add(IngestionJob(source="PPS_AWARD", mode="LIVE", status=job_status, window_json={},
                request_json={"automation_attempt_id": token}, api_calls=42, matched=0,
                created_at=NOW - timedelta(minutes=20)))
        session.commit()
    result = post(client, "plan")
    if job_status == "COMPLETED":
        assert result["no_results"] == 1 and result["budget_reserved_24h"] == 0
    elif job_status == "PARTIAL":
        assert result["partial"] == 1 and result["api_calls_24h"] == 42
    else:
        assert result["failed"] == 1 and result["budget_reserved_24h"] == 150
    assert result["running"] == result["eligible"] == 0


def test_source_change_prevents_wrong_service_calls_and_changed_title_requeues(client, setup, monkeypatch):
    _clock, add = setup
    notice_id = add()
    captured = fake_collector(client, monkeypatch)
    post(client, "plan")
    with client.app.state.session_factory() as session:
        session.get(Notice, notice_id).category = "물품"
        session.commit()
    assert post(client, "run")["unsupported"] == 1
    assert not captured
    with client.app.state.session_factory() as session:
        session.get(Notice, notice_id).category = "용역"
        session.commit()
    assert post(client, "plan")["requeued"] == 1
    assert post(client, "run")["complete"] == 1
    with client.app.state.session_factory() as session:
        session.get(Notice, notice_id).title = "가상 새 교육 사업"
        session.commit()
    assert post(client, "plan")["requeued"] == 1


def test_protected_routes_and_request_limits(client, setup):
    assert client.post(f"{BASE}/run", json={"max_notices": 11}).status_code == 422
    assert client.post(f"{BASE}/run", json={"daily_api_budget": 1001}).status_code == 422
    for body in ({"max_notices": True}, {"max_notices": "10"}, {"daily_api_budget": 1.5}):
        assert client.post(f"{BASE}/run", json=body).status_code == 422
    assert client.post(f"{BASE}/run", json={"force_ai": True}).status_code == 422
    assert client.post(f"{BASE}/plan", json={"refresh_after_days": 0}).status_code == 422
    client.headers.pop("X-PAI-LOOP-API-KEY")
    assert client.get(f"{BASE}/status").status_code == 401
    assert client.post(f"{BASE}/plan", json={}).status_code == 401


def test_never_attempted_priority_before_stale_refresh(client, setup, monkeypatch):
    _clock, add = setup
    old_id = add("OLD", age=20)
    current_id = add("CURRENT")
    captured = fake_collector(client, monkeypatch)
    post(client, "plan")
    assert post(client, "run")["notice_key"] == "SYN-QUEUE-CURRENT"
    with client.app.state.session_factory() as session:
        session.get(AwardRefreshState, current_id).refreshed_at = NOW - timedelta(days=31)
        session.commit()
    assert post(client, "plan")["requeued"] == 1
    assert post(client, "run")["notice_key"] == "SYN-QUEUE-OLD"
    assert len(captured) == 2
    with client.app.state.session_factory() as session:
        assert session.get(AwardRefreshState, old_id).status == "COMPLETED"


def test_durable_actual_usage_survives_operational_job_retention(client, setup, monkeypatch):
    _clock, add = setup
    add()
    fake_collector(client, monkeypatch, calls=47)
    post(client, "plan")
    result = post(client, "run")
    with client.app.state.session_factory() as session:
        session.delete(session.get(IngestionJob, result["job_id"]))
        session.commit()
    status = client.get(f"{BASE}/status").json()
    assert status["api_calls_24h"] == 47 and status["budget_reserved_24h"] == 0


def test_no_key_or_searchable_keyword_never_calls_provider(client, setup, monkeypatch):
    _clock, add = setup
    add(title="2026년 사업 용역 공고")
    captured = fake_collector(client, monkeypatch)
    assert post(client, "plan")["unsupported"] == 1
    assert post(client, "run")["attempted"] == 0
    client.app.state.settings = replace(client.app.state.settings, pps_api_key=None)
    assert client.post(f"{BASE}/run", json={}).status_code == 503
    assert not captured


@pytest.mark.parametrize("records", [0, 2])
def test_fresh_legacy_broad_search_requeues_under_demand_agency_scope(client, setup, monkeypatch, records):
    _clock, add = setup
    notice_id = add()
    captured = fake_collector(client, monkeypatch, records=records)
    post(client, "plan")
    completed = post(client, "run")
    with client.app.state.session_factory() as session:
        notice = session.get(Notice, notice_id)
        state = session.get(AwardRefreshState, notice_id)
        state.basis_sha256 = hashlib.sha256(
            f"{notice.notice_key}\n{notice.category}\n{notice.title}".encode()).hexdigest()
        session.commit()
    result = post(client, "plan")
    assert result["requeued"] == result["pending"] == result["eligible"] == 1
    assert result["complete"] == result["no_results"] == 0
    with client.app.state.session_factory() as session:
        state = session.get(AwardRefreshState, notice_id)
        assert state.attempts == 1 and state.cycle_attempts == 0
        assert state.last_job_id == completed["job_id"]
        assert session.get(IngestionJob, completed["job_id"]) is not None
    assert post(client, "run")["attempted"] == 1
    assert len(captured) == 2


@pytest.mark.parametrize("changed_field", ["demand_agency_name", "demand_agency_code"])
def test_demand_agency_identity_change_requeues_fresh_completion(client, setup, monkeypatch, changed_field):
    _clock, add = setup
    notice_id = add()
    fake_collector(client, monkeypatch)
    post(client, "plan")
    post(client, "run")
    with client.app.state.session_factory() as session:
        old_basis = session.get(AwardRefreshState, notice_id).basis_sha256
        add_scope_version(session, session.get(Notice, notice_id), version_no=2,
                          **{changed_field: "SYN-CHANGED"})
        session.commit()
    result = post(client, "plan")
    assert result["requeued"] == result["pending"] == 1 and result["complete"] == 0
    with client.app.state.session_factory() as session:
        assert session.get(AwardRefreshState, notice_id).basis_sha256 != old_basis
    assert post(client, "plan")["requeued"] == 0


def test_same_agency_metadata_version_does_not_requeue_completed_scope(client, setup, monkeypatch):
    _clock, add = setup
    notice_id = add()
    fake_collector(client, monkeypatch)
    post(client, "plan")
    post(client, "run")
    with client.app.state.session_factory() as session:
        add_scope_version(session, session.get(Notice, notice_id), version_no=2)
        session.commit()
    result = post(client, "plan")
    assert result["requeued"] == result["eligible"] == 0 and result["complete"] == 1


def test_changed_keyword_derivation_invalidates_completed_scope(client, setup, monkeypatch):
    _clock, add = setup
    add()
    fake_collector(client, monkeypatch)
    post(client, "plan")
    post(client, "run")
    monkeypatch.setattr(module, "_derive_award_keyword", lambda title: "SYN changed keyword")
    assert post(client, "plan")["requeued"] == 1


@pytest.mark.parametrize("remove_after_plan", [False, True])
def test_missing_actual_demand_agency_never_reserves_or_calls_provider(client, setup, monkeypatch, remove_after_plan):
    _clock, add = setup
    notice_id = add() if remove_after_plan else add(demand_agency_name=None, demand_agency_code=None)
    captured = fake_collector(client, monkeypatch)
    plan = post(client, "plan")
    if remove_after_plan:
        assert plan["pending"] == 1
        with client.app.state.session_factory() as session:
            add_scope_version(session, session.get(Notice, notice_id), version_no=2,
                              demand_agency_name=None, demand_agency_code=None)
            session.commit()
    else:
        assert plan["unsupported"] == 1 and plan["eligible"] == 0
    result = post(client, "run")
    assert result["unsupported"] == 1 and result["attempted"] == result["api_calls"] == result["ai_calls"] == 0
    assert result["api_calls_24h"] == result["budget_reserved_24h"] == 0
    assert not captured
    with client.app.state.session_factory() as session:
        state = session.get(AwardRefreshState, notice_id)
        assert state.status == "UNSUPPORTED" and state.reason == "AWARD_AGENCY_UNAVAILABLE"
        assert session.scalar(select(func.count()).select_from(AwardRefreshAttempt)) == 0


@pytest.mark.parametrize("status,age", [("CLOSED", 0), ("CANCELLED", 0), ("OPEN", 121)])
def test_inactive_notice_is_skipped_before_missing_agency_check(client, setup, monkeypatch, status, age):
    _clock, add = setup
    notice_id = add(status=status, age=age, demand_agency_name=None, demand_agency_code=None)
    captured = fake_collector(client, monkeypatch)
    plan = post(client, "plan")
    assert plan["skipped"] == 1 and plan["unsupported"] == plan["eligible"] == 0
    assert post(client, "run")["attempted"] == 0
    assert not captured
    with client.app.state.session_factory() as session:
        assert session.get(AwardRefreshState, notice_id).reason == "INACTIVE_NOTICE"


def test_scope_changed_after_plan_is_recorded_when_claimed(client, setup, monkeypatch):
    _clock, add = setup
    notice_id = add()
    fake_collector(client, monkeypatch)
    post(client, "plan")
    with client.app.state.session_factory() as session:
        add_scope_version(session, session.get(Notice, notice_id), version_no=2,
                          demand_agency_code="SYN-CHANGED")
        session.commit()
    assert post(client, "run")["complete"] == 1
    assert post(client, "plan")["requeued"] == 0
