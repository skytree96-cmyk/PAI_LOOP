from __future__ import annotations

from fastapi.testclient import TestClient

from pai_loop.main import create_app
from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.public_notice_seed import PUBLIC_NOTICE_SOURCE_KEY, import_public_notice_seed
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeEstimateRequest,
    QuantitativeFact,
    ScoreBracket,
    SourceAnchor,
    estimate_quantitative_score,
    load_quantitative_profile_catalog,
    quantitative_request_from_candidate_profile,
)
from pai_loop.quantitative_rule_extraction import (
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)


ACTUAL_RFP_SHA256 = "53b24e9dae63328d4f692e4cbe21e7148e0f24614dedbec5c356e2adbfc84648"
PUBLIC_PERFORMANCE_EVIDENCE_KEY = (
    "PUBLIC-PERFORMANCE:2026.08.17-v3:"
    "be3d1bed864aed0142515b1ae9ba1dcc1fb78ef85bc9df2f0a35527d548879f7"
)


def _anchor(*, page: int = 1) -> SourceAnchor:
    return SourceAnchor(
        document_label="publication-safe scoring table.pdf",
        document_sha256="a" * 64,
        section="quantitative table",
        page=page,
    )


def _active_request(**values) -> QuantitativeEstimateRequest:
    return QuantitativeEstimateRequest(
        source_validation_status="SOURCE_VALIDATED",
        activation_status="AUTO_ACTIVE",
        **values,
    )


def _numeric_criterion(
    *,
    criterion_id: str = "Q-1",
    metric_key: str = "metric.amount",
    max_points: float = 10,
    floor: float = 0,
) -> QuantitativeCriterion:
    return QuantitativeCriterion(
        criterion_id=criterion_id,
        category="synthetic",
        label="synthetic numeric criterion",
        max_points=max_points,
        metric_key=metric_key,
        unit="unit",
        formula_type="BRACKET",
        formula="below 50 is 4; 50 or above is 10",
        brackets=[
            ScoreBracket(bracket_id="low", label="below 50", max_value=50, points=4),
            ScoreBracket(bracket_id="high", label="50 or above", min_value=50, points=10),
        ],
        rule_floor_points=floor,
        floor_condition="valid synthetic submission" if floor else None,
        source_anchor=_anchor(),
        required_evidence_keys=["EVIDENCE-SYNTHETIC"],
    )


def test_confirmed_synthetic_score_is_deterministic_and_separate_from_go() -> None:
    boolean = QuantitativeCriterion(
        criterion_id="Q-2",
        category="synthetic",
        label="synthetic boolean criterion",
        max_points=5,
        metric_key="metric.boolean",
        formula_type="BOOLEAN",
        formula="true is 5; false is 0",
        brackets=[
            ScoreBracket(bracket_id="false", label="false", boolean_value=False, points=0),
            ScoreBracket(bracket_id="true", label="true", boolean_value=True, points=5),
        ],
        source_anchor=_anchor(page=2),
        required_evidence_keys=["EVIDENCE-SYNTHETIC"],
    )
    result = estimate_quantitative_score(
        _active_request(
            ruleset_version="synthetic-confirmed-v1",
            minimum_score=12,
            criteria=[_numeric_criterion(), boolean],
            facts=[
                QuantitativeFact(
                    metric_key="metric.amount",
                    status="CONFIRMED",
                    value=75,
                    evidence_key="EVIDENCE-SYNTHETIC",
                    confidence=1,
                ),
                QuantitativeFact(
                    metric_key="metric.boolean",
                    status="CONFIRMED",
                    value=True,
                    evidence_key="EVIDENCE-SYNTHETIC",
                    confidence=1,
                ),
            ],
            source_anchor=_anchor(),
        )
    )

    assert result.overall_status == "CONFIRMED"
    assert (result.lower_points, result.upper_points, result.estimated_points) == (15, 15, 15)
    assert result.evidence_coverage_pct == 100
    assert result.readiness_pct == 100
    assert result.readiness_band == "GREEN"
    assert result.meets_minimum is True
    assert "GO/NO-GO" in result.separation_notice


