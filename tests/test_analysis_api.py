from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from pai_loop.analysis_pipeline import AnalysisPipelineError
from pai_loop.models import AnalysisRun, IngestionJob, Notice, NoticeVersion
from pai_loop.pps_enrichment import (
    PPS_METADATA_SCHEMA,
    PpsEnrichmentResult,
    build_attachment_manifest,
    enrich_notice_from_pps,
)
from pai_loop.public_notice_seed import import_public_notice_seed


NOTICE_KEY = "MANUAL-INCHON-2025-17"


def _seed_public_notice(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        result = import_public_notice_seed(session)
    assert result.requirement_count == 23


def _run_segment(client: TestClient, plan: dict) -> None:
    assert plan["segment_id"]
    assert len(plan["chunk_indices"]) == len(plan["chunks"])
    for chunk_index, chunk in zip(
        plan["chunk_indices"], plan["chunks"], strict=True
    ):
        response = client.post(
            "/api/v1/notices/analysis/batch",
            json={
                "notice_keys": chunk,
                "dry_run": plan["dry_run"],
                "enrich_missing": True,
                "max_notices": len(chunk),
                "max_attachments_per_notice": 10,
                "operation_id": plan["job_id"],
                "segment_id": plan["segment_id"],
                "chunk_index": chunk_index,
            },
        )
        assert response.status_code == 200, response.text


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


def test_attachment_continuation_exact_retry_replays_stored_child(
    client: TestClient,
    monkeypatch,
) -> None:
    notice_key = "PPS-CONTINUATION-REPLAY"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": "R26BK-CONTINUATION-REPLAY",
            "title": "다중 첨부 응답 재전송 검증",
            "agency": "공공기관",
            "published_at": "2026-08-20T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    calls = 0

    def continuation(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return PpsEnrichmentResult(
            status="SKIPPED",
            attachments_discovered=5,
            attachments_attempted=2,
            attachments_processed=2,
            warnings=[
                "ATTACHMENT_CONTINUATION_REQUIRED",
                "ATTACHMENT_COVERAGE_INCOMPLETE",
            ],
        )

    monkeypatch.setattr("pai_loop.analysis_api._has_accepted_pps_extraction", lambda *_: False)
    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", continuation)
    plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [notice_key],
            "chunk_size": 1,
            "execution_limit": 1,
        },
    ).json()
    payload = {
        "notice_keys": [notice_key],
        "enrich_missing": True,
        "max_notices": 1,
        "max_attachments_per_notice": 10,
        "operation_id": plan["job_id"],
        "segment_id": plan["segment_id"],
        "chunk_index": plan["chunk_indices"][0],
    }
    first = client.post("/api/v1/notices/analysis/batch", json=payload)
    replay = client.post("/api/v1/notices/analysis/batch", json=payload)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert calls == 1

    released = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": plan["segment_id"]},
    )
    assert released.status_code == 200
    assert released.json()["remaining"] == 1
    resumed = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={"queue_name": "DAILY", "chunk_size": 1, "execution_limit": 1},
    ).json()
    assert resumed["segment_id"] != plan["segment_id"]
    assert resumed["notice_keys"] == [notice_key]


def test_batch_finalization_failure_requeues_and_reuses_durable_attachment(
    client: TestClient,
    monkeypatch,
) -> None:
    notice_key = "PPS-CONTINUATION-ATOMIC"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": "R26BK-CONTINUATION-ATOMIC",
            "title": "첨부 커서 원자성 검증",
            "agency": "공공기관",
            "published_at": "2026-08-20T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    durable = False
    paid_calls = 0

    def resumable(*_args, **_kwargs):
        nonlocal durable, paid_calls
        if not durable:
            durable = True
            paid_calls += 1
            return PpsEnrichmentResult(
                status="SKIPPED",
                attachments_discovered=3,
                attachments_attempted=2,
                attachments_processed=2,
                openai_calls=1,
                warnings=[
                    "ATTACHMENT_CONTINUATION_REQUIRED",
                    "ATTACHMENT_COVERAGE_INCOMPLETE",
                ],
            )
        return PpsEnrichmentResult(
            status="REUSED",
            attachments_discovered=3,
            attachments_attempted=3,
            attachments_processed=3,
        )

    monkeypatch.setattr("pai_loop.analysis_api._has_accepted_pps_extraction", lambda *_: False)
    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", resumable)
    plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [notice_key],
            "chunk_size": 1,
            "execution_limit": 1,
        },
    ).json()
    payload = {
        "notice_keys": [notice_key],
        "enrich_missing": True,
        "max_notices": 1,
        "max_attachments_per_notice": 10,
        "operation_id": plan["job_id"],
        "segment_id": plan["segment_id"],
        "chunk_index": plan["chunk_indices"][0],
    }
    from pai_loop import analysis_api

    original_store = analysis_api._store_batch_response
    store_calls = 0

    def fail_once(*args, **kwargs):
        nonlocal store_calls
        store_calls += 1
        if store_calls == 1:
            raise RuntimeError("synthetic finalization failure")
        return original_store(*args, **kwargs)

    monkeypatch.setattr("pai_loop.analysis_api._store_batch_response", fail_once)
    with pytest.raises(RuntimeError, match="synthetic finalization failure"):
        client.post("/api/v1/notices/analysis/batch", json=payload)
    progress = client.get(f"/api/v1/operations/analysis-backfills/{plan['job_id']}")
    assert progress.status_code == 200
    assert progress.json()["remaining"] == 1

    recovered = client.post("/api/v1/notices/analysis/batch", json=payload)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["job_id"]
    assert paid_calls == 1
    with client.app.state.session_factory() as session:
        child = session.get(IngestionJob, recovered.json()["job_id"])
        assert child is not None
        assert "requeue_notice_keys" not in child.request_json


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
    oversized_segment = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={"execution_limit": 31},
    )
    assert oversized_segment.status_code == 422
    refresh_not_subset = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": ["ONE"],
            "refresh_notice_keys": ["TWO"],
        },
    )
    assert refresh_not_subset.status_code == 422
    refresh_on_backfill = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "BACKFILL",
            "notice_keys": ["ONE"],
            "refresh_notice_keys": ["ONE"],
        },
    )
    assert refresh_on_backfill.status_code == 422
    retry_without_epoch = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": ["ONE"],
            "retry_notice_keys": ["ONE"],
        },
    )
    assert retry_without_epoch.status_code == 422


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
                "max_attachments_per_notice": 10,
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
            "attachments_attempted": 0,
            "attachments_processed": 0,
            "downloaded_bytes": 0,
            "source_characters": 0,
            "analysis_input_characters": 0,
            "source_read_complete": True,
            "analysis_input_complete": True,
            "members_discovered": 0,
            "members_processed": 0,
            "openai_calls": 0,
            "warnings": [],
            "attachment_results": [],
        }
        assert enrichment["attempted"] == (
            enrichment["completed"] + enrichment["skipped"] + enrichment["failed"]
        )


