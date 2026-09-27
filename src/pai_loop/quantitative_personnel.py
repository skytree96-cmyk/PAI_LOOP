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
* A blank credential column is unknown, not an absence of credentials. A count is
  therefore a lower bound, and a lower bound may only be scored when it
  already reaches the top bracket -- below that the true count could sit in a
  higher band, so the row goes to review instead.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Literal, Sequence

from pydantic import (
    BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator,
)

PersonnelBasis = Literal["PAYROLL", "DEGREE", "RESEARCH_GRADE", "CREDENTIAL"]
DegreeLevel = Literal["NONE", "BACHELOR", "MASTER", "DOCTORATE"]
ResearchGrade = Literal["RESEARCH_ASSISTANT", "RESEARCHER", "LEAD_RESEARCHER"]

PERSONNEL_METRIC_KEY = "company.personnel.count"
# Raw operator input, not a scoreable fact.
PERSONNEL_ROSTER_FACT_KEY = "company.personnel.roster"
PERSONNEL_ROSTER_SCHEMA_VERSION = "pai-loop-personnel-roster-1.0"
_KST = timezone(timedelta(hours=9))

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

    @field_validator(
        "joined_on", "snapshot_date", "verified_through", "projection_through",
        "reference_date", mode="before", check_fields=False,
    )
    @classmethod
    def require_calendar_date(cls, value: object) -> object:
        if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
            return value
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        raise ValueError("calendar dates must be ISO dates without a time")


class PersonnelRecognitionScope(PersonnelQuantModel):
    """The exact population a criterion counts."""

    metric_key: Literal["company.personnel.count"]
    basis: PersonnelBasis
    degree_floor: DegreeLevel | None = None
    research_grade_floor: ResearchGrade | None = None
    credential_keywords: tuple[str, ...] = Field(default=(), max_length=8)
    major_keywords: tuple[str, ...] = Field(default=(), max_length=8)
    minimum_tenure_months: int = Field(default=0, ge=0, le=600)
    reference_basis: Literal["DEADLINE", "PUBLICATION", "FIXED_DATE"] = "DEADLINE"
    reference_date: date | None = None
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
        if (self.reference_basis == "FIXED_DATE") != (self.reference_date is not None):
            raise ValueError("only a fixed-date basis requires a reference date")
        return self


