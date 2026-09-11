"""Derive a financial-ratio value for one source-bound criterion.

This mirrors :mod:`pai_loop.quantitative_performance`. A company financial
statement is a company-level scalar, so unlike a headcount it carries no
per-bid interpretation: 유동비율 191% is 191% whichever notice reads it. That
is what makes automatic derivation safe here and unsafe for participation
headcounts, where the source asks about the team assigned to one bid rather
than the whole payroll.

Derivation stays fail-closed. A criterion that does not name which ratio it
scores yields no scope, and no scope means no automatic value.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FinancialRatioKind = Literal[
    "EQUITY_TO_ASSETS",
    "CURRENT_RATIO",
    "DEBT_TO_EQUITY",
]
FinancialFiscalBasis = Literal["LATEST", "PRIOR_YEAR"]
FINANCIAL_METRIC_KEY = "company.financial.ratio"
# Raw operator input, not a scoreable fact: derivation reads it and stamps the
# criterion's own binding on the value it produces.
FINANCIAL_STATEMENT_FACT_KEY = "company.financial.statement"


class FinancialQuantModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FinancialRecognitionScope(FinancialQuantModel):
    """The exact ratio, fiscal year and benchmark a criterion scores."""

    metric_key: Literal["company.financial.ratio"]
    ratio_kind: FinancialRatioKind
    fiscal_basis: FinancialFiscalBasis = "LATEST"
    # ``기준비율 31.63%`` printed in the source. When present the score input is
    # the company ratio expressed as a percentage OF that benchmark, which is
    # how 경영상태 평가 is written; when absent the ratio itself is the input.
    benchmark_pct: float | None = Field(default=None, gt=0, le=10_000)
    source_literal: str = Field(min_length=1, max_length=2_000)


class CompanyFinancialYear(FinancialQuantModel):
    """One fiscal year of the operator-maintained statement, in KRW thousands."""

    fiscal_year: int = Field(ge=1900, le=2200)
    total_assets: Decimal = Field(gt=0)
    equity: Decimal
    current_assets: Decimal = Field(ge=0)
    current_liabilities: Decimal = Field(gt=0)
    non_current_liabilities: Decimal = Field(default=Decimal(0), ge=0)

    @model_validator(mode="after")
    def validate_equity(self) -> "CompanyFinancialYear":
        if self.equity > self.total_assets:
            raise ValueError("equity cannot exceed total assets")
        return self


class DerivedFinancialValue(FinancialQuantModel):
    status: Literal["ESTIMATED", "REVIEW"]
    value: float | None = None
    rationale: str = Field(min_length=1, max_length=1_000)
    evidence_reference: str | None = Field(default=None, max_length=300)


_RATIO_PATTERNS: tuple[tuple[FinancialRatioKind, re.Pattern[str]], ...] = (
    (
        "EQUITY_TO_ASSETS",
        re.compile(r"자기\s*자본\s*비율|자기\s*자본\s*/\s*총\s*자산"),
    ),
    (
        "CURRENT_RATIO",
        re.compile(r"유동\s*비율|유동\s*자산\s*/\s*유동\s*부채"),
    ),
    (
        "DEBT_TO_EQUITY",
        re.compile(r"부채\s*비율|총\s*부채\s*/\s*자기\s*자본"),
    ),
)
_BENCHMARK_RE = re.compile(r"기준\s*비율\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%")
_PRIOR_YEAR_RE = re.compile(r"직전\s*년도|전년도|전기")
# A row that scores what the bidder proposes, not what the company is.
_BID_PROPOSAL_RE = re.compile(r"인건비\s*편성|기초\s*금액\s*대비|투찰|제안\s*가격")


def parse_financial_recognition_scope(
    literal: str,
    *,
    metric_key: str,
) -> FinancialRecognitionScope | None:
    """Parse only an explicitly named ratio from a source-bound rule.

    ``경영상태`` on its own is not enough: an authority may score it from a
    credit rating, an equity ratio or a composite. Without the ratio named in
    the source there is nothing to compute against, so this returns None and
    the criterion stays manual.
    """

    if metric_key != FINANCIAL_METRIC_KEY:
        return None
    text = " ".join(literal.split())
    if not text or _BID_PROPOSAL_RE.search(text):
        return None

    matched = [kind for kind, pattern in _RATIO_PATTERNS if pattern.search(text)]
    if len(matched) != 1:
        # Silent about the ratio, or scoring more than one in a single row.
        return None

    benchmark = _BENCHMARK_RE.search(text)
    return FinancialRecognitionScope(
        metric_key=FINANCIAL_METRIC_KEY,
        ratio_kind=matched[0],
        fiscal_basis="PRIOR_YEAR" if _PRIOR_YEAR_RE.search(text) else "LATEST",
        benchmark_pct=float(benchmark.group(1)) if benchmark else None,
        source_literal=text[:2_000],
    )


def _ratio_pct(year: CompanyFinancialYear, kind: FinancialRatioKind) -> Decimal | None:
    if kind == "EQUITY_TO_ASSETS":
        return year.equity / year.total_assets * 100
    if kind == "CURRENT_RATIO":
        return year.current_assets / year.current_liabilities * 100
    if kind == "DEBT_TO_EQUITY":
        if year.equity <= 0:
            return None
        total_debt = year.current_liabilities + year.non_current_liabilities
        return total_debt / year.equity * 100
    return None


def derive_financial_value(
    scope: FinancialRecognitionScope,
    statements: list[CompanyFinancialYear],
    *,
    as_of: datetime,
) -> DerivedFinancialValue:
    """Compute the score input this criterion asks for, or refuse to guess."""

    usable = sorted(
        (item for item in statements if item.fiscal_year <= as_of.year),
        key=lambda item: item.fiscal_year,
        reverse=True,
    )
    if not usable:
        return DerivedFinancialValue(
            status="REVIEW",
            rationale=(
                "평가 기준일 이전 회계연도의 재무제표가 없어 자동 계산을 중지했습니다."
            ),
        )
    index = 1 if scope.fiscal_basis == "PRIOR_YEAR" else 0
    if index >= len(usable):
        return DerivedFinancialValue(
            status="REVIEW",
            rationale=(
                "원문이 요구하는 회계연도의 재무제표가 없어 자동 계산을 중지했습니다."
            ),
        )
    year = usable[index]
    ratio = _ratio_pct(year, scope.ratio_kind)
    if ratio is None:
        return DerivedFinancialValue(
            status="REVIEW",
            rationale=(
                "자기자본이 0 이하여서 해당 비율을 산정할 수 없습니다."
            ),
        )

    label = {
        "EQUITY_TO_ASSETS": "자기자본비율",
        "CURRENT_RATIO": "유동비율",
        "DEBT_TO_EQUITY": "부채비율",
    }[scope.ratio_kind]

    if scope.benchmark_pct is None:
        return DerivedFinancialValue(
            status="ESTIMATED",
            value=float(round(ratio, 2)),
            rationale=(
                f"{year.fiscal_year}년도 {label} {ratio:.1f}%를 원문 구간에 그대로 "
                "적용했습니다."
            ),
            evidence_reference=f"재무제표 {year.fiscal_year}년도 {label}",
        )

    relative = ratio / Decimal(str(scope.benchmark_pct)) * 100
    return DerivedFinancialValue(
        status="ESTIMATED",
        value=float(round(relative, 2)),
        rationale=(
            f"{year.fiscal_year}년도 {label} {ratio:.1f}%를 원문이 명시한 기준비율 "
            f"{scope.benchmark_pct:g}%로 나눠 {relative:.1f}%로 환산했습니다."
        ),
        evidence_reference=(
            f"재무제표 {year.fiscal_year}년도 {label} / 기준비율 {scope.benchmark_pct:g}%"
        ),
    )


def load_financial_statement(
    facts: object,
) -> list[CompanyFinancialYear]:
    """Read the operator statement from the ``company.financial.statement`` fact.

    The statement is six figures a year, so it lives as one JSON company fact
    rather than its own table. It is raw input to derivation, never a scoreable
    fact itself, so it needs no criterion binding.
    """

    years: list[CompanyFinancialYear] = []
    for fact in facts or ():  # type: ignore[union-attr]
        if getattr(fact, "fact_key", None) != FINANCIAL_STATEMENT_FACT_KEY:
            continue
        raw = getattr(fact, "value", None)
        rows = raw.get("years") if isinstance(raw, dict) else None
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            try:
                years.append(
                    CompanyFinancialYear(
                        fiscal_year=int(row["fiscal_year"]),
                        total_assets=Decimal(str(row["total_assets"])),
                        equity=Decimal(str(row["equity"])),
                        current_assets=Decimal(str(row["current_assets"])),
                        current_liabilities=Decimal(str(row["current_liabilities"])),
                        non_current_liabilities=Decimal(
                            str(row.get("non_current_liabilities", 0))
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # One malformed year must not silently reshape the others.
                return []
    by_year = {item.fiscal_year: item for item in years}
    return sorted(by_year.values(), key=lambda item: item.fiscal_year)


__all__ = [
    "FINANCIAL_STATEMENT_FACT_KEY",
    "CompanyFinancialYear",
    "load_financial_statement",
    "DerivedFinancialValue",
    "FINANCIAL_METRIC_KEY",
    "FinancialRatioKind",
    "FinancialRecognitionScope",
    "derive_financial_value",
    "parse_financial_recognition_scope",
]
