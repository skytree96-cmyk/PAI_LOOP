from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.models import Notice
from pai_loop.public_notice_seed import import_public_notice_seed


NOTICE_KEY = "MANUAL-INCHON-2025-17"


def _seed_public_notice(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        result = import_public_notice_seed(session)
    assert result.requirement_count == 23


def test_analysis_batch_persists_and_reuses_snapshot(client: TestClient) -> None:
    _seed_public_notice(client)
    first = client.post(
        "/api/v1/notices/analysis/batch",
        json={"notice_keys": [NOTICE_KEY], "dry_run": False, "force": False},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["processed"] == 1
    assert body["completed"] == 1
    assert body["failed"] == 0
    assert body["openai_calls"] == 0
    assert body["document_materialized"] == 1
    assert body["evaluations_created"] == 1
    assert body["snapshots_refreshed"] == 1
    assert body["results"][0]["analysis_run_id"]
    assert body["results"][0]["input_sha256"]
    assert body["results"][0]["requirement_snapshots"] == 23
    assert body["results"][0]["score_snapshots"] == 8
    assert body["results"][0]["recommendation_snapshots"] >= 1
    assert body["results"][0]["analysis_state"] == "ANALYZED"
    assert body["results"][0]["analysis_reason_code"] == "ANALYZED"
    assert body["results"][0]["analysis_reason"]

    repeated = client.post(
        "/api/v1/notices/analysis/batch",
        json={"notice_keys": [NOTICE_KEY], "dry_run": False},
    )
    assert repeated.status_code == 200
    assert repeated.json()["results"][0]["reused"] is True
    assert repeated.json()["document_materialized"] == 0
    assert repeated.json()["evaluations_created"] == 0
    assert repeated.json()["snapshots_refreshed"] == 0

    history = client.get(f"/api/v1/notices/{NOTICE_KEY}/analysis-runs")
    assert history.status_code == 200
    assert history.json()["count"] == 1
    run = history.json()["runs"][0]
    assert run["basis_versions"]["company_profile"]
    assert len(run["requirement_results"]) == 23
    assert len(run["scores"]) == 8
    assert any(item["recommendation_key"] == "bid:system" for item in run["recommendations"])

    # This assertion targets snapshot projection, so make the historical
    # fixture an explicitly open notice for the requested as-of instant.
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == NOTICE_KEY))
        assert notice is not None
        notice.status = "OPEN"
        session.commit()

    briefing = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "as_of": "2026-01-03T09:00:00+09:00"},
    )
    assert briefing.status_code == 200
    snapshot = briefing.json()["notices"][0]["analysis_snapshot"]
    assert snapshot["analysis_run_id"] == body["results"][0]["analysis_run_id"]
    assert len(snapshot["scores"]) == 8


def test_analysis_batch_dry_run_and_missing_notice_write_no_snapshots(client: TestClient) -> None:
    _seed_public_notice(client)
    response = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [NOTICE_KEY, "MISSING-NOTICE"],
            "dry_run": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PARTIAL"
    assert body["completed"] == 0
    assert body["skipped"] == 1
    assert body["failed"] == 1
    assert body["document_materialized"] == 0
    assert body["evaluations_created"] == 0
    assert body["snapshots_refreshed"] == 0
    assert body["openai_calls"] == 0
    assert client.get(f"/api/v1/notices/{NOTICE_KEY}/analysis-runs").json()["count"] == 0


def test_analysis_batch_rejects_duplicates_and_force(client: TestClient) -> None:
    duplicate = client.post(
        "/api/v1/notices/analysis/batch",
        json={"notice_keys": ["ONE", "ONE"]},
    )
    assert duplicate.status_code == 422
    force = client.post(
        "/api/v1/notices/analysis/batch",
        json={"notice_keys": ["ONE"], "force": True},
    )
    assert force.status_code == 422


def test_analysis_enrichment_reuse_preserves_workflow_partition_invariant(
    client: TestClient,
    monkeypatch,
) -> None:
    _seed_public_notice(client)
    monkeypatch.setattr(
        "pai_loop.analysis_api._has_accepted_pps_extraction",
        lambda _request, _notice_id: True,
    )

    for _day in range(2):
        response = client.post(
            "/api/v1/notices/analysis/batch",
            json={
                "notice_keys": [NOTICE_KEY],
                "enrich_missing": True,
                "max_notices": 1,
                "max_attachments_per_notice": 1,
            },
        )
        assert response.status_code == 200, response.text
        enrichment = response.json()["enrichment"]
        assert enrichment == {
            "requested": 1,
            "attempted": 1,
            "completed": 1,
            "skipped": 0,
            "failed": 0,
            "attachments_discovered": 0,
            "attachments_processed": 0,
            "openai_calls": 0,
            "warnings": [],
        }
        assert enrichment["attempted"] == (
            enrichment["completed"] + enrichment["skipped"] + enrichment["failed"]
        )
