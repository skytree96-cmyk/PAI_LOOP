from __future__ import annotations

from fastapi.testclient import TestClient

import pytest

from pai_loop.main import create_app


def test_health_and_empty_dashboard(client: TestClient) -> None:
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database"] == "ok"

    dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["totals"] == {"notices": 0, "evaluations": 0, "decisions": 0}


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


def test_production_api_auth_bypasses_health_but_protects_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "production")
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
