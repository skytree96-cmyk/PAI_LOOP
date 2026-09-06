from __future__ import annotations

import pytest

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pai_loop.analysis_api import AnalysisBatchResponse
from pai_loop.integrations.openai_extraction import (
    OpenAIAttemptTelemetry,
    OpenAIProviderUsage,
    OpenAITelemetry,
    aggregate_openai_attempts,
)
from pai_loop.main import create_app
from pai_loop.manual_analysis import (
    _diagnostic_has_metric_tokens,
    _quantitative_diagnostics,
)
from pai_loop.models import IngestionJob, PpsNoticeAuthority
from pai_loop.pps_enrichment import PublicAnalysisReason


SERVER_HEADERS = {"X-PAI-LOOP-API-KEY": "server-only-secret"}
SAME_ORIGIN_HEADERS = {
    "Origin": "http://testserver",
    "Sec-Fetch-Site": "same-origin",
}
EXTRACTION_ALLOWED = {"run_extraction": True}


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
            json=EXTRACTION_ALLOWED,
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
            json=EXTRACTION_ALLOWED,
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
                model="gpt-5.6-luna",
                service_tier="default",
                usage=OpenAIProviderUsage(
                    input_tokens=2_000,
                    cached_input_tokens=500,
                    cache_write_tokens=250,
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
                model="gpt-5.6-luna",
                service_tier="default",
                usage=OpenAIProviderUsage(
                    input_tokens=3_000,
                    cached_input_tokens=0,
                    cache_write_tokens=0,
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
            json=EXTRACTION_ALLOWED,
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
        assert telemetry["cache_write_tokens"] == 250
        assert telemetry["models"] == ["gpt-5.6-luna"]
        assert telemetry["service_tiers"] == ["default"]
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


def test_manual_batch_exception_marks_cost_accounting_incomplete(monkeypatch) -> None:
    app = _app(monkeypatch)

    def fail_batch(_payload, _request):
        raise RuntimeError("synthetic failure after an unknown provider boundary")

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fail_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json=EXTRACTION_ALLOWED,
        )
        assert queued.status_code == 200, queued.text
        request_id = queued.json()["request_id"]
        completed = client.get(
            f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{request_id}",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert completed.status_code == 200, completed.text
        body = completed.json()
        assert body["outcome"] == "REVIEW"
        assert body["openai_telemetry"]["accounting_complete"] is False


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
    monkeypatch.setattr(
        "pai_loop.manual_analysis.latest_current_evaluation",
        lambda _notice: object(),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._has_complete_current_attachment_audit",
        lambda _request, _notice: True,
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


def test_analysed_notice_with_stale_extraction_contract_can_reextract(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)
    calls = []
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
    monkeypatch.setattr(
        "pai_loop.manual_analysis.latest_current_evaluation",
        lambda _notice: object(),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._has_complete_current_attachment_audit",
        lambda _request, _notice: False,
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._manual_jobs_since",
        lambda *_args, **_kwargs: [],
    )

    def fake_batch(payload, request, *, retry_reviewed_version_ids=frozenset()):
        calls.append((payload, request, retry_reviewed_version_ids))
        return _review_batch("batch-job-stale-extraction-contract")

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": True, "retry_reviewed": True},
        )

    assert queued.status_code == 200, queued.text
    assert queued.json()["outcome"] == "QUEUED"
    assert len(calls) == 1
    assert calls[0][0].enrich_missing is True
    assert calls[0][2] == frozenset()


def test_current_analysis_can_be_recomputed_from_stored_evidence(
    monkeypatch,
) -> None:
    app = _app(monkeypatch, openai_configured=False)
    calls = []
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
    monkeypatch.setattr(
        "pai_loop.manual_analysis.latest_current_evaluation",
        lambda _notice: object(),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._has_complete_current_attachment_audit",
        lambda _request, _notice: True,
    )

    def fake_batch(payload, request):
        calls.append((payload, request))
        return _review_batch("batch-job-recompute-current")

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        invalid = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": True, "recompute_current": True},
        )
        assert invalid.status_code == 422

        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": False, "recompute_current": True},
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["outcome"] == "QUEUED"
        assert len(calls) == 1
        assert calls[0][0].enrich_missing is False

        with app.state.session_factory() as session:
            job = session.get(IngestionJob, queued.json()["request_id"])
            assert job is not None
            assert job.request_json["evaluation_only"] is True
            assert job.request_json["recompute_current"] is True
            assert job.request_json["retry_reviewed"] is False


def test_explicit_review_retry_can_bypass_general_cooldown(monkeypatch) -> None:
    app = _app(monkeypatch)
    calls = []
    retry_version_ids = frozenset({"review-version-before-manual-request"})
    monkeypatch.setattr(
        "pai_loop.manual_analysis._reason",
        lambda _notice: PublicAnalysisReason(
            state="REVIEW",
            reason_code="OPENAI_REVIEW",
            reason="첨부 재검증이 필요합니다.",
            attachment_count=1,
            attempted=True,
        ),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._manual_jobs_since",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.current_retryable_review_version_ids",
        lambda _versions: retry_version_ids,
    )
    batches = [
        _review_batch("batch-job-explicit-reviewed-retry-1", continuation=True),
        _review_batch("batch-job-explicit-reviewed-retry-2"),
    ]

    def fake_batch(payload, request, *, retry_reviewed_version_ids=frozenset()):
        calls.append((payload, request, retry_reviewed_version_ids))
        return batches.pop(0)

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        cooled = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": True},
        )
        assert cooled.status_code == 200, cooled.text
        assert cooled.json()["outcome"] == "COOLDOWN"

        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": True, "retry_reviewed": True},
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["outcome"] == "QUEUED"
        assert len(calls) == 2
        assert calls[0][0].enrich_missing is True
        assert [call[2] for call in calls] == [retry_version_ids, retry_version_ids]

        with app.state.session_factory() as session:
            job = session.get(IngestionJob, queued.json()["request_id"])
            assert job is not None
            assert job.request_json["retry_reviewed"] is True


