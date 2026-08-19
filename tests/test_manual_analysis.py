from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from pai_loop.analysis_api import AnalysisBatchResponse
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


def _review_batch(job_id: str = "batch-job-public-manual") -> AnalysisBatchResponse:
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
            "openai_calls": 0,
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
            "warnings": [],
            "enrichment": {
                "requested": 1,
                "attempted": 1,
                "completed": 0,
                "skipped": 1,
                "failed": 0,
                "attachments_discovered": 0,
                "attachments_processed": 0,
                "openai_calls": 0,
                "warnings": [],
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
        assert first.json()["outcome"] == "REVIEW"
        assert first.json()["openai_calls"] == 0
        assert "api_key" not in first.text.casefold()
        assert len(calls) == 1
        payload = calls[0][0]
        assert payload.notice_keys == ["PPS-MANUAL-001"]
        assert payload.dry_run is False
        assert payload.force is False
        assert payload.enrich_missing is True
        assert payload.max_notices == 1
        assert payload.max_attachments_per_notice == 1

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