def test_internal_enrichment_failure_is_persisted_as_attempted_retryable_review(
    client: TestClient,
    monkeypatch,
) -> None:
    notice_key = "PPS-INTERNAL-ENRICHMENT-FAILURE"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": "R26BK-INTERNAL-FAILURE",
            "title": "내부 보강 오류 재시도 계약 검증",
            "agency": "가상 기관",
            "published_at": "2026-08-19T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    manifest = build_attachment_manifest(
        {
            "bidNtceNo": "R26BK-INTERNAL-FAILURE",
            "bidNtceOrd": "000",
            "ntceSpecFileNm1": "제안요청서.pdf",
            "ntceSpecDocUrl1": (
                "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
                "?bidPbancNo=R26BK00000001&bidPbancOrd=000&fileSeq=1"
                "&fileType=1&prcmBsneSeCd=01"
            ),
        }
    )
    assert len(manifest) == 1
    assert client.post(
        f"/api/v1/notices/{notice_key}/versions",
        json={
            "version_no": 1,
            "file_sha256": "e" * 64,
            "source_payload": {
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": manifest,
            },
        },
    ).status_code == 201

    class UnexpectedClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract(self, **_kwargs: object):
            raise UnicodeEncodeError(
                "utf-8",
                "\udb80",
                0,
                1,
                "surrogates not allowed",
            )

    monkeypatch.setattr(
        "pai_loop.pps_enrichment.download_public_attachment",
        lambda *_args, **_kwargs: b"synthetic-pdf",
    )
    monkeypatch.setattr(
        "pai_loop.pps_enrichment.extract_pps_document_content",
        lambda *_args, **_kwargs: SimpleNamespace(
            text="입찰 참가 자격과 제출 요건을 확인합니다.",
            complete=True,
            warnings=(),
            members_discovered=1,
            members_processed=1,
            member_issues=(),
        ),
    )

    def fail_inside_selected_enrichment(
        request,
        *,
        notice_id,
        payload,
        deadline_monotonic,
    ):
        del deadline_monotonic
        with request.app.state.session_factory() as session:
            return enrich_notice_from_pps(
                session,
                notice_id=notice_id,
                openai_api_key="test-key",
                openai_model="test-model",
                max_attachments=payload.max_attachments_per_notice,
                dry_run=payload.dry_run,
                openai_client_factory=UnexpectedClient,
            )

    monkeypatch.setattr(
        "pai_loop.analysis_api._enrich_one_notice",
        fail_inside_selected_enrichment,
    )
    response = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [notice_key],
            "enrich_missing": True,
            "max_notices": 1,
                "max_attachments_per_notice": 10,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PARTIAL"
    assert body["enrichment"]["attempted"] == 1
    assert body["enrichment"]["failed"] == 1
    assert body["results"][0]["status"] == "COMPLETED"
    assert body["results"][0]["analysis_state"] == "REVIEW"
    assert body["results"][0]["analysis_reason_code"] == "OPENAI_REVIEW"
    assert "INTERNAL_ENRICHMENT_ERROR" in body["results"][0]["warnings"]

    detail = client.get(f"/api/v1/notices/{notice_key}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["analysis_state"] == "REVIEW"
    assert detail.json()["analysis_reason_code"] == "OPENAI_REVIEW"
    assert detail.json()["analysis_attempted"] is True

    cooled_at = datetime.now(timezone.utc) - timedelta(hours=25)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        assert notice is not None
        attempts = [
            version
            for version in notice.versions
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
        ]
        assert len(attempts) == 1
        assert attempts[0].source_payload["error_code"] == "INTERNAL_ENRICHMENT_ERROR"
        for version in notice.versions:
            version.created_at = cooled_at
        session.commit()

    retry = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [notice_key],
            "retry_notice_keys": [notice_key],
            "retry_epoch": "2026-08-20",
            "dry_run": True,
            "include_retryable": True,
            "chunk_size": 1,
            "execution_limit": 1,
            "retry_cooldown_hours": 24,
        },
    )
    assert retry.status_code == 200, retry.text
    plan = retry.json()
    assert plan["planned"] == 1
    assert plan["offered"] == 1
    assert plan["notice_keys"] == [notice_key]
    assert not any(
        warning.startswith("RETRY_KEYS_NOT_ELIGIBLE")
        for warning in plan["warnings"]
    )


def test_dry_run_unexpected_enrichment_error_never_persists_attempt_marker(
    client: TestClient,
    monkeypatch,
) -> None:
    notice_key = "PPS-DRY-RUN-INTERNAL-FAILURE"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": "R26BK-DRY-RUN-FAILURE",
            "title": "dry-run 무기록 계약 검증",
            "agency": "가상 기관",
            "published_at": "2026-08-19T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    assert client.post(
        f"/api/v1/notices/{notice_key}/versions",
        json={
            "version_no": 1,
            "file_sha256": "d" * 64,
            "source_payload": {
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": [],
            },
        },
    ).status_code == 201

    monkeypatch.setattr(
        "pai_loop.analysis_api._enrich_one_notice",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic")),
    )
    response = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [notice_key],
            "dry_run": True,
            "enrich_missing": True,
            "max_notices": 1,
        },
    )

    assert response.status_code == 200, response.text
    item = response.json()["results"][0]
    assert item["status"] == "SKIPPED"
    assert "INTERNAL_ENRICHMENT_ERROR" in item["warnings"]
    assert "DRY_RUN_NO_WRITES" in item["warnings"]
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        assert notice is not None
        assert len(notice.versions) == 1
        assert session.query(AnalysisRun).count() == 0
        assert not notice.evaluations


def test_backfill_plan_chunks_resumes_and_tracks_child_audits(client: TestClient) -> None:
    for index in range(7):
        response = client.post(
            "/api/v1/notices",
            json={
                "notice_key": f"MANUAL-BACKFILL-{index}",
                "bid_notice_no": f"BACKFILL-{index}",
                "title": f"신규 교육 컨설팅 공고 {index}",
                "agency": "가상 공공기관",
                "published_at": f"2026-08-{10 + index:02d}T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        )
        assert response.status_code == 201

    plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "dry_run": True,
            "chunk_size": 1,
            "max_total": 20,
            "include_retryable": False,
        },
    )
    assert plan.status_code == 200, plan.text
    planned = plan.json()
    assert planned["planned"] == 7
    assert [len(chunk) for chunk in planned["chunks"]] == [1] * 7
    assert planned["chunk_indices"] == list(range(7))
    assert planned["segment_id"]
    assert planned["notice_keys"][0] == "MANUAL-BACKFILL-6"

    overlapping = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "dry_run": True,
            "chunk_size": 1,
            "max_total": 20,
            "include_retryable": False,
        },
    )
    assert overlapping.status_code == 200
    assert overlapping.json()["segment_id"] == planned["segment_id"]
    assert overlapping.json()["offered"] == 0
    assert overlapping.json()["chunks"] == []

    _run_segment(client, planned)

    progress = client.get(
        f"/api/v1/operations/analysis-backfills/{planned['job_id']}"
    )
    assert progress.status_code == 200
    assert progress.json()["attempted"] == 7
    assert progress.json()["remaining"] == 0
    assert progress.json()["offered"] == 0

    final = client.post(
        f"/api/v1/operations/analysis-backfills/{planned['job_id']}/complete",
        json={"segment_id": planned["segment_id"]},
    )
    assert final.status_code == 200
    assert final.json()["remaining"] == 0
    assert final.json()["attempted"] == 7
    assert final.json()["child_jobs"] == 7
    assert final.json()["status"] == "PARTIAL"  # dry-run rows are SKIPPED