def test_estimated_range_and_missing_fact_do_not_create_exact_points() -> None:
    ranged = estimate_quantitative_score(
        _active_request(
            ruleset_version="synthetic-range-v1",
            criteria=[_numeric_criterion(floor=2)],
            facts=[
                QuantitativeFact(
                    metric_key="metric.amount",
                    status="ESTIMATED",
                    lower_value=20,
                    upper_value=80,
                    evidence_key="EVIDENCE-SYNTHETIC",
                    confidence=0.5,
                    rationale="publication-safe candidate range",
                )
            ],
        )
    )
    assert ranged.overall_status == "ESTIMATED"
    assert ranged.estimated_points is None
    assert (ranged.lower_points, ranged.upper_points) == (4, 10)
    assert ranged.criteria[0].status == "ESTIMATED"
    assert ranged.readiness_band == "RED"  # coverage is deliberately still zero

    missing = estimate_quantitative_score(
        _active_request(
            ruleset_version="synthetic-missing-v1",
            criteria=[_numeric_criterion(floor=2)],
        )
    )
    assert missing.overall_status == "UNSCORABLE"
    assert missing.estimated_points is None
    assert (missing.lower_points, missing.upper_points) == (2, 10)
    assert missing.unscorable_points == 8
    assert "원문상 최소점수 2점" in missing.criteria[0].assumptions[-1]


def test_invalid_or_missing_rules_fail_closed_to_review() -> None:
    invalid = _numeric_criterion()
    invalid.brackets = []
    result = estimate_quantitative_score(
        _active_request(
            ruleset_version="synthetic-invalid-v1",
            criteria=[invalid],
            facts=[
                QuantitativeFact(
                    metric_key="metric.amount",
                    status="CONFIRMED",
                    value=75,
                    evidence_key="EVIDENCE-SYNTHETIC",
                )
            ],
        )
    )
    assert result.overall_status == "REVIEW"
    assert result.criteria[0].estimated_points is None
    assert result.criteria[0].rationale == "배점 구간이 정의되지 않았습니다."

    no_table = estimate_quantitative_score(
        QuantitativeEstimateRequest(
            ruleset_version="missing-table-v1",
            rule_source_status="MISSING",
            source_validation_status="MISSING",
            missing_reason="score table unavailable",
        )
    )
    assert no_table.overall_status == "REVIEW"
    assert no_table.readiness_band == "GRAY"
    assert no_table.total_max_points is None
    assert no_table.lower_points is None

    wrong_evidence = estimate_quantitative_score(
        _active_request(
            ruleset_version="wrong-evidence-v1",
            criteria=[_numeric_criterion(floor=2)],
            facts=[
                QuantitativeFact(
                    metric_key="metric.amount",
                    status="CONFIRMED",
                    value=75,
                    evidence_key="EVIDENCE-NOT-ALLOWED",
                )
            ],
        )
    )
    assert wrong_evidence.overall_status == "REVIEW"
    assert wrong_evidence.criteria[0].estimated_points is None
    assert wrong_evidence.criteria[0].lower_points == 2

    unanchored = _numeric_criterion(floor=2)
    unanchored.source_anchor = None
    unanchored_result = estimate_quantitative_score(
        _active_request(
            ruleset_version="unanchored-floor-v1",
            criteria=[unanchored],
        )
    )
    assert unanchored_result.criteria[0].status == "REVIEW"
    assert unanchored_result.criteria[0].lower_points == 0


def test_duplicate_metric_facts_require_review() -> None:
    result = estimate_quantitative_score(
        _active_request(
            ruleset_version="duplicate-fact-v1",
            criteria=[_numeric_criterion()],
            facts=[
                QuantitativeFact(
                    metric_key="metric.amount",
                    status="CONFIRMED",
                    value=40,
                    evidence_key="EVIDENCE-SYNTHETIC",
                ),
                QuantitativeFact(
                    metric_key="metric.amount",
                    status="CONFIRMED",
                    value=70,
                    evidence_key="EVIDENCE-SYNTHETIC",
                ),
            ],
        )
    )
    assert result.overall_status == "REVIEW"
    assert "중복" in result.criteria[0].rationale


