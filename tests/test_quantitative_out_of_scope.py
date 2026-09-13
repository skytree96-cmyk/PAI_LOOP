"""Preserve classification boundaries and keep excluded points out of totals.

Synthetic arithmetic cases enforce the approved separation of quantitative
scores from qualitative/price points, without hiding unverified source rows.
"""

from __future__ import annotations

import pytest

from pai_loop.quantitative_out_of_scope import out_of_scope_reason
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeEstimateRequest,
    QuantitativeFact,
    QuantitativeReviewCriterion,
    ScoreBracket,
    SourceAnchor,
    _public_quantitative_projection,
    build_public_quantitative_criteria_snapshot,
    estimate_quantitative_score,
)


def reason_for(label: str, *, in_registry: bool = False) -> str | None:
    return out_of_scope_reason(
        label=label, criterion_literal=None, metric_in_registry=in_registry
    )


@pytest.mark.parametrize(
    "label",
    [
        # 정성 — 심사위원 주관
        "사업이해도",
        "과업에 대한 이해도",
        "사업수행 세부추진계획",
        "사업에 대한 접근방법",
        "구성의 독창성, 이행가능성 및 자료집의 충실도",
        "제안사의 부문별 수행능력",
        "제안기관의 역량",
        # 총괄·기준선
        "기술능력평가",
        "협상적격자 선정기준",
        "우선협상대상자 합산점수 산정",
        "정량적 평가",
        "종합평점",
        # 가격 — 투찰가가 있어야 계산됨
        "입찰가격평가",
        "가격평가",
        "입찰가격의 적정성",
        "낙찰하한율",
        # 라벨 추출 실패
        "1-1",
        "2-3",
    ],
)
def test_a_row_the_engine_cannot_score_is_set_aside(label: str) -> None:
    assert reason_for(label) is not None


@pytest.mark.parametrize(
    "label",
    [
        "교육전문인력 보유현황(학사학위이상)",
        "제안업체 인력 보유상태",
        "최근 3년간 용역 수행완료 실적",
        "자기자본비율",
        "제안업체 경영상태",
        "기술자 보유현황(자격증 보유자)",
    ],
)
def test_a_scoreable_row_is_left_alone(label: str) -> None:
    assert reason_for(label) is None


def test_a_registered_metric_is_never_set_aside() -> None:
    """Wording never overrides a metric the engine can actually score."""

    # This label reads qualitative, but the row carries PERSONNEL_COUNT.
    assert reason_for("전문인력 역량 및 수행계획", in_registry=True) is None
    assert reason_for("전문인력 역량 및 수행계획", in_registry=False) is not None


def _scoreable(points: float, binding: str) -> QuantitativeCriterion:
    return QuantitativeCriterion(
        criterion_id="자기자본비율",
        category="FINANCIAL_RATIO",
        label="최근년도 자기자본비율",
        max_points=points,
        metric_key="company.financial.ratio",
        unit="PERCENT",
        formula_type="BRACKET",
        formula="50% 이상 만점",
        brackets=[
            ScoreBracket(bracket_id="hi", label="50% 이상", min_value=50, points=points),
            ScoreBracket(bracket_id="lo", label="50% 미만", max_value=50, points=0),
        ],
        required_evidence_keys=["company.financial.ratio"],
        fact_binding_sha256=binding,
        source_anchor=SourceAnchor(
            document_label="ATT-1",
            document_sha256="a" * 64,
            section="배점표",
            quote="5건 이상 만점",
        ),
    )


def _qualitative(points: float) -> QuantitativeCriterion:
    return QuantitativeCriterion(
        criterion_id="사업이해도",
        category="UNKNOWN",
        label="사업이해도",
        max_points=points,
        metric_key="company.unknown",
        unit="COUNT",
        formula_type="BRACKET",
        formula="사업이해도",
        brackets=[
            ScoreBracket(bracket_id="only", label="정성", min_value=0, points=points)
        ],
        required_evidence_keys=[],
        source_anchor=SourceAnchor(
            document_label="ATT-1",
            document_sha256="a" * 64,
            section="배점표",
            quote="사업이해도",
        ),
    )


def _request(
    *criteria: QuantitativeCriterion, facts=(), minimum_score=None, review_criteria=()
) -> QuantitativeEstimateRequest:
    return QuantitativeEstimateRequest(
        ruleset_version="out-of-scope-test",
        rule_source_status="AVAILABLE",
        source_validation_status="REVIEW_REQUIRED" if review_criteria else "SOURCE_VALIDATED",
        activation_status="PARTIAL_ACTIVE" if review_criteria else "AUTO_ACTIVE",
        activation_reasons=["PARTIAL_QUANTITATIVE_SOURCE_REVIEW"] if review_criteria else [],
        minimum_score=minimum_score,
        criteria=list(criteria),
        review_criteria=list(review_criteria),
        facts=list(facts),
    )


