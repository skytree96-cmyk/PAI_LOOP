from __future__ import annotations

from decimal import Decimal

import pytest

from pai_loop.integrations.openai_extraction import (
    EvidenceAnchor,
    QuantitativeCaseLiteral,
    QuantitativeRuleCandidate,
)
from pai_loop.quantitative_formula import (
    CREDIT_RATING_ORDER,
    CaseTableAwardKind,
    CaseTableOperator,
    CaseTableRowLiteral,
    CompiledCaseTable,
    case_table_points,
    compile_case_table,
    compile_credit_rating_values,
    parse_credit_rating,
)
from pai_loop.quantitative_rule_extraction import validate_quantitative_rule_candidate
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion,
    QuantitativeEstimateRequest,
    QuantitativeFact,
    SourceAnchor,
    estimate_quantitative_score,
)


def _numeric_row(
    operator: CaseTableOperator,
    comparison_value: float,
    award_value: float,
    *,
    award_kind: CaseTableAwardKind = "POINTS",
) -> CaseTableRowLiteral:
    return CaseTableRowLiteral(
        operator=operator,
        comparison_value=comparison_value,
        award_kind=award_kind,
        award_value=award_value,
    )


def _category_row(
    values: tuple[str, ...],
    award_value: float,
    *,
    award_kind: CaseTableAwardKind = "POINTS",
) -> CaseTableRowLiteral:
    return CaseTableRowLiteral(
        operator="IN",
        category_values=values,
        award_kind=award_kind,
        award_value=award_value,
    )


def _credit_row(
    values: tuple[str, ...],
    award_value: float,
    *,
    source_literal: str | None = None,
    award_kind: CaseTableAwardKind = "POINTS",
) -> CaseTableRowLiteral:
    literal = source_literal or "\n".join(values)
    return CaseTableRowLiteral(
        operator="IN",
        category_values=values,
        source_literal=literal,
        award_kind=award_kind,
        award_value=award_value,
    )


def _valid_credit_rows() -> tuple[CaseTableRowLiteral, ...]:
    return (
        _credit_row(
            ("AAA", "AA+", "AA0", "AA-", "A+", "A0", "A-", "BBB+", "BBB0"),
            100,
            source_literal="AAA, AA+, AA0, AA-, A+, A0, A-, BBB+, BBB0 배점의 100%",
            award_kind="PERCENT_OF_MAX",
        ),
        _credit_row(
            ("BBB-", "BB+", "BB0", "BB-"),
            95,
            source_literal="BBB-, BB+, BB0, BB- 배점의 95%",
            award_kind="PERCENT_OF_MAX",
        ),
        _credit_row(
            ("B+", "B0", "B-"),
            90,
            source_literal="B+, B0, B- 배점의 90%",
            award_kind="PERCENT_OF_MAX",
        ),
        _credit_row(
            ("CCC+ 이하",),
            70,
            source_literal="CCC+ 이하 배점의 70%",
            award_kind="PERCENT_OF_MAX",
        ),
    )


def test_ordered_descending_gte_uses_first_match_without_inventing_fallback() -> None:
    table = compile_case_table(
        (
            _numeric_row("GTE", 200_000_000, 6),
            _numeric_row("GTE", 150_000_000, 5.5),
            _numeric_row("GTE", 100_000_000, 5),
        ),
        value_kind="NUMERIC",
        maximum_points=6,
    )

    assert table is not None
    assert case_table_points(table, 250_000_000) == 6
    assert case_table_points(table, 175_000_000) == 5.5
    assert case_table_points(table, Decimal("100000000")) == 5
    assert case_table_points(table, 99_999_999) is None


