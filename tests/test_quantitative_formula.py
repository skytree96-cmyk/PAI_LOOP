from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import pai_loop.quantitative_scoring as quantitative_scoring
from pai_loop.quantitative_formula import (
    CategoryScore,
    DeterministicFormula,
    boolean_categories_complete,
    category_points,
    category_values_are_disjoint,
    compile_arithmetic_formula,
    compile_category_formula,
    evaluate_formula,
    evaluate_formula_range,
)
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeEstimateRequest,
    QuantitativeFact,
    ScoreBracket,
    SourceAnchor,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
)
from pai_loop.quantitative_performance import (
    PerformanceRecognitionScope,
    derive_performance_value,
    parse_performance_recognition_scope,
)
from pai_loop.quantitative_rule_extraction import (
    AttachmentDocumentBinding,
    ImmutableEvidenceAnchor,
    ImmutableQuantitativeRuleCandidate,
    ImmutableQuantitativeTable,
    QuantitativeCandidateProfile,
)


ANCHOR = SourceAnchor(
    document_label="rfp.pdf",
    document_sha256="a" * 64,
    section="정량평가표",
    page=10,
    quote="원문상 검증된 정량 산식",
)


def _request(criterion: QuantitativeCriterion, fact: QuantitativeFact):
    return QuantitativeEstimateRequest(
        ruleset_version="formula-regression-v1",
        rule_source_status="AVAILABLE",
        source_validation_status="SOURCE_VALIDATED",
        activation_status="AUTO_ACTIVE",
        criteria=[criterion],
        facts=[fact],
    )