def _full_marks_fact(binding: str) -> QuantitativeFact:
    return QuantitativeFact(
        metric_key="company.financial.ratio",
        status="CONFIRMED",
        value=52.25,
        evidence_key="company.financial.ratio",
        fact_binding_sha256=binding,
        confidence=1.0,
        rationale="2025년도 자기자본비율 52.25%",
    )


def test_a_qualitative_row_no_longer_sinks_the_notice() -> None:
    """정성 60 + 정량 40, 정량은 만점. 예전에는 REVIEW / RED 였다."""

    binding = "b" * 64
    result = estimate_quantitative_score(
        _request(
            _scoreable(40, binding),
            _qualitative(60),
            facts=[_full_marks_fact(binding)],
        )
    )

    assert result.overall_status == "CONFIRMED"
    assert result.readiness_band == "GREEN"
    # 산정 가능한 40점을 전부 확보했으므로 준비도는 만점이다.
    assert result.readiness_pct == 100
    assert result.evidence_coverage_pct == 100
    assert result.out_of_scope_points == 60


@pytest.mark.parametrize("fact_status", ["CONFIRMED", "ESTIMATED"])
def test_set_aside_points_are_separate_from_every_quantitative_total(fact_status) -> None:
    binding = "c" * 64
    result = estimate_quantitative_score(
        _request(
            _scoreable(40, binding),
            _qualitative(60),
            facts=[_full_marks_fact(binding).model_copy(update={"status": fact_status})],
        )
    )

    # 2026-09-13 user policy separates qualitative/price scores from quantitative
    # scores. The old upper=100 assumption must not survive as a quant estimate.
    assert result.lower_points == 40
    assert result.upper_points == 40
    assert result.total_max_points == 40
    assert result.confirmed_points == (40 if fact_status == "CONFIRMED" else 0)
    assert result.estimated_points == 40
    assert result.overall_status == fact_status
    assert result.out_of_scope_points == 60
    assert any("정량 합계·상하한" in item for item in result.assumptions)

    row = next(item for item in result.criteria if item.criterion_id == "사업이해도")
    assert row.status == "OUT_OF_SCOPE"
    assert row.lower_points == 0
    assert row.upper_points == 60
    assert "정성평가" in row.rationale
    assert row.estimated_points is None
    assert all("만점을 받는다고 가정" not in item for item in [*result.assumptions, *row.assumptions])


def test_set_aside_points_are_not_reported_as_unscorable() -> None:
    """설정상 뺀 배점은 '못 푼 점수'가 아니다."""

    binding = "d" * 64
    result = estimate_quantitative_score(
        _request(
            _scoreable(40, binding),
            _qualitative(60),
            facts=[_full_marks_fact(binding)],
        )
    )
    assert result.unscorable_points == 0


def test_a_notice_with_nothing_scoreable_is_not_reported_as_confirmed() -> None:
    """전부 정성이면 '확정'이 아니라 산정 불가다."""

    result = estimate_quantitative_score(_request(_qualitative(100)))
    assert result.overall_status == "UNSCORABLE"
    assert result.out_of_scope_points == 100
    assert result.readiness_pct is None
    assert result.total_max_points == 0
    assert result.lower_points == 0
    assert result.upper_points == 0
    assert result.estimated_points is None
    assert result.meets_minimum is None


def test_qualitative_and_price_rows_stay_separate_from_quantitative_uncertainty() -> None:
    binding = "e" * 64
    price = _qualitative(20).model_copy(update={
        "criterion_id": "SYN-PRICE", "label": "입찰가격평가", "formula": "입찰가격평가"
    })
    result = estimate_quantitative_score(_request(_scoreable(40, binding), _qualitative(40), price))
    assert result.overall_status == "UNSCORABLE"
    assert result.total_max_points == 40
    assert result.lower_points == 0
    assert result.upper_points == 40
    assert result.unscorable_points == 40
    assert result.confirmed_points == 0
    assert result.estimated_points is None
    assert result.out_of_scope_points == 60
    assert result.criteria[0].status == "UNSCORABLE"
    assert result.criteria[1].status == result.criteria[2].status == "OUT_OF_SCOPE"