def test_explicit_review_retry_can_reextract_accepted_quantitative_review(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)
    calls = []
    retry_version_ids = frozenset({"accepted-quantitative-review-before-request"})
    monkeypatch.setattr(
        "pai_loop.manual_analysis._reason",
        lambda _notice: PublicAnalysisReason(
            state="ANALYZED",
            reason_code="ANALYZED",
            reason="첨부 추출은 완료됐지만 정량표 재검증이 필요합니다.",
            attachment_count=1,
            attempted=True,
        ),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.latest_current_evaluation",
        lambda _notice: object(),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._manual_jobs_since",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.current_retryable_review_version_ids",
        lambda _versions: retry_version_ids,
    )

    def fake_batch(payload, request, *, retry_reviewed_version_ids=frozenset()):
        calls.append((payload, request, retry_reviewed_version_ids))
        return _review_batch("batch-job-accepted-quantitative-retry")

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": True, "retry_reviewed": True},
        )

    assert queued.status_code == 200, queued.text
    assert queued.json()["outcome"] == "QUEUED"
    assert len(calls) == 1
    assert calls[0][0].enrich_missing is True
    assert calls[0][2] == retry_version_ids


def test_explicit_quantitative_retry_wins_over_evaluation_only_without_current_result(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)
    calls = []
    retry_version_ids = frozenset({"accepted-quantitative-review-before-request"})
    monkeypatch.setattr(
        "pai_loop.manual_analysis._reason",
        lambda _notice: PublicAnalysisReason(
            state="ANALYZED",
            reason_code="ANALYZED",
            reason="첨부 추출은 완료됐지만 현재 판단은 없습니다.",
            attachment_count=1,
            attempted=True,
        ),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.latest_current_evaluation",
        lambda _notice: None,
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._has_complete_current_attachment_audit",
        lambda _request, _notice: True,
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._manual_jobs_since",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.current_retryable_review_version_ids",
        lambda _versions: retry_version_ids,
    )

    def fake_batch(payload, request, *, retry_reviewed_version_ids=frozenset()):
        calls.append((payload, request, retry_reviewed_version_ids))
        return _review_batch("batch-job-quantitative-retry-without-evaluation")

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": True, "retry_reviewed": True},
        )

    assert queued.status_code == 200, queued.text
    assert queued.json()["outcome"] == "QUEUED"
    assert len(calls) == 1
    assert calls[0][0].enrich_missing is True
    assert calls[0][2] == retry_version_ids