@pytest.mark.parametrize(
    "rows",
    [
        (_numeric_row("GTE", 100, 5), _numeric_row("GTE", 200, 4)),
        (_numeric_row("GTE", 200, 6), _numeric_row("GTE", 200, 5)),
        (_numeric_row("GTE", 200, 5), _numeric_row("GTE", 100, 6)),
        (_numeric_row("GTE", 200, 6), _numeric_row("EQ", 100, 5)),
    ],
)
def test_numeric_case_table_rejects_non_deterministic_rows(
    rows: tuple[CaseTableRowLiteral, ...],
) -> None:
    assert compile_case_table(rows, value_kind="NUMERIC", maximum_points=6) is None


def test_discrete_case_table_supports_leading_gte_then_exact_rows() -> None:
    table = compile_case_table(
        (
            _numeric_row("GTE", 5, 4),
            _numeric_row("EQ", 4, 3.7),
            _numeric_row("EQ", 3, 3.4),
            _numeric_row("EQ", 2, 3.1),
            _numeric_row("EQ", 1, 2.8),
        ),
        value_kind="DISCRETE",
        maximum_points=4,
    )

    assert table is not None
    assert case_table_points(table, 8) == 4
    assert case_table_points(table, 4) == 3.7
    assert case_table_points(table, 1) == 2.8
    assert case_table_points(table, 4.5) is None
    assert case_table_points(table, 0) is None


def test_discrete_case_table_rejects_shadowed_or_non_integral_rows() -> None:
    assert compile_case_table(
        (_numeric_row("EQ", 4, 4), _numeric_row("GTE", 3, 3)),
        value_kind="DISCRETE",
        maximum_points=4,
    ) is None
    assert compile_case_table(
        (_numeric_row("GTE", 4.5, 4),),
        value_kind="DISCRETE",
        maximum_points=4,
    ) is None


def test_categorical_in_rows_convert_percent_of_max_and_remain_disjoint() -> None:
    table = compile_case_table(
        (
            _category_row(
                ("AAA", "AA+", "AA0", "AA-", "A+", "A0", "A-", "BBB+", "BBB0"),
                100,
                award_kind="PERCENT_OF_MAX",
            ),
            _category_row(("BBB-", "BB+", "BB0", "BB-"), 95, award_kind="PERCENT_OF_MAX"),
            _category_row(("B+", "B0", "B-"), 90, award_kind="PERCENT_OF_MAX"),
            _category_row(("CCC+ 이하",), 70, award_kind="PERCENT_OF_MAX"),
        ),
        value_kind="CATEGORICAL",
        maximum_points=10,
    )

    assert table is not None
    assert case_table_points(table, "A0") == 10
    assert case_table_points(table, " A 0 ") == 10
    assert case_table_points(table, "BBB-") == 9.5
    assert case_table_points(table, "미등록등급") is None


def test_credit_registry_compiles_ranges_to_exact_canonical_grade_sets() -> None:
    rows = (
        _credit_row(("A0 이상",), 10),
        _credit_row(("A0 미만 BBB+ 이상",), 9.8),
        _credit_row(
            ("BBB+ 미만", "BB- 초과"),
            9.5,
            source_literal="BBB+ 미만\nBB- 초과",
        ),
        _credit_row(
            ("BB- 이하", "B+ 이상"),
            9,
            source_literal="BB- 이하\nB+ 이상",
        ),
        _credit_row(("B+ 미만",), 7),
    )

    table = compile_case_table(rows, value_kind="CREDIT_RATING", maximum_points=10)

    assert table is not None
    assert tuple(value for row in table.rows for value in row.category_values) == (
        CREDIT_RATING_ORDER
    )
    assert table.rows[0].category_values == ("AAA", "AA+", "AA0", "AA-", "A+", "A0")
    assert table.rows[1].category_values == ("A-", "BBB+")
    assert table.rows[2].category_values == ("BBB0", "BBB-", "BB+", "BB0")
    assert table.rows[3].category_values == ("BB-", "B+")
    assert table.rows[4].category_values == (
        "B0", "B-", "CCC+", "CCC0", "CCC-", "CC", "C", "D"
    )
    assert [case_table_points(table, value) for value in ("A0", "A", "A-", "BBB+", "BBB-", "BB-", "B+")] == [
        10,
        10,
        9.8,
        9.8,
        9.5,
        9,
        9,
    ]


