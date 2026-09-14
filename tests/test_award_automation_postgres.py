"""Award queue concurrency against the explicitly disposable PostgreSQL service.

The shared fixture rejects remote/production URLs, creates one generated SYN
schema, and skips unconfigured local runs. No real provider is called.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from pai_loop import award_automation as module
from pai_loop.award_automation_models import AwardRefreshAttempt, AwardRefreshState
from pai_loop.config import Settings
from pai_loop.database import Base, build_session_factory
from pai_loop.models import IngestionJob, Notice, new_id
from test_award_automation import add_scope_version
from test_postgres_department_accounts import _assert_waiters, postgres_account_engine

BASE = "/api/v1/operations/award-refresh"
HEADERS = {"X-PAI-LOOP-API-KEY": "SYN-award-queue-test-only"}


def _app(engine, monkeypatch):
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.state.session_factory = build_session_factory(engine)
    app.state.settings = Settings(api_key=HEADERS["X-PAI-LOOP-API-KEY"], pps_api_key="SYN-no-network-key")
    app.include_router(module.router)
    monkeypatch.setattr(module, "_source_kind", lambda notice: "PPS")
    # Remove the single-process fallback: these tests must prove that the real
    # PostgreSQL transaction lock alone arbitrates different worker sessions.
    monkeypatch.setattr(module, "_PROCESS_LOCK", nullcontext())
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as session:
        notice = Notice(notice_key="SYN-PG-AWARD-QUEUE", bid_notice_no="SYN-PG-AWARD",
            title="SYN 가상 교육", agency="SYN 가상 기관", category="용역", status="OPEN",
            deadline=now + timedelta(days=10), published_at=now)
        session.add(notice)
        session.flush()
        add_scope_version(session, notice)
        session.commit()
        notice_id = notice.id
    assert _post(app, "plan")["pending"] == 1
    return app, notice_id


def _post(app, path):
    # Each request owns its client and DB session, like separate production workers.
    with TestClient(app, headers=HEADERS) as client:
        response = client.post(f"{BASE}/{path}", json={})
        assert response.status_code == 200, response.text
        return response.json()


def test_postgres_concurrent_claim_releases_lock_before_provider_and_dispatches_once(postgres_account_engine, monkeypatch):
    engine = postgres_account_engine
    app, notice_id = _app(engine, monkeypatch)
    entered, release = Event(), Event()
    calls = []

    def collect(notice_key, payload, request, session):
        calls.append(notice_key)
        entered.set()
        assert release.wait(8), "synthetic collector was not released"
        now = datetime.now(timezone.utc)
        job = IngestionJob(source="PPS_AWARD", mode="LIVE", status="COMPLETED", window_json={},
            request_json={"automation_attempt_id": request.state.award_automation_attempt_id},
            notice_keys=[notice_key], api_calls=38, matched=0, created_at=now, completed_at=now)
        session.add(job)
        session.commit()
        return SimpleNamespace(job_id=job.id, status="COMPLETED", api_calls=38, records=0)

    monkeypatch.setattr(module, "refresh_award_history", collect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            with engine.begin() as blocker:
                blocker.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": module._LOCK_KEY})
                futures = [pool.submit(_post, app, "run") for _ in range(2)]
                _assert_waiters(engine, module._LOCK_KEY, 2)
            assert entered.wait(5)
            completed, pending = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert len(completed) == len(pending) == 1
            assert next(iter(completed)).result()["status"] == "BUSY"
            # The durable reservation exists while provider I/O is blocked, and
            # its transaction/advisory lock is already free for another process.
            with app.state.session_factory() as session:
                assert session.scalar(select(func.count()).select_from(AwardRefreshAttempt)) == 1
                assert session.get(AwardRefreshState, notice_id).status == "RUNNING"
            with engine.begin() as connection:
                assert connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": module._LOCK_KEY}) is True
        finally:
            release.set()
        outcomes = [future.result(timeout=5) for future in futures]
    assert sorted(result["status"] for result in outcomes) == ["BUSY", "COMPLETED"]
    assert len(calls) == 1
    completed = next(result for result in outcomes if result["status"] == "COMPLETED")
    assert completed["api_calls_24h"] == 38 and completed["budget_reserved_24h"] == 0
    assert completed["no_results"] == 1 and completed["ai_calls"] == 0
    assert _post(app, "run")["attempted"] == 0


def test_postgres_expired_unknown_lease_keeps_full_budget_reservation(postgres_account_engine, monkeypatch):
    app, notice_id = _app(postgres_account_engine, monkeypatch)
    now, token = datetime.now(timezone.utc), new_id()
    with app.state.session_factory() as session:
        state = session.get(AwardRefreshState, notice_id)
        state.status, state.lease_token, state.leased_until = "RUNNING", token, now - timedelta(seconds=1)
        state.attempts, state.cycle_attempts = 1, 1
        session.add(AwardRefreshAttempt(id=token, notice_id=notice_id, status="RUNNING",
            started_at=now - timedelta(minutes=20), reserved_calls=150))
        session.commit()
    recovered = _post(app, "plan")
    assert recovered["failed"] == 1 and recovered["running"] == recovered["eligible"] == 0
    assert recovered["api_calls_24h"] == 0 and recovered["budget_reserved_24h"] == 150
    with app.state.session_factory() as session:
        assert session.get(AwardRefreshAttempt, token).status == "LEASE_EXPIRED"
    assert _post(app, "run")["attempted"] == 0