def test_accepted_attachment_without_current_evaluation_continues_pipeline(
    monkeypatch,
) -> None:
    app = _app(monkeypatch, openai_configured=False)
    calls = []
    evaluation = {"created": False}
    monkeypatch.setattr(
        "pai_loop.manual_analysis._reason",
        lambda _notice: PublicAnalysisReason(
            state="ANALYZED",
            reason_code="ANALYZED",
            reason="첨부 추출은 완료됐지만 현재 판단은 없습니다.",
            attachment_count=1,
            attempted=True,
        ),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.latest_current_evaluation",
        lambda _notice: object() if evaluation["created"] else None,
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._has_complete_current_attachment_audit",
        lambda _request, _notice: True,
    )

    def fake_batch(payload, request):
        calls.append((payload, request))
        assert payload.enrich_missing is False
        evaluation["created"] = True
        return _review_batch("batch-job-missing-evaluation")

    monkeypatch.setattr("pai_loop.manual_analysis.run_notice_analysis_batch", fake_batch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": False},
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["outcome"] == "QUEUED"
        assert "저장된 첨부 근거" in queued.json()["message"]
        assert len(calls) == 1

        completed = client.get(
            f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{queued.json()['request_id']}",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["outcome"] == "COMPLETED"
        assert completed.json()["openai_calls"] == 0

        with app.state.session_factory() as session:
            job = session.get(IngestionJob, queued.json()["request_id"])
            assert job is not None
            assert job.request_json["evaluation_only"] is True
            assert job.request_json["enrich_missing"] is False


def test_manual_analysis_terminal_success_requires_current_evaluation(
    monkeypatch,
) -> None:
    app = _app(monkeypatch, openai_configured=False)
    monkeypatch.setattr(
        "pai_loop.manual_analysis._reason",
        lambda _notice: PublicAnalysisReason(
            state="ANALYZED",
            reason_code="ANALYZED",
            reason="첨부 추출은 완료됐지만 현재 판단은 없습니다.",
            attachment_count=1,
            attempted=True,
        ),
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.latest_current_evaluation",
        lambda _notice: None,
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._has_complete_current_attachment_audit",
        lambda _request, _notice: True,
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.run_notice_analysis_batch",
        lambda _payload, _request: _review_batch("batch-job-no-evaluation"),
    )

    with TestClient(app) as client:
        _create_open_pps_notice(client)
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": False},
        )
        assert queued.status_code == 200, queued.text
        completed = client.get(
            f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{queued.json()['request_id']}",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["outcome"] == "REVIEW"
        assert "보완" in completed.json()["message"]


def test_public_manual_analysis_rejects_authoritative_cancellation_before_job(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)

    def should_not_run(*_args, **_kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("cancelled notice must not execute a batch")

    monkeypatch.setattr(
        "pai_loop.manual_analysis.run_notice_analysis_batch",
        should_not_run,
    )
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        now = datetime.now(timezone.utc)
        with app.state.session_factory() as session:
            session.add(
                PpsNoticeAuthority(
                    bid_notice_no="R26BK-MANUAL-001",
                    revision_no="00",
                    event_kind="취소공고",
                    disposition="CANCELLED",
                    required_fields_complete=True,
                    direct_contract_signal=False,
                    published_at=now,
                    provider_changed_at=now,
                    deadline=None,
                    authority_sha256="c" * 64,
                )
            )
            session.commit()

        response = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 409
        assert "취소된 공고" in response.json()["detail"]
        with app.state.session_factory() as session:
            assert session.query(IngestionJob).filter(
                IngestionJob.source == "MANUAL_ANALYSIS"
            ).count() == 0


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
            json=EXTRACTION_ALLOWED,
        )
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "3600"


def test_zero_call_intent_rejects_a_state_that_requires_openai(monkeypatch) -> None:
    app = _app(monkeypatch)

    def should_not_run(*_args, **_kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("zero-call request must not cross the provider boundary")

    monkeypatch.setattr(
        "pai_loop.manual_analysis.run_notice_analysis_batch",
        should_not_run,
    )
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        response = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=SAME_ORIGIN_HEADERS,
            json={"run_extraction": False},
        )
        assert response.status_code == 409
        assert "비용 상한" in response.json()["detail"]
        with app.state.session_factory() as session:
            assert session.query(IngestionJob).filter(
                IngestionJob.source == "MANUAL_ANALYSIS"
            ).count() == 0


def test_production_manual_analysis_requires_scoped_operator_token(monkeypatch) -> None:
    app = _app(monkeypatch)
    app.state.settings = replace(
        app.state.settings,
        environment="production",
        public_manual_analysis_token="2468",
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis.run_notice_analysis_batch",
        lambda _payload, _request: _review_batch("batch-job-production-token"),
    )
    production_origin = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
    }
    with TestClient(app, base_url="https://testserver") as client:
        _create_open_pps_notice(client)
        runtime = client.get("/api/v1/runtime-profile")
        assert runtime.status_code == 200
        assert runtime.json()["manual_analysis_enabled"] is True
        assert runtime.json()["manual_analysis_auth_required"] is True

        unauthenticated = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers=production_origin,
            json=EXTRACTION_ALLOWED,
        )
        assert unauthenticated.status_code == 401
        wrong = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers={**production_origin, "X-PAI-Manual-Token": "1357"},
            json=EXTRACTION_ALLOWED,
        )
        assert wrong.status_code == 401
        queued = client.post(
            "/api/v1/notices/PPS-MANUAL-001/analysis/request",
            headers={
                **production_origin,
                "X-PAI-Manual-Token": "2468",
            },
            json=EXTRACTION_ALLOWED,
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["outcome"] == "QUEUED"
        assert "2468" not in queued.text
        with app.state.session_factory() as session:
            job = session.get(IngestionJob, queued.json()["request_id"])
            assert job is not None
            assert "2468" not in str(job.request_json)


def test_production_manual_analysis_is_hidden_until_token_is_configured(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)
    app.state.settings = replace(
        app.state.settings,
        environment="production",
        public_manual_analysis_token=None,
    )
    production_origin = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
    }
    with TestClient(app, base_url="https://testserver") as client:
        runtime = client.get("/api/v1/runtime-profile")
        assert runtime.status_code == 200
        assert runtime.json()["manual_analysis_enabled"] is False
        assert runtime.json()["manual_analysis_auth_required"] is False
        response = client.post(
            "/api/v1/notices/PPS-MISSING/analysis/request",
            headers=production_origin,
            json=EXTRACTION_ALLOWED,
        )
        assert response.status_code == 404


def test_quantitative_diagnostics_requires_same_origin_pin_and_disables_cache(
    monkeypatch,
) -> None:
    app = _app(monkeypatch)
    app.state.settings = replace(
        app.state.settings,
        environment="production",
        public_manual_analysis_token="2468",
    )
    production_origin = {
        "Origin": "https://testserver",
        "Sec-Fetch-Site": "same-origin",
    }
    path = "/api/v1/notices/PPS-MANUAL-001/analysis/quantitative-diagnostics"
    with TestClient(app, base_url="https://testserver") as client:
        _create_open_pps_notice(client)

        cross_origin = client.post(
            path,
            headers={"Origin": "https://attacker.invalid"},
        )
        assert cross_origin.status_code == 403
        missing_pin = client.post(path, headers=production_origin)
        assert missing_pin.status_code == 401
        missing_notice_without_pin = client.post(
            "/api/v1/notices/PPS-MISSING/analysis/quantitative-diagnostics",
            headers=production_origin,
        )
        assert missing_notice_without_pin.status_code == 401

        response = client.post(
            path,
            headers={
                **production_origin,
                "X-PAI-Manual-Token": "2468",
            },
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "notice_key": "PPS-MANUAL-001",
            "profile_status": "MISSING",
            "expected_attachment_count": 0,
            "processed_attachment_count": 0,
            "document_binding_count": 0,
            "table_status_counts": {},
            "available_candidate_count": 0,
            "review_candidate_count": 0,
            "issues": [],
            "review_candidate_issues": [],
            "activation_reasons": [],
            "available_candidate_shapes": [],
            "review_candidate_shapes": [],
        }
        assert "2468" not in response.text


