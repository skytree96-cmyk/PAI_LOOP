"""Derive a personnel headcount for one source-bound criterion.

This mirrors :mod:`pai_loop.quantitative_financial`. A roster count is only a
company-level fact when the source asks about the payroll -- and many notices
do not. ``[서식 2] 사업수행인력 투입계획에 따라 평가함`` scores the team the
bidder assigns to this one contract, which no company record can answer, so
those rows stay manual. Rows whose recognition conditions read ``4대보험
가입자 명부 제출 시에만 인정`` ask exactly what the roster holds, and those
are the rows this module derives.

Two properties of the roster shape the design:

* ``박사(수료)`` is a candidate, not a degree holder. Counting it as 박사
  would overstate the company in a bid document, so the rank mapping puts it
  at the master's level it actually holds.
* The credential column is blank for 57 of 154 members. A credential count is
  therefore a lower bound, and a lower bound may only be scored when it
  already reaches the top bracket -- below that the true count could sit in a
  higher band, so the row goes to review instead.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PersonnelBasis = Literal["PAYROLL", "DEGREE", "RESEARCH_GRADE", "CREDENTIAL"]
DegreeLevel = Literal["NONE", "BACHELOR", "MASTER", "DOCTORATE"]
ResearchGrade = Literal["RESEARCH_ASSISTANT", "RESEARCHER", "LEAD_RESEARCHER"]

PERSONNEL_METRIC_KEY = "company.personnel.count"
# Raw operator input, not a scoreable fact.
PERSONNEL_ROSTER_FACT_KEY = "company.personnel.roster"

_DEGREE_RANK: dict[str, int] = {
    "NONE": 0,
    "BACHELOR": 1,
    "MASTER": 2,
    "DOCTORATE": 3,
}
_GRADE_RANK: dict[str, int] = {
    "RESEARCH_ASSISTANT": 0,
    "RESEARCHER": 1,
    "LEAD_RESEARCHER": 2,
}


class PersonnelQuantModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PersonnelRecognitionScope(PersonnelQuantModel):
    """The exact population a criterion counts."""

    metric_key: Literal["company.personnel.count"]
    basis: PersonnelBasis
    degree_floor: DegreeLevel | None = None
    research_grade_floor: ResearchGrade | None = None
    credential_keywords: tuple[str, ...] = Field(default=(), max_length=8)
    major_keywords: tuple[str, ...] = Field(default=(), max_length=8)
    minimum_tenure_months: int = Field(default=0, ge=0, le=600)
    source_literal: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_basis(self) -> "PersonnelRecognitionScope":
        if self.basis == "DEGREE" and self.degree_floor in (None, "NONE"):
            raise ValueError("degree basis requires a degree floor")
        if self.basis == "RESEARCH_GRADE" and self.research_grade_floor is None:
            raise ValueError("research-grade basis requires a grade floor")
        if self.basis == "CREDENTIAL" and not self.credential_keywords:
            raise ValueError("credential basis requires a named credential")
        if self.basis != "CREDENTIAL" and self.credential_keywords:
            raise ValueError("credential keywords belong to the credential basis")
        return self


class CompanyPersonnelMember(PersonnelQuantModel):
    """One roster row, already stripped of identifying fields."""

    degree_level: DegreeLevel
    majors: tuple[str, ...] = Field(default=(), max_length=6)
    credentials: tuple[str, ...] = Field(default=(), max_length=24)
    credentials_recorded: bool = True
    research_grade: ResearchGrade | None = None
    tenure_months: int = Field(default=0, ge=0, le=1_200)


class DerivedPersonnelValue(PersonnelQuantModel):
    status: Literal["ESTIMATED", "REVIEW"]
    value: float | None = None
    rationale: str = Field(min_length=1, max_length=1_000)
    evidence_reference: str | None = Field(default=None, max_length=300)


# The team assigned to this one contract: a bid decision, never a company fact.
_ASSIGNMENT_RE = re.compile(
    r"참여\s*인력|참여\s*인원|참여\s*현황|사업\s*참여|참여\s*연구원|"
    r"투입|전담\s*인력|배치\s*인력|"
    r"본\s*과업에\s*직접|수행\s*인력|용역\s*수행\s*참여"
)
# Payroll evidence: the source is asking who is on the books.
_PAYROLL_EVIDENCE_RE = re.compile(
    r"4대\s*보험|사대\s*보험|건강\s*보험|고용\s*보험|국민\s*연금|고용\s*사실|"
    r"재직\s*증명|근로\s*계약|급여\s*대장|상시\s*근로|정규\s*직원|상근"
)
# Ascending, and the first match wins: a row that accepts ``박사 또는 석사``
# has a master's floor, so testing the doctorate first would undercount it.
_DEGREE_FLOOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("BACHELOR", re.compile(r"학사\s*(?:학위)?\s*이상|학사\s*학위\s*(?:소지|소유|보유)")),
    ("MASTER", re.compile(r"석사\s*이상|석사\s*(?:학위)?\s*(?:소지|소유|보유)\s*자")),
    ("DOCTORATE", re.compile(r"박사")),
)
_GRADE_FLOOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("LEAD_RESEARCHER", re.compile(r"책임\s*연구원")),
    ("RESEARCHER", re.compile(r"연구원\s*이상")),
)
_CREDENTIAL_RE = re.compile(
    r"[가-힣A-Za-z]{2,12}(?:지도사|상담사|분석사|기획사|기술사|산업기사|기능사|기사|"
    r"안내사|인솔자|교육사|복지사|정교사|관리사|평가사|노무사|회계사)"
)
_TENURE_RE = re.compile(r"(\d+)\s*(년|개월|월)\s*이상\s*(?:근무|재직|근속)")
_MAJOR_RE = re.compile(r"([가-힣]{2,10})\s*(?:및\s*관련\s*학과|관련\s*학과|관련\s*전공|전공자)")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _tenure_months(text: str) -> int:
    match = _TENURE_RE.search(text)
    if not match:
        return 0
    amount = int(match.group(1))
    return min(amount * 12 if match.group(2) == "년" else amount, 600)


def parse_personnel_recognition_scope(
    literal: str,
    *,
    metric_key: str,
    recognition_literal: str = "",
) -> PersonnelRecognitionScope | None:
    """Parse the population a row counts, or refuse when it is a bid decision.

    ``recognition_literal`` carries the row's 인정조건. It is what separates
    ``4대보험 가입자 명부`` (the payroll) from ``투입계획`` (the assigned team),
    and an unconditional ``보유 인력`` row with neither stays manual: without a
    stated recognition rule there is nothing binding the count to the payroll.
    """

    if metric_key != PERSONNEL_METRIC_KEY:
        return None
    head = " ".join(literal.split())
    conditions = " ".join(recognition_literal.split())
    if not head:
        return None
    if _ASSIGNMENT_RE.search(head) or _ASSIGNMENT_RE.search(conditions):
        return None

    combined = head + " " + conditions
    tenure = _tenure_months(combined)
    majors = tuple(dict.fromkeys(_MAJOR_RE.findall(head)))[:8]

    for grade, pattern in _GRADE_FLOOR_PATTERNS:
        if pattern.search(head):
            return PersonnelRecognitionScope(
                metric_key=PERSONNEL_METRIC_KEY,
                basis="RESEARCH_GRADE",
                research_grade_floor=grade,
                major_keywords=majors,
                minimum_tenure_months=tenure,
                source_literal=head[:2_000],
            )

    credentials = tuple(dict.fromkeys(_CREDENTIAL_RE.findall(combined)))[:8]
    degree_floor = next(
        (level for level, pattern in _DEGREE_FLOOR_PATTERNS if pattern.search(head)),
        None,
    )
    if credentials and degree_floor:
        # ``석사 이상 또는 평생교육사`` may mean either population, or their
        # union. Nothing in the row settles it, so it stays manual.
        return None

    if credentials:
        return PersonnelRecognitionScope(
            metric_key=PERSONNEL_METRIC_KEY,
            basis="CREDENTIAL",
            credential_keywords=credentials,
            major_keywords=majors,
            minimum_tenure_months=tenure,
            source_literal=head[:2_000],
        )

    if degree_floor:
        return PersonnelRecognitionScope(
            metric_key=PERSONNEL_METRIC_KEY,
            basis="DEGREE",
            degree_floor=degree_floor,
            major_keywords=majors,
            minimum_tenure_months=tenure,
            source_literal=head[:2_000],
        )

    if _PAYROLL_EVIDENCE_RE.search(conditions):
        return PersonnelRecognitionScope(
            metric_key=PERSONNEL_METRIC_KEY,
            basis="PAYROLL",
            major_keywords=majors,
            minimum_tenure_months=tenure,
            source_literal=head[:2_000],
        )
    return None


def _matches(member: CompanyPersonnelMember, scope: PersonnelRecognitionScope) -> bool:
    if member.tenure_months < scope.minimum_tenure_months:
        return False
    if scope.major_keywords and not any(
        _normalise(keyword) in _normalise(major)
        for keyword in scope.major_keywords
        for major in member.majors
    ):
        return False
    if scope.basis == "DEGREE":
        floor = scope.degree_floor or "NONE"
        return _DEGREE_RANK[member.degree_level] >= _DEGREE_RANK[floor]
    if scope.basis == "RESEARCH_GRADE":
        if member.research_grade is None:
            return False
        floor = scope.research_grade_floor or "RESEARCH_ASSISTANT"
        return _GRADE_RANK[member.research_grade] >= _GRADE_RANK[floor]
    if scope.basis == "CREDENTIAL":
        held = {_normalise(item) for item in member.credentials}
        return any(
            any(_normalise(keyword) in item for item in held)
            for keyword in scope.credential_keywords
        )
    return True


_BASIS_LABEL: dict[str, str] = {
    "PAYROLL": "재직 인력",
    "DEGREE": "학위 보유 인력",
    "RESEARCH_GRADE": "학술연구용역 등급 인력",
    "CREDENTIAL": "자격증 보유 인력",
}


def derive_personnel_value(
    scope: PersonnelRecognitionScope,
    roster: list[CompanyPersonnelMember],
    *,
    as_of: datetime,
    sufficiency_value: float | None = None,
) -> DerivedPersonnelValue:
    """Count the population the row asks for, or refuse to guess.

    ``sufficiency_value`` is the threshold above which a larger count cannot
    change the score -- the criterion's top bracket. It only matters when the
    count is a lower bound: reaching the top bracket makes the missing records
    irrelevant, and falling short of it makes them decisive.
    """

    if not roster:
        return DerivedPersonnelValue(
            status="REVIEW",
            rationale="회사 인력 명부가 없어 자동 계산을 중지했습니다.",
        )

    count = sum(1 for member in roster if _matches(member, scope))
    label = _BASIS_LABEL[scope.basis]
    qualifiers: list[str] = []
    if scope.major_keywords:
        qualifiers.append("전공 " + "·".join(scope.major_keywords))
    if scope.minimum_tenure_months:
        qualifiers.append("재직 %d개월 이상" % scope.minimum_tenure_months)
    if scope.basis == "CREDENTIAL":
        qualifiers.append("·".join(scope.credential_keywords))
    suffix = " (" + ", ".join(qualifiers) + ")" if qualifiers else ""

    if scope.basis == "CREDENTIAL":
        unrecorded = sum(1 for member in roster if not member.credentials_recorded)
        if unrecorded and (sufficiency_value is None or count < sufficiency_value):
            return DerivedPersonnelValue(
                status="REVIEW",
                rationale=(
                    "자격증 미기재 인원 %d명이 있어 %s%s %d명은 최소값입니다. "
                    "최상단 구간에 못 미쳐 실제 인원 확인이 필요합니다."
                    % (unrecorded, label, suffix, count)
                ),
                evidence_reference=("인력 명부 " + label + suffix)[:300],
            )

    return DerivedPersonnelValue(
        status="ESTIMATED",
        value=float(count),
        rationale=(
            "%d년 기준 인력 명부에서 %s%s %d명을 집계했습니다."
            % (as_of.year, label, suffix, count)
        ),
        evidence_reference=("인력 명부 " + label + suffix)[:300],
    )


def load_personnel_roster(facts: object) -> list[CompanyPersonnelMember]:
    """Read the operator roster from the ``company.personnel.roster`` fact.

    The roster is one JSON company fact rather than its own table, and it is
    raw input to derivation, never a scoreable fact, so it needs no criterion
    binding. One malformed member invalidates the whole roster: a count taken
    over a silently shortened list would be wrong in the direction that loses
    points.
    """

    members: list[CompanyPersonnelMember] = []
    for fact in facts or ():  # type: ignore[union-attr]
        if getattr(fact, "fact_key", None) != PERSONNEL_ROSTER_FACT_KEY:
            continue
        raw = getattr(fact, "value", None)
        rows = raw.get("members") if isinstance(raw, dict) else None
        for row in rows or ():
            if not isinstance(row, dict):
                return []
            try:
                members.append(
                    CompanyPersonnelMember(
                        degree_level=row["degree_level"],
                        majors=tuple(row.get("majors") or ()),
                        credentials=tuple(row.get("credentials") or ()),
                        credentials_recorded=bool(row.get("credentials_recorded", True)),
                        research_grade=row.get("research_grade"),
                        tenure_months=int(row.get("tenure_months", 0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return []
    return members


__all__ = [
    "PERSONNEL_METRIC_KEY",
    "load_personnel_roster",
    "PERSONNEL_ROSTER_FACT_KEY",
    "CompanyPersonnelMember",
    "DerivedPersonnelValue",
    "PersonnelBasis",
    "PersonnelRecognitionScope",
    "derive_personnel_value",
    "parse_personnel_recognition_scope",
]
