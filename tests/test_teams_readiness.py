from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from pai_loop.main import create_app
from pai_loop.models import IngestionJob, Notice


KST = timezone(timedelta(hours=9))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _kst_date() -> str:
    return _now().astimezone(KST).date().isoformat()


def _add_ingestion(
    client: TestClient,
    *,
    status: str = "COMPLETED",
    created: int = 0,
    updated: int = 0,
    matched: int = 0,
    created_at: datetime | None = None,
) -> str:
    now = created_at or _now()
    with client.app.state.session_factory() as session:
        job = IngestionJob(
            source="PPS",
            mode="LIVE",
            status=status,
            window_json={"from": _kst_date(), "to": _kst_date()},
            request_json={"page_size": 100, "max_pages": 5},
            created_count=created,
            updated_count=updated,
            matched=matched,
            notice_keys=[],
            warnings=[],
            created_at=now,
            completed_at=None if status == "RUNNING" else now,
        )
        session.add(job)
        session.commit()
        return job.id


def _add_daily_parent(
    client: TestClient,
    *,
    status: str,
    notice_keys: list[str],
    child_outcomes: dict[str, str] | None = None,
    completed: bool,
    created_at: datetime | None = None,
    lease_started_at: datetime | None = None,
) -> str:
    now = _now()
    config: dict[str, object] = {
        "queue_name": "DAILY",
        "dry_run": False,
        "chunk_size": 3,
        "execution_limit": 30,
        "max_continuations": 128,
        "reservation_ttl_hours": 6,
        "include_retryable": False,
        "retry_cooldown_hours": 24,
        "work_generations": {key: 0 for key in notice_keys},
    }
    if lease_started_at is not None:
        config["lease_started_at"] = lease_started_at.isoformat()
        config["lease_id"] = "11111111-1111-4111-8111-111111111111"
    with client.app.state.session_factory() as session:
        parent = IngestionJob(
            source="ANALYSIS_BACKFILL",
            mode="LIVE",
            status=status,
            window_json={"scope": "OPEN_NOT_SELECTED", "as_of": now.isoformat()},
            request_json=config,
            matched=len(notice_keys),
            notice_keys=notice_keys,
            warnings=[],
            created_at=created_at or now,
            completed_at=now if completed else None,
        )
        session.add(parent)
        session.flush()
        if child_outcomes:
            results = [
                {"notice_key": key, "status": outcome}
                for key, outcome in child_outcomes.items()
            ]
            completed_count = sum(value == "COMPLETED" for value in child_outcomes.values())
            failed_count = sum(value == "FAILED" for value in child_outcomes.values())
            skipped_count = sum(value == "SKIPPED" for value in child_outcomes.values())
            session.add(
                IngestionJob(
                    source="ANALYSIS",
                    mode="LIVE",
                    status="COMPLETED" if failed_count == 0 else "PARTIAL",
                    window_json={"scope": "NOTICE_KEYS"},
                    request_json={
                        "parent_job_id": parent.id,
                        "segment_id": "22222222-2222-4222-8222-222222222222",
                        "chunk_index": 0,
                        "work_generations": {key: 0 for key in child_outcomes},
                        "result_json": {
                            "openai_calls": 0,
                            "results": results,
                        },
                    },
                    created_count=completed_count,
                    duplicate_count=skipped_count,
                    quarantined_count=failed_count,
                    notice_keys=list(child_outcomes),
                    warnings=[],
                    created_at=now,
                    completed_at=now,
                )
            )
        session.commit()
        return parent.id


def _readiness(client: TestClient) -> dict:
    response = client.get("/api/v1/operations/teams-daily-readiness")
    assert response.status_code == 200
    return response.json()


def test_readiness_fails_closed_when_today_ingestion_was_not_planned(
    client: TestClient,
) -> None:
    body = _readiness(client)
    assert body["status"] == "NOT_PLANNED"
    assert body["ready"] is False
    assert body["reason_code"] == "TODAY_PPS_NOT_PLANNED"
    assert body["retry_after_seconds"] == 900
    assert body["source_calls"] == {"pps": 0, "openai": 0, "teams": 0}


def test_readiness_endpoint_remains_server_key_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "test")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "readiness-server-key")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'protected-readiness.db').as_posix()}",
        seed_synthetic=False,
    )
    with TestClient(app) as protected:
        assert protected.get("/api/v1/operations/teams-daily-readiness").status_code == 401
        response = protected.get(
            "/api/v1/operations/teams-daily-readiness",
            headers={"X-PAI-LOOP-API-KEY": "readiness-server-key"},
        )
        assert response.status_code == 200
        assert response.json()["reason_code"] == "TODAY_PPS_NOT_PLANNED"


def test_readiness_reports_running_and_failed_today_ingestion(
    client: TestClient,
) -> None:
    _add_ingestion(client, status="RUNNING")
    running = _readiness(client)
    assert (running["status"], running["reason_code"]) == (
        "RUNNING",
        "TODAY_PPS_RUNNING",
    )
    with client.app.state.session_factory() as session:
        current = session.get(IngestionJob, running["ingestion"]["job_id"])
        assert current is not None
        current.status = "FAILED"
        current.completed_at = _now()
        session.commit()
    failed = _readiness(client)
    assert (failed["status"], failed["reason_code"]) == (
        "FAILED",
        "TODAY_PPS_FAILED",
    )
    assert failed["ready"] is False