def test_quantitative_diagnostics_aggregates_and_redacts_untrusted_values(
    monkeypatch,
) -> None:
    sensitive = "SENSITIVE_SOURCE_SENTINEL"
    profile = SimpleNamespace(
        status="INCOMPLETE",
        expected_attachment_ids=(sensitive, "attachment-2"),
        processed_attachment_ids=(sensitive,),
        document_bindings=(SimpleNamespace(attachment_id=sensitive),),
        tables=(
            SimpleNamespace(status="AVAILABLE", label=sensitive),
            SimpleNamespace(status="INCOMPLETE", label=sensitive),
        ),
        available_candidates=(SimpleNamespace(label=sensitive),),
        review_candidates=(
            SimpleNamespace(
                status="REVIEW",
                issue_codes=("CASE_ROWS_INCOMPLETE", "<img src=x>"),
                label=sensitive,
            ),
        ),
        issues=(
            SimpleNamespace(
                code="VALIDATOR_VERSION_MISMATCH",
                disposition="INCOMPLETE",
                message=sensitive,
            ),
            SimpleNamespace(
                code="VALIDATOR_VERSION_MISMATCH",
                disposition="INCOMPLETE",
                message=sensitive,
            ),
            SimpleNamespace(
                code="<script>alert(1)</script>",
                disposition="REVIEW",
                message=sensitive,
            ),
            SimpleNamespace(
                code="A" * 64,
                disposition="INCOMPLETE",
                message=sensitive,
            ),
        ),
    )
    monkeypatch.setattr(
        "pai_loop.quantitative_scoring._current_dynamic_quantitative_profile",
        lambda _notice: profile,
    )
    monkeypatch.setattr(
        "pai_loop.quantitative_scoring._profile_activation_reasons",
        lambda _profile: [
            "SOURCE_VALIDATION_ISSUES_PRESENT",
            "LOGICAL_TABLE_CONFLICT|private-attachment|private-table|TOTAL_MISMATCH",
            "LOGICAL_CRITERION_CONFLICT|private-attachment|private-table|private-row",
            "B" * 64,
            "<unsafe>",
        ],
    )

    result = _quantitative_diagnostics(
        SimpleNamespace(notice_key="SYN-QUANT-DIAGNOSTIC")
    )
    payload = result.model_dump(mode="json")

    assert payload["profile_status"] == "INCOMPLETE"
    assert payload["expected_attachment_count"] == 2
    assert payload["processed_attachment_count"] == 1
    assert payload["table_status_counts"] == {"AVAILABLE": 1, "INCOMPLETE": 1}
    assert payload["issues"] == [
        {
            "code": "UNKNOWN_VALIDATION_ISSUE",
            "disposition": "INCOMPLETE",
            "count": 1,
        },
        {
            "code": "UNKNOWN_VALIDATION_ISSUE",
            "disposition": "REVIEW",
            "count": 1,
        },
        {
            "code": "VALIDATOR_VERSION_MISMATCH",
            "disposition": "INCOMPLETE",
            "count": 2,
        },
    ]
    assert payload["review_candidate_issues"] == [
        {"code": "CASE_ROWS_INCOMPLETE", "disposition": "REVIEW", "count": 1},
        {
            "code": "UNKNOWN_VALIDATION_ISSUE",
            "disposition": "REVIEW",
            "count": 1,
        },
    ]
    assert payload["activation_reasons"] == [
        "LOGICAL_CRITERION_CONFLICT",
        "LOGICAL_TABLE_CONFLICT",
        "SOURCE_VALIDATION_ISSUES_PRESENT",
        "UNKNOWN_VALIDATION_ISSUE",
    ]
    assert payload["available_candidate_shapes"] == []
    assert payload["review_candidate_shapes"] == []
    assert sensitive not in result.model_dump_json()
    assert "A" * 64 not in result.model_dump_json()
    assert "B" * 64 not in result.model_dump_json()
    assert "private-attachment" not in result.model_dump_json()


