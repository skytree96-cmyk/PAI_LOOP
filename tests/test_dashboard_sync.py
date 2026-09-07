from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pai_loop.models import IngestionJob


SYNC_AT = datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)


def _job(
    *,
    source: str = "PPS",
    mode: str = "LIVE",
    status: str = "COMPLETED",
    created_at: datetime = SYNC_AT - timedelta(minutes=10),
    completed_at: datetime | None = SYNC_AT,
) -> IngestionJob:
    return IngestionJob(
        source=source,
        mode=mode,
        status=status,
        window_json={"from": "2026-09-01", "to": "2026-09-01"},
        request_json={},
        notice_keys=[],
        warnings=[],
        created_at=created_at,
        completed_at=completed_at,
    )


def test_dashboard_without_source_ingestion_has_no_sync_time(client: TestClient) -> None:
    response = client.get("/api/v1/dashboard")

    assert response.status_code == 200, response.text
    assert response.json()["last_sync"] is None
    assert response.json()["generated_at"] is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"source": "MANUAL_ANALYSIS"},
        {"source": "PPS_OUTCOME"},
        {"source": "PPS_AWARD"},
        {"mode": "DRY_RUN"},
        {"status": "FAILED"},
        {"status": "PARTIAL"},
        {"status": "RUNNING"},
        {"completed_at": None},
    ],
)
def test_dashboard_ignores_jobs_that_do_not_complete_live_notice_sync(
    client: TestClient, fields: dict,
) -> None:
    with client.app.state.session_factory() as session:
        session.add(_job(**fields))
        session.commit()

    response = client.get("/api/v1/dashboard")

    assert response.status_code == 200, response.text
    assert response.json()["last_sync"] is None


@pytest.mark.parametrize("public_view", [False, True])
def test_dashboard_sync_uses_latest_successful_completion(
    client: TestClient, public_view: bool,
) -> None:
    client.app.state.settings = replace(
        client.app.state.settings, public_read_only=public_view,
    )
    with client.app.state.session_factory() as session:
        session.add_all([
            _job(created_at=SYNC_AT - timedelta(hours=2)),
            _job(
                created_at=SYNC_AT - timedelta(hours=1),
                completed_at=SYNC_AT - timedelta(minutes=30),
            ),
            _job(status="FAILED", completed_at=SYNC_AT + timedelta(hours=1)),
            _job(status="PARTIAL", completed_at=SYNC_AT + timedelta(hours=2)),
            _job(mode="DRY_RUN", completed_at=SYNC_AT + timedelta(hours=3)),
            _job(source="MANUAL_ANALYSIS", completed_at=SYNC_AT + timedelta(hours=4)),
        ])
        session.commit()

    response = client.get("/api/v1/dashboard")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert datetime.fromisoformat(payload["last_sync"]) == SYNC_AT
    assert payload["last_sync"] != payload["generated_at"]
