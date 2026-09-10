"""Financial-ratio derivation, pinned against the operator statement.

Every literal here is a verbatim production criterion. The figures are the
2023-2025 statement rows, whose printed ratios (52% / 191% / 91%) the
derivation reproduces to the same rounding.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pai_loop.quantitative_financial import (
    FINANCIAL_METRIC_KEY,
    CompanyFinancialYear,
    derive_financial_value,
    parse_financial_recognition_scope,
)

AS_OF = datetime(2026, 9, 10, tzinfo=timezone.utc)

STATEMENT = [
    CompanyFinancialYear(
        fiscal_year=2023,
        total_assets=Decimal("21989360"),
        equity=Decimal("10612766"),
        current_assets=Decimal("18693852"),
        current_liabilities=Decimal("10722597"),
        non_current_liabilities=Decimal("653997"),
    ),
    CompanyFinancialYear(
        fiscal_year=2024,
        total_assets=Decimal("26566360"),
        equity=Decimal("12422782"),
        current_assets=Decimal("23494890"),
        current_liabilities=Decimal("13956227"),
        non_current_liabilities=Decimal("187351"),
    ),
    CompanyFinancialYear(
        fiscal_year=2025,
        total_assets=Decimal("27501962"),
        equity=Decimal("14368825"),
        current_assets=Decimal("24614298"),
        current_liabilities=Decimal("12894252"),
        non_current_liabilities=Decimal("238885"),
    ),
]


def scope_for(literal: str):
    return parse_financial_recognition_scope(literal, metric_key=FINANCIAL_METRIC_KEY)


@pytest.mark.parametrize(
    ("literal", "kind", "expected"),
    [
        ("최근년도 자기자본비율(자기자본/총자산)", "EQUITY_TO_ASSETS", 52.25),
        ("최근년도 유동비율(유동자산/유동부채)", "CURRENT_RATIO", 190.89),
        ("부채비율(총부채/자기자본)", "DEBT_TO_EQUITY", 91.4),
    ],
)
def test_ratio_matches_the_printed_statement(literal, kind, expected) -> None:
    """The statement prints 52% / 191% / 91%; derivation must agree."""

    scope = scope_for(literal)
    assert scope is not None and scope.ratio_kind == kind
    derived = derive_financial_value(scope, STATEMENT, as_of=AS_OF)
    assert derived.status == "ESTIMATED"
    assert derived.value == pytest.approx(expected, abs=0.01)


def test_a_printed_benchmark_converts_the_ratio_to_a_percentage_of_it() -> None:
    """경영상태 rows score the ratio relative to a 기준비율 stated in the source."""

    scope = scope_for("가. 최근년도 자기자본비율(자기자본/총자산) - 기준비율 : 31.63%")
    assert scope is not None and scope.benchmark_pct == pytest.approx(31.63)
    derived = derive_financial_value(scope, STATEMENT, as_of=AS_OF)
    # 52.25 / 31.63 * 100
    assert derived.value == pytest.approx(165.19, abs=0.05)
    assert "기준비율" in derived.rationale


def test_prior_year_basis_selects_the_year_before_the_latest() -> None:
    scope = scope_for("직전년도 자기자본비율(자기자본/총자산)")
    assert scope is not None and scope.fiscal_basis == "PRIOR_YEAR"
    derived = derive_financial_value(scope, STATEMENT, as_of=AS_OF)
    # 2024: 12,422,782 / 26,566,360
    assert derived.value == pytest.approx(46.76, abs=0.01)
    assert "2024년도" in derived.rationale


@pytest.mark.parametrize(
    "literal",
    [
        # Names no ratio: an authority may score 경영상태 from a credit rating.
        "◯ 제안업체 경영상태",
        "경영상태 및 재무구조(5점)",
        # Scores what the bidder proposes, not what the company is.
        "기초금액 대비 ( 30 )% 이상 지급",
        "3-1. 인건비 편성 비율(10점)",
        # Two ratios in one row leave the subject undecided.
        "자기자본비율 및 유동비율 종합 평가",
        "",
    ],
)
def test_an_unnamed_or_bid_side_row_stays_manual(literal: str) -> None:
    assert scope_for(literal) is None


def test_a_foreign_metric_key_is_refused() -> None:
    assert (
        parse_financial_recognition_scope(
            "최근년도 유동비율", metric_key="company.personnel.count"
        )
        is None
    )


def test_a_statement_that_postdates_the_evaluation_is_not_used() -> None:
    scope = scope_for("최근년도 자기자본비율(자기자본/총자산)")
    assert scope is not None
    derived = derive_financial_value(
        scope, STATEMENT, as_of=datetime(2024, 6, 1, tzinfo=timezone.utc)
    )
    # 2025 is in the future for this evaluation, so 2024 is the latest usable.
    assert derived.value == pytest.approx(46.76, abs=0.01)


def test_no_usable_statement_refuses_rather_than_guesses() -> None:
    scope = scope_for("최근년도 유동비율(유동자산/유동부채)")
    assert scope is not None
    derived = derive_financial_value(scope, [], as_of=AS_OF)
    assert derived.status == "REVIEW"
    assert derived.value is None