def test_quantitative_diagnostics_returns_only_bounded_current_review_shapes(
    monkeypatch,
) -> None:
    digest = "a" * 64
    document_digest = "b" * 64
    attachment_id = "private-attachment-id"
    sensitive_marker = "SENSITIVE_SOURCE_SENTINEL"
    common_anchor = {
        "attachment_id": attachment_id,
        "page": 33,
        "section": "정량적 평가",
        "confidence": 0.98,
    }
    result_payload = {
        "document_type": "RFP",
        "requirements": [],
        "quantitative_tables": [
            {
                "table_id": "TABLE-PRIVATE-ID",
                "label": f"정량 평가표 {sensitive_marker}",
                "criteria": [
                    {
                        "criterion_id": "CRITERION-PRIVATE-ID",
                        "label": "용역수행 실적",
                        "criterion_literal": (
                            f"용역수행 실적(금액, 6점) {sensitive_marker}"
                        ),
                        "max_points": 6,
                        "scoring_method": "CASE_TABLE",
                        "metric": "PERFORMANCE_AMOUNT",
                        "unit": "억원",
                        "brackets": [],
                        "threshold": None,
                        "formula_literal": None,
                        "cases": [
                            {
                                "literal": "2억 원 이상\n6점",
                                "operator": "GTE",
                                "comparison_value": 2,
                                "category_values": [],
                                "award_kind": "POINTS",
                                "award_value": 6,
                                "row_order": 1,
                                "evidence": {
                                    **common_anchor,
                                    "quote": "2억 원 이상\n6점",
                                },
                            }
                        ],
                        "recognition_conditions": [],
                        "required_evidence": ["company.performance.amount"],
                        "evidence": {
                            **common_anchor,
                            "quote": "용역수행 실적\n(10점)",
                        },
                        "ambiguity_reason": None,
                    }
                ],
                "total_points": 20,
                "total_evidence": {
                    **common_anchor,
                    "quote": "정량적 평가\n20점",
                },
                "minimum_score": 85,
                "minimum_evidence": {**common_anchor, "quote": "SYN-전체 제안서 평가 85점 이상"},
                "ambiguity_reason": None,
            }
        ],
        "quantitative_table_not_applicable": None,
        "missing_or_unreadable": [],
        "summary": "정량평가표 추출",
    }
    profile = SimpleNamespace(
        manifest_sha256=digest,
        status="INCOMPLETE",
        expected_attachment_ids=(attachment_id,),
        processed_attachment_ids=(attachment_id,),
        document_bindings=(SimpleNamespace(attachment_id=attachment_id),),
        tables=(SimpleNamespace(status="INCOMPLETE"),),
        available_candidates=(),
        review_candidates=(
            SimpleNamespace(
                source_attachment_id=attachment_id,
                table_id="TABLE-PRIVATE-ID",
                criterion_id="CRITERION-PRIVATE-ID",
                status="INCOMPLETE",
                issue_codes=("CRITERION_LITERAL_MISMATCH",),
            ),
        ),
        issues=(
            SimpleNamespace(
                code="CRITERION_LITERAL_MISMATCH",
                disposition="INCOMPLETE",
            ),
        ),
    )
    notice = SimpleNamespace(
        notice_key="SYN-QUANT-SHAPE",
        versions=(
            SimpleNamespace(
                version_no=2,
                file_sha256=document_digest,
                source_payload={
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "source_kind": "PPS_PUBLIC_ATTACHMENT",
                    "status": "ACCEPTED",
                    "attachment_id": attachment_id,
                    "current_manifest_sha256": digest,
                    "quantitative_validation_record": {
                        "attachment_id": attachment_id,
                        "manifest_sha256": digest,
                        "document_sha256": document_digest,
                    },
                    "result": result_payload,
                },
            ),
            # A stale record with the same IDs must not replace the current shape.
            SimpleNamespace(
                version_no=1,
                file_sha256="c" * 64,
                source_payload={
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "source_kind": "PPS_PUBLIC_ATTACHMENT",
                    "status": "ACCEPTED",
                    "attachment_id": attachment_id,
                    "current_manifest_sha256": "d" * 64,
                    "quantitative_validation_record": {
                        "attachment_id": attachment_id,
                        "manifest_sha256": "d" * 64,
                        "document_sha256": "c" * 64,
                    },
                    "result": result_payload,
                },
            ),
        ),
    )
    monkeypatch.setattr(
        "pai_loop.quantitative_scoring._current_dynamic_quantitative_profile",
        lambda _notice: profile,
    )
    monkeypatch.setattr(
        "pai_loop.quantitative_scoring._profile_activation_reasons",
        lambda _profile: ["TABLE_CRITERIA_LINKAGE_INCOMPLETE"],
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._current_manifest_attempts",
        lambda _versions: ([], 0, {attachment_id: notice.versions[0]}),
    )

    payload = _quantitative_diagnostics(notice).model_dump(mode="json")

    assert "SYN-전체 제안서 평가" not in str(payload)
    assert payload["available_candidate_shapes"] == []
    assert payload["review_candidate_shapes"] == [
        {
            "source_ordinal": 1,
            "table_ordinal": 1,
            "criterion_ordinal": 1,
            "document_type": "RFP",
            "status": "INCOMPLETE",
            "issue_codes": ["CRITERION_LITERAL_MISMATCH"],
            "criterion_character_count": 41,
            "evidence_character_count": 13,
            "criterion_literal_matches_evidence": False,
            "criterion_has_metric_tokens": True,
            "evidence_has_metric_tokens": False,
            "criterion_point_values": [6.0],
            "evidence_point_values": [10.0],
            "max_points": 6.0,
            "max_points_within_safe_range": True,
            "scoring_method": "CASE_TABLE",
            "metric": "PERFORMANCE_AMOUNT",
            "table_total_points": 20.0,
            "table_minimum_score": 85.0,
            "minimum_evidence_character_count": len("SYN-전체 제안서 평가 85점 이상"),
            "minimum_evidence_point_values": [85.0],
            "unit_present": True,
            "bracket_count": 0,
            "brackets": [],
            "threshold_present": False,
            "formula_present": False,
            "recognition_condition_count": 0,
            "recognition_conditions": [],
            "case_count": 1,
            "cases": [
                {
                    "row_order": 1,
                    "operator": "GTE",
                    "comparison_value_present": True,
                    "category_value_count": 0,
                    "category_interpretations": [],
                    "award_kind": "POINTS",
                    "award_value": 6.0,
                    "award_value_within_safe_range": True,
                    "literal_point_values": [6.0],
                    "evidence_point_values": [6.0],
                    "literal_matches_evidence": True,
                }
            ],
        }
    ]
    encoded = _quantitative_diagnostics(notice).model_dump_json()
    assert attachment_id not in encoded
    assert "TABLE-PRIVATE-ID" not in encoded
    assert "CRITERION-PRIVATE-ID" not in encoded
    assert sensitive_marker not in encoded


