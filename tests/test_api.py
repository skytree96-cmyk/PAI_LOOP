from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

import pytest

from pai_loop.main import create_app
from pai_loop.models import AnalysisRun, Notice, RecommendationSnapshot
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


class _BrokenPpsClient(_FakePpsClient):
    def iter_notices(self, **_kwargs: object):
        raise RuntimeError("synthetic PPS failure")
        yield  # pragma: no cover


class _PageLimitedPpsClient(_FakePpsClient):
    def iter_notices(self, **kwargs: object):
        self.hit_page_limit = True
        yield from super().iter_notices(**kwargs)


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


def test_public_board_projects_only_latest_persisted_system_recommendation(
    client: TestClient,
) -> None:
    client.post("/api/v1/ingestion/replay")
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == "SYN-PASS-001"))
        assert notice is not None
        evaluation = max(notice.evaluations, key=lambda item: item.evaluated_at)
        version = max(notice.versions, key=lambda item: item.version_no)
        run = AnalysisRun(
            notice_id=notice.id,
            notice_version_id=version.id,
            evaluation_id=evaluation.id,
            status="COMPLETED",
            idempotency_key="public-board-system-recommendation-v1",
            input_sha256="a" * 64,
            output_summary={"eligibility": "PASS"},
        )
        run.recommendations.append(
            RecommendationSnapshot(
                recommendation_key="bid:system",
                rank=0,
                recommendation="GO",
            )
        )
        session.add(run)
        session.commit()

    summary = next(
        item
        for item in client.get("/api/v1/notices").json()
        if item["notice_key"] == "SYN-PASS-001"
    )
    assert summary["recommendation"] == "GO"
    assert summary["recommendation_updated_at"] is not None
    detail = client.get("/api/v1/notices/SYN-PASS-001").json()
    assert detail["recommendation"] == "GO"
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["recommendation_counts"] == {"GO": 1, "HOLD": 0, "NO_GO": 0}
    assert dashboard["go_count"] == 1


def test_public_board_does_not_fall_back_to_stale_system_recommendation(
    client: TestClient,
) -> None:
    client.post("/api/v1/ingestion/replay")
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == "SYN-PASS-001"))
        assert notice is not None
        evaluation = max(notice.evaluations, key=lambda item: item.evaluated_at)
        version = max(notice.versions, key=lambda item: item.version_no)
        completed = AnalysisRun(
            notice_id=notice.id,
            notice_version_id=version.id,
            evaluation_id=evaluation.id,
            status="COMPLETED",
            idempotency_key="stale-system-recommendation-v1",
            input_sha256="b" * 64,
            generated_at=datetime.fromisoformat("2026-08-18T00:00:00+00:00"),
            output_summary={},
        )
        completed.recommendations.append(
            RecommendationSnapshot(
                recommendation_key="bid:system",
                rank=0,
                recommendation="GO",
            )
        )
        superseding_failed = AnalysisRun(
            notice_id=notice.id,
            notice_version_id=version.id,
            evaluation_id=evaluation.id,
            status="FAILED",
            idempotency_key="superseding-failed-analysis-v1",
            input_sha256="c" * 64,
            generated_at=datetime.fromisoformat("2026-08-19T00:00:00+00:00"),
            output_summary={},
        )
        session.add_all([completed, superseding_failed])
        session.commit()

    summary = next(
        item
        for item in client.get("/api/v1/notices").json()
        if item["notice_key"] == "SYN-PASS-001"
    )
    assert summary["recommendation"] is None
    assert summary["recommendation_updated_at"] is None
    assert client.get("/api/v1/dashboard").json()["go_count"] == 0


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
    assert evaluation.json()["risk_score"] is None
    assert evaluation.json()["risk_band"] == "UNKNOWN"
    risk_basis = evaluation.json()["explanation"]["risk"]
    assert risk_basis["status"] == "INSUFFICIENT_EVIDENCE"
    assert risk_basis["evidenced_axis_count"] == 2
    assert risk_basis["axis_basis"]["qualification"]["source"] == (
        "NOTICE_RISK_DIMENSIONS_AUTHORITATIVE_OVERRIDE"
    )


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
        assert first.json()["created_notice_keys"] == first.json()["notice_keys"]
        assert first.json()["updated_notice_keys"] == []

        second = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert second.status_code == 200
        assert second.json()["created"] == 0
        assert second.json()["duplicates"] == 1
        assert second.json()["created_notice_keys"] == []
        assert second.json()["updated_notice_keys"] == []

        notices = live_client.get("/api/v1/notices").json()
        assert len(notices) == 1
        assert notices[0]["source_kind"] == "PPS"
        assert notices[0]["ingestion_state"] == "VERSIONED"
        assert notices[0]["analysis_state"] in {"PENDING", "REVIEW"}
        assert notices[0]["analysis_reason_code"] in {
            "NOT_SELECTED",
            "ATTACHMENT_NONE",
            "HWP_ONLY_UNSUPPORTED",
        }
        assert notices[0]["analysis_reason"]
        detail = live_client.get(f"/api/v1/notices/{notices[0]['notice_key']}").json()
        assert "redacted" not in detail["source_url"]
        assert "%2A%2A%2A" in detail["source_url"]
        assert len(detail["versions"]) == 1
        assert detail["versions"][0]["extraction_status"] == "METADATA"

        jobs = live_client.get("/api/v1/ingestion/jobs").json()
        assert len(jobs) == 2
        serialised = str(jobs)
        assert "server-side-key" not in serialised
        assert "contact" not in serialised


