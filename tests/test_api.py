from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

import pytest

from pai_loop.main import create_app
from pai_loop.integrations.openai_extraction import (
    EvidenceAnchor,
    ExtractedRequirement,
    ExtractionOutcome,
    ExtractionPayload,
)


class _FakePpsClient:
    def __init__(self, **_kwargs: object) -> None:
        self.request_count = 1
        self.hit_page_limit = False

    def __enter__(self) -> "_FakePpsClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_notices(self, **_kwargs: object):
        yield {
            "identity": "20260816001|00|2026-08-20T17:00:00+09:00",
            "bid_notice_no": "20260816001",
            "revision_no": "00",
            "title": "공공기관 실제 연수 용역",
            "agency": "공공기관",
            "published_at": datetime.fromisoformat("2026-08-16T09:00:00+09:00"),
            "deadline": datetime.fromisoformat("2026-08-20T17:00:00+09:00"),
            "estimated_amount": 100_000_000,
            "source_url": "https://example.go.kr/notice/1?serviceKey=redacted",
            "raw": {"contact": "discarded"},
        }


class _FakeOpenAIExtractionClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_FakeOpenAIExtractionClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract(self, *, document_text: str, allowed_attachment_ids: set[str]) -> ExtractionOutcome:
        attachment_id = next(iter(allowed_attachment_ids))
        quote = "입찰참가자격은 유효한 사업자등록을 보유한 업체입니다."
        assert quote in document_text
        return ExtractionOutcome(
            status="ACCEPTED",
            message="validated",
            response_id="resp_test",
            model="gpt-test",
            data=ExtractionPayload(
                document_type="NOTICE",
                requirements=[
                    ExtractedRequirement(
                        requirement_id="REQ-1",
                        category="ENTITY",
                        logic="SINGLE",
                        normalized_condition="유효한 사업자등록 보유",
                        mandatory=True,
                        deadline_basis="입찰 마감일",
                        evidence=[
                            EvidenceAnchor(
                                attachment_id=attachment_id,
                                page=1,
                                section="입찰참가자격",
                                quote=quote,
                                confidence=0.98,
                            )
                        ],
                        ambiguity_reason=None,
                    )
                ],
                missing_or_unreadable=[],
                summary="참가자격 1건",
            ),
        )


class _ReviewOpenAIExtractionClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_ReviewOpenAIExtractionClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract(self, **_kwargs: object) -> ExtractionOutcome:
        return ExtractionOutcome(
            status="REVIEW",
            review_code="R07",
            error_code="INCOMPLETE_RESPONSE",
            message="사람 검토 필요",
        )


def test_health_and_empty_dashboard(client: TestClient) -> None:
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database"] == "ok"

    dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["totals"] == {
        "notices": 0,
        "active": 0,
        "evaluations": 0,
        "decisions": 0,
    }
def test_synthetic_replay_is_idempotent_and_covers_three_states(client: TestClient) -> None:
    first = client.post("/api/v1/ingestion/replay")
    assert first.status_code == 200
    assert first.json()["created"] == 3
    assert first.json()["existing"] == 0

    second = client.post("/api/v1/ingestion/replay")
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["existing"] == 3

    notices = client.get("/api/v1/notices").json()
    assert len(notices) == 3
    assert {item["latest_evaluation"]["eligibility"] for item in notices} == {"PASS", "REVIEW", "FAIL"}

    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["eligibility_counts"] == {"PASS": 1, "REVIEW": 1, "FAIL": 1}
    assert dashboard["totals"]["evaluations"] == 3


