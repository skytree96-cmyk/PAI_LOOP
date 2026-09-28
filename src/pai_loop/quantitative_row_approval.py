"""Human-reviewed quantitative row programs bound to one exact source row.

A reviewed row approval lets a single notice row that failed only printed
literal checks contribute an ESTIMATED partial subtotal. It never changes the
global parser, never validates a table total or minimum, never yields a
CONFIRMED score, and stops applying as soon as the manifest, document, raw
extracted row, program or current validation codes change.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ROW_APPROVAL_FACT_KEY = "notice.quantitative.row_approval"
ROW_APPROVAL_SOURCE = "PRIVATE_ROW_APPROVAL"
ROW_APPROVAL_REASON = "HUMAN_REVIEWED_ROW_APPROVAL"
ROW_APPROVAL_WARNING = (
    "사람이 원문 행의 배점 해석을 승인해 계산한 추정치이며, 공고 원문과 제출 시점 증빙의 재확인이 필요합니다."
)
ROW_APPROVAL_ASSUMPTION = (
    "일부 항목은 원문 행 해석을 사람이 승인해 계산한 추정치입니다. 승인 이후 원문이나 검증 결과가 바뀌면 적용하지 않습니다."
)

# Only printed-literal checks a person can settle by reading the row itself.
# Semantic blockers (a model-declared ambiguous rule, a nondeterministic case
# table, missing evidence, an unknown metric, totals or minimums) are never
# waivable here.
WAIVABLE_ISSUE_CODES = frozenset({
    "CASE_NUMBER_MISMATCH",
    "MAX_POINTS_LITERAL_MISMATCH",
    "AMBIGUOUS_TABLE",
    "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION",
})

_SHA = r"^[a-f0-9]{64}$"
_DEDUCTION_RE = re.compile(r"감점|차감|공제|마이너스|벌점|minus", re.IGNORECASE)
# "5명이상-10점": the hyphen separates the condition from its award. The
# condition and the award must each be one plain number; nothing else fits.
_HYPHEN_CASE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>명|건|개|회|년|%)?\s*"
    r"(?P<comparator>이상|이하|미만)\s*-\s*(?P<points>\d+(?:\.\d+)?)\s*점"
)
_COMPARATOR = {"이상": "GTE", "이하": "LTE", "미만": "LT"}


class ApprovedCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    literal: str = Field(min_length=1, max_length=200)
    operator: Literal["GTE", "LTE", "LT"]
    comparison_value: float
    award_value: float = Field(ge=0)


class ApprovedRowProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=80)
    scoring_method: Literal["CASE_TABLE"]
    max_points: float = Field(gt=0)
    unit: str | None = Field(default=None, max_length=80)
    cases: list[ApprovedCase] = Field(min_length=1, max_length=20)


class QuantitativeRowApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["pai-loop-row-approval-1.0.0"] = "pai-loop-row-approval-1.0.0"
    attestation: Literal["HUMAN_REVIEWED_ROW_PROGRAM"]
    interpretation: Literal["HYPHEN_SEPARATED_POINTS"]
    accepted_on: date
    effective_through: date
    notice_key: str = Field(min_length=1, max_length=255)
    manifest_sha256: str = Field(pattern=_SHA)
    attachment_id: str = Field(min_length=1, max_length=255)
    document_sha256: str = Field(pattern=_SHA)
    table_id: str = Field(min_length=1, max_length=120)
    criterion_id: str = Field(min_length=1, max_length=120)
    raw_candidate_sha256: str = Field(pattern=_SHA)
    waived_issue_codes: list[str] = Field(min_length=1, max_length=len(WAIVABLE_ISSUE_CODES))
    program: ApprovedRowProgram

    @model_validator(mode="after")
    def validate_bounds(self) -> "QuantitativeRowApproval":
        if self.effective_through < self.accepted_on:
            raise ValueError("The approval horizon precedes its acceptance.")
        codes = self.waived_issue_codes
        if codes != sorted(set(codes)) or not set(codes) <= WAIVABLE_ISSUE_CODES:
            raise ValueError("Waived codes must be sorted, unique and individually reviewable.")
        return self

    def row_key(self) -> tuple[str, str, str]:
        return (self.attachment_id, self.table_id, self.criterion_id)


def raw_candidate_sha256(raw: BaseModel) -> str:
    """Digest of the exact raw extracted row (model output), never repaired text."""

    return hashlib.sha256(json.dumps(
        raw.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def row_program(raw: Any) -> dict[str, Any] | None:
    """The scoring program a reviewer sees, or None for an unsupported row."""

    if getattr(raw, "scoring_method", None) != "CASE_TABLE" or not getattr(raw, "cases", None):
        return None
    cases = sorted(raw.cases, key=lambda item: item.row_order)
    if any(
        case.operator not in {"GTE", "LTE", "LT"} or case.comparison_value is None
        or case.comparison_upper_value is not None or case.category_values
        or case.award_kind != "POINTS"
        for case in cases
    ):
        return None
    try:
        return ApprovedRowProgram(
            metric=raw.metric, scoring_method="CASE_TABLE", max_points=raw.max_points,
            unit=raw.unit,
            cases=[ApprovedCase(literal=case.literal, operator=case.operator,
                                comparison_value=case.comparison_value,
                                award_value=case.award_value) for case in cases],
        ).model_dump(mode="json")
    except ValueError:
        return None


def hyphen_case_rows_match(raw: Any) -> bool:
    """Check that every printed row reads exactly as its extracted program.

    This is a per-row consistency proof for a person's reading, not a parser:
    each case literal must be one ``N(unit)(이상|이하|미만)-M점`` token printed in
    the row's own quote, and the extracted operator, comparison and award must
    equal it. Any deduction wording keeps the row unapproved.
    """

    program = row_program(raw)
    if program is None or getattr(raw, "ambiguity_reason", None):
        return False
    texts = [raw.label, raw.criterion_literal, raw.evidence.quote,
             *(item.literal for item in raw.recognition_conditions)]
    if any(_DEDUCTION_RE.search(unicodedata.normalize("NFKC", text or "")) for text in texts):
        return False
    body = _compact(raw.criterion_literal)
    quote = _compact(raw.evidence.quote)
    units: set[str] = set()
    awards: list[Decimal] = []
    for case in raw.cases:
        literal = unicodedata.normalize("NFKC", case.literal).strip()
        match = _HYPHEN_CASE_RE.fullmatch(literal)
        if match is None:
            return False
        value, points = _decimal(match["value"]), _decimal(match["points"])
        if (
            _COMPARATOR[match["comparator"]] != case.operator
            or value is None or points is None
            or value != _decimal(case.comparison_value)
            or points != _decimal(case.award_value)
            or _compact(literal) not in body
            or _compact(literal) not in quote
            or _compact(literal) not in _compact(case.evidence.quote)
        ):
            return False
        if match["unit"]:
            units.add(match["unit"])
        awards.append(points)
    if len(units) > 1 or (units and raw.unit and next(iter(units)) != raw.unit):
        return False
    return max(awards) == _decimal(raw.max_points)


__all__ = [
    "ApprovedCase",
    "ApprovedRowProgram",
    "QuantitativeRowApproval",
    "ROW_APPROVAL_ASSUMPTION",
    "ROW_APPROVAL_FACT_KEY",
    "ROW_APPROVAL_REASON",
    "ROW_APPROVAL_SOURCE",
    "ROW_APPROVAL_WARNING",
    "WAIVABLE_ISSUE_CODES",
    "hyphen_case_rows_match",
    "raw_candidate_sha256",
    "row_program",
]