def test_direct_contract_is_audited_but_excluded_from_open_analysis_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")

    class _DirectContractClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            yield {
                "identity": "DIRECT-1|00|2026-08-20T17:00:00+09:00",
                "bid_notice_no": "DIRECT-1",
                "revision_no": "00",
                "title": "공공기관 교육 프로그램 소액수의 안내",
                "agency": "공공기관",
                "published_at": datetime.fromisoformat("2026-08-16T09:00:00+09:00"),
                "deadline": datetime.fromisoformat("2026-08-20T17:00:00+09:00"),
                "estimated_amount": 20_000_000,
                "notice_kind": "등록공고",
                "bid_method": "전자견적",
                "contract_method": "수의계약",
                "award_method": "최저가",
                "direct_contract_signal": True,
                "source_url": None,
                "raw": {
                    "ntceKindNm": "등록공고",
                    "bidMethdNm": "전자견적",
                    "cntrctCnclsMthdNm": "수의계약",
                    "sucsfbidMthdNm": "최저가",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _DirectContractClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={"from_date": "2026-08-16", "to_date": "2026-08-16"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 1
        assert body["matched"] == 1
        assert body["notice_keys"] == []
        assert body["created_notice_keys"] == []
        assert body["updated_notice_keys"] == []
        assert any("수의·직접계약 1건" in warning for warning in body["warnings"])
        assert live_client.get("/api/v1/notices", params={"status": "OPEN"}).json() == []
        all_rows = live_client.get("/api/v1/notices").json()
        assert len(all_rows) == 1
        assert all_rows[0]["status"] == "CLOSED"

        with app.state.session_factory() as session:
            notice = session.scalar(select(Notice).where(Notice.bid_notice_no == "DIRECT-1"))
            assert notice is not None
            metadata = notice.versions[-1].source_payload
            assert metadata["notice_metadata"] == {
                "notice_kind": "등록공고",
                "bid_method": "전자견적",
                "contract_method": "수의계약",
                "award_method": "최저가",
            }


def test_existing_open_notice_is_closed_when_provider_marks_it_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    run = {"direct": False}
    kst = timezone(timedelta(hours=9))
    future_deadline = datetime.now(kst) + timedelta(days=5)
    future_published = future_deadline - timedelta(days=1)

    class _ReclassifiedContractClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            direct = run["direct"]
            contract_method = "수의계약" if direct else "일반경쟁"
            yield {
                "identity": f"RECLASSIFY-1|00|{future_deadline.isoformat()}",
                "bid_notice_no": "RECLASSIFY-1",
                "revision_no": "00",
                "title": "공공기관 교육 프로그램 운영",
                "agency": "공공기관",
                "published_at": future_published,
                "deadline": future_deadline,
                "estimated_amount": 20_000_000,
                "notice_kind": "등록공고",
                "contract_method": contract_method,
                "direct_contract_signal": direct,
                "source_url": None,
                "raw": {"cntrctCnclsMthdNm": contract_method},
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _ReclassifiedContractClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200
        assert len(first.json()["created_notice_keys"]) == 1
        assert len(live_client.get("/api/v1/notices", params={"status": "OPEN"}).json()) == 1

        run["direct"] = True
        second = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert second.status_code == 200
        assert second.json()["updated"] == 1
        assert second.json()["notice_keys"] == []
        assert second.json()["updated_notice_keys"] == []
        assert live_client.get("/api/v1/notices", params={"status": "OPEN"}).json() == []


def test_live_pps_ingestion_requires_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PPS_API_KEY", raising=False)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as unconfigured_client:
        response = unconfigured_client.post(
            "/api/v1/ingestion/pps/notices",
            json={"from_date": "2026-08-16", "to_date": "2026-08-16"},
        )
    assert response.status_code == 503


def test_profile_ingestion_queries_every_department_with_bounded_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsClient", _FakePpsClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={
                "from_date": "2026-08-16",
                "to_date": "2026-08-16",
                "use_profile_keywords": True,
                "max_pages": 1,
                "dry_run": True,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["keywords_used"][:5] == ["교육", "컨설팅", "연수", "포럼", "위탁 운영"]
    assert len(body["keywords_used"]) == 29
    assert len(set(body["keywords_used"])) == 29
    assert body["provider_queries"] == 29
    assert body["department_coverage_count"] == 24


def test_profile_ingestion_reports_partial_when_only_subset_of_terms_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsClient", _FakePpsClient)
    clock = iter([0.0, 0.0, 200.0])
    monkeypatch.setattr(
        "pai_loop.api.time",
        SimpleNamespace(monotonic=lambda: next(clock, 200.0)),
    )
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={
                "from_date": "2026-08-16",
                "to_date": "2026-08-16",
                "use_profile_keywords": True,
                "max_pages": 1,
                "dry_run": True,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PARTIAL"
    assert body["provider_queries"] == 1
    assert len(body["keywords_used"]) == 29
    assert body["department_coverage_count"] == 24
    assert any("시간 제한" in warning for warning in body["warnings"])


def test_pps_page_cap_is_never_reported_as_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsClient", _PageLimitedPpsClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={
                "from_date": "2026-08-16",
                "to_date": "2026-08-17",
                "keywords": ["교육"],
                "page_size": 100,
                "max_pages": 1,
                "dry_run": True,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PARTIAL"
    assert body["provider_queries"] == 1
    assert body["created_notice_keys"]
    assert any("max_pages 제한" in warning for warning in body["warnings"])


def test_pps_unexpected_client_error_finishes_failed_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsClient", _BrokenPpsClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app, raise_server_exceptions=False) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={"from_date": "2026-08-16", "to_date": "2026-08-16"},
        )
        assert response.status_code == 500
        jobs = live_client.get("/api/v1/ingestion/jobs").json()
    assert jobs[0]["status"] == "FAILED"
    assert jobs[0]["error_code"] == "PPS_CLIENT_ERROR"
    assert jobs[0]["completed_at"] is not None


def test_new_pps_revision_closes_prior_row_and_open_filter_hides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    run = {"revision": "00"}

    class _RevisionClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            revision = run["revision"]
            deadline = "2026-08-20T17:00:00+09:00" if revision == "00" else "2026-08-22T17:00:00+09:00"
            yield {
                "identity": f"20260816001|{revision}|{deadline}",
                "bid_notice_no": "20260816001",
                "revision_no": revision,
                "title": "공공기관 교육 컨설팅 용역",
                "agency": "공공기관",
                "published_at": datetime.fromisoformat("2026-08-16T09:00:00+09:00"),
                "deadline": datetime.fromisoformat(deadline),
                "estimated_amount": 100_000_000,
                "source_url": None,
                "raw": {},
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _RevisionClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        assert live_client.post("/api/v1/ingestion/pps/notices", json=payload).status_code == 200
        run["revision"] = "01"
        assert live_client.post("/api/v1/ingestion/pps/notices", json=payload).status_code == 200
        all_rows = live_client.get("/api/v1/notices").json()
        open_rows = live_client.get("/api/v1/notices", params={"status": "OPEN"}).json()
    assert len(all_rows) == 2
    assert {item["revision_no"]: item["status"] for item in all_rows} == {
        "00": "CLOSED",
        "01": "OPEN",
    }
    assert [item["revision_no"] for item in open_rows] == ["01"]


def test_pps_cancel_notice_closes_existing_row_and_never_creates_open_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    run = {"cancelled": False}

    class _CancellationClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            yield {
                "identity": "R26BK-CANCEL|00|2026-08-22T17:00:00+09:00",
                "bid_notice_no": "R26BK-CANCEL",
                "revision_no": "00",
                "title": "공공기관 교육 용역",
                "agency": "공공기관",
                "published_at": datetime.fromisoformat("2026-08-16T09:00:00+09:00"),
                "deadline": datetime.fromisoformat("2026-08-22T17:00:00+09:00"),
                "estimated_amount": 100_000_000,
                "notice_kind": "취소공고" if run["cancelled"] else "등록공고",
                "source_url": None,
                "raw": {"ntceKindNm": "취소공고" if run["cancelled"] else "등록공고"},
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _CancellationClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200
        assert first.json()["created"] == 1
        run["cancelled"] = True
        cancelled = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert cancelled.status_code == 200
        assert cancelled.json()["matched"] == 0
        assert cancelled.json()["updated"] == 1
        assert live_client.get("/api/v1/notices", params={"status": "OPEN"}).json() == []
        all_rows = live_client.get("/api/v1/notices").json()
    assert len(all_rows) == 1
    assert all_rows[0]["status"] == "CLOSED"


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
    fact_set = next(item for item in first.json()["card"]["body"] if item["type"] == "FactSet")
    competition_fact = next(item for item in fact_set["facts"] if item["title"] == "경쟁·집중 리스크")
    assert competition_fact["value"] == "UNKNOWN · 표본/커버리지 부족"

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