def test_backfill_child_rejects_unplanned_key(client: TestClient) -> None:
    response = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "MANUAL-PLANNED",
            "bid_notice_no": "PLANNED",
            "title": "교육 공고",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    )
    assert response.status_code == 201
    plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={"dry_run": True},
    ).json()
    rejected = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": ["PPS-NOT-PLANNED"],
            "dry_run": True,
            "max_notices": 1,
            "operation_id": plan["job_id"],
            "segment_id": plan["segment_id"],
            "chunk_index": plan["chunk_indices"][0],
        },
    )
    assert rejected.status_code == 409


def test_daily_operation_offers_bounded_page_and_persists_continuation(
    client: TestClient,
) -> None:
    keys: list[str] = []
    for index in range(35):
        key = f"MANUAL-DAILY-CONT-{index:02d}"
        keys.append(key)
        response = client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": f"당일 신규 또는 정정 공고 {index}",
                "agency": "가상 공공기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        )
        assert response.status_code == 201

    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": keys,
            "dry_run": True,
            "chunk_size": 1,
            "max_total": 3000,
            "execution_limit": 30,
            "max_continuations": 128,
            "request_token": "w10:execution-a:pps-job-a",
        },
    )
    assert first.status_code == 200, first.text
    plan = first.json()
    assert plan["queue_name"] == "DAILY"
    assert plan["planned"] == 35
    assert plan["remaining"] == 35
    assert plan["offered"] == 30
    assert plan["continuation_required"] is True
    assert len(plan["chunks"]) == 30

    # A lost HTTP response causes the same n8n execution to retry the plan
    # node. The lease owner token must replay the exact response, not turn the
    # execution into zero-work for six hours.
    replayed = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": keys,
            "dry_run": True,
            "chunk_size": 1,
            "max_total": 3000,
            "execution_limit": 30,
            "max_continuations": 128,
            "request_token": "w10:execution-a:pps-job-a",
        },
    ).json()
    assert replayed["segment_id"] == plan["segment_id"]
    assert replayed["offered"] == plan["offered"]
    assert replayed["notice_keys"] == plan["notice_keys"]
    assert replayed["chunks"] == plan["chunks"]
    assert replayed["chunk_indices"] == plan["chunk_indices"]

    overlapping = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "dry_run": True,
            "chunk_size": 1,
            "max_total": 3000,
            "execution_limit": 30,
            "max_continuations": 128,
            "request_token": "w10:execution-b:pps-job-b",
        },
    ).json()
    assert overlapping["segment_id"] == plan["segment_id"]
    assert overlapping["offered"] == 0
    assert overlapping["chunks"] == []

    _run_segment(client, plan)

    partial = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": plan["segment_id"]},
    )
    assert partial.status_code == 200
    assert partial.json()["status"] == "PARTIAL"
    assert partial.json()["attempted"] == 30
    assert partial.json()["remaining"] == 5
    assert partial.json()["offered"] == 0
    assert partial.json()["segment_id"] is None
    assert partial.json()["continuation_round"] == 1

    resumed = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "dry_run": True,
            "chunk_size": 1,
            "max_total": 3000,
            "execution_limit": 30,
            "max_continuations": 128,
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["job_id"] == plan["job_id"]
    assert resumed.json()["notice_keys"] == keys[30:]
    assert resumed.json()["segment_id"] != plan["segment_id"]
    assert resumed.json()["chunk_indices"] == list(range(30, 35))
    assert resumed.json()["continuation_round"] == 2

    _run_segment(client, resumed.json())
    completed = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": resumed.json()["segment_id"]},
    )
    assert completed.status_code == 200
    assert completed.json()["remaining"] == 0
    assert completed.json()["attempted"] == 35


def test_operation_chunk_retry_returns_stored_result_and_rejects_rebinding(
    client: TestClient,
) -> None:
    for key in ("MANUAL-CLAIM-A", "MANUAL-CLAIM-B"):
        response = client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": "분석 claim 테스트 공고",
                "agency": "가상 기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        )
        assert response.status_code == 201
    plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": ["MANUAL-CLAIM-A", "MANUAL-CLAIM-B"],
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 2,
        },
    ).json()
    payload = {
        "notice_keys": ["MANUAL-CLAIM-A"],
        "dry_run": True,
        "enrich_missing": True,
        "max_notices": 1,
        "operation_id": plan["job_id"],
        "segment_id": plan["segment_id"],
        "chunk_index": plan["chunk_indices"][0],
    }
    first = client.post("/api/v1/notices/analysis/batch", json=payload)
    repeated = client.post("/api/v1/notices/analysis/batch", json=payload)
    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    with client.app.state.session_factory() as session:
        children = [
            job
            for job in session.query(IngestionJob).filter_by(source="ANALYSIS").all()
            if job.request_json.get("parent_job_id") == plan["job_id"]
        ]
    assert len(children) == 1

    rebound = client.post(
        "/api/v1/notices/analysis/batch",
        json={**payload, "notice_keys": ["MANUAL-CLAIM-B"]},
    )
    assert rebound.status_code == 409
    overlap = client.post(
        "/api/v1/notices/analysis/batch",
        json={**payload, "chunk_index": 1},
    )
    assert overlap.status_code == 409


