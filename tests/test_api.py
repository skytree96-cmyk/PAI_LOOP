from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

import pytest

from pai_loop.api import _authoritative_pps_events, _stored_notice_authority_row
from pai_loop.main import create_app
from pai_loop.models import (
    AnalysisRun,
    Evaluation,
    IngestionJob,
    Notice,
    PpsNoticeAuthority,
    RecommendationSnapshot,
)
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
    assert dashboard.json()["ended_count"] == 0
    assert dashboard.json()["analyzed_ended_count"] == 0
    assert dashboard.json()["closed_count"] == 0
    assert dashboard.json()["expired_count"] == 0
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


def test_ended_scope_combines_expired_and_closed_with_dashboard_list_parity(
    client: TestClient,
) -> None:
    client.post("/api/v1/ingestion/replay")
    with client.app.state.session_factory() as session:
        expired = session.scalar(select(Notice).where(Notice.notice_key == "SYN-PASS-001"))
        closed = session.scalar(select(Notice).where(Notice.notice_key == "SYN-REVIEW-001"))
        assert expired is not None and closed is not None
        expired.status = "OPEN"
        expired.deadline = datetime.fromisoformat("2020-01-01T09:00:00+00:00")
        closed.status = "CLOSED"
        session.commit()

    dashboard = client.get("/api/v1/dashboard").json()
    ended_rows = client.get("/api/v1/notices", params={"status": "ENDED"}).json()
    analyzed_ended_rows = client.get(
        "/api/v1/notices",
        params={"status": "ENDED", "analysis_state": "EVALUATED"},
    ).json()
    assert dashboard["totals"]["active"] == 1
    assert dashboard["ended_count"] == len(ended_rows) == 2
    assert dashboard["analyzed_ended_count"] == len(analyzed_ended_rows) == 2
    assert dashboard["closed_count"] == 1
    assert dashboard["expired_count"] == 1
    assert {item["status"] for item in analyzed_ended_rows} == {"CLOSED", "EXPIRED"}
    assert len(client.get("/api/v1/notices", params={"status": "CLOSED"}).json()) == 1
    assert len(client.get("/api/v1/notices", params={"status": "EXPIRED"}).json()) == 1
    assert len(client.get("/api/v1/notices", params={"status": "OPEN"}).json()) == 1


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
        with live_client.app.state.session_factory() as session:
            ingestion_job = session.get(IngestionJob, first.json()["job_id"])
            assert ingestion_job is not None
            scope = ingestion_job.request_json
            assert scope["material_scope_version"] == "pps-material-notice-keys-v1"
            assert scope["material_notice_keys"] == sorted(first.json()["notice_keys"])
            assert scope["material_notice_key_count"] == 1
            assert len(scope["material_notice_keys_sha256"]) == 64

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
    first_deadline = datetime.now(timezone.utc) + timedelta(days=5)

    class _RevisionClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            revision = run["revision"]
            deadline = (
                first_deadline
                if revision == "00"
                else first_deadline + timedelta(days=2)
            )
            yield {
                "identity": f"20260816001|{revision}|{deadline.isoformat()}",
                "bid_notice_no": "20260816001",
                "revision_no": revision,
                "title": "공공기관 교육 컨설팅 용역",
                "agency": "공공기관",
                "published_at": datetime.fromisoformat("2026-08-16T09:00:00+09:00"),
                "deadline": deadline,
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


@pytest.mark.parametrize("reverse_rows", [False, True])
def test_authoritative_pps_event_keeps_keyword_union_when_changed_at_selects_winner(
    reverse_rows: bool,
) -> None:
    published = datetime(2026, 8, 16, 9, tzinfo=timezone.utc)
    first_deadline = published + timedelta(days=5)
    extended_deadline = first_deadline + timedelta(days=3)
    original = {
        "identity": f"R26BK-KEYWORD-UNION|00|{first_deadline.isoformat()}",
        "bid_notice_no": "R26BK-KEYWORD-UNION",
        "revision_no": "00",
        "title": "최초 공고",
        "published_at": published,
        "provider_changed_at": published + timedelta(minutes=1),
        "deadline": first_deadline,
        "notice_kind": "등록공고",
        "_search_keywords": ["교육", "AI"],
    }
    extension = {
        **original,
        "identity": f"R26BK-KEYWORD-UNION|00|{extended_deadline.isoformat()}",
        "title": "기한 연장 공고",
        "provider_changed_at": published + timedelta(minutes=2),
        "deadline": extended_deadline,
        "notice_kind": "연장공고",
        "_search_keywords": ["컨설팅", "AI"],
    }
    rows = [original, extension]
    if reverse_rows:
        rows.reverse()

    authoritative, superseded = _authoritative_pps_events(
        rows,
        now=published,
    )

    assert superseded == 1
    assert len(authoritative) == 1
    assert authoritative[0]["title"] == "기한 연장 공고"
    assert authoritative[0]["deadline"] == extended_deadline
    assert authoritative[0]["_search_keywords"] == ["AI", "교육", "컨설팅"]
    assert extension["_search_keywords"] == ["컨설팅", "AI"]


def test_multi_keyword_ingestion_persists_all_query_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    published = datetime.now(timezone.utc)
    deadline = published + timedelta(days=5)

    class _MultiKeywordDuplicateClient(_FakePpsClient):
        def iter_notices(self, **kwargs: object):
            extra_params = kwargs.get("extra_params")
            assert isinstance(extra_params, dict)
            keyword = str(extra_params["bidNtceNm"])
            changed_offset = {"교육": 1, "컨설팅": 2}[keyword]
            yield {
                "identity": f"R26BK-MULTI-QUERY|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-MULTI-QUERY",
                "revision_no": "00",
                "title": "교육 컨설팅 통합 용역",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published + timedelta(minutes=changed_offset),
                "deadline": deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "등록공고",
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-MULTI-QUERY",
                    "bidNtceOrd": "00",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _MultiKeywordDuplicateClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={
                "from_date": "2026-08-16",
                "to_date": "2026-08-16",
                "keywords": ["교육", "컨설팅"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["created"] == 1
        with app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(Notice.bid_notice_no == "R26BK-MULTI-QUERY")
            )
            assert notice is not None
            metadata = max(notice.versions, key=lambda version: version.version_no)
            assert metadata.source_payload["provenance"]["search_keywords"] == [
                "교육",
                "컨설팅",
            ]


def test_undated_same_revision_cancellation_is_fail_closed_but_higher_revision_wins() -> None:
    published = datetime(2026, 8, 16, 9, tzinfo=timezone.utc)
    registered = {
        "identity": "R26BK-UNDATED-CANCEL|00|registered",
        "bid_notice_no": "R26BK-UNDATED-CANCEL",
        "revision_no": "00",
        "title": "등록 공고",
        "published_at": published,
        "provider_changed_at": None,
        "deadline": published + timedelta(days=5),
        "notice_kind": "등록공고",
    }
    undated_cancellation = {
        **registered,
        "identity": "R26BK-UNDATED-CANCEL|00|cancelled",
        "title": "취소 공고",
        "published_at": None,
        "deadline": None,
        "notice_kind": "취소공고",
    }

    authoritative, _superseded = _authoritative_pps_events(
        [registered, undated_cancellation],
        now=published,
    )
    assert authoritative[0]["notice_kind"] == "취소공고"

    higher_revision = {
        **registered,
        "identity": "R26BK-UNDATED-CANCEL|01|registered",
        "revision_no": "01",
        "title": "상위 차수 재등록 공고",
    }
    authoritative, _superseded = _authoritative_pps_events(
        [registered, undated_cancellation, higher_revision],
        now=published,
    )
    assert authoritative[0]["revision_no"] == "01"
    assert authoritative[0]["notice_kind"] == "등록공고"


def test_historical_cancellation_does_not_suppress_newer_re_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    future = datetime.now(timezone.utc) + timedelta(days=10)

    class _CancelledThenRegisteredClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            common = {
                "bid_notice_no": "R26BK-REOPENED",
                "title": "연장 후 재등록된 교육 용역",
                "agency": "공공기관",
                "estimated_amount": 100_000_000,
                "source_url": None,
                "raw": {},
            }
            yield {
                **common,
                "identity": f"R26BK-REOPENED|00|{future.isoformat()}",
                "revision_no": "00",
                "published_at": future - timedelta(days=3),
                "deadline": future,
                "notice_kind": "취소공고",
            }
            yield {
                **common,
                "identity": f"R26BK-REOPENED|01|{(future + timedelta(days=2)).isoformat()}",
                "revision_no": "01",
                "published_at": future - timedelta(days=2),
                "deadline": future + timedelta(days=2),
                "notice_kind": "등록공고",
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _CancelledThenRegisteredClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={"from_date": "2026-08-16", "to_date": "2026-08-16"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 1
        assert len(body["created_notice_keys"]) == 1
        assert not any("취소공고" in warning for warning in body["warnings"])
        rows = live_client.get("/api/v1/notices", params={"status": "OPEN"}).json()
    assert len(rows) == 1
    assert rows[0]["revision_no"] == "01"


def test_newer_expired_revision_closes_older_still_open_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    now = datetime.now(timezone.utc)

    class _NewerExpiredRevisionClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            common = {
                "bid_notice_no": "R26BK-CORRECTED",
                "title": "마감 정정 교육 용역",
                "agency": "공공기관",
                "estimated_amount": 100_000_000,
                "notice_kind": "정정공고",
                "source_url": None,
                "raw": {},
            }
            yield {
                **common,
                "identity": f"R26BK-CORRECTED|00|{(now + timedelta(days=5)).isoformat()}",
                "revision_no": "00",
                "published_at": now - timedelta(days=4),
                "deadline": now + timedelta(days=5),
            }
            yield {
                **common,
                "identity": f"R26BK-CORRECTED|01|{(now - timedelta(days=1)).isoformat()}",
                "revision_no": "01",
                "published_at": now - timedelta(days=2),
                "deadline": now - timedelta(days=1),
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _NewerExpiredRevisionClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as live_client:
        response = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={"from_date": "2026-08-16", "to_date": "2026-08-16"},
        )
        assert response.status_code == 200, response.text
        rows = live_client.get("/api/v1/notices").json()
        open_rows = live_client.get("/api/v1/notices", params={"status": "OPEN"}).json()
    assert len(rows) == 1
    assert rows[0]["revision_no"] == "01"
    assert rows[0]["status"] == "EXPIRED"
    assert open_rows == []


def test_deadline_extension_with_same_revision_supersedes_and_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    first_deadline = datetime.now(timezone.utc) + timedelta(days=5)
    extended_deadline = first_deadline + timedelta(days=7)
    run = {"extended": False}

    class _DeadlineExtensionClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            deadlines = (
                [first_deadline, extended_deadline]
                if run["extended"]
                else [first_deadline]
            )
            for index, deadline in enumerate(deadlines):
                yield {
                    "identity": f"R26BK-EXTENDED|00|{deadline.isoformat()}",
                    "bid_notice_no": "R26BK-EXTENDED",
                    "revision_no": "00",
                    "title": "접수기한 연장 교육 용역",
                    "agency": "공공기관",
                    "published_at": datetime.now(timezone.utc) + timedelta(minutes=index),
                    "deadline": deadline,
                    "estimated_amount": 100_000_000,
                    "notice_kind": "연장공고" if index else "등록공고",
                    "source_url": None,
                    "raw": {},
                }

    monkeypatch.setattr("pai_loop.api.PpsClient", _DeadlineExtensionClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200, first.text
        original_key = first.json()["created_notice_keys"][0]

        run["extended"] = True
        second = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert second.status_code == 200, second.text
        body = second.json()
        assert len(body["created_notice_keys"]) == 1
        assert body["created_notice_keys"][0] != original_key
        rows = live_client.get("/api/v1/notices").json()
        open_rows = live_client.get("/api/v1/notices", params={"status": "OPEN"}).json()
    assert len(rows) == 2
    assert {item["notice_key"]: item["status"] for item in rows}[original_key] == "CLOSED"
    assert len(open_rows) == 1
    assert open_rows[0]["notice_key"] == body["created_notice_keys"][0]
    assert datetime.fromisoformat(open_rows[0]["deadline"]).replace(
        tzinfo=timezone.utc
    ) == extended_deadline


def test_same_key_provider_update_hides_stale_evaluation_and_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    updated = {"value": False}
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _SameKeyMaterialUpdateClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            yield {
                "identity": f"R26BK-SAME-KEY|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-SAME-KEY",
                "revision_no": "00",
                "title": "동일 키 정정 교육 용역",
                "agency": "공공기관",
                "published_at": datetime.now(timezone.utc),
                "deadline": deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "정정공고" if updated["value"] else "등록공고",
                "source_url": None,
                "raw": {
                    "ntceKindNm": "정정공고" if updated["value"] else "등록공고"
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _SameKeyMaterialUpdateClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200, first.text
        notice_key = first.json()["created_notice_keys"][0]

        with app.state.session_factory() as session:
            notice = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
            assert notice is not None
            basis = max(notice.versions, key=lambda item: item.version_no)
            evaluation = Evaluation(
                notice_id=notice.id,
                notice_version_id=basis.id,
                deadline_snapshot_at=notice.deadline,
                eligibility="PASS",
                reason_code="PASS",
                readiness_score=100,
                readiness_status="GREEN",
                evidence_coverage=100,
                risk_score=10,
                risk_band="GO",
                ruleset_version="test",
                atomic_results=[],
                explanation={},
            )
            run = AnalysisRun(
                notice_id=notice.id,
                notice_version_id=basis.id,
                status="COMPLETED",
                idempotency_key="same-key-before-provider-update",
                input_sha256="d" * 64,
                output_summary={"eligibility": "PASS"},
            )
            run.evaluation = evaluation
            run.recommendations.append(
                RecommendationSnapshot(
                    recommendation_key="bid:system",
                    rank=0,
                    recommendation="GO",
                )
            )
            session.add(run)
            session.commit()

        before = live_client.get(f"/api/v1/notices/{notice_key}").json()
        assert before["latest_evaluation"]["eligibility"] == "PASS"
        assert before["recommendation"] == "GO"

        updated["value"] = True
        refresh = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert refresh.status_code == 200, refresh.text
        assert refresh.json()["updated_notice_keys"] == [notice_key]

        after = live_client.get(f"/api/v1/notices/{notice_key}").json()
        assert after["latest_evaluation"] is None
        assert after["recommendation"] is None
        assert after["ingestion_state"] == "VERSIONED"
        current_evaluated = live_client.get(
            "/api/v1/notices",
            params={"analysis_state": "EVALUATED"},
        ).json()
        assert all(item["notice_key"] != notice_key for item in current_evaluated)
        briefing = live_client.get("/api/v1/operations/daily-briefing").json()
    assert briefing["notices"][0]["fit"]["eligibility"] == "PENDING"
    assert briefing["notices"][0]["analysis_snapshot"] is None


def test_keyword_and_changed_clock_only_aggregate_provenance_without_reanalysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _ProvenanceOnlyClient(_FakePpsClient):
        def iter_notices(self, **kwargs: object):
            extra = kwargs.get("extra_params")
            keyword = str(extra["bidNtceNm"]) if isinstance(extra, dict) else ""
            changed = published + timedelta(
                minutes=2 if keyword == "컨설팅" else 1
            )
            yield {
                "identity": f"R26BK-PROVENANCE-API|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-PROVENANCE-API",
                "revision_no": "00",
                "title": "교육 컨설팅 용역",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": changed,
                "deadline": deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "등록공고",
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-PROVENANCE-API",
                    "bidNtceOrd": "00",
                    "ntceKindNm": "등록공고",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _ProvenanceOnlyClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    base = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={**base, "keywords": ["교육"]},
        )
        assert first.status_code == 200, first.text
        with app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(
                    Notice.bid_notice_no == "R26BK-PROVENANCE-API"
                )
            )
            assert notice is not None
            first_basis = max(notice.versions, key=lambda value: value.version_no)
            original = (
                first_basis.id,
                first_basis.version_no,
                first_basis.file_sha256,
            )

        second = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={**base, "keywords": ["교육", "컨설팅"]},
        )
        assert second.status_code == 200, second.text
        assert second.json()["updated_notice_keys"] == []
        with app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(
                    Notice.bid_notice_no == "R26BK-PROVENANCE-API"
                )
            )
            assert notice is not None
            metadata = [
                value
                for value in notice.versions
                if value.source_payload.get("kind") == "PPS_NOTICE_METADATA"
            ]
            assert len(metadata) == 1
            assert (
                metadata[0].id,
                metadata[0].version_no,
                metadata[0].file_sha256,
            ) == original
            assert metadata[0].source_payload["provenance"]["search_keywords"] == [
                "교육",
                "컨설팅",
            ]
            second_job = session.get(IngestionJob, second.json()["job_id"])
            assert second_job is not None
            assert second_job.request_json["material_notice_keys"] == []
            assert second_job.request_json["material_notice_key_count"] == 0


@pytest.mark.parametrize(
    ("field", "updated_value", "basis_key", "expected_basis"),
    [
        ("title", "정정된 교육 용역", "title", "정정된 교육 용역"),
        ("agency", "정정 발주기관", "agency", "정정 발주기관"),
        ("estimated_amount", 250_000_000, "estimated_amount", "250000000"),
    ],
)
def test_same_key_canonical_material_change_versions_and_requeues(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    updated_value: object,
    basis_key: str,
    expected_basis: object,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    state = {"changed": False}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _CanonicalChangeClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            values = {
                "title": "교육 용역",
                "agency": "공공기관",
                "estimated_amount": 100_000_000,
            }
            if state["changed"]:
                values[field] = updated_value
            yield {
                "identity": f"R26BK-CANON-{field}|00|{deadline.isoformat()}",
                "bid_notice_no": f"R26BK-CANON-{field}",
                "revision_no": "00",
                **values,
                "published_at": published,
                "provider_changed_at": published
                + timedelta(minutes=2 if state["changed"] else 1),
                "deadline": deadline,
                "notice_kind": "등록공고",
                "source_url": None,
                "raw": {
                    "bidNtceNo": f"R26BK-CANON-{field}",
                    "bidNtceOrd": "00",
                    "ntceKindNm": "등록공고",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _CanonicalChangeClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200, first.text
        notice_key = first.json()["created_notice_keys"][0]
        state["changed"] = True
        second = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert second.status_code == 200, second.text
        assert second.json()["updated_notice_keys"] == [notice_key]
        with app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(Notice.notice_key == notice_key)
            )
            assert notice is not None
            metadata = sorted(
                (
                    value
                    for value in notice.versions
                    if value.source_payload.get("kind") == "PPS_NOTICE_METADATA"
                ),
                key=lambda value: value.version_no,
            )
            assert len(metadata) == 2
            assert metadata[0].file_sha256 != metadata[1].file_sha256
            assert metadata[1].source_payload["canonical_notice_basis"][basis_key] == (
                expected_basis
            )


def test_cross_run_cancellation_tombstone_survives_retention_and_blocks_stale_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"value": "registered"}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _CrossRunCancelClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            cancelled = phase["value"] == "cancelled"
            yield {
                "identity": f"R26BK-CROSS-CANCEL|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-CROSS-CANCEL",
                "revision_no": "00",
                "title": "취소 공고" if cancelled else "교육 용역",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published
                + timedelta(minutes=2 if cancelled else 1),
                "deadline": None if cancelled else deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "취소공고" if cancelled else "등록공고",
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-CROSS-CANCEL",
                    "bidNtceOrd": "00",
                    "ntceKindNm": "취소공고" if cancelled else "등록공고",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _CrossRunCancelClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post(
            "/api/v1/ingestion/pps/notices", json=payload
        )
        assert first.status_code == 200
        notice_key = first.json()["created_notice_keys"][0]
        with app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(Notice.notice_key == notice_key)
            )
            assert notice is not None
            basis = max(notice.versions, key=lambda value: value.version_no)
            evaluation = Evaluation(
                notice_id=notice.id,
                notice_version_id=basis.id,
                deadline_snapshot_at=notice.deadline,
                eligibility="PASS",
                reason_code="PASS",
                readiness_score=100,
                readiness_status="GREEN",
                evidence_coverage=100,
                risk_score=10,
                risk_band="GO",
                ruleset_version="same-rev-reopen-test",
                atomic_results=[],
                explanation={},
            )
            run = AnalysisRun(
                notice_id=notice.id,
                notice_version_id=basis.id,
                status="COMPLETED",
                idempotency_key="same-rev-reopen-before-cancel",
                input_sha256="a" * 64,
                output_summary={"eligibility": "PASS"},
            )
            run.evaluation = evaluation
            run.recommendations.append(
                RecommendationSnapshot(
                    recommendation_key="bid:system",
                    rank=0,
                    recommendation="GO",
                )
            )
            session.add(run)
            session.commit()
        phase["value"] = "cancelled"
        cancelled = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert cancelled.status_code == 200, cancelled.text
        with app.state.session_factory() as session:
            old = datetime.now(timezone.utc) - timedelta(days=8)
            for job in session.scalars(select(IngestionJob)).all():
                job.created_at = old
                job.completed_at = old
            session.commit()
        retained = live_client.post(
            "/api/v1/operations/retention",
            json={"retention_days": 7, "dry_run": False},
        )
        assert retained.status_code == 200, retained.text
        assert "pps_notice_authorities" in retained.json()["preserved"]
        with app.state.session_factory() as session:
            assert session.get(IngestionJob, cancelled.json()["job_id"]) is None
            authority = session.get(
                PpsNoticeAuthority,
                "R26BK-CROSS-CANCEL",
            )
            assert authority is not None
            assert authority.disposition == "CANCELLED"

        phase["value"] = "registered"
        stale = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert stale.status_code == 200, stale.text
        assert stale.json()["matched"] == 0
        assert any("상태를 되돌리지 않고" in item for item in stale.json()["warnings"])
        rows = live_client.get("/api/v1/notices").json()
        assert len(rows) == 1
        assert rows[0]["status"] == "CLOSED"


def test_cross_run_extension_authority_blocks_stale_original_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"value": "original"}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    original_deadline = datetime.now(timezone.utc) + timedelta(days=5)
    extended_deadline = original_deadline + timedelta(days=5)

    class _CrossRunExtensionClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            extended = phase["value"] == "extended"
            deadline = extended_deadline if extended else original_deadline
            yield {
                "identity": f"R26BK-CROSS-EXTEND|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-CROSS-EXTEND",
                "revision_no": "00",
                "title": "교육 용역 연장" if extended else "교육 용역",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published
                + timedelta(minutes=2 if extended else 1),
                "deadline": deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "연장공고" if extended else "등록공고",
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-CROSS-EXTEND",
                    "bidNtceOrd": "00",
                    "ntceKindNm": "연장공고" if extended else "등록공고",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _CrossRunExtensionClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200, first.text
        phase["value"] = "extended"
        extended = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert extended.status_code == 200, extended.text
        extended_key = extended.json()["created_notice_keys"][0]
        phase["value"] = "original"
        stale = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert stale.status_code == 200, stale.text
        assert stale.json()["matched"] == 0
        open_rows = live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json()
        assert [item["notice_key"] for item in open_rows] == [extended_key]


def test_dry_run_previews_lifecycle_close_and_stale_authority_like_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"value": "registered"}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _DryRunAuthorityClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            cancelled = phase["value"] == "cancelled"
            yield {
                "identity": f"R26BK-DRY-AUTH|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-DRY-AUTH",
                "revision_no": "00",
                "title": "취소" if cancelled else "교육 용역",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published
                + timedelta(minutes=2 if cancelled else 1),
                "deadline": None if cancelled else deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "취소공고" if cancelled else "등록공고",
                "source_url": None,
                "raw": {},
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _DryRunAuthorityClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    base_payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post(
            "/api/v1/ingestion/pps/notices", json=base_payload
        )
        assert first.status_code == 200, first.text

        phase["value"] = "cancelled"
        preview_close = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={**base_payload, "dry_run": True},
        )
        assert preview_close.status_code == 200, preview_close.text
        assert preview_close.json()["updated"] == 1
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json()

        live_close = live_client.post(
            "/api/v1/ingestion/pps/notices", json=base_payload
        )
        assert live_close.status_code == 200, live_close.text
        assert live_close.json()["updated"] == preview_close.json()["updated"]
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []

        phase["value"] = "registered"
        preview_stale = live_client.post(
            "/api/v1/ingestion/pps/notices",
            json={**base_payload, "dry_run": True},
        )
        live_stale = live_client.post(
            "/api/v1/ingestion/pps/notices", json=base_payload
        )
        assert preview_stale.status_code == live_stale.status_code == 200
        for field in ("matched", "duplicates", "updated", "notice_keys"):
            assert preview_stale.json()[field] == live_stale.json()[field]
        assert preview_stale.json()["matched"] == 0
        assert preview_stale.json()["duplicates"] == 1
        assert any(
            "상태를 되돌리지 않고" in warning
            for warning in preview_stale.json()["warnings"]
        )
        assert any(
            "상태를 되돌리지 않고" in warning
            for warning in live_stale.json()["warnings"]
        )
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []


def test_malformed_authority_is_fail_closed_only_when_newer_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"value": "valid-01"}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _MalformedAuthorityClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            config = {
                "valid-01": ("01", "교육 용역 최신", deadline, 2),
                "malformed-00": ("00", "", None, 3),
                "malformed-02": ("02", "", None, 4),
                "stale-valid-01": ("01", "교육 용역 구버전", deadline, 5),
            }[phase["value"]]
            revision, title, event_deadline, minute = config
            yield {
                "identity": (
                    f"R26BK-MALFORMED|{revision}|"
                    f"{event_deadline.isoformat() if event_deadline else ''}"
                ),
                "bid_notice_no": "R26BK-MALFORMED",
                "revision_no": revision,
                "title": title,
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published + timedelta(minutes=minute),
                "deadline": event_deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "정정공고",
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-MALFORMED",
                    "bidNtceOrd": revision,
                    "ntceKindNm": "정정공고",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _MalformedAuthorityClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200, first.text
        phase["value"] = "malformed-00"
        lower = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert lower.status_code == 200, lower.text
        assert lower.json()["matched"] == 0
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json()

        phase["value"] = "malformed-02"
        higher = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert higher.status_code == 200, higher.text
        assert higher.json()["matched"] == 0
        assert any("기존 하위 공고를 종료" in item for item in higher.json()["warnings"])
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []

        phase["value"] = "stale-valid-01"
        stale = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert stale.status_code == 200, stale.text
        assert stale.json()["matched"] == 0
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []


def test_same_revision_newer_dated_registration_reopens_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"value": "registered"}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _SameRevisionReopenClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            phases = {"registered": (1, "등록공고"), "cancelled": (2, "취소공고"), "reopened": (3, "등록공고")}
            minute, kind = phases[phase["value"]]
            yield {
                "identity": f"R26BK-SAME-REV-REOPEN|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-SAME-REV-REOPEN",
                "revision_no": "00",
                "title": "재등록 교육 용역",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published + timedelta(minutes=minute),
                "deadline": None if kind == "취소공고" else deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": kind,
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-SAME-REV-REOPEN",
                    "bidNtceOrd": "00",
                    "ntceKindNm": kind,
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _SameRevisionReopenClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post(
            "/api/v1/ingestion/pps/notices", json=payload
        )
        assert first.status_code == 200
        notice_key = first.json()["created_notice_keys"][0]
        with app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(Notice.notice_key == notice_key)
            )
            assert notice is not None
            basis = max(notice.versions, key=lambda value: value.version_no)
            evaluation = Evaluation(
                notice_id=notice.id,
                notice_version_id=basis.id,
                deadline_snapshot_at=notice.deadline,
                eligibility="PASS",
                reason_code="PASS",
                readiness_score=100,
                readiness_status="GREEN",
                evidence_coverage=100,
                risk_score=10,
                risk_band="GO",
                ruleset_version="same-rev-reopen-test",
                atomic_results=[],
                explanation={},
            )
            run = AnalysisRun(
                notice_id=notice.id,
                notice_version_id=basis.id,
                status="COMPLETED",
                idempotency_key="same-rev-reopen-before-cancel",
                input_sha256="a" * 64,
                output_summary={"eligibility": "PASS"},
            )
            run.evaluation = evaluation
            run.recommendations.append(
                RecommendationSnapshot(
                    recommendation_key="bid:system",
                    rank=0,
                    recommendation="GO",
                )
            )
            session.add(run)
            session.commit()
        phase["value"] = "cancelled"
        assert live_client.post(
            "/api/v1/ingestion/pps/notices", json=payload
        ).status_code == 200
        phase["value"] = "reopened"
        reopened = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["updated_notice_keys"] == [notice_key]
        open_rows = live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json()
        assert len(open_rows) == 1
        assert open_rows[0]["bid_notice_no"] == "R26BK-SAME-REV-REOPEN"
        detail = live_client.get(f"/api/v1/notices/{notice_key}").json()
        assert detail["latest_evaluation"] is None
        assert detail["recommendation"] is None
        with app.state.session_factory() as session:
            notice = session.scalar(
                select(Notice).where(Notice.notice_key == notice_key)
            )
            assert notice is not None
            metadata = [
                value
                for value in notice.versions
                if value.source_payload.get("kind") == "PPS_NOTICE_METADATA"
            ]
            assert len(metadata) == 2
            assert metadata[0].id != metadata[1].id
            assert metadata[0].file_sha256 == metadata[1].file_sha256


def test_direct_contract_tie_cannot_reopen_from_partial_provider_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"value": "direct"}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _DirectTieClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            direct = phase["value"] == "direct"
            corrected = phase["value"] == "corrected"
            yield {
                "identity": f"R26BK-DIRECT-TIE|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-DIRECT-TIE",
                "revision_no": "00",
                "title": "교육 프로그램 운영",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published
                + timedelta(minutes=2 if corrected else 1),
                "deadline": deadline,
                "estimated_amount": 20_000_000,
                "notice_kind": "정정공고" if corrected else "등록공고",
                "direct_contract_signal": direct,
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-DIRECT-TIE",
                    "bidNtceOrd": "00",
                    "ntceKindNm": "정정공고" if corrected else "등록공고",
                    **({"cntrctCnclsMthdNm": "수의계약"} if direct else {}),
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _DirectTieClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200, first.text
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []

        phase["value"] = "partial"
        partial = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert partial.status_code == 200, partial.text
        assert partial.json()["matched"] == 0
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []

        phase["value"] = "corrected"
        corrected = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert corrected.status_code == 200, corrected.text
        open_rows = live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json()
        assert len(open_rows) == 1
        assert open_rows[0]["bid_notice_no"] == "R26BK-DIRECT-TIE"


@pytest.mark.parametrize(
    ("contract_method", "bid_method", "direct_title"),
    [
        ("직접계약", "", "교육 프로그램 운영"),
        ("", "직접 계약", "교육 프로그램 운영"),
        ("", "", "교육 프로그램 수의 운영"),
    ],
)
def test_legacy_direct_contract_metadata_blocks_equal_partial_reopen(
    monkeypatch: pytest.MonkeyPatch,
    contract_method: str,
    bid_method: str,
    direct_title: str,
) -> None:
    """Legacy metadata must reconstruct the same direct-contract signal.

    Deployments upgrading from before ``pps_notice_authorities`` have CLOSED
    notices and PPS metadata, but no authority row.  An equal-clock partial
    provider response must remain closed; a genuinely later competitive
    correction is allowed to reopen deterministically.
    """

    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"value": "direct"}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _LegacyDirectClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            direct = phase["value"] == "direct"
            corrected = phase["value"] == "corrected"
            yield {
                "identity": f"R26BK-LEGACY-DIRECT|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-LEGACY-DIRECT",
                "revision_no": "00",
                "title": direct_title if direct else "교육 프로그램 일반경쟁 운영",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published
                + timedelta(minutes=2 if corrected else 1),
                "deadline": deadline,
                "estimated_amount": 20_000_000,
                "notice_kind": "정정공고" if corrected else "등록공고",
                "bid_method": bid_method if direct else "",
                "contract_method": contract_method if direct else "",
                "direct_contract_signal": direct,
                "source_url": None,
                "raw": (
                    {
                        "cntrctCnclsMthdNm": contract_method,
                        "bidMethdNm": bid_method,
                    }
                    if direct
                    else {}
                ),
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _LegacyDirectClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        first = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert first.status_code == 200, first.text
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []

        # Simulate an upgraded database that predates the compact authority
        # table while retaining its real Notice/PPS metadata history.
        with app.state.session_factory() as session:
            authority = session.get(PpsNoticeAuthority, "R26BK-LEGACY-DIRECT")
            assert authority is not None
            legacy_notice = session.scalar(
                select(Notice).where(
                    Notice.bid_notice_no == "R26BK-LEGACY-DIRECT"
                )
            )
            assert legacy_notice is not None
            assert _stored_notice_authority_row(legacy_notice)[
                "direct_contract_signal"
            ] is True
            session.delete(authority)
            session.commit()

        phase["value"] = "partial"
        partial = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert partial.status_code == 200, partial.text
        assert partial.json()["matched"] == 0
        assert live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json() == []

        phase["value"] = "corrected"
        corrected = live_client.post(
            "/api/v1/ingestion/pps/notices", json=payload
        )
        assert corrected.status_code == 200, corrected.text
        open_rows = live_client.get(
            "/api/v1/notices", params={"status": "OPEN"}
        ).json()
        assert len(open_rows) == 1
        assert open_rows[0]["bid_notice_no"] == "R26BK-LEGACY-DIRECT"


def test_never_seen_cancellation_authority_survives_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    phase = {"cancelled": True}
    published = datetime(2026, 8, 16, 1, tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) + timedelta(days=7)

    class _NeverSeenCancellationClient(_FakePpsClient):
        def iter_notices(self, **_kwargs: object):
            cancelled = phase["cancelled"]
            yield {
                "identity": f"R26BK-NEVER-SEEN-CANCEL|00|{deadline.isoformat()}",
                "bid_notice_no": "R26BK-NEVER-SEEN-CANCEL",
                "revision_no": "00",
                "title": "취소" if cancelled else "오래된 교육 용역",
                "agency": "공공기관",
                "published_at": published,
                "provider_changed_at": published
                + timedelta(minutes=2 if cancelled else 1),
                "deadline": None if cancelled else deadline,
                "estimated_amount": 100_000_000,
                "notice_kind": "취소공고" if cancelled else "등록공고",
                "source_url": None,
                "raw": {
                    "bidNtceNo": "R26BK-NEVER-SEEN-CANCEL",
                    "bidNtceOrd": "00",
                    "ntceKindNm": "취소공고" if cancelled else "등록공고",
                },
            }

    monkeypatch.setattr("pai_loop.api.PpsClient", _NeverSeenCancellationClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    payload = {"from_date": "2026-08-16", "to_date": "2026-08-16"}
    with TestClient(app) as live_client:
        cancelled = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert cancelled.status_code == 200, cancelled.text
        assert live_client.get("/api/v1/notices").json() == []
        with app.state.session_factory() as session:
            old = datetime.now(timezone.utc) - timedelta(days=8)
            job = session.get(IngestionJob, cancelled.json()["job_id"])
            assert job is not None
            job.created_at = old
            job.completed_at = old
            session.commit()
        assert live_client.post(
            "/api/v1/operations/retention",
            json={"retention_days": 7, "dry_run": False},
        ).status_code == 200
        with app.state.session_factory() as session:
            assert session.get(IngestionJob, cancelled.json()["job_id"]) is None
            authority = session.get(
                PpsNoticeAuthority,
                "R26BK-NEVER-SEEN-CANCEL",
            )
            assert authority is not None
            assert authority.disposition == "CANCELLED"
        phase["cancelled"] = False
        stale = live_client.post("/api/v1/ingestion/pps/notices", json=payload)
        assert stale.status_code == 200, stale.text
        assert stale.json()["matched"] == 0
        assert live_client.get("/api/v1/notices").json() == []


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