@pytest.mark.parametrize("label", ["SYN 미검증 실적", "SYN 수행계획"])
def test_review_source_rows_remain_in_quantitative_upper_bound(label) -> None:
    """A validation failure is not proof that a row is qualitative or irrelevant."""
    binding = "f" * 64
    review = QuantitativeReviewCriterion(
        criterion_id="SYN-REVIEW", category="UNKNOWN", label=label, max_points=20,
        issue_codes=["CASE_NUMBER_MISMATCH"],
    )
    result = estimate_quantitative_score(_request(
        _scoreable(20, binding), _qualitative(60), facts=[_full_marks_fact(binding)],
        review_criteria=[review],
    ))
    assert result.overall_status == "REVIEW"
    assert result.total_max_points == 40
    assert result.confirmed_points == result.lower_points == 20
    assert result.upper_points == 40
    assert result.unscorable_points == 20
    assert result.out_of_scope_points == 60
    assert result.estimated_points is None
    assert result.readiness_pct == result.evidence_coverage_pct == 50
    row = next(item for item in result.criteria if item.criterion_id == "SYN-REVIEW")
    assert row.status == "REVIEW" and row.upper_points == 20
    assert "CASE_NUMBER_MISMATCH" in row.rationale
    assert build_public_quantitative_criteria_snapshot(result) is not None
    public = _public_quantitative_projection(result)
    assert public.total_max_points == public.upper_points == 40
    assert public.confirmed_points == public.lower_points == 20
    assert public.out_of_scope_points == 60
    assert [item.status for item in public.criteria] == ["CONFIRMED", "OUT_OF_SCOPE", "REVIEW"]


@pytest.mark.parametrize("minimum", [0, 30, 40, 41, 85])
def test_mixed_request_cannot_decide_an_unbound_minimum(minimum) -> None:
    binding = "1" * 64
    result = estimate_quantitative_score(_request(
        _scoreable(40, binding), _qualitative(60), facts=[_full_marks_fact(binding)],
        minimum_score=minimum,
    ))
    assert result.minimum_score == minimum
    assert result.meets_minimum is None
    assert any("최소점수의 적용 범위" in item for item in result.assumptions)


@pytest.mark.parametrize("minimum,expected", [(0, True), (30, True), (40, True), (41, False)])
def test_pure_quantitative_minimum_comparison_is_unchanged(minimum, expected) -> None:
    binding = "2" * 64
    result = estimate_quantitative_score(_request(
        _scoreable(40, binding), facts=[_full_marks_fact(binding)], minimum_score=minimum,
    ))
    assert result.total_max_points == result.lower_points == result.upper_points == 40
    assert result.out_of_scope_points == 0
    assert result.meets_minimum is expected


@pytest.mark.parametrize("fact_status", ["CONFIRMED", "ESTIMATED", "MISSING"])
def test_mixed_engine_result_builds_public_rows_without_readding_excluded_points(fact_status) -> None:
    binding = "3" * 64
    result = estimate_quantitative_score(_request(
        _scoreable(40, binding), _qualitative(60),
        facts=[] if fact_status == "MISSING" else [
            _full_marks_fact(binding).model_copy(update={"status": fact_status})
        ],
    ))
    snapshot = build_public_quantitative_criteria_snapshot(result)
    assert snapshot is not None
    assert [row["max_points"] for row in snapshot["items"]] == [40, 60]
    assert snapshot["items"][1]["status"] == "OUT_OF_SCOPE"
    public = _public_quantitative_projection(result)
    assert public.total_max_points == public.upper_points == 40
    assert public.out_of_scope_points == 60
    assert public.lower_points == (0 if fact_status == "MISSING" else 40)
    assert len(public.criteria) == 2
    assert public.criteria[1].estimated_points is None
    assert public.criteria[1].status == "OUT_OF_SCOPE"
    assert "정량 합계에서 제외" in public.criteria[1].rationale
    assert public.criteria[1].source_anchor is None
    # A mismatched excluded aggregate must fail persistence just as a wrong
    # quantitative total does. Public projection must not launder the mismatch.
    assert build_public_quantitative_criteria_snapshot(
        result.model_copy(update={"out_of_scope_points": 59})
    ) is None


def test_all_excluded_engine_result_preserves_public_rows_without_a_zero_score() -> None:
    result = estimate_quantitative_score(_request(_qualitative(100)))
    assert build_public_quantitative_criteria_snapshot(result) is not None
    public = _public_quantitative_projection(result)
    assert len(public.criteria) == 1
    assert public.criteria[0].status == "OUT_OF_SCOPE"
    assert public.out_of_scope_points == 100
    assert public.total_max_points == 0
    assert public.overall_status == "UNSCORABLE"
    assert public.estimated_points is public.readiness_pct is None
    assert public.readiness_band == "GRAY"
    assert "RED 구간" not in result.opinion
