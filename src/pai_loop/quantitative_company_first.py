"""Company-first beta scoring: apply the company's own facts to printed rows.

Prototype mode (default on, ``PAI_QUANT_COMPANY_FIRST_BETA=false`` disables it)
for rows the strict source-validation path leaves unscored. Instead of proving
every condition of a row, it reads only what a score needs and applies the
company's stored facts to the printed rows:

* credit: the row whose grade set contains the registered grade;
* sanctions: the printed "해당 없음" row (the company has never been sanctioned);
* performance: validated, completed register contracts inside the printed
  period and above the printed single-contract amount whose name/overview
  names the row's field (or the notice title's field when the row only says 유사);
* personnel: roster members meeting every roster-checkable condition (degree,
  major, research grade, credential, tenure, regular employment, reference
  date); a printed career of N years is read as N years of company tenure and
  residence/assignment as every approved member being available; conditions
  the roster cannot show at all (age, demographic status) are treated as unmet.

When the company value reaches no printed row, the table's lowest printed award
is used so a subtotal exists. This is an explicit user exception to the "no
zero inference" rule, limited to this beta flag. Every result is ESTIMATED and
labeled as a beta lower bound; it never becomes a confirmed score.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

COMPANY_FIRST_REASON = "COMPANY_FIRST_BETA"
COMPANY_FIRST_LABEL = "베타·회사 기준 역산·하한 추정"
COMPANY_FIRST_FLOOR_NOTE = "미충족 → 최하점 적용(하한)"
COMPANY_FIRST_STATEMENT = (
    f"{COMPANY_FIRST_LABEL}: 일부 항목은 회사 보유 사실을 원문 배점 행에 대입했고, "
    f"확인되지 않은 조건은 미충족으로 보아 {COMPANY_FIRST_FLOOR_NOTE}했습니다. "
    "베타 하한 합계이며 공고 총점이 아닙니다."
)
_KST = timezone(timedelta(hours=9))


def company_first_enabled() -> bool:
    return os.getenv("PAI_QUANT_COMPANY_FIRST_BETA", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _norm(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _compact(text: str | None) -> str:
    return re.sub(r"\s+", "", _norm(text))


def _row_text(raw: Any) -> str:
    return _norm(" ".join(value for value in (
        raw.label, raw.criterion_literal, getattr(raw, "formula_literal", None) or "",
        *(item.literal for item in raw.recognition_conditions),
    ) if value))


@dataclass(frozen=True)
class PrintedRow:
    literal: str
    points: float
    operator: str
    value: float | None = None
    upper: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    min_inclusive: bool = True
    max_inclusive: bool = False
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class BetaScore:
    points: float
    row_literal: str
    basis: str
    floor_applied: bool


def printed_rows(raw: Any) -> list[PrintedRow]:
    """Every printed row of one criterion, with its award in points."""

    rows: list[PrintedRow] = []
    for case in sorted(raw.cases, key=lambda item: item.row_order):
        if case.operator == "NOT_SUBMITTED":
            continue
        points = case.award_value if case.award_kind == "POINTS" else raw.max_points * case.award_value / 100
        rows.append(PrintedRow(
            literal=case.literal, points=float(points), operator=case.operator,
            value=case.comparison_value, upper=case.comparison_upper_value,
            categories=tuple(case.category_values),
        ))
    for bracket in raw.brackets:
        rows.append(PrintedRow(
            literal=bracket.literal, points=float(bracket.points), operator="BRACKET",
            min_value=bracket.min_value, max_value=bracket.max_value,
            min_inclusive=bracket.min_inclusive, max_inclusive=bracket.max_inclusive,
        ))
    threshold = raw.threshold
    if threshold is not None:
        rows.append(PrintedRow(
            literal=threshold.literal, points=float(threshold.points_if_met),
            operator=threshold.operator, value=threshold.threshold_value,
        ))
        if threshold.points_if_not_met is not None:
            rows.append(PrintedRow(
                literal=f"{threshold.literal} (미충족)", points=float(threshold.points_if_not_met),
                operator="ELSE",
            ))
    # A row printed above the item maximum is read as the maximum.
    return [
        row if row.points <= raw.max_points else PrintedRow(**{**row.__dict__, "points": float(raw.max_points)})
        for row in rows if row.points >= 0
    ]


_BONUS = re.compile(r"가\s*점")


def lowest_row(rows: Sequence[PrintedRow], basis: str, *, bonus: bool = False) -> BetaScore:
    """The table's lowest printed award, or 0 when nothing lower is printed.

    A bonus (가점) table prints only the qualifying rows, so not qualifying
    earns nothing rather than its smallest bonus.
    """

    if not rows or bonus:
        return BetaScore(0.0, "(해당 행 없음 → 0점)", basis, True)
    lowest = min(rows, key=lambda row: row.points)
    return BetaScore(lowest.points, lowest.literal, basis, True)


def _numeric_match(row: PrintedRow, value: float) -> bool:
    if row.operator == "BRACKET":
        if row.min_value is None and row.max_value is None:
            return False
        if row.min_value is not None and (
            value < row.min_value or (value == row.min_value and not row.min_inclusive)
        ):
            return False
        return row.max_value is None or value < row.max_value or (
            value == row.max_value and row.max_inclusive
        )
    if row.value is None:
        return False
    return {
        "GTE": value >= row.value, "GT": value > row.value,
        "LTE": value <= row.value, "LT": value < row.value, "EQ": value == row.value,
        "BETWEEN": row.upper is not None and row.value <= value <= row.upper,
    }.get(row.operator, False)


def _row_bound(row: PrintedRow) -> float | None:
    values = [value for value in (row.value, row.upper, row.min_value, row.max_value) if value is not None]
    return max(values) if values else None


def score_numeric(rows: Sequence[PrintedRow], value: float, basis: str, *, bonus: bool = False) -> BetaScore:
    """The highest printed award the value reaches, else the lowest printed award.

    A value above every printed bound of an ascending table (for example
    ``0/1/2/3/4회 → 6/7/8/9/10점`` with 12 contracts) takes the top row.
    """

    numeric = [row for row in rows if not row.categories]
    hits = [row for row in numeric if _numeric_match(row, value)]
    if hits:
        best = max(hits, key=lambda row: row.points)
        return BetaScore(best.points, best.literal, basis, False)
    bounded = [(bound, row) for row in numeric if (bound := _row_bound(row)) is not None]
    if bounded and value > max(bound for bound, _row in bounded):
        ordered = sorted(bounded, key=lambda item: item[0])
        if all(left[1].points <= right[1].points for left, right in zip(ordered, ordered[1:])):
            top = ordered[-1][1]
            return BetaScore(top.points, top.literal, basis, False)
    return lowest_row(numeric or rows, basis, bonus=bonus)


_GRADE_TOKEN = re.compile(r"(?<![A-Za-z])(AAA|AA|A|BBB|BB|B|CCC|CC|C|D)(\s*[+\-0]|(?=\s*[~∼～]))?(?![A-Za-z0-9])")



def _grade_index(token: str, sign: str | None) -> int | None:
    from .quantitative_formula import CREDIT_RATING_ORDER

    sign = (sign or "").strip()
    if token in {"CC", "C", "D"}:
        return CREDIT_RATING_ORDER.index(token)
    for candidate in (token + sign, token + "0", token):
        if candidate in CREDIT_RATING_ORDER:
            return CREDIT_RATING_ORDER.index(candidate)
    return None


def _tolerant_grades(text: str) -> set[int] | None:
    """Grade indexes one printed credit row covers, read loosely; None if unreadable."""

    from .quantitative_formula import CREDIT_RATING_ORDER

    text = _norm(text).replace("–", "~").replace("∼", "~").replace("～", "~")
    # Commercial-paper grades (A1~A3) sit beside the enterprise grade in many
    # tables; drop them and read only the long-term grades of the row.
    text = re.sub(r"(?<![A-Za-z])A\s*[123]\s*[+\-0]?(?:\s*(?:이상|이하|미만|초과))?", " ", text)
    text = re.sub(r"기업\s*어음[^/\n]*", " ", text)
    tokens = [(match.group(1), match.group(2), match.start(), match.end()) for match in _GRADE_TOKEN.finditer(text)]
    indexes = [(_grade_index(token, sign), start, end) for token, sign, start, end in tokens]
    indexes = [item for item in indexes if item[0] is not None]
    if not indexes:
        return None
    last = len(CREDIT_RATING_ORDER) - 1
    if len(indexes) == 2 and re.search(
        r"~|\s-\s|(?<=[0+\-])-(?=[A-Z])|(?<=[A-Z])-(?=[A-Z])", text[indexes[0][1]:indexes[1][1] + 1],
    ):
        low, high = sorted((indexes[0][0], indexes[1][0]))
        return set(range(low, high + 1))
    if len(indexes) == 1:
        index = indexes[0][0]
        tail = text[indexes[0][2]:]
        if re.match(r"\s*(?:등급)?\s*이상", tail):
            return set(range(0, index + 1))
        if re.match(r"\s*(?:등급)?\s*초과", tail):
            return set(range(0, index))
        if re.match(r"\s*(?:등급)?\s*이하", tail):
            return set(range(index, last + 1))
        if re.match(r"\s*(?:등급)?\s*미만", tail):
            return set(range(index + 1, last + 1))
    return {index for index, _start, _end in indexes}


def score_grade(rows: Sequence[PrintedRow], grade: str, basis: str) -> BetaScore | None:
    from .quantitative_formula import CREDIT_RATING_ORDER, compile_credit_rating_values, parse_credit_rating

    canonical = parse_credit_rating(grade)
    if canonical is None:
        return None
    target = CREDIT_RATING_ORDER.index(canonical)
    hits = []
    for row in rows:
        text = "\n".join(row.categories) if row.categories else row.literal
        expanded = compile_credit_rating_values(row.categories, source_literal=text) if row.categories else None
        if expanded is not None:
            if canonical in expanded:
                hits.append(row)
            continue
        covered = _tolerant_grades(text)
        if covered is not None and target in covered:
            hits.append(row)
    if hits:
        best = max(hits, key=lambda row: row.points)
        return BetaScore(best.points, best.literal, basis, False)
    return lowest_row(rows, basis)


_SANCTION = re.compile(r"부정당|제재|입찰\s*참가\s*자격\s*제한|행정\s*처분|영업\s*정지|과징금|계약\s*불이행")
_CLEAR_ROW = re.compile(r"없음|없는|없을|해당\s*없|미해당|무\s*$|^\s*무|0\s*회|0\s*건")


def standard_credit_rows(max_points: float) -> list[PrintedRow]:
    """The standard negotiated-contract credit table: 100/95/90/70% of the item."""

    bands = (("AAA ~ BBB0", 100), ("BBB- ~ BB-", 95), ("B+ ~ B-", 90), ("CCC+ 이하", 70))
    return [PrintedRow(literal=f"[표준표] {band} 배점의 {pct}%", points=float(max_points) * pct / 100,
                       operator="IN", categories=(band,)) for band, pct in bands]


def is_sanction_row(raw: Any) -> bool:
    head = _norm(f"{raw.label} {raw.criterion_literal}")
    if _SANCTION.search(head):
        return True
    rows = " ".join(row.literal for row in printed_rows(raw))
    return bool(re.search(r"신인도|신용\s*도\s*평가", head) and _SANCTION.search(_norm(rows)))


def score_sanction(raw: Any, rows: Sequence[PrintedRow]) -> BetaScore:
    """A company never sanctioned since founding takes the printed clear row.

    When only the sanctioned cases are printed (a deduction-style table), no
    printed row applies and the item keeps its full points.
    """

    basis = "창사 이래 부정당제재·입찰참가자격 제한 이력 없음(회사 확인 사실)"
    clear = [row for row in rows if _CLEAR_ROW.search(_norm(row.literal))]
    numeric_zero = [row for row in rows if not row.categories and _numeric_match(row, 0.0)]
    candidates = clear or numeric_zero
    if candidates:
        best = max(candidates, key=lambda row: row.points)
        return BetaScore(best.points, best.literal, basis, False)
    return BetaScore(float(raw.max_points), "(제재 해당 행 없음 → 만점)", basis, False)


def _year_ratios(item: Any) -> dict[str, float]:
    return {
        "자기자본비율": float(item.equity / item.total_assets * 100),
        "유동비율": float(item.current_assets / item.current_liabilities * 100),
        "부채비율": float((item.total_assets - item.equity) / item.equity * 100),
    }


def financial_ratios(facts: Iterable[Any]) -> tuple[int, dict[str, float], dict[int, dict[str, float]]] | None:
    """Latest registered fiscal year's three ratios in percent, plus every year's."""

    from .quantitative_financial import load_financial_statement

    try:
        years = load_financial_statement(list(facts))
    except (TypeError, ValueError):
        return None
    fiscal = [item.fiscal_year for item in years]
    usable = {item.fiscal_year: item for item in years if fiscal.count(item.fiscal_year) == 1 and item.equity > 0}
    if not usable:
        return None
    latest = max(usable)
    return latest, _year_ratios(usable[latest]), {year: _year_ratios(item) for year, item in usable.items()}


_RATIO_NAMES = {"자기자본비율": r"자기\s*자본\s*비\s*율", "유동비율": r"유동\s*비\s*율", "부채비율": r"부채\s*비\s*율"}


def _ratio_score(name: str, rows: Sequence[PrintedRow], head: str, ratios: dict[str, float], year: str,
                 *, bonus: bool) -> BetaScore:
    value = ratios[name]
    basis = f"{year} {name} {value:.1f}%"
    # "기준비율 50% 이상" prints the threshold itself; "기준비율의 50%" or an
    # industry average asks for a ratio to a benchmark the source must print.
    relative = re.search(r"기준\s*비\s*율\s*의|기준\s*비\s*율\s*대비|업종\s*평균", head + " ".join(row.literal for row in rows))
    if relative:
        benchmark = re.search(r"기준\s*비\s*율\s*[(:：]?\s*(\d+(?:\.\d+)?)\s*%\s*[)]?\s*(?:로|으로|을|를|에)?\s*(?:나눈|대비|대한)", head)
        if benchmark is None:
            return lowest_row(rows, basis + " (업종 기준비율 미기재로 대비율 계산 불가)", bonus=bonus)
        value = value / float(benchmark.group(1)) * 100
        basis += f" ÷ 기준비율 {benchmark.group(1)}% = {value:.1f}%"
    return score_numeric(rows, value, basis, bonus=bonus)


def score_financial(raw: Any, rows: Sequence[PrintedRow], statement: tuple[Any, ...] | None,
                    *, bonus: bool) -> BetaScore:
    """Registered-statement ratios applied to printed rows; one ratio per row group."""

    head = _norm(f"{raw.label} {raw.criterion_literal}")
    if statement is None:
        return lowest_row(rows, "등록된 재무제표 없음", bonus=bonus)
    year, ratios = statement[0], dict(statement[1])
    if re.search(r"평균", head):
        # "최근 3년 평균": only when every one of those years is registered.
        span = re.search(r"최근\s*(\d+)\s*(?:개\s*)?(?:년|개년|회계\s*연도)", head)
        per_year = statement[2] if len(statement) > 2 else {}
        wanted = range(year - int(span.group(1)) + 1, year + 1) if span else ()
        if not wanted or any(item not in per_year for item in wanted):
            return lowest_row(rows, "여러 해 평균 비율에 필요한 연도 재무제표가 등록되지 않음", bonus=bonus)
        ratios = {name: sum(per_year[item][name] for item in wanted) / len(wanted) for name in ratios}
        year_label = f"{wanted[0]}~{year}년 평균"
    else:
        year_label = f"{year}년"
    named = [name for name, pattern in _RATIO_NAMES.items() if re.search(pattern, _norm(raw.label))]
    if len(named) != 1:
        named = [name for name, pattern in _RATIO_NAMES.items() if re.search(pattern, head)]
    if len(named) == 1:
        return _ratio_score(named[0], rows, head, ratios, year_label, bonus=bonus)
    # One item holding several ratios (자기자본 3점 + 유동 2점): score each
    # ratio on the rows that name it and add them up.
    groups: dict[str, list[PrintedRow]] = {}
    for row in rows:
        owners = [name for name in named if re.search(_RATIO_NAMES[name], _norm(row.literal))]
        if len(owners) != 1:
            return lowest_row(rows, "평가 대상 재무비율을 하나로 특정할 수 없음", bonus=bonus)
        groups.setdefault(owners[0], []).append(row)
    if len(groups) < 2:
        return lowest_row(rows, "평가 대상 재무비율을 하나로 특정할 수 없음", bonus=bonus)
    parts = [_ratio_score(name, group, head, ratios, year_label, bonus=bonus) for name, group in groups.items()]
    return BetaScore(
        sum(part.points for part in parts), " + ".join(part.row_literal for part in parts),
        "; ".join(part.basis for part in parts), any(part.floor_applied for part in parts),
    )


# Roster cannot show these; the people they name are treated as not qualifying.
# Careers (including similar-project careers) are read as company tenure and
# residence/assignment as "every approved member is available" instead.
_UNVERIFIABLE_PERSONNEL = re.compile(
    r"나이|연령|만\s*\d+\s*세|\d+\s*세\s*(?:이하|미만|이상)|"
    r"지역\s*주민|장애인|여성|청년|경력\s*단절"
)
_CAREER_YEARS = re.compile(r"(?:경력\s*(\d+)\s*년\s*이상|(\d+)\s*년\s*이상\s*(?:의\s*)?경력)")
_DEGREE_WORDS = (("박사", "DOCTORATE"), ("석사", "MASTER"), ("학사", "BACHELOR"), ("대졸", "BACHELOR"),
                 ("4년제", "BACHELOR"))
_DEGREE_RANK = {"BACHELOR": 1, "MASTER": 2, "DOCTORATE": 3}
_GRADE_RANK = {"RESEARCH_ASSISTANT": 0, "RESEARCHER": 1, "LEAD_RESEARCHER": 2}
_TIERS = ("초급", "중급", "고급", "특급")
_CREDENTIAL_WORDS = re.compile(
    r"[가-힣A-Za-z]{2,12}(?:지도사|상담사|분석사|기획사|기술사|산업기사|기능사|기사|안내사|"
    r"인솔자|교육사|복지사|정교사|관리사|평가사|노무사|회계사)"
)
_MAJOR_STOP = {"이상", "이하", "졸업", "해당", "관련", "전공", "학과", "분야", "대학", "전문", "기타", "동일"}
_OR_WORDS = re.compile(r"\s*(?:이거나|거나|혹은|또는)\s*")


@dataclass(frozen=True)
class _Requirement:
    degree: str | None = None
    grade: str | None = None
    credentials: tuple[str, ...] = ()
    majors: tuple[str, ...] = ()
    tenure: int = 0

    def merged(self, other: "_Requirement") -> "_Requirement":
        degree = max((self.degree, other.degree), key=lambda level: _DEGREE_RANK.get(level or "", 0))
        grade = max((self.grade, other.grade), key=lambda level: _GRADE_RANK.get(level or "", -1))
        return _Requirement(degree, grade, other.credentials or self.credentials,
                            other.majors or self.majors, max(self.tenure, other.tenure))

    def labels(self) -> list[str]:
        return [label for label, present in (
            ("학위", self.degree), ("전공", self.majors), ("연구원 등급", self.grade),
            ("자격", self.credentials), ("근속·경력", self.tenure),
        ) if present]


def _requirement(text: str) -> _Requirement:
    degree = next((level for word, level in _DEGREE_WORDS if word in text), None)
    grade = "LEAD_RESEARCHER" if re.search(r"책임\s*연구원", text) else (
        "RESEARCHER" if re.search(r"연구원\s*이상|선임\s*연구원", text) else None)
    credentials = list(dict.fromkeys(_CREDENTIAL_WORDS.findall(text)))
    tier = re.search(r"(초급|중급|고급|특급)\s*(?:기술자|기술인|기술인력|이상)", text)
    if tier:
        credentials.extend(_TIERS[_TIERS.index(tier.group(1)):])
    majors = tuple(word for word in dict.fromkeys(re.findall(
        r"([가-힣]{2,10})\s*(?:및\s*관련\s*학과|관련\s*학과|관련\s*전공|전공자)", text)) if word not in _MAJOR_STOP)
    tenure = 0
    for match in re.finditer(r"(\d+)\s*(년|개월|월)\s*이상\s*(?:근무|재직|근속)", text):
        tenure = max(tenure, int(match.group(1)) * (12 if match.group(2) == "년" else 1))
    for match in _CAREER_YEARS.finditer(text):
        # Tenure at the company is a lower bound of the total career asked for.
        tenure = max(tenure, int(match.group(1) or match.group(2)) * 12)
    return _Requirement(degree, grade, tuple(credentials), majors, tenure)


def _requirements(raw: Any) -> list[_Requirement]:
    """One requirement per printed alternative (``…이거나 석사 이상``), sharing the rest."""

    common = _Requirement()
    alternatives: list[_Requirement] = []
    parts = [raw.label, *re.split(r"[\n/]", raw.criterion_literal or ""),
             *(item.literal for item in raw.recognition_conditions)]
    for part in (_norm(item) for item in parts if item):
        pieces = [piece for piece in _OR_WORDS.split(part) if re.search(r"[가-힣A-Za-z0-9]", piece)]
        options = [_requirement(piece) for piece in pieces]
        if len(options) > 1 and all(option.labels() for option in options):
            alternatives = [
                left.merged(right) for left in (alternatives or [_Requirement()]) for right in options
            ][:16]
        else:
            common = common.merged(_requirement(part))
    return [common.merged(option) for option in alternatives] or [common]


def _meets(member: Any, requirement: _Requirement, cutoff: date, regular: bool) -> bool:
    if member.joined_on > cutoff or _months_between(member.joined_on, cutoff) < requirement.tenure:
        return False
    if regular and member.regular_employee is not True:
        return False
    degrees = tuple(member.degrees)
    if requirement.degree is not None:
        degrees = tuple(item for item in degrees if _DEGREE_RANK[item.level] >= _DEGREE_RANK[requirement.degree])
        if not degrees:
            return False
    if requirement.majors and not any(
        _compact(keyword) in _compact(item.major) for item in degrees if item.major for keyword in requirement.majors
    ):
        return False
    if requirement.grade is not None and (
        member.research_grade is None or _GRADE_RANK[member.research_grade] < _GRADE_RANK[requirement.grade]
    ):
        return False
    return not requirement.credentials or any(
        _compact(keyword) in _compact(held) for held in member.credentials for keyword in requirement.credentials
    )


def _months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    return months - (end.day < start.day)


def personnel_count(raw: Any, members: Sequence[Any], *, deadline: datetime,
                    published_at: datetime | None) -> tuple[int, str]:
    """Count roster members meeting every roster-checkable condition of one row.

    Alternatives printed with 이거나/또는 are each tried; a generic "관련 학과·
    분야" without a named major is taken as met. A printed career of N years
    (similar-project careers included) is read as N years of company tenure,
    and residence/assignment as every approved member being available.
    Conditions the roster cannot show at all (age, demographic status) count
    nobody.
    """

    text = _row_text(raw)
    if _UNVERIFIABLE_PERSONNEL.search(text):
        return 0, "명부로 확인할 수 없는 조건(나이·연령·신분 요건 등)이 있어 해당 인원을 미충족으로 보았습니다."
    reference = deadline
    if published_at is not None and re.search(r"공고일\s*(?:기준|현재)|공고일", text) and not re.search(r"마감일", text):
        reference = published_at
    fixed = re.search(r"(20\d{2})\s*(?:[./-]|년)\s*(\d{1,2})\s*(?:[./-]|월)\s*(\d{1,2})\s*(?:[.]|일)?\s*기준", text)
    cutoff = date(int(fixed.group(1)), int(fixed.group(2)), int(fixed.group(3))) if fixed else (
        reference if reference.tzinfo else reference.replace(tzinfo=timezone.utc)).astimezone(_KST).date()
    regular = bool(re.search(r"정규\s*직|정규\s*근로자|상근", text))
    requirements = _requirements(raw)
    count = sum(1 for member in members if any(_meets(member, item, cutoff, regular) for item in requirements))
    notes = list(dict.fromkeys(label for item in requirements for label in item.labels()))
    if regular:
        notes.append("정규직")
    condition = ", ".join(notes) if notes else "재직 인원 전체"
    options = f" 중 하나({len(requirements)}가지)" if len(requirements) > 1 else ""
    return count, (
        f"{cutoff.isoformat()} 기준 명부에서 {condition}{options} 조건을 충족한 {count}명"
        " (경력은 당사 근속으로 대체, 관련 학과·분야는 충족 가정, 승인 인원 전원 상주·투입 가정)."
    )


_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(억|천만|백만|만)\s*원?\s*(?:이상|초과)")
_UNIT_KRW = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000}
_PERIOD = re.compile(r"최근\s*(\d+)\s*(년|개월)")


def _unit_scale(unit: str | None, rows: Sequence[PrintedRow]) -> float:
    unit = _compact(unit)
    for word in ("억", "천만", "백만"):
        if word in unit:
            return _UNIT_KRW[word]
    if unit.startswith("만"):
        return 10_000
    if "원" in unit:
        return 1
    bounds = [value for row in rows for value in (row.value, row.min_value, row.max_value) if value]
    return 1 if bounds and max(bounds) >= 1_000_000 else 100_000_000


# Field words that make a contract "similar". A row naming one of these only
# counts register contracts whose name, overview or keywords name the same field.
_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("행사", ("행사", "박람회", "박람", "전시", "축제", "페스타", "엑스포", "EXPO", "컨퍼런스", "콘퍼런스", "포럼",
             "페어", "대회", "기념식", "시상식", "설명회", "세미나", "심포지엄", "페스티벌", "MICE")),
    ("해외연수·여행", ("해외연수", "국외연수", "해외 연수", "국외 연수", "교육여행", "수학여행", "테마형", "여행",
                   "탐방", "견학", "체험학습")),
    ("교육·연수", ("교육", "연수", "훈련", "아카데미", "캠프", "강의", "워크숍", "역량강화")),
    ("연구·조사", ("연구", "조사", "분석", "학술")),
    ("컨설팅", ("컨설팅", "진단", "자문", "코칭")),
    ("홍보", ("홍보", "마케팅", "광고", "캠페인", "콘텐츠", "영상")),
    ("채용·시험", ("채용", "시험", "출제", "면접")),
    ("인증", ("인증",)),
    ("정보시스템", ("시스템", "플랫폼", "소프트웨어", "정보화", "홈페이지", "웹사이트", "전산")),
)
# A named technology is narrower than its field: only the technology itself counts.
_SPECIFIC = ("디지털트윈", "디지털 트윈", "메타버스", "블록체인", "드론", "DRT", "GIS", "BIM", "빅데이터", "인공지능")
# Institution names that carry field words without describing the work.
_INSTITUTION = re.compile(r"[가-힣]*(?:교육청|교육지원청|교육부|교육원|교육대학교|연구원|연구소|진흥원|개발원|평가원)")
_SIMILAR = re.compile(r"유사|동종|동일|관련|해당\s*분야|같은\s*분야")


def _fields_in(text: str) -> tuple[str, tuple[str, ...]] | None:
    text = _INSTITUTION.sub(" ", _norm(text))
    specific = tuple(term for term in _SPECIFIC if _compact(term).casefold() in _compact(text).casefold())
    if specific:
        return "·".join(dict.fromkeys(specific)), specific
    compact = _compact(text)
    spans: dict[str, list[tuple[int, int]]] = {}
    for name, terms in _FIELDS:
        for term in terms:
            needle = _compact(term)
            spans.setdefault(name, []).extend(
                (match.start(), match.start() + len(needle)) for match in re.finditer(re.escape(needle), compact))
    spans = {name: found for name, found in spans.items() if found}

    def inside(span: tuple[int, int], owner: str) -> bool:
        return any(other[0] <= span[0] and span[1] <= other[1] and other != span
                   for name, found in spans.items() if name != owner for other in found)

    # "해외연수" or "교육여행" names travel, not generic training: a field whose
    # every hit sits inside another field's longer word is not asked for.
    found = [(name, terms) for name, terms in _FIELDS
             if name in spans and not all(inside(span, name) for span in spans[name])]
    if not found:
        return None
    return "·".join(name for name, _terms in found), tuple(term for _name, terms in found for term in terms)


def similarity_terms(raw: Any, notice_title: str | None) -> tuple[str, tuple[str, ...]] | None:
    """The field a performance row asks for: from its own text, else the notice title."""

    row = " ".join(value for value in (
        raw.label, raw.criterion_literal, *(item.literal for item in raw.recognition_conditions),
    ) if value)
    own = _fields_in(row)
    if own is not None:
        return f"원문 '{own[0]}'", own[1]
    if notice_title and _SIMILAR.search(_norm(row)):
        derived = _fields_in(notice_title)
        if derived is not None:
            return f"공고명 기준 '{derived[0]}'", derived[1]
    return None


def _record_text(record: Any) -> str:
    keywords = getattr(record, "keywords", None) or []
    return _compact(_INSTITUTION.sub(" ", _norm(" ".join(str(value) for value in (
        getattr(record, "project_name", "") or "", getattr(record, "overview", "") or "",
        " ".join(str(item) for item in keywords),
    ))))).casefold()


def performance_value(raw: Any, records: Sequence[Any], *, deadline: datetime,
                      published_at: datetime | None, rows: Sequence[PrintedRow],
                      notice_title: str | None = None) -> tuple[float, str]:
    """Count (or sum) register contracts by printed period, amount and field.

    The field comes from the row's own words, or from the notice title when the
    row only says 유사/관련; a contract counts when its name, overview or
    keywords name that field. Without any field word no similarity filter applies.
    """

    text = _row_text(raw)
    end = deadline.astimezone(_KST).date()
    if published_at is not None and re.search(r"공고일", text):
        end = published_at.astimezone(_KST).date()
    period = _PERIOD.search(text)
    months = (int(period.group(1)) * (12 if period.group(2) == "년" else 1)) if period else 60
    start_month = end.year * 12 + end.month - 1 - months
    start = date(start_month // 12, start_month % 12 + 1, min(end.day, 28))
    minimum = 0
    amount_match = _AMOUNT.search(text)
    if amount_match and raw.metric == "PERFORMANCE_COUNT":
        minimum = int(float(amount_match.group(1).replace(",", "")) * _UNIT_KRW[amount_match.group(2)])
    field = similarity_terms(raw, notice_title)
    needles = tuple(_compact(term).casefold() for term in field[1]) if field else ()
    amounts = []
    for record in records:
        amount = getattr(record, "gross_contract_amount_krw", None) or getattr(record, "contract_amount", None) or 0
        share = getattr(record, "share_pct", None)
        closed = getattr(record, "end_date", None)
        if (
            getattr(record, "record_status", None) == "VALIDATED"
            and getattr(record, "completed", None) is True
            and closed is not None and start <= closed <= end and amount >= minimum
            and (not needles or any(needle in _record_text(record) for needle in needles))
        ):
            amounts.append(amount * (share if share else 100) / 100)
    window = f"{start.isoformat()}~{end.isoformat()}"
    similar = f"{field[0]} 관련 계약만(사업명·개요 기준)" if field else "분야 표현 없음 → 유사성 필터 없음"
    if raw.metric == "PERFORMANCE_AMOUNT":
        scale = _unit_scale(raw.unit, rows)
        return sum(amounts) / scale, (
            f"{window} 완료 실적 {len(amounts)}건 금액 합계 {sum(amounts):,.0f}원(대장 기준, {similar})."
        )
    rule = f", 단일 계약 {minimum:,}원 이상" if minimum else ""
    return float(len(amounts)), f"{window} 완료 실적 {len(amounts)}건(대장 기준{rule}, {similar})."


def company_credit_grade(facts: Iterable[Any]) -> str | None:
    """The registered enterprise grade: a stored fact or a registered certificate."""

    from .quantitative_formula import parse_credit_rating

    grades: set[str] = set()
    for fact in facts:
        key = getattr(fact, "fact_key", None)
        if not str(key or "").startswith("company.credit_rating") and key != "notice.quantitative.sufficient_row":
            continue
        if getattr(fact, "verified", None) is not True:
            continue
        value = getattr(fact, "value", None)
        if key == "company.credit_rating" and isinstance(value, str) and parse_credit_rating(value):
            grades.add(parse_credit_rating(value))
        evidence = getattr(fact, "evidence", None)
        metadata = getattr(evidence, "metadata_json", None) if evidence is not None else None
        registration = metadata.get("registration") if isinstance(metadata, dict) else None
        rating = registration.get("rating") if isinstance(registration, dict) else None
        if isinstance(rating, str) and parse_credit_rating(rating):
            grades.add(parse_credit_rating(rating))
    if len(grades) == 1:
        return next(iter(grades))
    fallback = os.getenv("PAI_QUANT_BETA_CREDIT_GRADE", "").strip()
    return parse_credit_rating(fallback) if fallback else None


_NOT_COMPANY_FACT = re.compile(
    r"제안서\s*평가|평가\s*점수가|적격\s*업체로\s*선정|기술\s*(?:능력\s*)?평가|정성\s*평가|발표|면접|필기|"
    r"가격\s*평가|입찰\s*가격|투찰\s*가격|계획서\s*평가"
)


def is_company_fact_row(raw: Any) -> bool:
    """False for proposal-evaluation thresholds that no company fact can answer."""

    head = _norm(f"{raw.label} {raw.criterion_literal}")
    return not (raw.metric == "UNKNOWN" and (raw.max_points >= 50 or _NOT_COMPANY_FACT.search(head)))


def beta_score(
    raw: Any,
    *,
    deadline: datetime,
    published_at: datetime | None,
    credit_grade: str | None,
    roster: Sequence[Any] | None,
    records: Sequence[Any],
    statement: tuple[Any, ...] | None = None,
    notice_title: str | None = None,
) -> BetaScore | None:
    """Apply company facts to one criterion's printed rows; None when not a company row."""

    rows = printed_rows(raw)
    standard = False
    if not rows and raw.metric == "CREDIT_RATING":
        # "신용평가등급에 의한 경영상태 평가기준 의거" / "세부기준: 붙임" point to the
        # standard negotiated-contract table instead of printing it.
        rows, standard = standard_credit_rows(raw.max_points), True
    if not rows or not is_company_fact_row(raw):
        return None
    bonus = bool(_BONUS.search(_norm(f"{raw.label} {raw.criterion_literal}")))
    if is_sanction_row(raw):
        return score_sanction(raw, rows)
    metric = raw.metric
    if metric == "CREDIT_RATING":
        if credit_grade is None:
            return lowest_row(rows, "등록된 기업신용평가등급 없음", bonus=bonus)
        return score_grade(rows, credit_grade, f"등록된 기업신용평가등급 {credit_grade}" + (
            " (원문이 참조한 표준 신용평가 기준표 적용)" if standard else ""))
    if metric in {"PERFORMANCE_COUNT", "PERFORMANCE_AMOUNT"}:
        value, basis = performance_value(raw, records, deadline=deadline, published_at=published_at, rows=rows,
                                         notice_title=notice_title)
        return score_numeric(rows, value, basis, bonus=bonus)
    if metric == "PERSONNEL_COUNT":
        if roster is None:
            return lowest_row(rows, "적용 가능한 확인 인력 명부 없음", bonus=bonus)
        if "%" in _norm(raw.unit or "") or "비율" in _norm(raw.label):
            return lowest_row(rows, "인력 비율 산식은 명부만으로 계산하지 않음", bonus=bonus)
        count, basis = personnel_count(raw, roster, deadline=deadline, published_at=published_at)
        return score_numeric(rows, float(count), basis, bonus=bonus)
    if metric == "FINANCIAL_RATIO" or (
        metric == "UNKNOWN" and any(re.search(p, _norm(raw.label)) for p in _RATIO_NAMES.values())
    ):
        return score_financial(raw, rows, statement, bonus=bonus)
    return lowest_row(rows, "회사 사실로 역산할 수 없는 항목", bonus=bonus)


__all__ = [
    "BetaScore", "COMPANY_FIRST_FLOOR_NOTE", "COMPANY_FIRST_LABEL", "COMPANY_FIRST_REASON",
    "COMPANY_FIRST_STATEMENT", "PrintedRow", "beta_score", "company_credit_grade",
    "company_first_enabled", "financial_ratios", "is_company_fact_row", "is_sanction_row",
    "lowest_row", "performance_value", "score_financial",
    "personnel_count", "printed_rows", "score_grade", "score_numeric", "score_sanction",
]