def _performance_record(key: str, **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "record_key": key,
        "revision": 1,
        "record_status": "VALIDATED",
        "project_name": "데이터 분석 컨설팅",
        "overview": "공공기관 데이터 분석",
        "keywords": ["데이터", "분석"],
        "contract_date": date(2025, 1, 1),
        "start_date": date(2025, 1, 1),
        "end_date": date(2025, 6, 1),
        "contract_amount": 50_000_000,
        "vat_basis": "INCLUDED",
        "completed": True,
        "share_pct": 100,
        "certificate_status": "AVAILABLE",
        "evidence_reference": "실적증명서",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_compiles_and_evaluates_bounded_single_metric_formula() -> None:
    compiled = compile_arithmetic_formula(
        "점수 = (실적금액 / 100000000) * 10, 최대 10점, 소수점 2자리 반올림",
        maximum_points=10,
    )

    assert compiled is not None
    assert evaluate_formula(compiled, 50_000_000) == 5
    assert evaluate_formula(compiled, 300_000_000) == 10
    assert evaluate_formula_range(compiled, 40_000_000, 80_000_000) == (4, 8)

    source_unit_formula = compile_arithmetic_formula(
        "점수 = (실적금액 / 1억원) * 10, 최대 10점",
        maximum_points=10,
        source_unit_scale=100_000_000,
    )
    assert source_unit_formula is not None
    assert evaluate_formula(source_unit_formula, 50_000_000) == 5

    shorthand_unit_formula = compile_arithmetic_formula(
        "점수=(실적금액/1억)*10, 최대 10점",
        maximum_points=10,
        source_unit_scale=100_000_000,
    )
    assert shorthand_unit_formula is not None
    assert evaluate_formula(shorthand_unit_formula, 50_000_000) == 5


def test_free_form_or_multi_variable_formula_fails_closed() -> None:
    assert compile_arithmetic_formula(
        "위원회가 사업 난이도와 수행계획을 종합하여 산정",
        maximum_points=10,
    ) is None


def test_formula_range_uses_conservative_interval_and_rejects_zero_crossing() -> None:
    quadratic = compile_arithmetic_formula(
        "점수 = (값 * 값) / 10, 최대 10점",
        maximum_points=10,
    )
    assert quadratic is not None
    assert evaluate_formula_range(quadratic, -5, 5) == (0, 2.5)

    reciprocal = compile_arithmetic_formula(
        "점수 = 10 / (값 - 2.3), 최대 10점",
        maximum_points=10,
    )
    assert reciprocal is not None
    try:
        evaluate_formula_range(reciprocal, 2, 3)
    except ValueError as exc:
        assert "crosses zero" in str(exc)
    else:  # pragma: no cover - documents the fail-closed contract
        raise AssertionError("a denominator crossing zero must not produce a score range")


def test_formula_contract_rejects_unsafe_shapes_and_applies_rounding_modes() -> None:
    with pytest.raises(ValidationError):
        DeterministicFormula(
            expression="x",
            minimum_points=2,
            maximum_points=1,
        )
    with pytest.raises(ValidationError):
        CategoryScore(values=("AA+", " aa+ "), points=1)

    assert compile_arithmetic_formula("", maximum_points=10) is None
    assert compile_arithmetic_formula("점수=x, 최대 11점", maximum_points=10) is None
    assert compile_arithmetic_formula("점수=x", maximum_points=0) is None
    assert compile_arithmetic_formula("점수=abs(x)", maximum_points=10) is None
    assert compile_arithmetic_formula("점수=x/0", maximum_points=10) is None
    assert compile_arithmetic_formula("점수=실적금액+건수", maximum_points=10) is None

    expected = {
        "반올림": 1.3,
        "버림": 1.2,
        "절사": 1.2,
        "올림": 1.3,
    }
    for label, points in expected.items():
        formula = compile_arithmetic_formula(
            f"점수=x, 최대 10점, 소수점 1자리 {label}",
            maximum_points=10,
        )
        assert formula is not None
        assert evaluate_formula(formula, 1.26) == points

    bounded = compile_arithmetic_formula(
        "점수=x-10, 최소 2점, 최대 8점",
        maximum_points=8,
    )
    assert bounded is not None
    assert evaluate_formula_range(bounded, 0, 30) == (2, 8)
    assert compile_arithmetic_formula(
        "점수 = 최저가격 / 입찰가격 * 10",
        maximum_points=10,
    ) is None


def test_explicit_category_rows_compile_without_fuzzy_grouping() -> None:
    categories = compile_category_formula(
        "AAA=5점; AA+=4점; A=3점",
        maximum_points=5,
    )

    assert categories is not None
    assert category_points(categories, "aa+") == 4
    assert category_points(categories, "BBB") is None
    assert compile_category_formula(
        "AAA, AA+ 5점, AA0 4점",
        maximum_points=5,
    ) is None
    assert compile_category_formula(
        "AAA=5점; AA+=4점; A는 별도 검토",
        maximum_points=5,
    ) is None


def test_boolean_category_aliases_are_complete_and_unambiguous() -> None:
    categories = compile_category_formula(
        "보유=5점; 미보유=0점",
        maximum_points=5,
    )

    assert categories is not None
    assert boolean_categories_complete(categories) is True
    assert category_points(categories, True) == 5
    assert category_points(categories, False) == 0

    ambiguous = compile_category_formula(
        "Y=5점; 보유=4점; 미보유=0점",
        maximum_points=5,
    )
    assert ambiguous is not None
    assert boolean_categories_complete(ambiguous) is False
    assert category_points(ambiguous, True) is None

    unicode_duplicate = (
        CategoryScore(values=("A",), points=5),
        CategoryScore(values=("Ａ",), points=4),
    )
    assert category_values_are_disjoint(unicode_duplicate) is False
    assert category_points(unicode_duplicate, "A") is None


def test_dynamic_local_presence_formula_scores_korean_boolean_rows() -> None:
    literal = "지역사무소 보유 여부: 보유=5점; 미보유=0점 (최대 5점)"
    formula_literal = "보유=5점; 미보유=0점"
    evidence = ImmutableEvidenceAnchor(
        attachment_id="att-local",
        page=3,
        section="정량평가표",
        quote=literal,
        confidence=1,
    )
    candidate = ImmutableQuantitativeRuleCandidate(
        source_attachment_id="att-local",
        table_id="table-local",
        criterion_id="local-presence",
        label="지역사무소 보유",
        criterion_literal=literal,
        max_points=5,
        scoring_method="FORMULA",
        metric="LOCAL_PRESENCE",
        unit="여부",
        brackets=(),
        threshold=None,
        formula_literal=formula_literal,
        required_evidence=("company.local_presence",),
        evidence=evidence,
    )
    table = ImmutableQuantitativeTable(
        source_attachment_id="att-local",
        table_id="table-local",
        label="정량평가표",
        status="AVAILABLE",
        total_points=5,
        total_evidence=evidence,
        minimum_score=None,
        minimum_evidence=None,
        criterion_ids=("local-presence",),
        available_criterion_ids=("local-presence",),
        review_criterion_ids=(),
    )
    profile = QuantitativeCandidateProfile(
        status="AVAILABLE",
        manifest_sha256="b" * 64,
        document_bindings=(
            AttachmentDocumentBinding(
                attachment_id="att-local",
                document_sha256="c" * 64,
            ),
        ),
        expected_attachment_ids=("att-local",),
        processed_attachment_ids=("att-local",),
        tables=(table,),
        available_candidates=(candidate,),
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(),
    )

    request = quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "AUTO_ACTIVE"
    assert request.criteria[0].formula_type == "CATEGORICAL"
    binding = request.criteria[0].fact_binding_sha256
    assert binding is not None

    for value, expected_points in ((True, 5), (False, 0)):
        fact = QuantitativeFact(
            metric_key="company.local_presence",
            status="CONFIRMED",
            value=value,
            evidence_key="company.local_presence",
            fact_binding_sha256=binding,
            confidence=1,
        )
        result = estimate_quantitative_score(
            request.model_copy(update={"facts": [fact]})
        )
        assert result.criteria[0].estimated_points == expected_points

    ambiguous_candidate = candidate.model_copy(
        update={"formula_literal": "Y=5점; 보유=4점; 미보유=0점"}
    )
    ambiguous_profile = profile.model_copy(
        update={"available_candidates": (ambiguous_candidate,)}
    )
    unsafe_request = quantitative_request_from_candidate_profile(ambiguous_profile)
    assert unsafe_request.activation_status == "REVIEW_REQUIRED"
    assert "UNSUPPORTED_SCORING_DSL" in unsafe_request.activation_reasons


def test_dynamic_currency_formula_binds_shorthand_unit_to_won_scale() -> None:
    literal = (
        "최근 3년 데이터 분석 관련 수행완료 실적, VAT 포함, 공동수급 전체 인정, "
        "점수=(실적금액/1억)*10, 최대 10점"
    )
    evidence = ImmutableEvidenceAnchor(
        attachment_id="att-amount",
        page=4,
        section="정량평가표",
        quote=literal,
        confidence=1,
    )
    candidate = ImmutableQuantitativeRuleCandidate(
        source_attachment_id="att-amount",
        table_id="table-amount",
        criterion_id="performance-amount",
        label="유사실적 금액",
        criterion_literal=literal,
        max_points=10,
        scoring_method="FORMULA",
        metric="PERFORMANCE_AMOUNT",
        unit="억원",
        brackets=(),
        threshold=None,
        formula_literal="점수=(실적금액/1억)*10, 최대 10점",
        required_evidence=("company.performance.amount",),
        evidence=evidence,
    )
    table = ImmutableQuantitativeTable(
        source_attachment_id="att-amount",
        table_id="table-amount",
        label="정량평가표",
        status="AVAILABLE",
        total_points=10,
        total_evidence=evidence,
        minimum_score=None,
        minimum_evidence=None,
        criterion_ids=("performance-amount",),
        available_criterion_ids=("performance-amount",),
        review_criterion_ids=(),
    )
    profile = QuantitativeCandidateProfile(
        status="AVAILABLE",
        manifest_sha256="d" * 64,
        document_bindings=(
            AttachmentDocumentBinding(
                attachment_id="att-amount",
                document_sha256="e" * 64,
            ),
        ),
        expected_attachment_ids=("att-amount",),
        processed_attachment_ids=("att-amount",),
        tables=(table,),
        available_candidates=(candidate,),
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(),
    )

    request = quantitative_request_from_candidate_profile(profile)

    assert request.activation_status == "AUTO_ACTIVE"
    assert request.criteria[0].performance_scope is not None
    formula = request.criteria[0].deterministic_formula
    assert formula is not None
    assert evaluate_formula(formula, 50_000_000) == 5


def test_quantitative_engine_scores_formula_category_and_equality_threshold() -> None:
    formula = compile_arithmetic_formula(
        "점수=(실적금액/100000000)*10, 최대 10점",
        maximum_points=10,
    )
    assert formula is not None
    formula_criterion = QuantitativeCriterion(
        criterion_id="FORMULA-1",
        category="PERFORMANCE_AMOUNT",
        label="실적 비례점수",
        max_points=10,
        metric_key="company.performance.amount",
        unit="KRW",
        formula_type="FORMULA",
        formula="점수=(실적금액/100000000)*10",
        deterministic_formula=formula,
        source_anchor=ANCHOR,
        required_evidence_keys=["company.performance.amount"],
    )
    formula_result = estimate_quantitative_score(
        _request(
            formula_criterion,
            QuantitativeFact(
                metric_key="company.performance.amount",
                status="CONFIRMED",
                value=75_000_000,
                evidence_key="company.performance.amount",
                confidence=1,
            ),
        )
    )
    assert formula_result.estimated_points == 7.5

    categories = compile_category_formula("AAA=5점; AA+=4점; A=3점", maximum_points=5)
    assert categories is not None
    category_criterion = QuantitativeCriterion(
        criterion_id="CATEGORY-1",
        category="CREDIT_RATING",
        label="신용등급",
        max_points=5,
        metric_key="company.credit_rating",
        unit="RATING",
        formula_type="CATEGORICAL",
        formula="AAA=5점; AA+=4점; A=3점",
        categories=list(categories),
        source_anchor=ANCHOR,
        required_evidence_keys=["company.credit_rating"],
    )
    category_result = estimate_quantitative_score(
        _request(
            category_criterion,
            QuantitativeFact(
                metric_key="company.credit_rating",
                status="CONFIRMED",
                value="AA+",
                evidence_key="company.credit_rating",
                confidence=1,
            ),
        )
    )
    assert category_result.estimated_points == 4

    threshold_criterion = QuantitativeCriterion(
        criterion_id="THRESHOLD-1",
        category="PERFORMANCE_COUNT",
        label="정확히 3건",
        max_points=2,
        metric_key="company.performance.count",
        unit="COUNT",
        formula_type="THRESHOLD",
        formula="3건이면 2점, 그 외 0점",
        threshold_operator="EQ",
        threshold_value=3,
        threshold_points_if_met=2,
        threshold_points_if_not_met=0,
        source_anchor=ANCHOR,
        required_evidence_keys=["company.performance.count"],
    )
    threshold_result = estimate_quantitative_score(
        _request(
            threshold_criterion,
            QuantitativeFact(
                metric_key="company.performance.count",
                status="CONFIRMED",
                value=3,
                evidence_key="company.performance.count",
                confidence=1,
            ),
        )
    )
    assert threshold_result.estimated_points == 2


def test_engine_uses_half_up_points_and_rejects_non_finite_values() -> None:
    criterion = QuantitativeCriterion(
        criterion_id="ROUND-HALF-UP",
        category="CREDIT_RATING",
        label="반올림 경계",
        max_points=2,
        metric_key="company.credit_rating",
        unit="RATING",
        formula_type="CATEGORICAL",
        formula="A=1.005점; B=0점",
        categories=[
            CategoryScore(values=("A",), points=1.005),
            CategoryScore(values=("B",), points=0),
        ],
        source_anchor=ANCHOR,
        required_evidence_keys=["company.credit_rating"],
    )
    result = estimate_quantitative_score(
        _request(
            criterion,
            QuantitativeFact(
                metric_key="company.credit_rating",
                status="CONFIRMED",
                value="A",
                evidence_key="company.credit_rating",
                confidence=1,
            ),
        )
    )
    assert result.criteria[0].estimated_points == 1.01

    with pytest.raises(ValidationError):
        ScoreBracket(
            bracket_id="NAN",
            label="invalid",
            points=float("nan"),
        )
    with pytest.raises(ValidationError):
        QuantitativeFact(
            metric_key="company.performance.amount",
            status="CONFIRMED",
            value=float("inf"),
        )


def test_boolean_rules_require_exactly_one_true_and_false_row() -> None:
    criterion = QuantitativeCriterion(
        criterion_id="BOOLEAN-EXTRA",
        category="LOCAL_PRESENCE",
        label="보유 여부",
        max_points=5,
        metric_key="company.local_presence",
        unit="BOOLEAN",
        formula_type="BOOLEAN",
        formula="보유 5점, 미보유 0점",
        brackets=[
            ScoreBracket(
                bracket_id="TRUE",
                label="보유",
                boolean_value=True,
                points=5,
            ),
            ScoreBracket(
                bracket_id="FALSE",
                label="미보유",
                boolean_value=False,
                points=0,
            ),
            ScoreBracket(
                bracket_id="UNBOUND",
                label="판단보류",
                points=1,
            ),
        ],
        source_anchor=ANCHOR,
        required_evidence_keys=["company.local_presence"],
    )
    result = estimate_quantitative_score(
        _request(
            criterion,
            QuantitativeFact(
                metric_key="company.local_presence",
                status="CONFIRMED",
                value=True,
                evidence_key="company.local_presence",
                confidence=1,
            ),
        )
    )

    assert result.criteria[0].status == "REVIEW"
    assert "true/false" in result.criteria[0].rationale


def test_scoring_rule_shapes_reject_cross_formula_metadata() -> None:
    criterion = QuantitativeCriterion(
        criterion_id="CATEGORY-MIXED",
        category="CREDIT_RATING",
        label="신용등급",
        max_points=5,
        metric_key="company.credit_rating",
        unit="RATING",
        formula_type="CATEGORICAL",
        formula="A=5점; B=0점",
        categories=[
            CategoryScore(values=("A",), points=5),
            CategoryScore(values=("B",), points=0),
        ],
        threshold_value=3,
        source_anchor=ANCHOR,
        required_evidence_keys=["company.credit_rating"],
    )
    result = estimate_quantitative_score(
        _request(
            criterion,
            QuantitativeFact(
                metric_key="company.credit_rating",
                status="CONFIRMED",
                value="A",
                evidence_key="company.credit_rating",
                confidence=1,
            ),
        )
    )

    assert result.criteria[0].status == "REVIEW"
    assert "완전하게" in result.criteria[0].rationale


def test_equality_threshold_range_is_exact_at_singleton_and_wide_when_crossing() -> None:
    criterion = QuantitativeCriterion(
        criterion_id="EQ-RANGE",
        category="PERFORMANCE_COUNT",
        label="정확히 3건",
        max_points=2,
        metric_key="company.performance.count",
        unit="COUNT",
        formula_type="THRESHOLD",
        formula="3건이면 2점, 그 외 0점",
        threshold_operator="EQ",
        threshold_value=3,
        threshold_points_if_met=2,
        threshold_points_if_not_met=0,
        source_anchor=ANCHOR,
        required_evidence_keys=["company.performance.count"],
    )

    exact = estimate_quantitative_score(
        _request(
            criterion,
            QuantitativeFact(
                metric_key="company.performance.count",
                status="ESTIMATED",
                lower_value=3,
                upper_value=3,
                evidence_key="company.performance.count",
                confidence=0.8,
            ),
        )
    )
    assert exact.criteria[0].estimated_points == 2
    assert (exact.criteria[0].lower_points, exact.criteria[0].upper_points) == (2, 2)

    crossing = estimate_quantitative_score(
        _request(
            criterion,
            QuantitativeFact(
                metric_key="company.performance.count",
                status="ESTIMATED",
                lower_value=2,
                upper_value=4,
                evidence_key="company.performance.count",
                confidence=0.8,
            ),
        )
    )
    assert crossing.criteria[0].estimated_points is None
    assert (crossing.criteria[0].lower_points, crossing.criteria[0].upper_points) == (0, 2)


def test_validated_performance_register_applies_all_source_dimensions() -> None:
    scope = parse_performance_recognition_scope(
        "최근 3년 AI·데이터·교육 관련 수행완료 실적, VAT 포함, "
        "단일계약 3천만원 이상, 합계금액 10점",
        metric_key="company.performance.amount",
    )
    assert scope is not None
    records = [
        SimpleNamespace(
            record_key="PERF-1",
            revision=2,
            record_status="VALIDATED",
            project_name="AI 데이터 활용 교육",
            overview="공공기관 교육",
            keywords=["AI", "데이터", "교육"],
            contract_date=datetime(2025, 1, 1, tzinfo=timezone.utc).date(),
            start_date=datetime(2025, 1, 1, tzinfo=timezone.utc).date(),
            end_date=datetime(2025, 6, 1, tzinfo=timezone.utc).date(),
            contract_amount=50_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="AVAILABLE",
            evidence_reference="실적증명서-1",
        ),
        SimpleNamespace(
            record_key="DRAFT-IGNORED",
            revision=1,
            record_status="DRAFT",
            project_name="AI 데이터 교육",
            overview="",
            keywords=["AI", "데이터", "교육"],
            contract_date=datetime(2025, 1, 1, tzinfo=timezone.utc).date(),
            start_date=None,
            end_date=datetime(2025, 6, 1, tzinfo=timezone.utc).date(),
            contract_amount=100_000_000,
            vat_basis="INCLUDED",
            completed=True,
            share_pct=100,
            certificate_status="AVAILABLE",
            evidence_reference="draft",
        ),
    ]

    derived = derive_performance_value(
        scope,
        records,
        as_of=datetime(2026, 8, 23, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.value == 50_000_000
    assert derived.matched_record_keys == ("PERF-1",)
    assert derived.evidence_sha256 is not None


def test_performance_scope_without_vat_or_completion_rule_stays_unmodeled() -> None:
    assert parse_performance_recognition_scope(
        "최근 3년 AI 교육 유사실적 합계",
        metric_key="company.performance.amount",
    ) is None


def test_performance_scope_accepts_common_korean_variants_and_missing_evidence_reviews() -> None:
    scope = parse_performance_recognition_scope(
        "최근 3개년 AI·교육 관련 계약이 완료된 실적, 부가세 포함, 실적증명원 제출",
        metric_key="company.performance.count",
    )
    assert scope is not None
    assert scope.lookback_years == 3
    assert scope.vat_basis == "INCLUDED"
    assert scope.certificate_required is True
    assert scope.similarity_keywords == ("AI", "교육")

    record = SimpleNamespace(
        record_key="PERF-MISSING-EVIDENCE",
        revision=1,
        record_status="VALIDATED",
        project_name="AI 교육",
        overview="",
        keywords=["AI", "교육"],
        contract_date=datetime(2025, 1, 1, tzinfo=timezone.utc).date(),
        start_date=None,
        end_date=datetime(2025, 6, 1, tzinfo=timezone.utc).date(),
        contract_amount=50_000_000,
        vat_basis="INCLUDED",
        completed=True,
        share_pct=100,
        certificate_status="ISSUED",
        evidence_reference=None,
    )

    derived = derive_performance_value(
        scope,
        [record],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert derived.status == "REVIEW"
    assert derived.value is None


def test_performance_scope_parses_amount_share_and_explicit_fallback_syntax() -> None:
    with pytest.raises(ValidationError):
        PerformanceRecognitionScope(
            metric_key="company.performance.count",
            lookback_years=3,
            similarity_keywords=("교육",),
            vat_basis="EXCLUDED",
            completion_required=False,
            aggregation="SUM_AMOUNT",
            consortium_share_rule="FULL_AMOUNT",
            source_literal="invalid aggregation",
        )

    scope = parse_performance_recognition_scope(
        "최근 5년 유사용역: AI 교육; 완료 여부 무관, VAT 제외, "
        "건당 1.5억원 이상, 공동수급 지분율 적용",
        metric_key="company.performance.amount",
    )
    assert scope is not None
    assert scope.lookback_years == 5
    assert scope.similarity_keywords == ("AI", "교육")
    assert scope.minimum_single_contract_amount_krw == 150_000_000
    assert scope.completion_required is False
    assert scope.consortium_share_rule == "APPLY_SHARE"

    full_amount = parse_performance_recognition_scope(
        "최근 2년 AI 교육 관련 완료 실적, 부가가치세 포함, 컨소시엄 전체 인정",
        metric_key="company.performance.count",
    )
    assert full_amount is not None
    assert full_amount.consortium_share_rule == "FULL_AMOUNT"
    shorthand_amount = parse_performance_recognition_scope(
        "최근 3년 데이터 분석 관련 완료 실적, VAT 포함, 단일 계약 1억 이상",
        metric_key="company.performance.amount",
    )
    assert shorthand_amount is not None
    assert shorthand_amount.minimum_single_contract_amount_krw == 100_000_000
    assert parse_performance_recognition_scope(
        "최근 3년 AI 관련 완료 실적, VAT 포함",
        metric_key="company.unknown",
    ) is None


def test_performance_derivation_applies_share_minimum_any_match_and_date_bounds() -> None:
    scope = PerformanceRecognitionScope(
        metric_key="company.performance.amount",
        lookback_years=3,
        similarity_keywords=("AI", "교육"),
        match_mode="ANY",
        minimum_single_contract_amount_krw=30_000_000,
        vat_basis="INCLUDED",
        completion_required=False,
        aggregation="SUM_AMOUNT",
        consortium_share_rule="APPLY_SHARE",
        certificate_required=False,
        source_literal="테스트용 명시 조건",
    )

    def record(key: str, **changes: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "record_key": key,
            "revision": 1,
            "record_status": "VALIDATED",
            "project_name": "AI 컨설팅",
            "overview": "",
            "keywords": ["AI"],
            "contract_date": datetime(2025, 1, 1, tzinfo=timezone.utc).date(),
            "start_date": None,
            "end_date": datetime(2025, 6, 1, tzinfo=timezone.utc).date(),
            "contract_amount": 80_000_000,
            "vat_basis": "INCLUDED",
            "completed": True,
            "share_pct": 50,
            "certificate_status": "NOT_REQUESTED",
            "evidence_reference": "계약서",
        }
        values.update(changes)
        return SimpleNamespace(**values)

    derived = derive_performance_value(
        scope,
        [
            record("SHARED"),
            record("BELOW", contract_amount=40_000_000, share_pct=50),
            record(
                "OLD",
                contract_date=datetime(2020, 1, 1, tzinfo=timezone.utc).date(),
            ),
            record("UNRELATED", project_name="시설 유지보수", keywords=[]),
        ],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.value == 40_000_000
    assert derived.matched_record_keys == ("SHARED",)

    uncertain = derive_performance_value(
        scope,
        [record("BAD-SHARE", share_pct=None)],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert uncertain.status == "REVIEW"


def test_performance_scope_strips_year_suffix_and_supports_vat_separate() -> None:
    scope = parse_performance_recognition_scope(
        "최근 3년간 데이터 분석 관련 수행완료 실적, 부가세 별도",
        metric_key="company.performance.amount",
    )

    assert scope is not None
    assert scope.similarity_keywords == ("데이터", "분석")
    assert scope.vat_basis == "EXCLUDED"
    derived = derive_performance_value(
        scope,
        [
            _performance_record(
                "BOUNDARY-START",
                end_date=date(2023, 8, 24),
                vat_basis="EXCLUDED",
                contract_amount=10_000_000,
            ),
            _performance_record(
                "BOUNDARY-END",
                end_date=date(2026, 8, 24),
                vat_basis="EXCLUDED",
                contract_amount=20_000_000,
            ),
            _performance_record(
                "OUTSIDE",
                end_date=date(2023, 8, 23),
                vat_basis="EXCLUDED",
                contract_amount=100_000_000,
            ),
        ],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )

    assert derived.status == "ESTIMATED"
    assert derived.value == 30_000_000
    assert derived.matched_record_keys == ("BOUNDARY-END", "BOUNDARY-START")

    wrong_vat = derive_performance_value(
        scope,
        [_performance_record("VAT-UNKNOWN", vat_basis="INCLUDED")],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert wrong_vat.status == "REVIEW"


def test_performance_count_does_not_require_amount_and_skips_zero_share() -> None:
    scope = PerformanceRecognitionScope(
        metric_key="company.performance.count",
        lookback_years=3,
        similarity_keywords=("데이터", "분석"),
        vat_basis="INCLUDED",
        completion_required=True,
        aggregation="COUNT",
        consortium_share_rule="APPLY_SHARE",
        source_literal="최근 3년 데이터 분석 관련 수행완료 실적, VAT 포함, 공동수급 지분율 적용",
    )

    derived = derive_performance_value(
        scope,
        [
            _performance_record("COUNTED", contract_amount=None),
            _performance_record("ZERO-SHARE", contract_amount=None, share_pct=0),
        ],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert derived.status == "ESTIMATED"
    assert derived.value == 1
    assert derived.matched_record_keys == ("COUNTED",)

    invalid_amount = derive_performance_value(
        scope,
        [_performance_record("BOOL-AMOUNT", contract_amount=True)],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert invalid_amount.status == "REVIEW"
    invalid_share = derive_performance_value(
        scope,
        [_performance_record("BOOL-SHARE", contract_amount=None, share_pct=True)],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert invalid_share.status == "REVIEW"
    ambiguous_partial_count = derive_performance_value(
        scope,
        [_performance_record("HALF-COUNT", contract_amount=None, share_pct=50)],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert ambiguous_partial_count.status == "REVIEW"


def test_performance_derivation_is_deterministic_and_missing_is_not_zero() -> None:
    scope = PerformanceRecognitionScope(
        metric_key="company.performance.amount",
        lookback_years=3,
        similarity_keywords=("데이터", "분석"),
        vat_basis="INCLUDED",
        completion_required=True,
        aggregation="SUM_AMOUNT",
        consortium_share_rule="FULL_AMOUNT",
        source_literal="최근 3년 데이터 분석 관련 수행완료 실적, VAT 포함, 공동수급 전체 인정",
    )
    first = _performance_record("B", revision=2, contract_amount=20_000_000)
    second = _performance_record("A", revision=1, contract_amount=10_000_000)

    forward = derive_performance_value(
        scope,
        [first, second],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    reverse = derive_performance_value(
        scope,
        [second, first],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert forward.matched_record_keys == reverse.matched_record_keys == ("A", "B")
    assert forward.evidence_sha256 == reverse.evidence_sha256

    missing = derive_performance_value(
        scope,
        [
            _performance_record(
                "UNRELATED",
                project_name="시설 유지보수",
                overview="",
                keywords=[],
            )
        ],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert missing.status == "UNSCORABLE"
    assert missing.value is None

    malformed_date = derive_performance_value(
        scope,
        [_performance_record("DATETIME", end_date=datetime(2025, 6, 1, tzinfo=timezone.utc))],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert malformed_date.status == "REVIEW"

    untraceable = derive_performance_value(
        scope,
        [_performance_record("", revision=True)],
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    assert untraceable.status == "REVIEW"


def test_dynamic_performance_rule_requires_matching_recognition_scope() -> None:
    formula = compile_arithmetic_formula("점수=x, 최대 10점", maximum_points=10)
    assert formula is not None
    criterion = QuantitativeCriterion(
        criterion_id="PERFORMANCE-SCOPE",
        category="PERFORMANCE_AMOUNT",
        label="수행실적",
        max_points=10,
        metric_key="company.performance.amount",
        unit="KRW",
        formula_type="FORMULA",
        formula="점수=x",
        deterministic_formula=formula,
        source_anchor=ANCHOR,
        required_evidence_keys=["company.performance.amount"],
        fact_binding_sha256="d" * 64,
    )
    fact = QuantitativeFact(
        metric_key="company.performance.amount",
        status="CONFIRMED",
        value=5,
        evidence_key="company.performance.amount",
        fact_binding_sha256="d" * 64,
        confidence=1,
    )
    missing_scope = estimate_quantitative_score(_request(criterion, fact))
    assert missing_scope.criteria[0].status == "REVIEW"
    assert "원문 인정기간" in missing_scope.criteria[0].rationale

    mismatched_scope = PerformanceRecognitionScope(
        metric_key="company.performance.count",
        lookback_years=3,
        similarity_keywords=("데이터",),
        vat_basis="INCLUDED",
        completion_required=True,
        aggregation="COUNT",
        consortium_share_rule="FULL_AMOUNT",
        source_literal="최근 3년 데이터 관련 완료 실적, VAT 포함",
    )
    mismatch = estimate_quantitative_score(
        _request(
            criterion.model_copy(update={"performance_scope": mismatched_scope}),
            fact,
        )
    )
    assert mismatch.criteria[0].status == "REVIEW"
    assert "회사 사실 키" in mismatch.criteria[0].rationale


def test_notice_scoped_performance_fact_replaces_only_unusable_generic_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formula = compile_arithmetic_formula("점수=x, 최대 10점", maximum_points=10)
    assert formula is not None
    criterion = QuantitativeCriterion(
        criterion_id="MERGE-1",
        category="PERFORMANCE_AMOUNT",
        label="실적",
        max_points=10,
        metric_key="company.performance.amount",
        unit="KRW",
        formula_type="FORMULA",
        formula="점수=x",
        deterministic_formula=formula,
        source_anchor=ANCHOR,
        required_evidence_keys=["company.performance.amount"],
        fact_binding_sha256="b" * 64,
    )
    request = QuantitativeEstimateRequest(
        ruleset_version="merge-test",
        rule_source_status="AVAILABLE",
        source_validation_status="SOURCE_VALIDATED",
        activation_status="AUTO_ACTIVE",
        criteria=[criterion],
        facts=[],
    )
    generic_unusable = QuantitativeFact(
        metric_key="company.performance.amount",
        status="UNSCORABLE",
        evidence_key="company.performance.amount",
        rationale="generic binding mismatch",
    )
    scoped = QuantitativeFact(
        metric_key="company.performance.amount",
        status="ESTIMATED",
        value=5,
        lower_value=5,
        upper_value=5,
        evidence_key="company.performance.amount",
        fact_binding_sha256="b" * 64,
    )
    captured: list[QuantitativeFact] = []
    monkeypatch.setattr(
        quantitative_scoring,
        "_current_dynamic_quantitative_profile",
        lambda notice: object(),
    )
    monkeypatch.setattr(
        quantitative_scoring,
        "quantitative_request_from_candidate_profile",
        lambda profile, **kwargs: request,
    )
    monkeypatch.setattr(
        quantitative_scoring,
        "resolve_verified_quantitative_facts",
        lambda *args, **kwargs: [generic_unusable],
    )
    monkeypatch.setattr(
        quantitative_scoring,
        "resolve_performance_register_facts",
        lambda *args, **kwargs: [scoped],
    )

    def capture(result: QuantitativeEstimateRequest) -> str:
        captured[:] = result.facts
        return "captured"

    monkeypatch.setattr(quantitative_scoring, "estimate_quantitative_score", capture)
    notice = SimpleNamespace(deadline=datetime(2026, 8, 24, tzinfo=timezone.utc))
    assert quantitative_scoring.estimate_for_notice(notice, [object()], [object()]) == "captured"
    assert captured == [scoped]

    exact = generic_unusable.model_copy(
        update={
            "status": "CONFIRMED",
            "value": 7,
            "fact_binding_sha256": "b" * 64,
        }
    )
    monkeypatch.setattr(
        quantitative_scoring,
        "resolve_verified_quantitative_facts",
        lambda *args, **kwargs: [exact],
    )
    assert quantitative_scoring.estimate_for_notice(notice, [object()], [object()]) == "captured"
    assert captured == [exact]