def test_verified_xlsx_business_year_rule_auto_activates_without_assuming_fact() -> None:
    attachment_id = "ATT-XLSX-QUANT"
    source = """[SHEET 정량평가]
업력 20점
10년 이상 20점
5년 이상 10년 미만 15점
5년 미만 10점
정량평가 총점 20점
통과 최저점 12점
"""

    def evidence(quote: str) -> dict:
        return {
            "attachment_id": attachment_id,
            "page": None,
            "section": "정량평가 worksheet",
            "quote": quote,
            "confidence": 0.99,
        }

    payload = ExtractionPayload.model_validate(
        {
            "document_type": "FORM",
            "requirements": [],
            "quantitative_tables": [
                {
                    "table_id": "XLSX-Q-1",
                    "label": "정량평가",
                    "criteria": [
                        {
                            "criterion_id": "BUSINESS-YEARS",
                            "label": "업력",
                            "criterion_literal": "업력 20점",
                            "max_points": 20,
                            "scoring_method": "BRACKET",
                            "metric": "BUSINESS_YEARS",
                            "unit": "년",
                            "brackets": [
                                {
                                    "label": "10년 이상",
                                    "literal": "10년 이상 20점",
                                    "min_value": 10,
                                    "max_value": None,
                                    "min_inclusive": True,
                                    "max_inclusive": False,
                                    "points": 20,
                                    "evidence": evidence("10년 이상 20점"),
                                },
                                {
                                    "label": "5년 이상 10년 미만",
                                    "literal": "5년 이상 10년 미만 15점",
                                    "min_value": 5,
                                    "max_value": 10,
                                    "min_inclusive": True,
                                    "max_inclusive": False,
                                    "points": 15,
                                    "evidence": evidence(
                                        "5년 이상 10년 미만 15점"
                                    ),
                                },
                                {
                                    "label": "5년 미만",
                                    "literal": "5년 미만 10점",
                                    "min_value": None,
                                    "max_value": 5,
                                    "min_inclusive": False,
                                    "max_inclusive": False,
                                    "points": 10,
                                    "evidence": evidence("5년 미만 10점"),
                                },
                            ],
                            "threshold": None,
                            "formula_literal": None,
                            "required_evidence": ["company.business.years"],
                            "evidence": evidence("업력 20점"),
                            "ambiguity_reason": None,
                        }
                    ],
                    "total_points": 20,
                    "total_evidence": evidence("정량평가 총점 20점"),
                    "minimum_score": 12,
                    "minimum_evidence": evidence("통과 최저점 12점"),
                    "ambiguity_reason": None,
                }
            ],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "XLSX 정량평가 규칙",
        }
    )
    manifest_sha256 = "b" * 64
    document_sha256 = "a" * 64
    record = validate_quantitative_attachment_extraction(
        payload,
        source_text=source,
        attachment_id=attachment_id,
        document_sha256=document_sha256,
        manifest_sha256=manifest_sha256,
    )
    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={attachment_id: document_sha256},
        manifest_sha256=manifest_sha256,
    )
    assert profile.status == "AVAILABLE"

    no_fact_request = quantitative_request_from_candidate_profile(profile)
    no_fact = estimate_quantitative_score(no_fact_request)
    assert no_fact.rule_source_status == "AVAILABLE"
    assert no_fact.source_validation_status == "SOURCE_VALIDATED"
    assert no_fact.activation_status == "AUTO_ACTIVE"
    assert no_fact.activation_reasons == []
    assert no_fact.total_max_points == 20
    assert no_fact.overall_status == "UNSCORABLE"
    assert no_fact.estimated_points is None
    assert (no_fact.lower_points, no_fact.upper_points) == (0, 20)

    metric_key = no_fact_request.criteria[0].metric_key
    assert metric_key == "company.business.years"
    assert no_fact_request.criteria[0].unit == "YEAR"
    confirmed_request = quantitative_request_from_candidate_profile(
        profile,
        facts=[
            QuantitativeFact(
                metric_key=metric_key,
                status="CONFIRMED",
                value=10,
                evidence_key="company.business.years",
                fact_binding_sha256=no_fact_request.criteria[0].fact_binding_sha256,
                confidence=1,
            )
        ],
    )
    confirmed = estimate_quantitative_score(confirmed_request)
    assert confirmed.overall_status == "CONFIRMED"
    assert confirmed.estimated_points == 20
    assert confirmed.meets_minimum is True

    wrong_evidence_request = quantitative_request_from_candidate_profile(
        profile,
        facts=[
            QuantitativeFact(
                metric_key=metric_key,
                status="CONFIRMED",
                value=10,
                evidence_key="company.performance.amount",
                fact_binding_sha256=no_fact_request.criteria[0].fact_binding_sha256,
                confidence=1,
            )
        ],
    )
    wrong_evidence = estimate_quantitative_score(wrong_evidence_request)
    assert wrong_evidence.overall_status == "REVIEW"
    assert wrong_evidence.estimated_points is None