def test_readiness_allows_verified_zero_work_day_without_daily_parent(
    client: TestClient,
) -> None:
    ingestion_id = _add_ingestion(client, status="COMPLETED")
    before = None
    with client.app.state.session_factory() as session:
        job = session.get(IngestionJob, ingestion_id)
        assert job is not None
        before = (job.status, job.completed_at, dict(job.request_json))

    body = _readiness(client)

    assert body["status"] == "READY"
    assert body["ready"] is True
    assert body["reason_code"] == "READY_EMPTY"
    assert body["analysis"] == {
        "parent_job_id": None,
        "parent_status": None,
        "terminal": False,
        "planned": 0,
        "attempted": 0,
        "remaining": 0,
        "in_flight": 0,
        "completed": 0,
        "partial": 0,
        "failed": 0,
        "queue_pending": 0,
    }
    assert body["retry_after_seconds"] is None
    with client.app.state.session_factory() as session:
        job = session.get(IngestionJob, ingestion_id)
        assert job is not None
        assert (job.status, job.completed_at, dict(job.request_json)) == before


def test_empty_fallback_refuses_material_ingestion_or_pending_queue(
    client: TestClient,
) -> None:
    _add_ingestion(client, status="COMPLETED", created=1, matched=1)
    body = _readiness(client)
    assert (body["status"], body["reason_code"]) == (
        "NOT_PLANNED",
        "DAILY_ANALYSIS_NOT_PLANNED",
    )


def test_empty_fallback_refuses_unplanned_stored_analysis_queue(
    client: TestClient,
) -> None:
    now = _now()
    _add_ingestion(client, status="COMPLETED")
    with client.app.state.session_factory() as session:
        session.add(
            Notice(
                notice_key="MANUAL-READINESS-PENDING",
                bid_notice_no="MANUAL-READINESS-PENDING",
                revision_no="00",
                title="공공기관 교육 운영 용역",
                agency="가상 공공기관",
                published_at=now,
                deadline=now + timedelta(days=5),
                status="OPEN",
                category="용역",
                estimated_amount=100_000_000,
            )
        )
        session.commit()
    body = _readiness(client)
    assert body["status"] == "NOT_PLANNED"
    assert body["reason_code"] == "DAILY_ANALYSIS_NOT_PLANNED"
    assert body["analysis"]["queue_pending"] == 1


def test_active_daily_parent_blocks_delivery_until_terminal(
    client: TestClient,
) -> None:
    _add_ingestion(client, status="COMPLETED", created=1, matched=1)
    parent_id = _add_daily_parent(
        client,
        status="RUNNING",
        notice_keys=["PPS-RUNNING-001"],
        completed=False,
    )
    body = _readiness(client)
    assert body["status"] == "RUNNING"
    assert body["reason_code"] == "DAILY_ANALYSIS_RUNNING"
    assert body["analysis"]["parent_job_id"] == parent_id
    assert body["analysis"]["terminal"] is False
    assert body["analysis"]["remaining"] == 1
    assert body["ready"] is False


def test_completed_daily_parent_is_ready_only_with_exact_terminal_coverage(
    client: TestClient,
) -> None:
    _add_ingestion(client, status="COMPLETED", created=2, matched=2)
    keys = ["PPS-COMPLETE-001", "PPS-COMPLETE-002"]
    parent_id = _add_daily_parent(
        client,
        status="COMPLETED",
        notice_keys=keys,
        child_outcomes={key: "COMPLETED" for key in keys},
        completed=True,
    )
    body = _readiness(client)
    assert body["status"] == "READY"
    assert body["reason_code"] == "DAILY_ANALYSIS_COMPLETE"
    assert body["analysis"] == {
        "parent_job_id": parent_id,
        "parent_status": "COMPLETED",
        "terminal": True,
        "planned": 2,
        "attempted": 2,
        "remaining": 0,
        "in_flight": 0,
        "completed": 2,
        "partial": 0,
        "failed": 0,
        "queue_pending": 0,
    }


def test_terminal_partial_or_inconsistent_daily_parent_fails_closed(
    client: TestClient,
) -> None:
    _add_ingestion(client, status="COMPLETED", created=1, matched=1)
    _add_daily_parent(
        client,
        status="PARTIAL",
        notice_keys=["PPS-FAILED-001"],
        child_outcomes={"PPS-FAILED-001": "FAILED"},
        completed=True,
    )
    body = _readiness(client)
    assert body["status"] == "FAILED"
    assert body["reason_code"] == "DAILY_ANALYSIS_TERMINAL_FAILURE"
    assert body["analysis"]["failed"] == 1
    assert body["ready"] is False


def test_prior_day_active_parent_is_not_mistaken_for_ready_empty(
    client: TestClient,
) -> None:
    _add_ingestion(client, status="COMPLETED")
    _add_daily_parent(
        client,
        status="RUNNING",
        notice_keys=["PPS-PRIOR-ACTIVE-001"],
        completed=False,
        created_at=_now() - timedelta(days=1),
        lease_started_at=_now(),
    )
    body = _readiness(client)
    assert body["status"] == "RUNNING"
    assert body["analysis"]["remaining"] == 1


def test_prior_run_completed_today_cannot_satisfy_new_today_ingestion(
    client: TestClient,
) -> None:
    key = "PPS-PRIOR-COMPLETED-001"
    _add_daily_parent(
        client,
        status="COMPLETED",
        notice_keys=[key],
        child_outcomes={key: "COMPLETED"},
        completed=True,
        created_at=_now() - timedelta(days=1),
        lease_started_at=_now(),
    )
    _add_ingestion(client, status="COMPLETED", created=1, matched=1)

    body = _readiness(client)

    assert (body["status"], body["reason_code"]) == (
        "NOT_PLANNED",
        "DAILY_ANALYSIS_NOT_PLANNED",
    )
    assert body["analysis"]["parent_job_id"] is None
    assert body["ready"] is False
