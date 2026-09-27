"""SYN financial years are selected by the source's exact reference date."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pai_loop.models import CompanyFact
from pai_loop.quantitative_financial import (
    FINANCIAL_METRIC_KEY, CompanyFinancialYear, derive_financial_value,
    parse_financial_recognition_scope,
)
from pai_loop.quantitative_scoring import estimate_for_notice
from test_quantitative_financial_binding import _profile


def _year(year, ratio):
    return CompanyFinancialYear(
        fiscal_year=year, total_assets=Decimal(1000), equity=Decimal(ratio * 10),
        current_assets=Decimal(ratio * 10), current_liabilities=Decimal(1000),
    )


def _scope(literal="SYN 전년도 자기자본비율"):
    scope = parse_financial_recognition_scope(literal, metric_key=FINANCIAL_METRIC_KEY)
    assert scope is not None
    return scope


@pytest.mark.parametrize("literal", ["SYN 전년도 자기자본비율", "SYN 직전년도 자기자본비율", "SYN 직전 연도 자기자본비율"])
def test_one_exact_prior_year_is_sufficient_without_a_second_statement(literal):
    result = derive_financial_value(_scope(literal), [_year(2025, 40)],
                                    as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result.status == "ESTIMATED" and result.value == 40
    assert "2025년도" in result.evidence_reference


@pytest.mark.parametrize("years", [[2023, 2024], [2023, 2026], [2026, 2027], []])
def test_missing_exact_prior_year_never_uses_latest_or_second_latest(years):
    result = derive_financial_value(_scope(), [_year(year, 10) for year in years],
                                    as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result.status == "REVIEW" and result.value is None
    assert "2025년도" in result.rationale


def test_current_and_future_years_cannot_displace_the_required_prior_year():
    result = derive_financial_value(_scope(), [_year(2027, 90), _year(2026, 80), _year(2025, 40), _year(2023, 20)],
                                    as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result.value == 40


def test_latest_excludes_unfinished_current_year_and_future_annual_statements():
    result = derive_financial_value(_scope("SYN 최근년도 자기자본비율"),
                                    [_year(2027, 90), _year(2026, 80), _year(2025, 40)],
                                    as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result.status == "ESTIMATED" and result.value == 40


def test_latest_can_use_the_latest_older_year_without_inventing_the_prior_year():
    result = derive_financial_value(_scope("SYN 최근년도 자기자본비율"),
                                    [_year(2023, 20), _year(2024, 30)],
                                    as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result.status == "ESTIMATED" and result.value == 30
    assert "2024년도" in result.evidence_reference


def test_latest_with_only_current_or_future_annual_statements_stays_review():
    result = derive_financial_value(_scope("SYN 최근년도 자기자본비율"),
                                    [_year(2026, 80), _year(2027, 90)],
                                    as_of=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result.status == "REVIEW" and result.value is None


@pytest.mark.parametrize(("reference", "expected"), [
    (datetime(2025, 12, 31, 14, 59, 59, tzinfo=timezone.utc), 20),
    (datetime(2025, 12, 31, 15, 0, tzinfo=timezone.utc), 40),
    (datetime(2025, 12, 31, 15, 0), 40),
])
def test_calendar_year_uses_kst_and_naive_database_datetimes_are_utc(reference, expected):
    result = derive_financial_value(_scope(), [_year(2024, 20), _year(2025, 40)], as_of=reference)
    assert result.value == expected


def test_publication_basis_does_not_fall_back_to_deadline_when_missing():
    result = derive_financial_value(_scope("SYN 공고일 기준 전년도 자기자본비율"), [_year(2025, 40)],
                                    as_of=datetime(2026, 1, 10, tzinfo=timezone.utc))
    assert result.status == "REVIEW" and result.value is None
    assert "공고일" in result.rationale


@pytest.mark.parametrize("literal", [
    "SYN 전기 자기자본비율", "SYN 전기말 유동비율", "SYN 전전년도 유동비율",
    "SYN 2024년도 유동비율", "SYN 공고일 전월 기준 유동비율",
    "SYN 공고일 기준 및 마감일 기준 전년도 유동비율",
])
def test_ambiguous_or_unmodeled_reference_is_not_guessed(literal):
    assert parse_financial_recognition_scope(literal, metric_key=FINANCIAL_METRIC_KEY) is None


@pytest.mark.parametrize(("prefix", "year", "expected_points"), [
    ("공고일 기준", 2024, 3), ("마감일 기준", 2025, 4),
])
def test_native_validated_rules_and_notice_resolver_use_the_source_reference(monkeypatch, prefix, year, expected_points):
    profile = _profile((f"SYN {prefix} 전년도 자기자본비율", f"SYN {prefix} 전년도 유동비율"))
    monkeypatch.setattr("pai_loop.quantitative_scoring._current_dynamic_quantitative_profile", lambda _notice: profile)
    years = [_year(2024, 40), _year(2025, 80)]
    statement = CompanyFact(fact_key="company.financial.statement", value={
        "years": [item.model_dump(mode="json") for item in years],
    })
    notice = SimpleNamespace(deadline=datetime(2026, 1, 10, tzinfo=timezone.utc),
                             published_at=datetime(2025, 12, 31, 14, tzinfo=timezone.utc))
    result = estimate_for_notice(notice, [statement])
    assert len(result.criteria) == 2
    assert all(item.status == "ESTIMATED" for item in result.criteria)
    assert sum(item.estimated_points for item in result.criteria) == expected_points
    assert all(f"{year}년도" in item.rationale for item in result.criteria)