def test_daily_updated_key_reopens_only_that_key_with_version_aware_generation(
    client: TestClient,
) -> None:
    keys = [f"MANUAL-REFRESH-GENERATION-{suffix}" for suffix in "ABCD"]
    for key in keys:
        assert client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": f"version-aware refresh {key}",
                "agency": "가상 기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        ).status_code == 201
    assert client.post(
        f"/api/v1/notices/{keys[0]}/versions",
        json={
            "version_no": 1,
            "file_sha256": "1" * 64,
            "source_payload": {
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": [],
            },
        },
    ).status_code == 201

    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": keys,
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 3,
        },
    ).json()
    assert first["notice_keys"] == keys[:3]
    _run_segment(client, first)
    first_complete = client.post(
        f"/api/v1/operations/analysis-backfills/{first['job_id']}/complete",
        json={"segment_id": first["segment_id"]},
    ).json()
    assert first_complete["attempted"] == 3
    assert first_complete["remaining"] == 1

    assert client.post(
        f"/api/v1/notices/{keys[0]}/versions",
        json={
            "version_no": 2,
            "file_sha256": "2" * 64,
            "source_payload": {
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": [{"attachment_id": "corrected-rfp"}],
            },
        },
    ).status_code == 201
    refresh_payload = {
        "queue_name": "DAILY",
        "notice_keys": [keys[0]],
        "refresh_notice_keys": [keys[0]],
        "dry_run": True,
            "chunk_size": 1,
        "execution_limit": 3,
    }
    refreshed = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=refresh_payload,
    )
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()
    assert second["job_id"] == first["job_id"]
    assert second["notice_keys"] == [keys[0], keys[3]]
    assert second["attempted"] == 2
    assert second["remaining"] == 2

    _run_segment(client, second)
    # Simulate the analysis output version a live child appends. This is an
    # output, not a new PPS input, so the same refresh request must not create
    # generation 2 or another child.
    assert client.post(
        f"/api/v1/notices/{keys[0]}/versions",
        json={
            "version_no": 3,
            "file_sha256": "3" * 64,
            "source_payload": {
                "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                "status": "ACCEPTED",
                "manifest_sha256": "2" * 64,
            },
        },
    ).status_code == 201
    overlapping_retry = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=refresh_payload,
    ).json()
    assert overlapping_retry["segment_id"] == second["segment_id"]
    assert overlapping_retry["offered"] == 0
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first["job_id"])
        assert parent is not None
        assert parent.request_json["work_generations"][keys[0]] == 1

    final = client.post(
        f"/api/v1/operations/analysis-backfills/{first['job_id']}/complete",
        json={"segment_id": second["segment_id"]},
    )
    assert final.status_code == 200, final.text
    body = final.json()
    assert body["status"] == "PARTIAL"
    assert body["planned"] == 4
    assert body["attempted"] == 4
    assert body["remaining"] == 0
    assert body["completed"] + body["partial"] + body["failed"] == 4

    with client.app.state.session_factory() as session:
        children = [
            child
            for child in session.scalars(
                select(IngestionJob).where(IngestionJob.source == "ANALYSIS")
            ).all()
            if child.request_json.get("parent_job_id") == first["job_id"]
        ]
    target_children = [child for child in children if keys[0] in child.notice_keys]
    assert len(target_children) == 2
    assert [
        child.request_json["work_generations"][keys[0]]
        for child in target_children
    ] == [0, 1]
    assert sum(keys[1] in child.notice_keys for child in children) == 1
    assert sum(keys[2] in child.notice_keys for child in children) == 1


def test_superseded_stale_running_generation_is_terminalized_before_requeue(
    client: TestClient,
) -> None:
    target = "MANUAL-SUPERSEDED-STALE-A"
    waiting = "MANUAL-SUPERSEDED-STALE-B"
    for key in (target, waiting):
        assert client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": f"superseded stale {key}",
                "agency": "가상 기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        ).status_code == 201
    assert client.post(
        f"/api/v1/notices/{target}/versions",
        json={
            "version_no": 1,
            "file_sha256": "a" * 64,
            "source_payload": {
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": [],
            },
        },
    ).status_code == 201
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [target, waiting],
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 1,
            "reservation_ttl_hours": 1,
        },
    ).json()
    stale_at = datetime.now(timezone.utc) - timedelta(hours=2)
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first["job_id"])
        assert parent is not None
        config = dict(parent.request_json)
        config["lease_started_at"] = stale_at.isoformat()
        parent.request_json = config
        stale_child = IngestionJob(
            source="ANALYSIS",
            mode="DRY_RUN",
            status="RUNNING",
            window_json={"scope": "NOTICE_KEYS"},
            request_json={
                "parent_job_id": first["job_id"],
                "segment_id": first["segment_id"],
                "chunk_index": first["chunk_indices"][0],
                "work_generations": {target: 0},
            },
            matched=1,
            notice_keys=[target],
            created_at=stale_at,
        )
        session.add(stale_child)
        session.commit()
        stale_child_id = stale_child.id

    assert client.post(
        f"/api/v1/notices/{target}/versions",
        json={
            "version_no": 2,
            "file_sha256": "b" * 64,
            "source_payload": {
                "kind": "PPS_NOTICE_METADATA",
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": [{"attachment_id": "new-input"}],
            },
        },
    ).status_code == 201
    refreshed = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [target],
            "refresh_notice_keys": [target],
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 1,
            "reservation_ttl_hours": 1,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    second = refreshed.json()
    assert second["notice_keys"] == [target], second
    with client.app.state.session_factory() as session:
        old_child = session.get(IngestionJob, stale_child_id)
        assert old_child is not None
        assert old_child.status == "FAILED"
        assert old_child.error_code == "SUPERSEDED_STALE_ANALYSIS_CLAIM"
        assert old_child.completed_at is not None
        assert old_child.request_json["requeue_notice_keys"] == [target]

    _run_segment(client, second)
    with client.app.state.session_factory() as session:
        old_child = session.get(IngestionJob, stale_child_id)
        assert old_child is not None
        old_config = dict(old_child.request_json)
        old_config["segment_id"] = second["segment_id"]
        old_config["chunk_index"] = second["chunk_indices"][0]
        old_child.request_json = old_config
        session.commit()

    exact_retry = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [target],
            "dry_run": True,
            "enrich_missing": True,
            "max_notices": 1,
                "max_attachments_per_notice": 10,
            "operation_id": first["job_id"],
            "segment_id": second["segment_id"],
            "chunk_index": second["chunk_indices"][0],
        },
    )
    assert exact_retry.status_code == 200, exact_retry.text
    with client.app.state.session_factory() as session:
        children = [
            child
            for child in session.scalars(
                select(IngestionJob).where(IngestionJob.source == "ANALYSIS")
            ).all()
            if child.request_json.get("parent_job_id") == first["job_id"]
        ]
    assert not any(child.status == "RUNNING" for child in children)
    assert [
        child.request_json["work_generations"][target]
        for child in children
        if target in child.notice_keys
    ] == [0, 1]