def test_past_open_notice_is_exposed_as_expired_and_not_counted_active(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "PAST-NOTICE",
            "bid_notice_no": "PAST-001",
            "title": "과거 공개 공고",
            "deadline": "2020-01-01T09:00:00Z",
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "EXPIRED"
    assert client.get("/api/v1/dashboard").json()["totals"]["active"] == 0


def test_notice_detail_manual_evaluation_and_user_decision(client: TestClient) -> None:
    client.post("/api/v1/ingestion/replay")
    detail = client.get("/api/v1/notices/SYN-REVIEW-001")
    assert detail.status_code == 200
    assert detail.json()["requirements"][0]["linked_review_code"] == "R01"
    assert detail.json()["latest_evaluation"]["eligibility"] == "REVIEW"
    assert detail.json()["latest_evaluation"]["readiness_status"] == "GREEN"

    evaluation = client.post(
        "/api/v1/notices/SYN-REVIEW-001/evaluate",
        json={"ruleset_version": "test-v1"},
    )
    assert evaluation.status_code == 201
    assert evaluation.json()["eligibility"] == "REVIEW"
    assert evaluation.json()["ruleset_version"] == "test-v1"

    decision = client.post(
        "/api/v1/notices/SYN-REVIEW-001/decisions",
        json={
            "evaluation_id": evaluation.json()["id"],
            "choice": "CONDITIONAL_GO",
            "actor_label": "심사 담당자",
            "rationale": "서면 질의 결과를 확인하는 조건으로 검토를 계속합니다.",
            "conditions": ["발주처 서면 답변 확보"],
        },
    )
    assert decision.status_code == 201
    assert decision.json()["choice"] == "CONDITIONAL_GO"

    decisions = client.get("/api/v1/notices/SYN-REVIEW-001/decisions")
    assert decisions.status_code == 200
    assert len(decisions.json()) == 1


def test_create_vertical_slice_through_public_api(client: TestClient) -> None:
    notice = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "API-SYN-001",
            "bid_notice_no": "API-2026-1",
            "title": "API 합성 공고",
            "agency": "가상 발주기관",
            "deadline": "2026-08-20T09:00:00Z",
            "risk_dimensions": {"qualification": 15, "execution": 20},
        },
    )
    assert notice.status_code == 201

    version = client.post(
        "/api/v1/notices/API-SYN-001/versions",
        json={"version_no": 1, "file_sha256": "f" * 64, "extraction_confidence": 0.99},
    )
    assert version.status_code == 201

    evidence = client.post(
        "/api/v1/evidence",
        json={
            "evidence_key": "API-E-1",
            "name": "합성 증빙",
            "evidence_type": "TEST",
            "issued_at": "2025-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
        },
    )
    assert evidence.status_code == 201

    company_fact = client.post(
        "/api/v1/company-facts",
        json={
            "fact_key": "api.entity_status",
            "value": "active",
            "effective_from": "2025-01-01T00:00:00Z",
            "evidence_key": "API-E-1",
            "verified": True,
            "source": "TEST",
        },
    )
    assert company_fact.status_code == 201

    requirement = client.post(
        f"/api/v1/notice-versions/{version.json()['id']}/requirements",
        json={
            "requirement_key": "API-Q-1",
            "group_key": "G-ENTITY",
            "path_key": "PATH-ENTITY",
            "label": "법인 상태가 유효해야 함",
            "fact_key": "api.entity_status",
            "operator": "eq",
            "required_value": "active",
            "pass_rule_id": "P-ENTITY",
            "source_excerpt": "합성 조건",
        },
    )
    assert requirement.status_code == 201

    evaluation = client.post("/api/v1/notices/API-SYN-001/evaluate", json={})
    assert evaluation.status_code == 201
    assert evaluation.json()["eligibility"] == "PASS"
    assert evaluation.json()["evidence_coverage"] == 100


def test_decision_rejects_evaluation_from_another_notice(client: TestClient) -> None:
    client.post("/api/v1/ingestion/replay")
    evaluation = client.get("/api/v1/notices/SYN-PASS-001").json()["latest_evaluation"]
    response = client.post(
        "/api/v1/notices/SYN-FAIL-001/decisions",
        json={
            "evaluation_id": evaluation["id"],
            "choice": "NO_GO",
            "rationale": "다른 공고의 평가 ID는 연결할 수 없습니다.",
        },
    )
    assert response.status_code == 422


def test_production_fails_closed_without_server_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "production")
    monkeypatch.delenv("PAI_LOOP_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PAI_LOOP_API_KEY"):
        create_app(database_url="sqlite:///:memory:")


def test_configured_api_auth_bypasses_health_but_protects_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as protected_client:
        assert protected_client.get("/healthz").status_code == 200
        assert protected_client.post("/api/v1/ingestion/replay").status_code == 401
        assert protected_client.post(
            "/api/v1/ingestion/replay",
            headers={"X-PAI-LOOP-API-KEY": "wrong"},
        ).status_code == 401
        response = protected_client.post(
            "/api/v1/ingestion/replay",
            headers={"X-PAI-LOOP-API-KEY": "server-only-secret"},
        )
        assert response.status_code == 200


def test_production_rejects_local_sqlite_even_with_server_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "production")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    with pytest.raises(RuntimeError, match="managed PostgreSQL"):
        create_app(database_url="sqlite:///:memory:")


def test_live_pps_ingestion_is_idempotent_and_discards_raw_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsClient", _FakePpsClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        payload = {
            "from_date": "2026-08-16",
            "to_date": "2026-08-16",
            "keyword": "연수",
            "page_size": 100,
            "max_pages": 2,
            "dry_run": False,
        }
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200
        assert first.json()["created"] == 1
        assert first.json()["matched"] == 1
        assert first.json()["source"] == "PPS"

        second = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert second.status_code == 200
        assert second.json()["created"] == 0
        assert second.json()["duplicates"] == 1

        notices = live_client.get("/api/v1/notices").json()
        assert len(notices) == 1
        assert notices[0]["source_kind"] == "PPS"
        assert notices[0]["ingestion_state"] == "COLLECTED"
        detail = live_client.get(f"/api/v1/notices/{notices[0]['notice_key']}").json()
        assert "redacted" not in detail["source_url"]
        assert "%2A%2A%2A" in detail["source_url"]
        assert detail["versions"] == []

        jobs = live_client.get("/api/v1/ingestion/jobs").json()
        assert len(jobs) == 2
        serialised = str(jobs)
        assert "server-side-key" not in serialised
        assert "contact" not in serialised


