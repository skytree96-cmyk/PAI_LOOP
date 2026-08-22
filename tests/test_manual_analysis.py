from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from pai_loop.analysis_api import AnalysisBatchResponse
from pai_loop.integrations.openai_extraction import (
    OpenAIAttemptTelemetry,
    OpenAIProviderUsage,
    OpenAITelemetry,
    aggregate_openai_attempts,
)
from pai_loop.main import create_app
from pai_loop.models import IngestionJob
from pai_loop.pps_enrichment import PublicAnalysisReason


SERVER_HEADERS = {"X-PAI-LOOP-API-KEY": "server-only-secret"}
SAME_ORIGIN_HEADERS = {
    "Origin": "http://testserver",
    "Sec-Fetch-Site": "same-origin",
}


def _app(monkeypatch, *, enabled: bool = True, openai_configured: bool = True):
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    monkeypatch.setenv(
        "PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_ENABLED",
        "true" if enabled else "false",
    )
    monkeypatch.setenv("PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_HOURLY_LIMIT", "12")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_COOLDOWN_HOURS", "24")
    if openai_configured:
        monkeypatch.setenv("OPENAI_API_KEY", "test-server-only-openai-key")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return create_app(database_url="sqlite:///:memory:", seed_synthetic=False)


def _create_open_pps_notice(client: TestClient, notice_key: str = "PPS-MANUAL-001") -> None:
    response = client.post(
        "/api/v1/notices",
        headers=SERVER_HEADERS,
        json={
            "notice_key": notice_key,
            "bid_notice_no": "R26BK-MANUAL-001",
            "revision_no": "00",
            "title": "수동 분석 계약 테스트 공고",
            "agency": "공공기관",
            "deadline": "2027-01-01T09:00:00+09:00",
            "status": "OPEN",
        },
    )
    assert response.status_code == 201, response.text


def _review_batch(
    job_id: str = "batch-job-public-manual",
    *,
    openai_telemetry: OpenAITelemetry | None = None,
    continuation: bool = False,
) -> AnalysisBatchResponse:
    telemetry = openai_telemetry or OpenAITelemetry()
    warnings = ["ATTACHMENT_CONTINUATION_REQUIRED"] if continuation else []
    return AnalysisBatchResponse.model_validate(
        {
            "job_id": job_id,
            "status": "COMPLETED",
            "dry_run": False,
            "requested": 1,
            "processed": 1,
            "completed": 0,
            "skipped": 1,
            "failed": 0,
            "document_materialized": 0,
            "evaluations_created": 0,
            "snapshots_refreshed": 0,
            "openai_calls": telemetry.api_calls,
            "openai_telemetry": telemetry.model_dump(mode="json"),
            "results": [
                {
                    "notice_key": "PPS-MANUAL-001",
                    "status": "SKIPPED",
                    "document_status": "ATTACHMENT_NONE",
                    "evaluation_status": "SKIPPED",
                    "snapshot_status": "SKIPPED",
                    "analysis_state": "REVIEW",
                    "analysis_reason_code": "ATTACHMENT_NONE",
                    "analysis_reason": "공개 첨부를 찾지 못했습니다.",
                }
            ],
            "warnings": warnings,
            "enrichment": {
                "requested": 1,
                "attempted": 1,
                "completed": 0,
                "skipped": 1,
                "failed": 0,
                "attachments_discovered": 0,
                "attachments_processed": 0,
                "openai_calls": telemetry.api_calls,
                "openai_telemetry": telemetry.model_dump(mode="json"),
                "warnings": warnings,
            },
        }
    )