def test_cooled_retry_key_reopens_once_inside_active_daily_parent(
    client: TestClient,
    monkeypatch,
) -> None:
    target = "PPS-COOLED-RETRY-A"
    waiting = "PPS-COOLED-RETRY-B"
    for key in (target, waiting):
        assert client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": f"cooled retry {key}",
                "agency": "가상 기관",
                "published_at": "2026-08-15T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        ).status_code == 201
    version_response = client.post(
        f"/api/v1/notices/{target}/versions",
        json={"version_no": 1, "file_sha256": "c" * 64},
    )
    assert version_response.status_code == 201
    with client.app.state.session_factory() as session:
        version = session.get(NoticeVersion, version_response.json()["id"])
        assert version is not None
        version.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        session.commit()

    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [target, waiting],
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 1,
        },
    ).json()
    _run_segment(client, first)
    first_complete = client.post(
        f"/api/v1/operations/analysis-backfills/{first['job_id']}/complete",
        json={"segment_id": first["segment_id"]},
    ).json()
    assert first_complete["remaining"] == 1

    monkeypatch.setattr(
        "pai_loop.analysis_api.public_analysis_reason",
        lambda *_args, **_kwargs: SimpleNamespace(
            state="REVIEW",
            reason_code="OPENAI_REVIEW",
            reason="cooldown retry test",
        ),
    )
    retry_payload = {
        "queue_name": "DAILY",
        "notice_keys": [target],
        "retry_notice_keys": [target],
        "retry_epoch": "2026-08-18",
        "dry_run": True,
        "chunk_size": 1,
        "execution_limit": 1,
        "retry_cooldown_hours": 24,
    }
    retried = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=retry_payload,
    )
    assert retried.status_code == 200, retried.text
    second = retried.json()
    assert second["job_id"] == first["job_id"]
    assert second["notice_keys"] == [target]
    assert second["attempted"] == 0
    assert second["remaining"] == 2

    repeated = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=retry_payload,
    ).json()
    assert repeated["segment_id"] == second["segment_id"]
    assert repeated["offered"] == 0
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first["job_id"])
        assert parent is not None
        assert parent.request_json["work_generations"][target] == 1
        assert parent.request_json["retry_tokens"][target] == "2026-08-18"

    _run_segment(client, second)
    after_terminal_retry = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=retry_payload,
    ).json()
    assert after_terminal_retry["segment_id"] == second["segment_id"]
    assert after_terminal_retry["offered"] == 0
    with client.app.state.session_factory() as session:
        children = [
            child
            for child in session.scalars(
                select(IngestionJob).where(IngestionJob.source == "ANALYSIS")
            ).all()
            if child.request_json.get("parent_job_id") == first["job_id"]
            and target in child.notice_keys
        ]
    assert len(children) == 2
    assert [
        child.request_json["work_generations"][target] for child in children
    ] == [0, 1]


def test_new_daily_parent_keeps_mislabeled_not_selected_backlog_as_generation_zero(
    client: TestClient,
) -> None:
    target = "MANUAL-NEVER-ATTEMPTED-BACKLOG"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": target,
            "bid_notice_no": target,
            "title": "미시도 backlog 계약 검증",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201

    response = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [target],
            # Compatibility check for the pre-partition W10 payload: a
            # NOT_SELECTED key must remain ordinary gen-0 work, not disappear.
            "retry_notice_keys": [target],
            "retry_epoch": "2026-08-17",
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 1,
        },
    )

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["planned"] == 1
    assert plan["offered"] == 1
    assert plan["notice_keys"] == [target]
    assert not any(
        warning.startswith("RETRY_KEYS_NOT_ELIGIBLE")
        for warning in plan["warnings"]
    )
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, plan["job_id"])
        assert parent is not None
        assert parent.request_json["work_generations"][target] == 0
        assert target not in parent.request_json["retry_tokens"]


def test_legacy_analyzed_without_snapshot_is_retried_once_with_zero_openai_calls(
    client: TestClient,
) -> None:
    _seed_public_notice(client)
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        notice = session.scalar(
            select(Notice).where(Notice.notice_key == NOTICE_KEY)
        )
        assert notice is not None
        notice.status = "OPEN"
        notice.published_at = now - timedelta(days=1)
        notice.deadline = now + timedelta(days=14)
        for version in notice.versions:
            version.created_at = now - timedelta(hours=25)
        session.commit()

    evaluated = client.post(
        f"/api/v1/notices/{NOTICE_KEY}/evaluate",
        json={"ruleset_version": "legacy-without-analysis-run"},
    )
    assert evaluated.status_code == 201, evaluated.text
    with client.app.state.session_factory() as session:
        assert session.query(AnalysisRun).count() == 0

    payload = {
        "queue_name": "DAILY",
        "notice_keys": [NOTICE_KEY],
        "retry_notice_keys": [NOTICE_KEY],
        "retry_epoch": "2026-08-17",
        "dry_run": False,
        "chunk_size": 1,
        "execution_limit": 1,
        "retry_cooldown_hours": 24,
    }
    planned = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["planned"] == 1
    assert plan["offered"] == 1

    analysed = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [NOTICE_KEY],
            "dry_run": False,
            "enrich_missing": True,
            "max_notices": 1,
                "max_attachments_per_notice": 10,
            "operation_id": plan["job_id"],
            "segment_id": plan["segment_id"],
            "chunk_index": plan["chunk_indices"][0],
        },
    )
    assert analysed.status_code == 200, analysed.text
    result = analysed.json()
    assert result["completed"] == 1
    assert result["openai_calls"] == 0
    assert result["snapshots_refreshed"] == 1

    # Same epoch/workflow retry sees the active durable lease and must not
    # create another generation or another analysis child.
    repeated = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["job_id"] == plan["job_id"]
    assert repeated.json()["offered"] == 0
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, plan["job_id"])
        assert parent is not None
        assert parent.request_json["work_generations"][NOTICE_KEY] == 0
        assert parent.request_json["retry_tokens"][NOTICE_KEY] == "2026-08-17"
        children = [
            child
            for child in session.scalars(
                select(IngestionJob).where(IngestionJob.source == "ANALYSIS")
            ).all()
            if child.request_json.get("parent_job_id") == plan["job_id"]
        ]
        assert len(children) == 1