def test_busan_credit_range_scores_ccc0_at_seventy_percent() -> None:
    table = compile_case_table(
        _valid_credit_rows(),
        value_kind="CREDIT_RATING",
        maximum_points=10,
    )

    assert table is not None
    assert case_table_points(table, "CCC0") == 7
    assert case_table_points(table, "A") == case_table_points(table, "A0") == 10
    assert case_table_points(table, "미등록등급") is None
    assert parse_credit_rating("A") == "A0"
    assert parse_credit_rating("AA") is None


@pytest.mark.parametrize(
    ("source", "canonical"),
    (("A−", "A-"), ("BBB−", "BBB-"), ("CCC−", "CCC-")),
)
def test_credit_parser_normalizes_unicode_minus_variants(
    source: str,
    canonical: str,
) -> None:
    assert parse_credit_rating(source) == canonical
    assert compile_credit_rating_values((source,), source_literal=source) == (
        canonical,
    )


def test_credit_range_compiler_normalizes_unicode_minus_in_bounds_and_source() -> None:
    assert compile_credit_rating_values(
        ("A-",),
        source_literal="A−",
    ) == ("A-",)
    assert compile_credit_rating_values(
        ("BBB− 이하", "CCC− 초과"),
        source_literal="BBB− 이하\nCCC− 초과",
    ) == ("BBB-", "BB+", "BB0", "BB-", "B+", "B0", "B-", "CCC+", "CCC0")


@pytest.mark.parametrize(
    "rows",
    (
        (_credit_row(("A1",), 10),),
        (
            _credit_row(
                ("A0",),
                10,
                source_literal="A0\nBBB0 배점의 100%",
            ),
        ),
        (
            _credit_row(("BBB- 이상",), 10),
            _credit_row(("BBB- 이하",), 9),
        ),
        (
            _credit_row(("BBB- 초과",), 10),
            _credit_row(("BBB- 미만",), 9),
        ),
        (_credit_row(("BBB- 이상",), 10),),
        (
            *_valid_credit_rows()[:-1],
            _credit_row(("CCC+ 이하",), 70, source_literal="CCC+ 이하\nD+"),
        ),
    ),
)
def test_credit_registry_rejects_unknown_ambiguous_overlap_gap_missing_or_extra(
    rows: tuple[CaseTableRowLiteral, ...],
) -> None:
    assert (
        compile_case_table(rows, value_kind="CREDIT_RATING", maximum_points=10)
        is None
    )


def test_percent_awards_require_maximum_and_category_overlap_is_rejected() -> None:
    percent_row = _category_row(("A0",), 100, award_kind="PERCENT_OF_MAX")
    assert compile_case_table((percent_row,), value_kind="CATEGORICAL") is None
    assert compile_case_table(
        (percent_row, _category_row(("A 0",), 95, award_kind="PERCENT_OF_MAX")),
        value_kind="CATEGORICAL",
        maximum_points=10,
    ) is None


def test_direct_point_rows_can_compile_without_maximum_but_respect_one_when_given() -> None:
    row = _numeric_row("GTE", 1, 2)
    table = compile_case_table((row,), value_kind="NUMERIC")

    assert table is not None
    assert case_table_points(table, 1) == 2
    assert case_table_points(table, "1") is None
    assert compile_case_table(
        (_numeric_row("GTE", 1, 11),),
        value_kind="NUMERIC",
        maximum_points=10,
    ) is None