class PersonnelDegree(PersonnelQuantModel):
    """A completed degree and its own major; null means an unknown major."""

    level: Literal["BACHELOR", "MASTER", "DOCTORATE"]
    major: str | None = Field(max_length=200)

    @field_validator("major")
    @classmethod
    def validate_major(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("major must be nonblank or explicitly unknown")
        return value


class CompanyPersonnelMember(PersonnelQuantModel):
    """One roster row, already stripped of identifying fields."""

    member_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    joined_on: date
    degrees: tuple[PersonnelDegree, ...] = Field(max_length=12)
    credentials: tuple[str, ...] = Field(max_length=24)
    credentials_recorded: StrictBool
    research_grade: ResearchGrade | None = None

    @field_validator("credentials")
    @classmethod
    def validate_credentials(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        stripped = tuple(value.strip() for value in values)
        if any(not value or len(value) > 200 for value in stripped):
            raise ValueError("credentials must contain nonblank bounded strings")
        if len(set(stripped)) != len(stripped):
            raise ValueError("duplicate credential")
        return stripped

    @model_validator(mode="after")
    def validate_degrees(self) -> "CompanyPersonnelMember":
        pairs = [(degree.level, degree.major) for degree in self.degrees]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate degree and major")
        return self


class PersonnelRosterEnvelope(PersonnelQuantModel):
    """A user-attested snapshot, never independently verified score evidence."""

    schema_version: Literal["pai-loop-personnel-roster-1.0"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_date: date
    verified_through: date
    projection_through: date | None = None
    projection_assumption: Literal["CURRENT_ROSTER_UNCHANGED"] | None = None
    verification_attestation: Literal["HUMAN_REVIEWED_ROSTER_SNAPSHOT"]
    research_grade_basis: Literal["CORRECTED_FINAL"]
    members: tuple[CompanyPersonnelMember, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PersonnelRosterEnvelope":
        if self.snapshot_date > self.verified_through:
            raise ValueError("snapshot must precede the attestation horizon")
        if (self.projection_through is None) != (self.projection_assumption is None):
            raise ValueError("projection needs both a bounded horizon and an explicit assumption")
        if self.projection_through is not None and self.projection_through <= self.verified_through:
            raise ValueError("projection must extend beyond, not replace, the attestation horizon")
        keys = [member.member_key for member in self.members]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate member key")
        if any(member.joined_on > self.snapshot_date for member in self.members):
            raise ValueError("a snapshot cannot include a future joiner")
        return self


class PersonnelRosterLoadResult(PersonnelQuantModel):
    roster: PersonnelRosterEnvelope | None = None
    projected: bool = False
    reason: str = Field(min_length=1, max_length=1_000)


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


_FIXED_REFERENCE_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:[./-]|년)\s*(?P<month>\d{1,2})\s*"
    r"(?:[./-]|월)\s*(?P<day>\d{1,2})\s*(?:[.]|일)?\s*기준"
)
_PUBLICATION_REFERENCE_RE = re.compile(r"(?:입찰\s*)?공고일\s*(?:기준|현재)")
_DEADLINE_REFERENCE_RE = re.compile(r"(?:입찰\s*|제안서\s*제출\s*)?마감일\s*(?:기준|현재)")
_UNSUPPORTED_REFERENCE_RE = re.compile(
    r"(?:평가일|심사일|제출일|접수일|작성일|신청일|등록일|발급일)\s*기준|"
    r"(?:공고일|마감일)\s*(?:전일|전날|이전|다음|이후)"
)


def _reference_fields(text: str) -> dict[str, object] | None:
    if _UNSUPPORTED_REFERENCE_RE.search(text):
        return None
    fixed = list(_FIXED_REFERENCE_RE.finditer(text))
    publication = bool(_PUBLICATION_REFERENCE_RE.search(text))
    deadline = bool(_DEADLINE_REFERENCE_RE.search(text))
    if ("공고일" in text and not publication) or ("마감일" in text and not deadline):
        return None
    if "현재" in text and not (publication or deadline):
        return None
    if sum((bool(fixed), publication, deadline)) > 1:
        return None
    if fixed:
        try:
            dates = {
                date(int(match["year"]), int(match["month"]), int(match["day"]))
                for match in fixed
            }
        except ValueError:
            return None
        if len(dates) != 1:
            return None
        return {"reference_basis": "FIXED_DATE", "reference_date": dates.pop()}
    # An incomplete calendar reference cannot silently fall back to the deadline.
    if re.search(r"20\d{2}[^\n]{0,20}기준", text):
        return None
    return {"reference_basis": "PUBLICATION" if publication else "DEADLINE"}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def personnel_reference_time(
    scope: PersonnelRecognitionScope,
    *,
    as_of: datetime,
    bid_notice_at: datetime | None = None,
) -> datetime | None:
    if scope.reference_basis == "PUBLICATION":
        return _aware(bid_notice_at) if bid_notice_at is not None else None
    if scope.reference_basis == "FIXED_DATE":
        assert scope.reference_date is not None
        return datetime.combine(scope.reference_date, time.max, tzinfo=_KST)
    return _aware(as_of)


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
    # A general roster cannot prove domain experience or specialist employment.
    if re.search(r"경력\s*자|업무\s*경력|유사\s*경력|관련\s*경력", combined):
        return None
    reference = _reference_fields(combined)
    if reference is None:
        return None
    tenure = _tenure_months(combined)
    head_majors = tuple(dict.fromkeys(_MAJOR_RE.findall(head)))
    condition_majors = tuple(dict.fromkeys(_MAJOR_RE.findall(conditions)))
    if head_majors and condition_majors and set(head_majors) != set(condition_majors):
        return None
    majors = head_majors or condition_majors
    if len(majors) > 8:
        return None
    credentials = tuple(dict.fromkeys(_CREDENTIAL_RE.findall(combined)))
    if len(credentials) > 8:
        return None
    floors = [
        next((level for level, pattern in _DEGREE_FLOOR_PATTERNS if pattern.search(text)), None)
        for text in (head, conditions)
    ]
    grades = [
        next((grade for grade, pattern in _GRADE_FLOOR_PATTERNS if pattern.search(text)), None)
        for text in (head, conditions)
    ]
    if len(set(floors) - {None}) > 1 or len(set(grades) - {None}) > 1:
        return None
    degree_floor = floors[0] or floors[1]
    grade_floor = grades[0] or grades[1]
    if sum((bool(credentials), bool(degree_floor), bool(grade_floor))) > 1:
        # Mixed populations need an explicit rule, not an inferred AND or OR.
        return None

    if grade_floor:
        return PersonnelRecognitionScope(
            metric_key=PERSONNEL_METRIC_KEY,
            basis="RESEARCH_GRADE",
            research_grade_floor=grade_floor,
            major_keywords=majors,
            minimum_tenure_months=tenure,
            source_literal=head[:2_000],
            **reference,
        )

    if credentials:
        return PersonnelRecognitionScope(
            metric_key=PERSONNEL_METRIC_KEY,
            basis="CREDENTIAL",
            credential_keywords=credentials,
            major_keywords=majors,
            minimum_tenure_months=tenure,
            source_literal=head[:2_000],
            **reference,
        )

    if degree_floor:
        return PersonnelRecognitionScope(
            metric_key=PERSONNEL_METRIC_KEY,
            basis="DEGREE",
            degree_floor=degree_floor,
            major_keywords=majors,
            minimum_tenure_months=tenure,
            source_literal=head[:2_000],
            **reference,
        )

    if _PAYROLL_EVIDENCE_RE.search(conditions):
        if re.search(r"전문\s*인력|전문\s*직원", combined):
            return None
        return PersonnelRecognitionScope(
            metric_key=PERSONNEL_METRIC_KEY,
            basis="PAYROLL",
            major_keywords=majors,
            minimum_tenure_months=tenure,
            source_literal=head[:2_000],
            **reference,
        )
    return None


def _major_match(
    degrees: Sequence[PersonnelDegree], keywords: tuple[str, ...]
) -> bool | None:
    if not keywords:
        return True
    if any(
        _normalise(keyword) in _normalise(degree.major)
        for degree in degrees
        if degree.major is not None
        for keyword in keywords
    ):
        return True
    return None if any(degree.major is None for degree in degrees) else False


def _matches(
    member: CompanyPersonnelMember, scope: PersonnelRecognitionScope, cutoff: date
) -> bool | None:
    months = (
        (cutoff.year - member.joined_on.year) * 12
        + cutoff.month - member.joined_on.month
    )
    months -= cutoff.day < member.joined_on.day
    if months < scope.minimum_tenure_months:
        return False
    if scope.basis == "DEGREE":
        floor = scope.degree_floor or "NONE"
        qualifying = tuple(
            degree for degree in member.degrees
            if _DEGREE_RANK[degree.level] >= _DEGREE_RANK[floor]
        )
        return _major_match(qualifying, scope.major_keywords) if qualifying else False
    major_match = _major_match(member.degrees, scope.major_keywords)
    basis_match: bool | None = True
    if scope.basis == "RESEARCH_GRADE":
        floor = scope.research_grade_floor or "RESEARCH_ASSISTANT"
        basis_match = (
            _GRADE_RANK[member.research_grade] >= _GRADE_RANK[floor]
            if member.research_grade is not None else None
        )
    elif scope.basis == "CREDENTIAL":
        held = {_normalise(item) for item in member.credentials}
        basis_match = any(
            any(_normalise(keyword) in item for item in held)
            for keyword in scope.credential_keywords
        )
        if not basis_match and not member.credentials_recorded:
            basis_match = None
    if major_match is False or basis_match is False:
        return False
    return None if major_match is None or basis_match is None else True


_BASIS_LABEL: dict[str, str] = {
    "PAYROLL": "재직 인력",
    "DEGREE": "학위 보유 인력",
    "RESEARCH_GRADE": "학술연구용역 등급 인력",
    "CREDENTIAL": "자격증 보유 인력",
}


def derive_personnel_value(
    scope: PersonnelRecognitionScope,
    roster: Sequence[CompanyPersonnelMember],
    *,
    as_of: datetime,
    bid_notice_at: datetime | None = None,
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

    reference = personnel_reference_time(scope, as_of=as_of, bid_notice_at=bid_notice_at)
    if reference is None:
        return DerivedPersonnelValue(
            status="REVIEW", rationale="인력 산정 기준 공고일이 없어 자동 계산을 중지했습니다.",
        )
    cutoff = reference.astimezone(_KST).date()
    if len({member.member_key for member in roster}) != len(roster):
        return DerivedPersonnelValue(
            status="REVIEW", rationale="중복 인력 식별자가 있어 명부 확인이 필요합니다.",
        )
    if any(member.joined_on > cutoff for member in roster):
        return DerivedPersonnelValue(
            status="REVIEW", rationale="산정 기준일 이후 입사자가 포함되어 명부 확인이 필요합니다.",
        )
    matches = [_matches(member, scope, cutoff) for member in roster]
    count = sum(match is True for match in matches)
    label = _BASIS_LABEL[scope.basis]
    qualifiers: list[str] = []
    if scope.major_keywords:
        qualifiers.append("전공 " + "·".join(scope.major_keywords))
    if scope.minimum_tenure_months:
        qualifiers.append("재직 %d개월 이상" % scope.minimum_tenure_months)
    if scope.basis == "CREDENTIAL":
        qualifiers.append("·".join(scope.credential_keywords))
    suffix = " (" + ", ".join(qualifiers) + ")" if qualifiers else ""

    unrecorded = sum(match is None for match in matches)
    can_use_lower_bound = scope.basis == "CREDENTIAL" and not scope.major_keywords
    if unrecorded and (
        not can_use_lower_bound or sufficiency_value is None or count < sufficiency_value
    ):
        return DerivedPersonnelValue(
            status="REVIEW",
            rationale=(
                "필요 정보 미기재 인원 %d명이 있어 %s%s %d명은 최소값입니다. "
                "현재 정보만으로 점수 구간을 확정할 수 없어 확인이 필요합니다."
                % (unrecorded, label, suffix, count)
            ),
            evidence_reference=("인력 명부 " + label + suffix)[:300],
        )

    return DerivedPersonnelValue(
        status="ESTIMATED",
        value=float(count),
        rationale=(
            "%s 기준 사용자 확인 명부에서 %s%s %d명을 집계했습니다. 증빙 확정 점수가 아닙니다.%s"
            % (
                cutoff.isoformat(), label, suffix, count,
                " 미기재 정보가 있어 인원은 최소값입니다." if unrecorded else "",
            )
        ),
        evidence_reference=("인력 명부 " + label + suffix)[:300],
    )


def load_personnel_roster(
    facts: Iterable[object], *, as_of: datetime
) -> PersonnelRosterLoadResult:
    """Select exactly one current, user-attested snapshot; never merge versions."""

    reference = _aware(as_of)
    candidates: list[object] = []
    for fact in facts:
        if getattr(fact, "fact_key", None) != PERSONNEL_ROSTER_FACT_KEY:
            continue
        start = getattr(fact, "effective_from", None)
        end = getattr(fact, "effective_to", None)
        if (
            (start is not None and not isinstance(start, datetime))
            or (end is not None and not isinstance(end, datetime))
        ):
            return PersonnelRosterLoadResult(reason="명부 적용 기간 형식이 잘못되어 확인이 필요합니다.")
        if start is not None and end is not None and _aware(start) > _aware(end):
            return PersonnelRosterLoadResult(reason="명부 적용 기간이 역전되어 확인이 필요합니다.")
        if (
            (start is not None and _aware(start) > reference)
            or (end is not None and _aware(end) < reference)
        ):
            continue
        candidates.append(fact)
    if not candidates:
        return PersonnelRosterLoadResult(reason="산정 기준일에 적용되는 인력 명부가 없습니다.")
    if len(candidates) != 1:
        return PersonnelRosterLoadResult(reason="산정 기준일에 적용되는 인력 명부 버전이 중복되어 확인이 필요합니다.")
    fact = candidates[0]
    if getattr(fact, "verified", None) is not True:
        return PersonnelRosterLoadResult(reason="사용자 확인이 완료되지 않은 인력 명부입니다.")
    if getattr(fact, "effective_from", None) is None:
        return PersonnelRosterLoadResult(reason="명부 적용 시작일이 없어 확인이 필요합니다.")
    try:
        roster = PersonnelRosterEnvelope.model_validate(getattr(fact, "value", None))
    except (TypeError, ValueError):
        return PersonnelRosterLoadResult(reason="날짜와 학위별 전공을 보존한 명부 형식이 필요합니다. 기존 또는 잘못된 명부는 사용할 수 없습니다.")
    cutoff = reference.astimezone(_KST).date()
    horizon = roster.projection_through or roster.verified_through
    if not roster.snapshot_date <= cutoff <= horizon:
        return PersonnelRosterLoadResult(reason="산정 기준일이 사용자 확인 명부의 유효 기간 밖입니다.")
    if cutoff > roster.verified_through:
        return PersonnelRosterLoadResult(
            roster=roster,
            projected=True,
            reason=(
                "%s까지 확인한 현재 명부가 %s까지 변하지 않는다는 가정의 추정입니다. "
                "미래 재직을 확인한 것이 아니므로 마감일에 다시 확인해야 합니다."
                % (roster.verified_through.isoformat(), roster.projection_through.isoformat())
            ),
        )
    return PersonnelRosterLoadResult(roster=roster, reason="산정 기준일에 유효한 사용자 확인 명부입니다. 추정 계산에만 사용합니다.")


__all__ = [
    "PERSONNEL_METRIC_KEY",
    "load_personnel_roster",
    "PERSONNEL_ROSTER_FACT_KEY",
    "PERSONNEL_ROSTER_SCHEMA_VERSION",
    "CompanyPersonnelMember",
    "PersonnelDegree",
    "PersonnelRosterEnvelope",
    "PersonnelRosterLoadResult",
    "DerivedPersonnelValue",
    "PersonnelBasis",
    "PersonnelRecognitionScope",
    "derive_personnel_value",
    "parse_personnel_recognition_scope",
    "personnel_reference_time",
]