def test_quantitative_diagnostics_returns_redacted_current_available_shapes(
    monkeypatch,
) -> None:
    digest = "e" * 64
    document_digest = "f" * 64
    attachment_id = "private-current-attachment"
    sensitive_marker = "AVAILABLE_PRIVATE_SOURCE_SENTINEL"
    stale_marker = "STALE_PRIVATE_SOURCE_SENTINEL"

    def extraction_payload(marker: str) -> dict[str, object]:
        anchor = {
            "attachment_id": attachment_id,
            "page": 33,
            "section": marker,
            "confidence": 0.99,
        }
        primary_condition_quote = f"실적 건수 기준 4점 {marker}"
        conditions = [
            {
                "literal": "4점",
                "evidence": {
                    **anchor,
                    "quote": primary_condition_quote,
                },
            },
            *[
                {
                    "literal": f"후속조건 {index} 5점 {marker}",
                    "evidence": {
                        **anchor,
                        "quote": f"후속조건 {index} 5점 {marker}",
                    },
                }
                for index in range(1, 13)
            ],
        ]
        return {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [
                {
                    "table_id": "TABLE-AVAILABLE-PRIVATE",
                    "label": marker,
                    "criteria": [
                        {
                            "criterion_id": "CRITERION-AVAILABLE-PRIVATE",
                            "label": marker,
                            "criterion_literal": primary_condition_quote,
                            "max_points": 6,
                            "scoring_method": "CASE_TABLE",
                            "metric": "PERFORMANCE_COUNT",
                            "unit": "건",
                            "brackets": [],
                            "threshold": None,
                            "formula_literal": None,
                            "cases": [
                                {
                                    "literal": "4점",
                                    "operator": "GTE",
                                    "comparison_value": 1,
                                    "category_values": [],
                                    "award_kind": "POINTS",
                                    "award_value": 4,
                                    "row_order": 1,
                                    "evidence": {**anchor, "quote": "4점"},
                                }
                            ],
                            "recognition_conditions": conditions,
                            "required_evidence": [],
                            "evidence": {
                                **anchor,
                                "quote": primary_condition_quote,
                            },
                            "ambiguity_reason": None,
                        },
                        {
                            "criterion_id": "CRITERION-OTHER-PRIVATE",
                            "label": marker,
                            "criterion_literal": f"타 평가 기준 4점 {marker}",
                            "max_points": 4,
                            "scoring_method": "CASE_TABLE",
                            "metric": "PERFORMANCE_COUNT",
                            "unit": "건",
                            "brackets": [],
                            "threshold": None,
                            "formula_literal": None,
                            "cases": [
                                {
                                    "literal": primary_condition_quote,
                                    "operator": "EQ",
                                    "comparison_value": 1,
                                    "category_values": [],
                                    "award_kind": "POINTS",
                                    "award_value": 4,
                                    "row_order": 1,
                                    "evidence": {
                                        **anchor,
                                        "quote": primary_condition_quote,
                                    },
                                }
                            ],
                            "recognition_conditions": [
                                {
                                    "literal": "4점",
                                    "evidence": {
                                        **anchor,
                                        "quote": primary_condition_quote,
                                    },
                                }
                            ],
                            "required_evidence": [],
                            "evidence": {
                                **anchor,
                                "quote": f"타 평가 기준 4점 {marker}",
                            },
                            "ambiguity_reason": None,
                        },
                    ],
                    "total_points": 10,
                    "total_evidence": {
                        **anchor,
                        "quote": f"정량평가 6점 {marker}",
                    },
                    "minimum_score": None,
                    "minimum_evidence": None,
                    "ambiguity_reason": None,
                },
                {
                    "table_id": "TABLE-FOREIGN-PRIVATE",
                    "label": marker,
                    "criteria": [
                        {
                            "criterion_id": "CRITERION-FOREIGN-PRIVATE",
                            "label": marker,
                            "criterion_literal": primary_condition_quote,
                            "max_points": 4,
                            "scoring_method": "CASE_TABLE",
                            "metric": "PERFORMANCE_COUNT",
                            "unit": "건",
                            "brackets": [],
                            "threshold": None,
                            "formula_literal": None,
                            "cases": [
                                {
                                    "literal": primary_condition_quote,
                                    "operator": "EQ",
                                    "comparison_value": 1,
                                    "category_values": [],
                                    "award_kind": "POINTS",
                                    "award_value": 4,
                                    "row_order": 1,
                                    "evidence": {
                                        **anchor,
                                        "quote": primary_condition_quote,
                                    },
                                }
                            ],
                            "recognition_conditions": [
                                {
                                    "literal": "4점",
                                    "evidence": {
                                        **anchor,
                                        "quote": primary_condition_quote,
                                    },
                                }
                            ],
                            "required_evidence": [],
                            "evidence": {
                                **anchor,
                                "quote": primary_condition_quote,
                            },
                            "ambiguity_reason": None,
                        }
                    ],
                    "total_points": 4,
                    "total_evidence": {
                        **anchor,
                        "quote": primary_condition_quote,
                    },
                    "minimum_score": None,
                    "minimum_evidence": None,
                    "ambiguity_reason": None,
                },
            ],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": marker,
        }

    current_version = SimpleNamespace(
        version_no=2,
        file_sha256=document_digest,
        source_payload={
            "kind": "OPENAI_REQUIREMENT_EXTRACTION",
            "source_kind": "PPS_PUBLIC_ATTACHMENT",
            "status": "ACCEPTED",
            "attachment_id": attachment_id,
            "current_manifest_sha256": digest,
            "result": extraction_payload(sensitive_marker),
        },
    )
    stale_version = SimpleNamespace(
        version_no=1,
        file_sha256="1" * 64,
        source_payload={
            "kind": "OPENAI_REQUIREMENT_EXTRACTION",
            "source_kind": "PPS_PUBLIC_ATTACHMENT",
            "status": "ACCEPTED",
            "attachment_id": attachment_id,
            "current_manifest_sha256": "2" * 64,
            "result": extraction_payload(stale_marker),
        },
    )
    profile = SimpleNamespace(
        manifest_sha256=digest,
        status="AVAILABLE",
        expected_attachment_ids=(attachment_id,),
        processed_attachment_ids=(attachment_id,),
        document_bindings=(SimpleNamespace(attachment_id=attachment_id),),
        tables=(SimpleNamespace(status="AVAILABLE"),),
        available_candidates=(
            SimpleNamespace(
                source_attachment_id=attachment_id,
                table_id="TABLE-AVAILABLE-PRIVATE",
                criterion_id="CRITERION-AVAILABLE-PRIVATE",
            ),
        ),
        review_candidates=(),
        issues=(),
    )
    notice = SimpleNamespace(
        notice_key="SYN-QUANT-AVAILABLE-SHAPE",
        versions=(current_version, stale_version),
    )
    monkeypatch.setattr(
        "pai_loop.quantitative_scoring._current_dynamic_quantitative_profile",
        lambda _notice: profile,
    )
    monkeypatch.setattr(
        "pai_loop.quantitative_scoring._profile_activation_reasons",
        lambda _profile: [],
    )
    monkeypatch.setattr(
        "pai_loop.manual_analysis._current_manifest_attempts",
        lambda _versions: ([], 0, {attachment_id: current_version}),
    )

    payload = _quantitative_diagnostics(notice).model_dump(mode="json")

    assert payload["review_candidate_shapes"] == []
    assert len(payload["available_candidate_shapes"]) == 1
    shape = payload["available_candidate_shapes"][0]
    assert shape["status"] == "AVAILABLE"
    assert shape["issue_codes"] == []
    assert shape["source_ordinal"] == 1
    assert shape["document_type"] == "RFP"
    assert shape["metric"] == "PERFORMANCE_COUNT"
    assert shape["recognition_condition_count"] == 13
    assert len(shape["recognition_conditions"]) == 12
    condition = shape["recognition_conditions"][0]
    assert {
        key: condition[key]
        for key in (
            "literal_character_count",
            "evidence_character_count",
            "literal_point_values",
            "evidence_point_values",
            "literal_is_point_only",
            "evidence_is_point_only",
            "literal_matches_evidence",
            "literal_has_metric_tokens",
            "evidence_has_metric_tokens",
        )
    } == {
        "literal_character_count": 2,
        "evidence_character_count": len(
            f"실적 건수 기준 4점 {sensitive_marker}"
        ),
        "literal_point_values": [4.0],
        "evidence_point_values": [4.0],
        "literal_is_point_only": True,
        "evidence_is_point_only": False,
        "literal_matches_evidence": True,
        "literal_has_metric_tokens": False,
        "evidence_has_metric_tokens": True,
    }

    def relation(
        *,
        targets: int,
        literal_overlap: int,
        evidence_overlap: int,
        literal_match: int,
        evidence_match: int,
        literal_occurrences: int,
        evidence_occurrences: int,
        equivalent: bool,
    ) -> dict[str, object]:
        return {
            "target_count": targets,
            "scanned_target_count": targets,
            "scan_truncated": False,
            "literal_overlaps": literal_overlap > 0,
            "literal_overlap_target_count": literal_overlap,
            "evidence_overlaps": evidence_overlap > 0,
            "evidence_overlap_target_count": evidence_overlap,
            "literal_matches": literal_match > 0,
            "literal_match_target_count": literal_match,
            "evidence_matches": evidence_match > 0,
            "evidence_match_target_count": evidence_match,
            "literal_occurrence_count": literal_occurrences,
            "evidence_occurrence_count": evidence_occurrences,
            "literal_evidence_occurrence_equivalent": equivalent,
        }

    assert condition["own_criterion_anchor"] == relation(
        targets=1,
        literal_overlap=1,
        evidence_overlap=1,
        literal_match=0,
        evidence_match=1,
        literal_occurrences=2,
        evidence_occurrences=2,
        equivalent=True,
    )
    assert condition["own_case_rows"] == relation(
        targets=1,
        literal_overlap=1,
        evidence_overlap=1,
        literal_match=1,
        evidence_match=0,
        literal_occurrences=2,
        evidence_occurrences=0,
        equivalent=False,
    )
    assert condition["other_criteria_anchors"] == relation(
        targets=1,
        literal_overlap=1,
        evidence_overlap=0,
        literal_match=0,
        evidence_match=0,
        literal_occurrences=2,
        evidence_occurrences=0,
        equivalent=False,
    )
    assert condition["other_case_rows"] == relation(
        targets=1,
        literal_overlap=1,
        evidence_overlap=1,
        literal_match=0,
        evidence_match=1,
        literal_occurrences=2,
        evidence_occurrences=2,
        equivalent=True,
    )
    assert condition["other_recognition_conditions"] == relation(
        targets=13,
        literal_overlap=1,
        evidence_overlap=1,
        literal_match=1,
        evidence_match=1,
        literal_occurrences=2,
        evidence_occurrences=1,
        equivalent=False,
    )
    encoded = _quantitative_diagnostics(notice).model_dump_json()
    assert attachment_id not in encoded
    assert "TABLE-AVAILABLE-PRIVATE" not in encoded
    assert "TABLE-FOREIGN-PRIVATE" not in encoded
    assert "CRITERION-AVAILABLE-PRIVATE" not in encoded
    assert "CRITERION-OTHER-PRIVATE" not in encoded
    assert "CRITERION-FOREIGN-PRIVATE" not in encoded
    assert sensitive_marker not in encoded
    assert "실적 건수 기준" not in encoded
    assert "후속조건" not in encoded

    # A persisted attempt bound to another manifest cannot supply a shape.
    monkeypatch.setattr(
        "pai_loop.manual_analysis._current_manifest_attempts",
        lambda _versions: ([], 0, {attachment_id: stale_version}),
    )
    stale_payload = _quantitative_diagnostics(notice).model_dump(mode="json")
    assert stale_payload["available_candidate_shapes"] == []
    assert stale_marker not in _quantitative_diagnostics(notice).model_dump_json()