def test_public_manual_analysis_is_same_origin_single_notice_and_idempotent(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)
    calls = []

    def fake_batch(payload, request):
        calls.append((payload, request))
        return _review_batch()

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)

        unprotected_batch = client.post(
            "/api/v1/notices/analysis/batch",
            json={
                "notice_keys": ["PPS-MANUAL-001"],
                "enrich_missing": True,
                "max_notices": 1,
                "max_attachments_per_notice": 10,
            },
        )
        assert unprotected_batch.status_code == 401

        missing_origin = client.post("/api/v1/notices/PPS-MANUAL-001/analysis/request")
        assert missing_origin.status_code == 403
        cross_origin = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers={"Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
        )
        assert cross_origin.status_code == 403
        cross_scheme = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers={"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        assert cross_scheme.status_code == 403
        cross_port = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers={"Origin": "http://testserver:8080", "Sec-Fetch-Site": "same-origin"},
        )
        assert cross_port.status_code == 403

        first = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert first.status_code == 200, first.text
        assert first.json()["outcome"] == "QUEUED"
        request_id = first.json()["request_id"]
        assert request_id
        assert "api_key" not in first.text.casefold()
        assert len(calls) == 1
        payload = calls[0][0]
        assert payload.notice_keys == ["PPS-MANUAL-001"]
        assert payload.dry_run is False
        assert payload.force is False
        assert payload.enrich_missing is True
        assert payload.max_notices == 1
        assert payload.max_attachments_per_notice == 10

        completed = client.get(
            f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{request_id}",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["outcome"] == "REVIEW"
        assert completed.json()["openai_calls"] == 0

        repeated = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert repeated.status_code == 200
        assert repeated.json()["outcome"] == "COOLDOWN"
        assert len(calls) == 1

        with app.state.session_factory() as session:
            jobs = list(
                session.query(IngestionJob)
                .filter(IngestionJob.source == "MANUAL_ANALYSIS")
                .all()
            )
            assert len(jobs) == 1
            assert jobs[0].status == "COMPLETED"
            assert jobs[0].notice_keys == ["PPS-MANUAL-001"]
            assert jobs[0].request_json["credential_exposed"] is False
            assert "test-server-only-openai-key" not in str(jobs[0].request_json)


def test_manual_async_result_aggregates_continuations_behind_same_origin(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)
    first_telemetry = aggregate_openai_attempts(
        [
            OpenAIAttemptTelemetry(
                attempt=1,
                request_latency_ms=410,
                response_received=True,
                usage=OpenAIProviderUsage(
                    input_tokens=2_000,
                    cached_input_tokens=500,
                    output_tokens=400,
                    reasoning_output_tokens=100,
                    total_tokens=2_400,
                ),
            )
        ]
    )
    second_telemetry = aggregate_openai_attempts(
        [
            OpenAIAttemptTelemetry(
                attempt=1,
                request_latency_ms=590,
                response_received=True,
                usage=OpenAIProviderUsage(
                    input_tokens=3_000,
                    cached_input_tokens=0,
                    output_tokens=600,
                    reasoning_output_tokens=150,
                    total_tokens=3_600,
                ),
            )
        ]
    )
    batches = [
        _review_batch(
            "batch-job-public-manual-1",
            openai_telemetry=first_telemetry,
            continuation=True,
        ),
        _review_batch(
            "batch-job-public-manual-2",
            openai_telemetry=second_telemetry,
        ),
    ]

    def fake_batch(_payload, _request):
        return batches.pop(0)

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert queued.status_code == 200, queued.text
        request_id = queued.json()["request_id"]

        blocked = client.get(
            f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{request_id}",
            headers={"Origin": "https://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
        )
        assert blocked.status_code == 403
        completed = client.get(
            f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{request_id}",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert completed.status_code == 200, completed.text
        body = completed.json()
        telemetry = body["openai_telemetry"]
        assert body["outcome"] == "REVIEW"
        assert body["openai_calls"] == telemetry["api_calls"] == 2
        assert telemetry["usage_reported_calls"] == 2
        assert telemetry["usage_unreported_calls"] == 0
        assert telemetry["input_tokens"] == 5_000
        assert telemetry["cached_input_tokens"] == 500
        assert telemetry["output_tokens"] == 1_000
        assert telemetry["reasoning_output_tokens"] == 250
        assert telemetry["total_tokens"] == 6_000
        assert telemetry["total_request_latency_ms"] == 1_000
        assert [item["attempt"] for item in telemetry["attempts"]] == [1, 2]
        assert "response_id" not in completed.text
        assert "test-server-only-openai-key" not in completed.text

        with app.state.session_factory() as session:
            job = session.get(IngestionJob, request_id)
            assert job is not None
            assert job.api_calls == 2
            assert job.request_json["openai_telemetry"] == telemetry


def test_public_manual_analysis_reuses_already_analysed_notice_without_batch(
    monkeypatch,
) -> None:
    app = _app(monkeypatch, openai_configured=False)
    # Status/idempotency reads do not depend on the upstream provider being
    # configured.  This keeps a known result available during provider outage.
    monkeypatch.setattr(
        "pai_loop.manual_analysis._reason",
        lambda _notice: PublicAnalysisReason(
            state="ANALYZED",
            reason_code="ANALYZED",
            reason="현재 공고 버전의 분석이 완료되었습니다.",
            attachment_count=1,
            attempted=True,
        ),
    )

    def should_not_run(*_args, **_kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("already analysed notice must not execute a batch")

    monkeypatch.setattr(
        "pai_loop.manual_analysis.run_notice_analysis_batch",
        should_not_run,
    )
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        response = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == "ALREADY_ANALYZED"
        assert response.json()["analysis_attempted"] is True
        assert response.json()["request_id"] is None


def test_public_manual_analysis_feature_fails_closed_and_runtime_is_explicit(
    monkeypatch,
) -> None:
    app = _app(monkeypatch, enabled=False)
    with TestClient(app) as client:
        runtime = client.get("/api/v1/runtime-profile")
        assert runtime.status_code == 200
        assert runtime.json()["manual_analysis_enabled"] is False
        assert runtime.json()["manual_analysis_policy"] is None
        response = client.post(
            "/api/v1/notices/PPS-MISSING/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 404


def test_public_manual_analysis_hourly_quota_is_persisted(monkeypatch) -> None:
    app = _app(monkeypatch)

    def should_not_run(*_args, **_kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("quota exhausted request must not execute a batch")

    monkeypatch.setattr(
        "pai_loop.manual_analysis.run_notice_analysis_batch",
        should_not_run,
    )
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        with app.state.session_factory() as session:
            for index in range(12):
                session.add(
                    IngestionJob(
                        source="MANUAL_ANALYSIS",
                        mode="LIVE",
                        status="COMPLETED",
                        window_json={"scope": "ONE_OPEN_PPS_NOTICE"},
                        request_json={"trigger": "PUBLIC_SAME_ORIGIN"},
                        matched=1,
                        notice_keys=[f"PPS-OTHER-{index:02d}"],
                        completed_at=datetime.now(timezone.utc),
                    )
                )
            session.commit()

        response = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "3600"
