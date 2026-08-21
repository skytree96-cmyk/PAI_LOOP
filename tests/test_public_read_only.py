from __future__ import annotations

from fastapi.testclient import TestClient

from pai_loop.api import _publication_safe_source_url
from pai_loop.integrations.openai_extraction import PROMPT_VERSION, SCHEMA_VERSION
from pai_loop.main import create_app
from pai_loop.pps_enrichment import PPS_PROCESSING_VERSION
from pai_loop.public_notice_seed import PUBLIC_NOTICE_SOURCE_KEY, import_public_notice_seed


SERVER_HEADERS = {"X-PAI-LOOP-API-KEY": "server-only-secret"}


def test_public_source_url_removes_credentials_and_local_paths() -> None:
    cleaned = _publication_safe_source_url(
        "https://example.test/notices?id=123&serviceKey=never-public#section"
    )
    assert cleaned == "https://example.test/notices?id=123"
    assert _publication_safe_source_url("file:///private/source.pdf") is None
    credentialed_url = "https://user:pass" + "@" + "example.test/private"
    assert _publication_safe_source_url(credentialed_url) is None


def test_public_read_only_exposes_only_curated_get_surface(monkeypatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        runtime = client.get("/api/v1/runtime-profile")
        assert runtime.status_code == 200
        assert runtime.json()["access_mode"] == "PUBLIC_READ_ONLY"
        assert runtime.json()["write_controls_enabled"] is False

        assert client.get("/api/v1/departments/keyword-profiles").status_code == 200
        assert client.get("/api/v1/company-profile").status_code == 200
        assert client.get("/api/v1/performance/summary").status_code == 200
        assert client.get("/api/v1/notices").status_code == 200

        assert client.get("/api/v1/ingestion/jobs").status_code == 401
        assert client.get("/api/v1/notifications/mock").status_code == 401
        assert client.post("/api/v1/ingestion/replay").status_code == 401
        assert client.get("/api/v1/private-data/status", headers=SERVER_HEADERS).status_code == 404
        assert client.get(
            "/api/v1/notices/example/analysis/private-match-preview",
            headers=SERVER_HEADERS,
        ).status_code == 404


def test_public_notice_response_removes_company_values_and_internal_decisions(monkeypatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)

    with TestClient(app) as client:
        replay = client.post("/api/v1/ingestion/replay", headers=SERVER_HEADERS)
        assert replay.status_code == 200
        notice_key = "SYN-PASS-001"
        private_detail = client.get(f"/api/v1/notices/{notice_key}", headers=SERVER_HEADERS).json()
        evaluation_id = private_detail["latest_evaluation"]["id"]
        decision = client.post(
            f"/api/v1/notices/{notice_key}/decisions",
            headers=SERVER_HEADERS,
            json={
                "evaluation_id": evaluation_id,
                "choice": "GO",
                "actor_label": "masked-reviewer",
                "rationale": "server-only decision",
            },
        )
        assert decision.status_code == 201

        public_detail = client.get(f"/api/v1/notices/{notice_key}")
        assert public_detail.status_code == 200
        payload = public_detail.json()
        assert payload["decisions"] == []
        assert payload["requirements"] == []
        assert payload["latest_evaluation"]["atomic_results"] == []
        assert payload["latest_evaluation"]["explanation"]["public_view"] is True
        serialized = public_detail.text
        assert "masked-reviewer" not in serialized
        assert "server-only decision" not in serialized

        public_policy = client.get(
            f"/api/v1/notices/{notice_key}/analysis/requirement-policy"
        )
        assert public_policy.status_code in {200, 422}
        assert client.post(
            f"/api/v1/notices/{notice_key}/decisions",
            json={"choice": "GO", "rationale": "must remain protected"},
        ).status_code == 401


def test_public_document_analysis_is_digest_bound_and_metadata_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/notices",
            headers=SERVER_HEADERS,
            json={
                "notice_key": "UNREVIEWED-PUBLIC-001",
                "bid_notice_no": "UNREVIEWED-PUBLIC-001",
                "revision_no": "00",
                "title": "공개 전 검토가 필요한 분석",
                "agency": "공개 발주기관",
                "deadline": "2027-01-01T09:00:00+09:00",
                "status": "OPEN",
            },
        )
        assert created.status_code == 201
        version = client.post(
            "/api/v1/notices/UNREVIEWED-PUBLIC-001/versions",
            headers=SERVER_HEADERS,
            json={
                "version_no": 1,
                "file_sha256": "a" * 64,
                "document_complete": True,
                "extraction_status": "ACCEPTED",
                "extraction_confidence": 0.99,
                "source_payload": {
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "status": "ACCEPTED",
                    "source_label": "internal attachment label",
                    "response_id": "provider-response-must-not-be-public",
                    "model": "internal-model-metadata",
                    "result": {
                        "document_type": "NOTICE",
                        "summary": "unreviewed extraction",
                        "requirements": [],
                    },
                },
            },
        )
        assert version.status_code == 201

        public_detail = client.get("/api/v1/notices/UNREVIEWED-PUBLIC-001")
        assert public_detail.status_code == 200
        assert public_detail.json()["document_analyses"] == []
        assert "provider-response-must-not-be-public" not in public_detail.text
        assert "internal attachment label" not in public_detail.text
        assert client.get(
            "/api/v1/notices/UNREVIEWED-PUBLIC-001/analysis/requirement-policy"
        ).status_code == 422

        with app.state.session_factory() as session:
            import_public_notice_seed(session)
        curated = client.get(f"/api/v1/notices/{PUBLIC_NOTICE_SOURCE_KEY}")
        assert curated.status_code == 200
        analyses = curated.json()["document_analyses"]
        assert len(analyses) == 1
        assert set(analyses[0]) == {
            "kind",
            "status",
            "document_name",
            "summary",
            "requirements",
        }
        assert "response_id" not in curated.text
        assert "model" not in analyses[0]

        policy = client.get(
            f"/api/v1/notices/{PUBLIC_NOTICE_SOURCE_KEY}/analysis/requirement-policy"
        )
        assert policy.status_code == 200
        assert policy.json()["counts"] == {
            "ELIGIBILITY": 6,
            "ACTION_REQUIRED": 1,
            "CHECKLIST": 13,
            "INFORMATION": 3,
        }