def test_actual_ai_training_profile_uses_rfp_anchors_and_candidate_range(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "PUBLIC-AI-TRAINING-2025",
            "bid_notice_no": "R25BK00764725",
            "title": "2025 AI 활용 역량 강화 교육 운영 용역",
            "agency": "공개 발주기관",
            "deadline": "2025-04-24T09:00:00+09:00",
        },
    )
    assert created.status_code == 201
    version = client.post(
        "/api/v1/notices/PUBLIC-AI-TRAINING-2025/versions",
        json={
            "version_no": 1,
            "file_sha256": ACTUAL_RFP_SHA256,
            "document_complete": True,
            "extraction_status": "REFERENCE",
            "extraction_confidence": 1,
            "source_payload": {"kind": "PUBLIC_DOCUMENT_REFERENCE"},
        },
    )
    assert version.status_code == 201

    response = client.get(
        "/api/v1/notices/PUBLIC-AI-TRAINING-2025/quantitative-estimate"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ruleset_version"] == "r25bk00764725-quant-actual-derived-v1"
    assert payload["rule_source_status"] == "AVAILABLE"
    assert payload["source_validation_status"] == "SOURCE_VALIDATED"
    assert payload["activation_status"] == "AUTO_ACTIVE"
    assert payload["activation_reasons"] == []
    assert payload["source_anchor"]["document_sha256"] == ACTUAL_RFP_SHA256
    assert payload["total_max_points"] == 20
    assert (payload["lower_points"], payload["upper_points"]) == (9.7, 20)
    assert payload["estimated_points"] is None
    assert payload["overall_status"] == "REVIEW"
    assert payload["readiness_band"] == "RED"
    assert payload["evidence_coverage_pct"] == 0

    criteria = {item["criterion_id"]: item for item in payload["criteria"]}
    assert criteria["R25-Q-CREDIT"]["status"] == "REVIEW"
    assert criteria["R25-Q-STAFF"]["status"] == "UNSCORABLE"
    assert criteria["R25-Q-PERFORMANCE"]["status"] == "ESTIMATED"
    assert (
        criteria["R25-Q-PERFORMANCE"]["lower_points"],
        criteria["R25-Q-PERFORMANCE"]["upper_points"],
    ) == (1.5, 3)
    assert criteria["R25-Q-PERFORMANCE"]["evidence_key"] == PUBLIC_PERFORMANCE_EVIDENCE_KEY
    assert criteria["R25-Q-SOCIAL"]["status"] == "REVIEW"
    assert criteria["R25-Q-SAFETY"]["status"] == "REVIEW"
    assert criteria["R25-Q-SAFETY"]["rule_base_points"] == 2
    assert criteria["R25-Q-SAFETY"]["rule_floor_points"] == 0
    assert all(
        item["source_anchor"]["document_sha256"] == ACTUAL_RFP_SHA256
        for item in payload["criteria"]
    )

    observations = {item["observation_key"]: item for item in payload["evidence_observations"]}
    assert observations["PUBLIC-PERFORMANCE-CANDIDATES"]["value"] == 37
    assert observations["PUBLIC-PERFORMANCE-CANDIDATE-AMOUNT"]["value"] == 17_809_102_812
    assert observations["PUBLIC-PERFORMANCE-CANDIDATES"]["status"] == "CANDIDATE_ONLY"


def test_profile_identity_without_exact_source_digest_stays_fail_closed(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "CORRECTED-AI-TRAINING-2025",
            "bid_notice_no": "R25BK00764725",
            "title": "2025 AI 활용 역량 강화 교육 운영 용역",
            "agency": "공개 발주기관",
            "deadline": "2025-04-24T09:00:00+09:00",
        },
    )
    assert created.status_code == 201
    wrong_version = client.post(
        "/api/v1/notices/CORRECTED-AI-TRAINING-2025/versions",
        json={
            "version_no": 1,
            "file_sha256": "f" * 64,
            "document_complete": True,
            "extraction_status": "REFERENCE",
            "extraction_confidence": 1,
            "source_payload": {"kind": "PUBLIC_DOCUMENT_REFERENCE"},
        },
    )
    assert wrong_version.status_code == 201

    payload = client.get(
        "/api/v1/notices/CORRECTED-AI-TRAINING-2025/quantitative-estimate"
    ).json()

    assert payload["rule_source_status"] == "INCOMPLETE"
    assert payload["source_validation_status"] == "INCOMPLETE"
    assert payload["activation_status"] == "REVIEW_REQUIRED"
    assert payload["overall_status"] == "REVIEW"
    assert payload["total_max_points"] is None
    assert "문서 해시" in payload["assumptions"][0]