def test_quantitative_diagnostic_metric_token_check_marks_unsupported_metrics() -> None:
    assert _diagnostic_has_metric_tokens("PERFORMANCE_AMOUNT", "실적 금액 6점") is True
    assert _diagnostic_has_metric_tokens("PERFORMANCE_AMOUNT", "실적 10점") is False
    assert _diagnostic_has_metric_tokens("PERSONNEL_COUNT", "전문인력 5명") is None


def test_bracket_diagnostics_hide_literals_and_bounds() -> None:
    from pai_loop.integrations.openai_extraction import QuantitativeBracketLiteral
    from pai_loop.manual_analysis import _diagnostic_bracket_shape
    bracket = QuantitativeBracketLiteral.model_validate({
        "label": "SYN-PRIVATE-LABEL", "literal": "98765 이상 3점", "min_value": 98765,
        "max_value": None, "min_inclusive": False, "max_inclusive": False, "points": 3,
        "evidence": {"attachment_id": "SYN-PRIVATE-ATTACHMENT", "page": 1, "section": "SYN-PRIVATE-SECTION", "quote": "98765 이상 3점", "confidence": 0.99},
    })
    result = _diagnostic_bracket_shape(bracket, 1).model_dump()
    assert result["expected_operators"] == ["GT"]
    assert result["parsed_operators"] == ["GTE"]
    assert result["comparator_values_match"] is True
    assert result["literal_matches_evidence"] is True
    assert result["parsed_operator_count"] == 1
    assert result["operator_scan_truncated"] is False
    serialized = str(result)
    assert "98765" not in serialized
    assert "SYN-PRIVATE" not in serialized
    assert "이상" not in serialized