@pytest.mark.parametrize(
    ("table", "lower", "upper"),
    [
        (
            compile_case_table(
                (
                    _numeric_row("GTE", 200, 6),
                    _numeric_row("GTE", 150, 5.5),
                    _numeric_row("GTE", 100, 5),
                ),
                value_kind="NUMERIC",
                maximum_points=6,
            ),
            50,
            250,
        ),
        (
            compile_case_table(
                (
                    _numeric_row("GTE", 5, 4),
                    _numeric_row("EQ", 3, 3.4),
                ),
                value_kind="DISCRETE",
                maximum_points=4,
            ),
            3,
            5,
        ),
    ],
)
def test_estimated_case_range_fails_closed_when_any_value_is_undefined(
    table: CompiledCaseTable,
    lower: float,
    upper: float,
) -> None:
    assert table is not None
    criterion = QuantitativeCriterion(
        criterion_id="case-range",
        category="PERFORMANCE_AMOUNT",
        label="실적",
        max_points=float(table.maximum_points or 0),
        metric_key="company.performance.amount",
        unit="KRW",
        formula_type="CASE_TABLE",
        formula="원문 CASE",
        case_table=table,
        source_anchor=SourceAnchor(
            document_label="rfp",
            document_sha256=None,
            section="정량표",
            page=1,
            quote="원문 CASE",
        ),
        required_evidence_keys=["company.performance.amount"],
    )
    result = estimate_quantitative_score(
        QuantitativeEstimateRequest(
            ruleset_version="case-range-v1",
            rule_source_status="AVAILABLE",
            source_validation_status="SOURCE_VALIDATED",
            activation_status="AUTO_ACTIVE",
            criteria=[criterion],
            facts=[
                QuantitativeFact(
                    metric_key="company.performance.amount",
                    status="ESTIMATED",
                    lower_value=lower,
                    upper_value=upper,
                    evidence_key="company.performance.amount",
                    confidence=0.8,
                )
            ],
        )
    )

    assert result.overall_status == "REVIEW"
    assert result.estimated_points is None
    assert result.criteria[0].status == "REVIEW"


def _validate_credit_case(
    literal: str,
    category_values: list[str],
):
    attachment_id = "credit-rfp"
    criterion_literal = "제안업체 경영상태 신용등급 (10점)"
    source = f"{criterion_literal}\n{literal}"
    candidate = QuantitativeRuleCandidate(
        criterion_id="credit",
        label="경영상태",
        criterion_literal=criterion_literal,
        max_points=10,
        scoring_method="CASE_TABLE",
        metric="CREDIT_RATING",
        unit="등급",
        brackets=[],
        threshold=None,
        formula_literal=None,
        cases=[
            QuantitativeCaseLiteral(
                literal=literal,
                operator="IN",
                comparison_value=None,
                category_values=category_values,
                award_kind="PERCENT_OF_MAX",
                award_value=100,
                row_order=1,
                evidence=EvidenceAnchor(
                    attachment_id=attachment_id,
                    page=1,
                    section="정량표",
                    quote=literal,
                    confidence=1,
                ),
            )
        ],
        required_evidence=["company.credit_rating"],
        evidence=EvidenceAnchor(
            attachment_id=attachment_id,
            page=1,
            section="정량표",
            quote=criterion_literal,
            confidence=1,
        ),
        ambiguity_reason=None,
    )
    return validate_quantitative_rule_candidate(
        candidate,
        source_attachment_id=attachment_id,
        table_id="credit-table",
        source_text_by_attachment_id={attachment_id: source},
        expected_attachment_ids=[attachment_id],
    )


def test_category_source_binding_rejects_a0_substring_inside_aa0() -> None:
    available, review, issues = _validate_credit_case("AA0 배점의 100%", ["A0"])

    assert available is None
    assert review is not None
    assert "CASE_CATEGORY_MISMATCH" in {item.code for item in issues}


def test_nfkc_duplicate_category_becomes_review_issue_instead_of_exception() -> None:
    available, review, issues = _validate_credit_case("Ａ, A 배점의 100%", ["Ａ", "A"])

    assert available is None
    assert review is not None
    assert review.status == "REVIEW"
    assert {
        "CASE_CATEGORY_DUPLICATE",
        "CASE_ROW_VALIDATION_FAILED",
    }.issubset({item.code for item in issues})