def test_completed_daily_parent_dedupes_same_epoch_retry_after_pipeline_failure(
    client: TestClient,
    monkeypatch,
) -> None:
    _seed_public_notice(client)
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        notice = session.scalar(
            select(Notice).where(Notice.notice_key == NOTICE_KEY)
        )
        assert notice is not None
        notice.status = "OPEN"
        notice.deadline = now + timedelta(days=14)
        for version in notice.versions:
            version.created_at = now - timedelta(hours=25)
        session.commit()

    monkeypatch.setattr(
        "pai_loop.analysis_api.public_analysis_reason",
        lambda *_args, **_kwargs: SimpleNamespace(
            state="REVIEW",
            reason_code="OPENAI_REVIEW",
            reason="persistent synthetic retry failure",
        ),
    )
    pipeline_calls = 0

    def fail_pipeline(*_args, **_kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        raise AnalysisPipelineError("synthetic persistent failure")

    monkeypatch.setattr("pai_loop.analysis_api.run_analysis_pipeline", fail_pipeline)
    payload = {
        "queue_name": "DAILY",
        "notice_keys": [NOTICE_KEY],
        "retry_notice_keys": [NOTICE_KEY],
        "retry_epoch": "2026-08-17",
        "dry_run": False,
        "chunk_size": 1,
        "execution_limit": 1,
        "retry_cooldown_hours": 24,
    }
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )
    assert first.status_code == 200, first.text
    first_plan = first.json()
    analysed = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [NOTICE_KEY],
            "dry_run": False,
            "enrich_missing": True,
            "max_notices": 1,
                "max_attachments_per_notice": 10,
            "operation_id": first_plan["job_id"],
            "segment_id": first_plan["segment_id"],
            "chunk_index": first_plan["chunk_indices"][0],
        },
    )
    assert analysed.status_code == 200, analysed.text
    assert analysed.json()["failed"] == 1
    assert analysed.json()["openai_calls"] == 0
    assert pipeline_calls == 1
    completed = client.post(
        f"/api/v1/operations/analysis-backfills/{first_plan['job_id']}/complete",
        json={"segment_id": first_plan["segment_id"]},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["remaining"] == 0

    repeated = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json=payload,
    )
    assert repeated.status_code == 200, repeated.text
    repeated_plan = repeated.json()
    assert repeated_plan["planned"] == 0
    assert repeated_plan["offered"] == 0
    assert "RETRY_EPOCH_ALREADY_CONSUMED:1" in repeated_plan["warnings"]
    assert pipeline_calls == 1
    with client.app.state.session_factory() as session:
        children = list(
            session.scalars(
                select(IngestionJob).where(IngestionJob.source == "ANALYSIS")
            ).all()
        )
        assert len(children) == 1


def test_resume_only_poll_without_active_parent_creates_no_audit(client: TestClient) -> None:
    for _ in range(2):
        response = client.post(
            "/api/v1/operations/analysis-backfills/plan",
            json={
                "queue_name": "ANY",
                "resume_only": True,
                "resume_active": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "NO_ACTIVE"
        assert response.json()["job_id"] is None
    with client.app.state.session_factory() as session:
        assert session.query(IngestionJob).filter_by(source="ANALYSIS_BACKFILL").count() == 0


def test_running_child_is_in_flight_and_prevents_parent_completion(
    client: TestClient,
) -> None:
    keys = ["MANUAL-IN-FLIGHT", "MANUAL-WAITING-BEHIND-FLIGHT"]
    for key in keys:
        response = client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": "진행 중 claim 공고",
                "agency": "가상 기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        )
        assert response.status_code == 201
    plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": keys,
            "dry_run": True,
            "chunk_size": 1,
            "execution_limit": 2,
        },
    ).json()
    with client.app.state.session_factory() as session:
        session.add(
            IngestionJob(
                source="ANALYSIS",
                mode="DRY_RUN",
                status="RUNNING",
                window_json={"scope": "NOTICE_KEYS"},
                request_json={
                    "parent_job_id": plan["job_id"],
                    "segment_id": plan["segment_id"],
                    "chunk_index": plan["chunk_indices"][0],
                },
                matched=1,
                notice_keys=["MANUAL-IN-FLIGHT"],
            )
        )
        session.commit()
    duplicate_claim = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": ["MANUAL-IN-FLIGHT"],
            "dry_run": True,
            "max_notices": 1,
            "operation_id": plan["job_id"],
            "segment_id": plan["segment_id"],
            "chunk_index": plan["chunk_indices"][0],
        },
    )
    assert duplicate_claim.status_code == 409
    assert duplicate_claim.json()["detail"] == "analysis chunk is already in flight"
    progress = client.get(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}"
    ).json()
    assert progress["attempted"] == 0
    assert progress["remaining"] == 2
    assert progress["in_flight"] == 1
    assert progress["offered"] == 0
    completed = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": plan["segment_id"]},
    )
    assert completed.status_code == 409
    assert completed.json()["detail"]["code"] == "ANALYSIS_SEGMENT_NOT_TERMINAL"
    retained = client.get(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}"
    ).json()
    assert retained["segment_id"] == plan["segment_id"]
    assert retained["remaining"] == 2
    assert retained["in_flight"] == 1


def test_any_continuation_poll_prioritises_daily_over_backfill(client: TestClient) -> None:
    for key in ("MANUAL-OLD-BACKFILL", "MANUAL-TODAY-DAILY"):
        assert client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": "우선순위 테스트 공고",
                "agency": "가상 기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        ).status_code == 201
    backfill = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "BACKFILL",
            "notice_keys": ["MANUAL-OLD-BACKFILL"],
            "dry_run": True,
        },
    ).json()
    daily = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": ["MANUAL-TODAY-DAILY"],
            "dry_run": True,
        },
    ).json()
    assert backfill["job_id"] != daily["job_id"]
    polled = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "ANY",
            "resume_only": True,
            "dry_run": True,
        },
    )
    assert polled.status_code == 200
    assert polled.json()["queue_name"] == "DAILY"
    assert polled.json()["job_id"] == daily["job_id"]


def test_any_plan_response_retry_prefers_exact_lease_owner_over_new_daily_parent(
    client: TestClient,
) -> None:
    backfill_keys = ["MANUAL-OWNER-BACKFILL-A", "MANUAL-OWNER-BACKFILL-B"]
    daily_key = "MANUAL-OWNER-DAILY"
    for key in [*backfill_keys, daily_key]:
        assert client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": f"lease owner priority {key}",
                "agency": "가상 기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        ).status_code == 201
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "BACKFILL",
            "notice_keys": backfill_keys,
            "dry_run": True,
            "execution_limit": 1,
            "request_token": "w11:manual-owner-setup",
        },
    ).json()
    _run_segment(client, first)
    completed = client.post(
        f"/api/v1/operations/analysis-backfills/{first['job_id']}/complete",
        json={"segment_id": first["segment_id"]},
    )
    assert completed.status_code == 200
    assert completed.json()["remaining"] == 1

    lost_response_plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "ANY",
            "resume_only": True,
            "dry_run": True,
            "execution_limit": 1,
            "request_token": "w11:lost-response-owner",
        },
    ).json()
    assert lost_response_plan["job_id"] == first["job_id"]
    assert lost_response_plan["notice_keys"] == [backfill_keys[1]]

    daily = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [daily_key],
            "dry_run": True,
            "execution_limit": 1,
            "request_token": "w10:new-daily-parent",
        },
    ).json()
    assert daily["job_id"] != first["job_id"]

    exact_retry = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "ANY",
            "resume_only": True,
            "dry_run": True,
            "execution_limit": 1,
            "request_token": "w11:lost-response-owner",
        },
    )
    assert exact_retry.status_code == 200, exact_retry.text
    replayed = exact_retry.json()
    assert replayed["queue_name"] == "BACKFILL"
    assert replayed["job_id"] == lost_response_plan["job_id"]
    assert replayed["segment_id"] == lost_response_plan["segment_id"]
    assert replayed["offered"] == lost_response_plan["offered"]
    assert replayed["chunks"] == lost_response_plan["chunks"]
    assert replayed["chunk_indices"] == lost_response_plan["chunk_indices"]