def test_public_live_pps_extraction_is_redacted_and_usable_by_policy(monkeypatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    email = "review" + "@" + "example.invalid"
    phone = "010" + "-" + "1234" + "-" + "5678"

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/notices",
            headers=SERVER_HEADERS,
            json={
                "notice_key": "PPS-LIVE-PUBLIC-001",
                "bid_notice_no": "R26BK-LIVE-001",
                "revision_no": "00",
                "title": "공개 교육 컨설팅 용역",
                "agency": "공공기관",
                "deadline": "2027-01-01T09:00:00+09:00",
                "status": "OPEN",
            },
        )
        assert created.status_code == 201
        attachment_id = "PPS-ATT-0123456789abcdef01234567"
        version = client.post(
            "/api/v1/notices/PPS-LIVE-PUBLIC-001/versions",
            headers=SERVER_HEADERS,
            json={
                "version_no": 1,
                "file_sha256": "c" * 64,
                "document_complete": True,
                "extraction_status": "ACCEPTED",
                "extraction_confidence": 0.97,
                "source_payload": {
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "source_kind": "PPS_PUBLIC_ATTACHMENT",
                    "attachment_id": attachment_id,
                    "source_label": "입찰공고문.pdf",
                    "document_sha256": "c" * 64,
                    "status": "ACCEPTED",
                    "prompt_version": PROMPT_VERSION,
                    "processing_version": PPS_PROCESSING_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "document_processing": {
                        "source_read_complete": True,
                        "analysis_input_complete": True,
                    },
                    "result": {
                        "document_type": "NOTICE",
                        "requirements": [
                            {
                                "requirement_id": "REQ-LIVE-1",
                                "category": "SUBMISSION",
                                "logic": "SINGLE",
                                "normalized_condition": "담당 합성가 주무관 " + email,
                                "mandatory": True,
                                "deadline_basis": "입찰 마감일",
                                "evidence": [
                                    {
                                        "attachment_id": attachment_id,
                                        "page": 1,
                                        "section": "문의 합성나",
                                        "quote": "문의 연락처 " + phone,
                                        "confidence": 0.97,
                                    }
                                ],
                                "ambiguity_reason": None,
                            }
                        ],
                        "missing_or_unreadable": [],
                        "summary": "제출 절차 1건, 담당자 합성다",
                        "quantitative_tables": [],
                        "quantitative_table_not_applicable": None,
                    },
                },
            },
        )
        assert version.status_code == 201, version.text

        detail = client.get("/api/v1/notices/PPS-LIVE-PUBLIC-001")
        assert detail.status_code == 200
        assert detail.json()["requirements"] == []
        assert len(detail.json()["document_analyses"]) == 1
        assert email not in detail.text
        assert phone not in detail.text
        assert all(name not in detail.text for name in ("합성가", "합성나", "합성다"))

        policy = client.get(
            "/api/v1/notices/PPS-LIVE-PUBLIC-001/analysis/requirement-policy"
        )
        assert policy.status_code == 200, policy.text
        assert policy.json()["counts"]["CHECKLIST"] == 1
        assert email not in policy.text
        assert phone not in policy.text
        assert all(name not in policy.text for name in ("합성가", "합성나", "합성다"))