def test_corrected_reference_cannot_reuse_stale_quantitative_profile(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "CORRECTED-RFP-AI-TRAINING-2025",
            "bid_notice_no": "R25BK00764725",
            "title": "2025 AI 활용 역량 강화 교육 운영 용역",
            "agency": "공개 발주기관",
            "deadline": "2025-04-24T09:00:00+09:00",
        },
    )
    assert created.status_code == 201
    for version_no, digest in ((1, ACTUAL_RFP_SHA256), (2, "f" * 64)):
        response = client.post(
            "/api/v1/notices/CORRECTED-RFP-AI-TRAINING-2025/versions",
            json={
                "version_no": version_no,
                "file_sha256": digest,
                "document_complete": True,
                "extraction_status": "REFERENCE",
                "extraction_confidence": 1,
                "source_payload": {"kind": "PUBLIC_DOCUMENT_REFERENCE"},
            },
        )
        assert response.status_code == 201

    payload = client.get(
        "/api/v1/notices/CORRECTED-RFP-AI-TRAINING-2025/quantitative-estimate"
    ).json()

    assert payload["rule_source_status"] == "INCOMPLETE"
    assert payload["source_validation_status"] == "INCOMPLETE"
    assert payload["activation_status"] == "REVIEW_REQUIRED"
    assert payload["total_max_points"] is None
    assert "현재 권위 공고 문서" in payload["assumptions"][0]


def test_incheon_actual_public_seed_stays_unscored_without_quant_table(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        import_public_notice_seed(session)

    response = client.get(
        f"/api/v1/notices/{PUBLIC_NOTICE_SOURCE_KEY}/quantitative-estimate"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["rule_source_status"] == "MISSING"
    assert payload["source_validation_status"] == "MISSING"
    assert payload["activation_status"] == "REVIEW_REQUIRED"
    assert payload["overall_status"] == "REVIEW"
    assert payload["readiness_band"] == "GRAY"
    assert payload["total_max_points"] is None
    assert payload["lower_points"] is None
    assert payload["upper_points"] is None
    assert payload["criteria"] == []
    observations = {item["observation_key"]: item for item in payload["evidence_observations"]}
    assert observations["PUBLIC-PERFORMANCE-CANDIDATES"]["value"] == 26
    assert observations["PUBLIC-PERFORMANCE-CANDIDATES"]["status"] == "CANDIDATE_ONLY"


def test_quantitative_profile_catalog_is_public_safe_and_versioned() -> None:
    catalog = load_quantitative_profile_catalog()
    assert catalog["classification"] == "PUBLIC_PROCUREMENT_DERIVED"
    assert catalog["profile_version"] == "2026.08.17-v2"
    serialized = str(catalog)
    assert "53b24e9dae63328d4f692e4cbe21e7148e0f24614dedbec5c356e2adbfc84648" in serialized
    assert "C:\\Users" not in serialized


def test_public_read_only_can_read_public_safe_quantitative_result(monkeypatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "server-only-secret")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as public_client:
        with app.state.session_factory() as session:
            import_public_notice_seed(session)
        response = public_client.get(
            f"/api/v1/notices/{PUBLIC_NOTICE_SOURCE_KEY}/quantitative-estimate"
        )

    assert response.status_code == 200
    assert response.json()["rule_source_status"] == "MISSING"
    assert "C:\\Users" not in response.text
