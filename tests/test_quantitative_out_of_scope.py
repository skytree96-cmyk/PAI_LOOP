"""Rows set aside must not sink the notice, and must not flatter it either.

Every label here is verbatim corpus text. The arithmetic case is the one that
motivated the change: a notice whose 정성 배점 outweighs its 정량 배점 used to
return REVIEW with a RED band even when every scoreable row was full marks.
"""

from __future__ import annotations

import pytest

from pai_loop.quantitative_out_of_scope import out_of_scope_reason
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeEstimateRequest,
    QuantitativeFact,
    ScoreBracket,
    SourceAnchor,
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


def _request(*criteria: QuantitativeCriterion, facts=()) -> QuantitativeEstimateRequest:
    return QuantitativeEstimateRequest(
        ruleset_version="out-of-scope-test",
        rule_source_status="AVAILABLE",
        source_validation_status="SOURCE_VALIDATED",
        activation_status="AUTO_ACTIVE",
        criteria=list(criteria),
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


def test_the_set_aside_points_stay_in_the_range_as_an_assumption() -> None:
    binding = "c" * 64
    result = estimate_quantitative_score(
        _request(
            _scoreable(40, binding),
            _qualitative(60),
            facts=[_full_marks_fact(binding)],
        )
    )

    # 하한은 정량만, 상한은 정성 만점을 가정해 100.
    assert result.lower_points == 40
    assert result.upper_points == 100
    assert result.total_max_points == 100
    # 범위가 벌어져 있으므로 단일 점수로 확정하지 않는다.
    assert result.estimated_points is None
    assert any("만점을 받는다고 가정" in item for item in result.assumptions)

    row = next(item for item in result.criteria if item.criterion_id == "사업이해도")
    assert row.status == "OUT_OF_SCOPE"
    assert row.lower_points == 0
    assert row.upper_points == 60
    assert "정성평가" in row.rationale


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