def test_stale_segment_lease_is_recovered_without_accepting_old_claim(
    client: TestClient,
) -> None:
    key = "MANUAL-STALE-LEASE"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": key,
            "bid_notice_no": key,
            "title": "stale lease 복구 테스트",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [key],
            "dry_run": True,
            "reservation_ttl_hours": 1,
        },
    ).json()
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first["job_id"])
        assert parent is not None
        config = dict(parent.request_json)
        config["lease_started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        parent.request_json = config
        session.commit()

    recovered_response = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "dry_run": True,
            "reservation_ttl_hours": 1,
        },
    )
    assert recovered_response.status_code == 200, recovered_response.text
    recovered = recovered_response.json()
    assert recovered["job_id"] == first["job_id"]
    assert recovered["segment_id"] != first["segment_id"]
    assert recovered["notice_keys"] == [key]
    assert recovered["chunk_indices"] == [1]
    assert "STALE_LEASE_RECOVERED" in recovered["warnings"]

    old_claim = client.post(
        "/api/v1/notices/analysis/batch",
        json={
            "notice_keys": [key],
            "dry_run": True,
            "max_notices": 1,
            "operation_id": first["job_id"],
            "segment_id": first["segment_id"],
            "chunk_index": first["chunk_indices"][0],
        },
    )
    assert old_claim.status_code == 409
    _run_segment(client, recovered)


def test_complete_requires_exact_current_segment_and_all_terminal_chunks(
    client: TestClient,
) -> None:
    key = "MANUAL-EXACT-FINALIZE"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": key,
            "bid_notice_no": key,
            "title": "exact finalize 테스트",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    plan = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [key],
            "dry_run": True,
        },
    ).json()
    wrong = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert wrong.status_code == 409
    premature = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": plan["segment_id"]},
    )
    assert premature.status_code == 409
    assert premature.json()["detail"]["lease_retained"] is True
    _run_segment(client, plan)
    final = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": plan["segment_id"]},
    )
    assert final.status_code == 200
    assert final.json()["remaining"] == 0
    assert final.json()["segment_id"] is None
    with client.app.state.session_factory() as session:
        parent_before_retry = session.get(IngestionJob, plan["job_id"])
        assert parent_before_retry is not None
        immutable_before_retry = {
            "status": parent_before_retry.status,
            "completed_at": parent_before_retry.completed_at,
            "request_json": dict(parent_before_retry.request_json),
        }
        assert parent_before_retry.request_json["last_finalized_aggregate"][
            "attempted"
        ] == 1

    exact_retry = client.post(
        f"/api/v1/operations/analysis-backfills/{plan['job_id']}/complete",
        json={"segment_id": plan["segment_id"]},
    )
    assert exact_retry.status_code == 200, exact_retry.text
    for field in (
        "job_id",
        "status",
        "planned",
        "attempted",
        "remaining",
        "completed",
        "partial",
        "failed",
        "child_jobs",
        "openai_calls",
    ):
        assert exact_retry.json()[field] == final.json()[field]
    with client.app.state.session_factory() as session:
        parent_after_retry = session.get(IngestionJob, plan["job_id"])
        assert parent_after_retry is not None
        assert parent_after_retry.status == immutable_before_retry["status"]
        assert parent_after_retry.completed_at == immutable_before_retry["completed_at"]
        assert parent_after_retry.request_json == immutable_before_retry["request_json"]


def test_stale_segments_are_bounded_by_continuation_dead_letter(
    client: TestClient,
) -> None:
    key = "MANUAL-BOUNDED-STALE"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": key,
            "bid_notice_no": key,
            "title": "bounded stale segment 테스트",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [key],
            "dry_run": True,
            "reservation_ttl_hours": 1,
            "max_continuations": 1,
        },
    ).json()
    assert first["continuation_round"] == 1
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first["job_id"])
        assert parent is not None
        config = dict(parent.request_json)
        config["lease_started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        parent.request_json = config
        session.commit()

    stopped = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "dry_run": True,
            "reservation_ttl_hours": 1,
            "max_continuations": 1,
        },
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "DEAD_LETTER"
    assert stopped.json()["offered"] == 0
    assert stopped.json()["continuation_round"] == 1
    assert "MAX_CONTINUATIONS_EXCEEDED" in stopped.json()["warnings"]
    no_active = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={"queue_name": "ANY", "resume_only": True, "dry_run": True},
    )
    assert no_active.status_code == 200
    assert no_active.json()["status"] == "NO_ACTIVE"


def test_stale_terminal_segment_auto_finalizes_parent_and_releases_any_poll(
    client: TestClient,
) -> None:
    key = "MANUAL-STALE-TERMINAL-FINALIZE"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": key,
            "bid_notice_no": key,
            "title": "stale terminal 자동 완료 테스트",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    first = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [key],
            "dry_run": True,
            "reservation_ttl_hours": 1,
        },
    ).json()
    _run_segment(client, first)
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first["job_id"])
        assert parent is not None
        config = dict(parent.request_json)
        config["lease_started_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        parent.request_json = config
        session.commit()

    recovered = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "ANY",
            "resume_only": True,
            "dry_run": True,
            "reservation_ttl_hours": 1,
        },
    )
    assert recovered.status_code == 200, recovered.text
    body = recovered.json()
    assert body["job_id"] == first["job_id"]
    assert body["status"] == "PARTIAL"
    assert body["remaining"] == 0
    assert body["in_flight"] == 0
    assert body["offered"] == 0
    assert body["segment_id"] is None
    assert body["continuation_required"] is False
    assert "STALE_SEGMENT_AUTO_FINALIZED" in body["warnings"]
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, first["job_id"])
        assert parent is not None
        assert parent.completed_at is not None

    no_active = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={"queue_name": "ANY", "resume_only": True, "dry_run": True},
    )
    assert no_active.status_code == 200
    assert no_active.json()["status"] == "NO_ACTIVE"