def test_live_pps_ingestion_requires_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PPS_API_KEY", raising=False)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as unconfigured_client:
        response = unconfigured_client.post(
            "/api/v1/ingestion/pps/notices",
            json={"from_date": "2026-08-16", "to_date": "2026-08-16"},
        )
    assert response.status_code == 503


def test_teams_mock_records_real_adaptive_card_without_external_delivery(client: TestClient) -> None:
    client.post("/api/v1/ingestion/replay")
    payload = {"correlation_id": "mock-SYN-REVIEW-001"}
    first = client.post(
        "/api/v1/notices/SYN-REVIEW-001/notifications/teams/mock",
        json=payload,
    )
    assert first.status_code == 201
    assert first.json()["status"] == "MOCK_RECORDED"
    assert first.json()["delivery_mode"] == "mock"
    assert first.json()["card"]["type"] == "AdaptiveCard"

    duplicate = client.post(
        "/api/v1/notices/SYN-REVIEW-001/notifications/teams/mock",
        json=payload,
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == first.json()["id"]
    records = client.get("/api/v1/notifications/mock").json()
    assert len(records) == 1


def test_openai_public_document_extraction_is_versioned_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "server-side-openai-key")
    monkeypatch.setattr("pai_loop.api.OpenAIExtractionClient", _FakeOpenAIExtractionClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    document_text = (
        "공개 입찰공고입니다. 입찰참가자격은 유효한 사업자등록을 보유한 업체입니다. "
        "제출 마감 전까지 관련 증빙을 제출해야 합니다."
    )
    with TestClient(app) as extraction_client:
        notice = extraction_client.post(
            "/api/v1/notices",
            json={
                "notice_key": "PUBLIC-REAL-001",
                "bid_notice_no": "2026-REAL-1",
                "title": "공개 실공고",
                "agency": "공공기관",
                "deadline": "2026-08-20T09:00:00Z",
            },
        )
        assert notice.status_code == 201
        request_payload = {
            "attachment_id": "ATT-PUBLIC-1",
            "source_label": "입찰공고문.pdf",
            "document_text": document_text,
        }
        first = extraction_client.post(
            "/api/v1/notices/PUBLIC-REAL-001/analysis/extractions",
            json=request_payload,
        )
        assert first.status_code == 201
        assert first.json()["status"] == "ACCEPTED"
        assert first.json()["reused"] is False
        second = extraction_client.post(
            "/api/v1/notices/PUBLIC-REAL-001/analysis/extractions",
            json=request_payload,
        )
        assert second.status_code == 201
        assert second.json()["reused"] is True
        assert second.json()["version_id"] == first.json()["version_id"]

        different_attachment = extraction_client.post(
            "/api/v1/notices/PUBLIC-REAL-001/analysis/extractions",
            json={**request_payload, "attachment_id": "ATT-PUBLIC-2"},
        )
        assert different_attachment.status_code == 201
        assert different_attachment.json()["reused"] is False
        assert different_attachment.json()["version_id"] != first.json()["version_id"]

        detail = extraction_client.get("/api/v1/notices/PUBLIC-REAL-001").json()
        assert detail["ingestion_state"] == "VERSIONED"
        assert len(detail["document_analyses"]) == 2
        serialised = str(detail)
        assert document_text not in serialised
        assert "server-side-openai-key" not in serialised

def test_review_extraction_is_not_reused_as_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "server-side-openai-key")
    monkeypatch.setattr("pai_loop.api.OpenAIExtractionClient", _ReviewOpenAIExtractionClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as extraction_client:
        notice = extraction_client.post(
            "/api/v1/notices",
            json={
                "notice_key": "PUBLIC-REVIEW-001",
                "bid_notice_no": "2026-REVIEW-1",
                "title": "재시도 대상 공개 공고",
                "agency": "공공기관",
                "deadline": "2026-08-20T09:00:00Z",
            },
        )
        assert notice.status_code == 201
        request_payload = {
            "attachment_id": "ATT-REVIEW-1",
            "source_label": "입찰공고문.pdf",
            "document_text": "공개 입찰공고 원문이며 재시도 동작 검증을 위한 충분한 길이입니다.",
        }
        first = extraction_client.post(
            "/api/v1/notices/PUBLIC-REVIEW-001/analysis/extractions",
            json=request_payload,
        ).json()
        second = extraction_client.post(
            "/api/v1/notices/PUBLIC-REVIEW-001/analysis/extractions",
            json=request_payload,
        ).json()

        assert first["status"] == second["status"] == "REVIEW"
        assert first["reused"] is second["reused"] is False
        assert first["version_id"] != second["version_id"]
        detail = extraction_client.get("/api/v1/notices/PUBLIC-REVIEW-001").json()
        assert len(detail["versions"]) == 2
        assert len(detail["document_analyses"]) == 1
        assert detail["document_analyses"][0]["status"] == "REVIEW"