@pytest.mark.parametrize(("value", "expected"), (("실적 없음", "NONE"), ("1건 이하", "COUNT_BOUND"), ("2건", "EXACT_COUNT"), ("SYN-PRIVATE-CATEGORY", "OTHER"), ("없음. 별도 조건", "OTHER")))
def test_category_diagnostics_return_only_fixed_codes(value: str, expected: str) -> None:
    from pai_loop.manual_analysis import _diagnostic_category_interpretation
    assert _diagnostic_category_interpretation(value) == expected


@pytest.mark.parametrize(("age_minutes", "worker_busy", "initial_status", "expected_status"), [
    (30, False, "RUNNING", "FAILED"),
    (30, True, "RUNNING", "RUNNING"),
    (1, False, "RUNNING", "RUNNING"),
    (30, False, "COMPLETED", "COMPLETED"),
])
def test_manual_poll_recovers_only_old_idle_reservations(
    monkeypatch, age_minutes, worker_busy, initial_status, expected_status,
):
    from datetime import timedelta
    import pai_loop.manual_analysis as manual
    app = _app(monkeypatch)
    job_id = "SYN-INTERRUPTED-MANUAL"
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        with app.state.session_factory() as session:
            session.add(IngestionJob(
                id=job_id, source="MANUAL_ANALYSIS", mode="LIVE", status=initial_status,
                created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
                notice_keys=["PPS-MANUAL-001"], api_calls=3,
                window_json={}, request_json={"evaluation_only": True}, warnings=["SYN-EXISTING-AUDIT"],
            ))
            session.commit()
        if worker_busy:
            assert manual._PUBLIC_MANUAL_PROCESS_LOCK.acquire(blocking=False)
        try:
            response = client.get(
                f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{job_id}",
                headers=SAME_ORIGIN_HEADERS,
            )
        finally:
            if worker_busy:
                manual._PUBLIC_MANUAL_PROCESS_LOCK.release()
        assert response.status_code == 200, response.text
        assert (response.json()["outcome"] == "QUEUED") == (expected_status == "RUNNING")
        with app.state.session_factory() as session:
            job = session.get(IngestionJob, job_id)
            assert job.status == expected_status
            assert job.api_calls == 3
            assert "SYN-EXISTING-AUDIT" in job.warnings
            if expected_status == "FAILED":
                assert job.completed_at is not None
                assert "MANUAL_WORKER_INTERRUPTED" in job.warnings
                assert job.request_json["openai_telemetry"]["accounting_complete"] is False
                assert response.json()["openai_telemetry"]["accounting_complete"] is False
        if expected_status == "FAILED":
            calls = []
            monkeypatch.setattr(manual, "run_notice_analysis_batch", lambda *_a, **_k: calls.append(1))
            manual._execute_reserved_manual_job(SimpleNamespace(app=app), job_id, "PPS-MANUAL-001", True)
            assert calls == []
            # Repeated polling leaves the same terminal audit untouched.
            again = client.get(f"/api/v1/notices/PPS-MANUAL-001/analysis/requests/{job_id}", headers=SAME_ORIGIN_HEADERS)
            assert again.json()["outcome"] == "REVIEW"


def test_manual_recovery_rejects_wrong_notice_or_missing_origin(monkeypatch):
    from datetime import timedelta
    app = _app(monkeypatch)
    with TestClient(app) as client:
        _create_open_pps_notice(client)
        with app.state.session_factory() as session:
            session.add(IngestionJob(
                id="SYN-BOUND-MANUAL", source="MANUAL_ANALYSIS", mode="LIVE", status="RUNNING",
                window_json={}, request_json={},
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                notice_keys=["PPS-MANUAL-001"],
            ))
            session.commit()
        url = "/api/v1/notices/PPS-MANUAL-001/analysis/requests/SYN-BOUND-MANUAL"
        assert client.get(url).status_code == 403
        wrong = client.get(url.replace("PPS-MANUAL-001", "PPS-SYN-WRONG"), headers=SAME_ORIGIN_HEADERS)
        assert wrong.status_code == 404
        with app.state.session_factory() as session:
            assert session.get(IngestionJob, "SYN-BOUND-MANUAL").status == "RUNNING"