def test_maximum_daily_plan_reaches_zero_within_128_continuations(
    client: TestClient,
) -> None:
    planned_count = 3012
    execution_limit = 30
    required_rounds = (planned_count + execution_limit - 1) // execution_limit
    assert required_rounds == 101
    assert required_rounds <= 128
    parent_id = "30030000-0000-4000-8000-000000000001"
    keys = [f"SYNTHETIC-DAILY-BOUNDARY-{index:04d}" for index in range(planned_count)]
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        session.add(
            IngestionJob(
                id=parent_id,
                source="ANALYSIS_BACKFILL",
                mode="DRY_RUN",
                status="COMPLETED",
                window_json={"scope": "SYNTHETIC_BOUNDARY"},
                request_json={
                    "queue_name": "DAILY",
            "chunk_size": 1,
                    "execution_limit": execution_limit,
                    "max_continuations": 128,
                    "continuation_round": required_rounds,
                    "reservation_ttl_hours": 6,
                },
                fetched=planned_count,
                matched=planned_count,
                created_count=planned_count,
                notice_keys=keys,
                warnings=[],
                completed_at=now,
            )
        )
        for chunk_index, start in enumerate(range(0, planned_count, 3)):
            chunk = keys[start : start + 3]
            session.add(
                IngestionJob(
                    source="ANALYSIS",
                    mode="DRY_RUN",
                    status="COMPLETED",
                    window_json={"scope": "NOTICE_KEYS"},
                    request_json={
                        "parent_job_id": parent_id,
                        "segment_id": f"synthetic-{chunk_index // 10}",
                        "chunk_index": chunk_index,
                        "result_json": {"openai_calls": 0},
                    },
                    fetched=len(chunk),
                    matched=len(chunk),
                    created_count=len(chunk),
                    notice_keys=chunk,
                    warnings=[],
                    completed_at=now,
                )
            )
        session.commit()

    response = client.get(f"/api/v1/operations/analysis-backfills/{parent_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["planned"] == planned_count
    assert body["attempted"] == planned_count
    assert body["remaining"] == 0
    assert body["continuation_round"] == required_rounds
    assert body["max_continuations"] == 128
    assert body["continuation_required"] is False
    assert "MAX_CONTINUATIONS_EXCEEDED" not in body["warnings"]


def test_concurrent_planners_share_one_parent_and_one_segment(
    client: TestClient,
) -> None:
    key = "MANUAL-CONCURRENT-PARENT"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": key,
            "bid_notice_no": key,
            "title": "동시 parent 생성 방지 테스트",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    barrier = Barrier(2)

    def reserve() -> tuple[int, dict]:
        barrier.wait(timeout=5)
        response = client.post(
            "/api/v1/operations/analysis-backfills/plan",
            json={
                "queue_name": "DAILY",
                "notice_keys": [key],
                "dry_run": True,
                "execution_limit": 1,
            },
        )
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _index: reserve(), range(2)))

    assert [status for status, _body in responses] == [200, 200]
    bodies = [body for _status, body in responses]
    assert len({body["job_id"] for body in bodies}) == 1
    assert len({body["segment_id"] for body in bodies}) == 1
    assert sorted(body["offered"] for body in bodies) == [0, 1]
    assert sum(len(body["chunks"]) for body in bodies) == 1
    with client.app.state.session_factory() as session:
        parents = session.scalars(
            select(IngestionJob).where(IngestionJob.source == "ANALYSIS_BACKFILL")
        ).all()
    assert len(parents) == 1
    assert parents[0].notice_keys == [key]


def test_concurrent_complete_and_daily_plan_never_append_to_terminal_parent(
    client: TestClient,
) -> None:
    old_key = "MANUAL-COMPLETE-RACE-OLD"
    new_key = "MANUAL-COMPLETE-RACE-NEW"
    for key in (old_key, new_key):
        assert client.post(
            "/api/v1/notices",
            json={
                "notice_key": key,
                "bid_notice_no": key,
                "title": f"complete-plan race {key}",
                "agency": "가상 기관",
                "published_at": "2026-08-17T08:00:00+09:00",
                "deadline": "2026-08-31T18:00:00+09:00",
                "status": "OPEN",
            },
        ).status_code == 201
    initial = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [old_key],
            "dry_run": True,
            "execution_limit": 1,
            "request_token": "w10:race-initial",
        },
    ).json()
    _run_segment(client, initial)
    barrier = Barrier(2)

    def complete_old() -> tuple[str, int, dict]:
        barrier.wait(timeout=5)
        response = client.post(
            f"/api/v1/operations/analysis-backfills/{initial['job_id']}/complete",
            json={"segment_id": initial["segment_id"]},
        )
        return "complete", response.status_code, response.json()

    def plan_new() -> tuple[str, int, dict]:
        barrier.wait(timeout=5)
        response = client.post(
            "/api/v1/operations/analysis-backfills/plan",
            json={
                "queue_name": "DAILY",
                "notice_keys": [new_key],
                "dry_run": True,
                "execution_limit": 1,
                "request_token": "w10:race-new",
            },
        )
        return "plan", response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        complete_future = executor.submit(complete_old)
        plan_future = executor.submit(plan_new)
        results = {
            row[0]: row for row in (complete_future.result(), plan_future.result())
        }
    assert results["complete"][1] == 200
    assert results["plan"][1] == 200

    with client.app.state.session_factory() as session:
        parents = list(
            session.scalars(
                select(IngestionJob).where(
                    IngestionJob.source == "ANALYSIS_BACKFILL"
                )
            ).all()
        )
        containing_new = [
            parent for parent in parents if new_key in (parent.notice_keys or [])
        ]
        assert len(containing_new) == 1
        assert containing_new[0].completed_at is None
        assert containing_new[0].status in {"RUNNING", "PARTIAL"}

    plan_body = results["plan"][2]
    if plan_body["offered"] == 0:
        plan_body = client.post(
            "/api/v1/operations/analysis-backfills/plan",
            json={
                "queue_name": "DAILY",
                "dry_run": True,
                "execution_limit": 1,
                "request_token": "w11:race-continuation",
            },
        ).json()
    assert plan_body["offered"] == 1
    assert plan_body["notice_keys"] == [new_key]


def test_cross_queue_planner_does_not_duplicate_pending_notice_key(
    client: TestClient,
) -> None:
    key = "MANUAL-CROSS-QUEUE-CLAIM"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": key,
            "bid_notice_no": key,
            "title": "queue 간 중복 parent key 방지 테스트",
            "agency": "가상 기관",
            "published_at": "2026-08-17T08:00:00+09:00",
            "deadline": "2026-08-31T18:00:00+09:00",
            "status": "OPEN",
        },
    ).status_code == 201
    backfill = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "BACKFILL",
            "notice_keys": [key],
            "dry_run": True,
        },
    )
    assert backfill.status_code == 200
    assert backfill.json()["notice_keys"] == [key]

    daily = client.post(
        "/api/v1/operations/analysis-backfills/plan",
        json={
            "queue_name": "DAILY",
            "notice_keys": [key],
            "dry_run": True,
        },
    )
    assert daily.status_code == 200
    assert daily.json()["planned"] == 0
    assert daily.json()["notice_keys"] == []
    with client.app.state.session_factory() as session:
        parents = session.scalars(
            select(IngestionJob).where(IngestionJob.source == "ANALYSIS_BACKFILL")
        ).all()
    assert sum(key in (parent.notice_keys or []) for parent in parents) == 1
