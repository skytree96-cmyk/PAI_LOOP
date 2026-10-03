from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal


PolicyClass = Literal["ELIGIBILITY", "ACTION_REQUIRED", "CHECKLIST", "INFORMATION"]

PROFILE_PATH = Path(__file__).with_name("data") / "company_public_profile.json"
# A stored decision is only revisited when this version moves: the planner
# re-queues a notice whose analysis ran under an older one. Any change that
# makes this module decide differently has to move it, or the new rule reaches
# new notices only and every existing decision keeps the answer it already had.
# v14 applies the approved current-company prototype baseline and groups
# equivalent display rows without removing their evaluation/source records.
# v17 generalizes the 2026-09-30 reviewer decisions on live REVIEW conditions
# (see docs/R_REVIEW_GENERALIZATION_20260930.md).
POLICY_VERSION = "pai-loop-requirement-policy-2026.10.03-v18"

# Approved prototype scope: assess these known company facts as they stand now.
# Other qualifications retain deadline-based evidence checks.
PROTOTYPE_FACT_KEYS = frozenset({
    "industry_code_inventory", "bidder_registration", "direct_production_certificate",
    "small_business_certificate", "sme_certificate", "sanction_clear",
    "conviction_clear", "disqualification_clear", "bidder_identity_consistent",
    "domestic_entity", "nonprofit_entity",
    "public_dues_arrears_clear", "court_receivership_clear", "contract_nonperformance_clear",
    "business_continuity_clear", "nonprofit_priority_procurement_exception",
    "large_enterprise_software_clear",
})


def prototype_fact_enabled(profile: dict[str, Any], fact_key: str) -> bool:
    return (
        profile.get("eligibility_assessment_mode") == "PROTOTYPE_CURRENT_FACTS"
        and fact_key in PROTOTYPE_FACT_KEYS
    )

# How many days a RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE / RECONFIRM_BEFORE_EACH_SUBMISSION
# fact may go without a fresh verification before we stop trusting it and force REVIEW.
# This is a placeholder default pending an explicit staleness policy from the operations
# team (see 2026-09-03 eligibility freshness fix changelog). Codex/ops should confirm and
# adjust this single constant rather than re-deriving the threshold ad hoc.
_FRESHNESS_STALENESS_DAYS = 180

_FORBIDDEN_KEYS = {
    "address",
    "birth_date",
    "business_registration_number",
    "contact",
    "email",
    "mobile",
    "person_name",
    "phone",
    "registration_number",
    "representative",
    "resident_registration_number",
}
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\d)0\d{1,2}[- ]?\d{3,4}[- ]?\d{4}(?!\d)")
_REGISTRATION_NUMBER = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")


def _assert_public_safe(value: Any, *, path: str = "profile") -> None:
    """Fail closed if a curated public profile grows a sensitive field."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"public company profile contains forbidden key: {path}.{key}")
            _assert_public_safe(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public_safe(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and (
        _EMAIL.search(value) or _PHONE.search(value) or _REGISTRATION_NUMBER.search(value)
    ):
        raise ValueError(f"public company profile contains a sensitive value at {path}")


def load_public_company_profile() -> dict[str, Any]:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    _assert_public_safe(profile)
    return copy.deepcopy(profile)


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _contains(text: str, *tokens: str) -> bool:
    return any(token.casefold() in text for token in tokens)


_INDUSTRY_CODE_LIST = re.compile(
    r"업종\s*코드\s*[:：]?\s*[\[(]?\s*"
    r"(?P<codes>\d{4}(?:\s*(?:,|·|/|또는|혹은|및)\s*\d{4})*)",
    re.IGNORECASE,
)
_PARENTHESIZED_INDUSTRY_CODE = re.compile(r"[\[(]\s*(\d{4})\s*[\])]")
_FOUR_DIGIT_CODE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _required_industry_codes(text: str, *, category: str) -> list[str]:
    """Extract only four-digit bidder industry codes, never years or NCS codes."""

    explicit_marker = _contains(text, "업종코드", "업종 코드")
    if not explicit_marker and not (
        _contains(text, "등록")
        and (category == "INDUSTRY_CODE" or _contains(text, "업종", "나라장터", "g2b"))
        and re.search(r"[\[(]\s*\d{4}\s*[\])]", text)
    ):
        return []
    candidates: list[str] = []
    for match in _INDUSTRY_CODE_LIST.finditer(text):
        candidates.extend(_FOUR_DIGIT_CODE.findall(match.group("codes")))
    if not explicit_marker:
        candidates.extend(_PARENTHESIZED_INDUSTRY_CODE.findall(text))

    result: list[str] = []
    for code in candidates:
        number = int(code)
        if 1900 <= number <= 2099 or code in result:
            continue
        result.append(code)
    return result


def _industry_code_operator(text: str, codes: list[str]) -> str | None:
    if len(codes) == 1:
        return "contains"
    positions: list[tuple[int, int]] = []
    cursor = 0
    for code in codes:
        pattern = (
            re.compile(rf"(?<!\d){re.escape(code)}(?!\d)")
            if code.isdigit()
            else re.compile(
                rf"(?<![0-9A-Za-z가-힣]){re.escape(code)}{_NCS_NAME_RIGHT_BOUNDARY}"
            )
        )
        match = pattern.search(text, cursor)
        if match is None:
            return None
        positions.append(match.span())
        cursor = match.end()
    connectors = [
        text[positions[index][1] : positions[index + 1][0]]
        for index in range(len(positions) - 1)
    ]
    suffix = re.split(r"[.!?。;\n]", text[positions[-1][1] :], maxsplit=1)[0][:40]
    suffix_operator = re.match(
        r"^\s*[)\]}]?\s*(?P<operator>중\s*하나|모두|동시|각각)",
        suffix,
    )
    suffix_token = suffix_operator.group("operator") if suffix_operator else ""
    suffix_or = bool(re.fullmatch(r"중\s*하나", suffix_token))
    suffix_and = suffix_token in {"모두", "동시", "각각"}
    has_or = suffix_or or any(
        _contains(part, "또는", "혹은", " 중 하나", " or ") for part in connectors
    )
    has_and = suffix_and or any(
        _contains(part, "및", "모두", "동시", "각각", " and ") for part in connectors
    )
    connector_missing = any(
        not _contains(
            part,
            "또는",
            "혹은",
            " 중 하나",
            " or ",
            "및",
            "모두",
            "동시",
            "각각",
            " and ",
        )
        for part in connectors
    ) and not (suffix_or or suffix_and)
    if any("/" in part for part in connectors) or (has_or and has_and) or connector_missing:
        return None
    if has_or:
        return "contains_any"
    return "contains_all"


_NCS_CODE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_NCS_TRAINING_NAMES = (
    "경영기획",
    "경영평가",
    "노무관리",
    "인사",
    "비서",
    "사무행정",
    "빅데이터플랫폼구축",
    "경력지도",
    "기업교육",
    "직무분석",
    "세무",
    "회계·감사",
)
_NCS_NAME_RIGHT_BOUNDARY = r"(?=(?:은|는|이|가|을|를|과|와|도|만)?(?:$|[\s,;:/()·]))"


def _exact_ncs_training_names(text: str) -> list[str]:
    """Return only complete NCS labels, not prefixes of another subclass."""

    return [
        name
        for name in _NCS_TRAINING_NAMES
        if re.search(
            rf"(?<![0-9A-Za-z가-힣]){re.escape(name)}{_NCS_NAME_RIGHT_BOUNDARY}",
            text,
        )
    ]


def _vocational_training_scope_requirement(
    text: str,
    *,
    category: str,
) -> tuple[str | None, str | None, Any] | None:
    facility = _contains(text, "지정직업훈련시설", "직업능력개발훈련시설")
    if not facility or (
        category not in {"CERTIFICATION", "INDUSTRY_CODE"}
        and not _has_explicit_bidder_gate(text)
    ):
        return None
    has_scope_marker = _contains(text, "ncs", "훈련직종", "훈련 직종", "세분류")
    if not has_scope_marker:
        return None
    codes = list(dict.fromkeys(_NCS_CODE.findall(text)))
    names = _exact_ncs_training_names(text)
    if codes:
        operator = _industry_code_operator(text, codes)
        expected: Any = codes[0] if operator == "contains" else codes
        return "designated_vocational_training_ncs_codes", operator, expected
    if names:
        operator = _industry_code_operator(text, names)
        expected = names[0] if operator == "contains" else names
        return "designated_vocational_training_ncs_names", operator, expected
    return None, None, None


def _policy_value_matches(actual: Any, operator: str, expected: Any) -> bool:
    if operator in {"contains_any", "contains_all"}:
        if not isinstance(actual, list) or not isinstance(expected, list):
            return False
        actual_n = [_normalise(item) for item in actual]
        expected_n = [_normalise(item) for item in expected]
        matches = [item in actual_n for item in expected_n]
        return any(matches) if operator == "contains_any" else all(matches)
    actual_n = [_normalise(item) for item in actual] if isinstance(actual, list) else _normalise(actual)
    expected_n = _normalise(expected)
    if operator == "eq":
        return actual_n == expected_n
    if operator == "contains":
        return isinstance(actual_n, (list, str)) and expected_n in actual_n
    return False


_DIRECT_PRODUCTION_CERT_PATTERN = r"직접\s*생산\s*확인(?:증명)?서"
_SMALL_BUSINESS_CERT_PATTERN = (
    r"(?:중소기업|소기업(?:\s*[·ㆍ]\s*소상공인)?|소상공인)\s*확인서"
)


def _has_scoped_nonprofit_exception(text: str, *, subject_pattern: str) -> bool:
    negated_exception = bool(
        re.search(r"(?:면제|예외)(?:\s*대상)?(?:이|가)?\s*아니", text)
        or re.search(r"적용하지\s*않는\s*것(?:은|이)?\s*아니", text)
    )
    if negated_exception:
        return False
    subject_then_entity = re.search(
        rf"(?:{subject_pattern})\s*(?:조건|요건)?(?:은|는|을|를|모두)?\s*"
        r"비영리법인(?:에|에게)?(?:은|는|도)?\s*(?:적용하지|면제|예외)",
        text,
    )
    entity_then_subject = re.search(
        r"비영리법인(?:에|에게)?(?:은|는|도)?\s*"
        rf"(?:{subject_pattern})\s*(?:조건|요건)?(?:은|는|을|를|모두)?\s*"
        r"(?:적용하지|면제|예외)",
        text,
    )
    return bool(subject_then_entity or entity_then_subject) and _contains(
        text,
        "참여 가능",
        "참가 가능",
        "적용하지",
        "면제",
        "예외",
    )


def _has_explicit_nonprofit_compound_exception(text: str) -> bool:
    joint_subject = (
        rf"(?:{_DIRECT_PRODUCTION_CERT_PATTERN}\s*(?:와|과|및|[·ㆍ])\s*"
        rf"{_SMALL_BUSINESS_CERT_PATTERN}|"
        rf"{_SMALL_BUSINESS_CERT_PATTERN}\s*(?:와|과|및|[·ㆍ])\s*"
        rf"{_DIRECT_PRODUCTION_CERT_PATTERN})"
    )
    return (
        _is_direct_production_certificate_eligibility(text)
        and _is_small_business_eligibility(text, category="CERTIFICATION")
        and _has_scoped_nonprofit_exception(text, subject_pattern=joint_subject)
        and "비영리법인" in text
    )


def _has_explicit_nonprofit_direct_production_exception(text: str) -> bool:
    return (
        _is_direct_production_certificate_eligibility(text)
        and "비영리법인" in text
        and _has_scoped_nonprofit_exception(
            text,
            subject_pattern=_DIRECT_PRODUCTION_CERT_PATTERN,
        )
    )


# Declarative sentence endings an extracted `normalized_condition` carries after
# the clause itself. Every token is a closed literal that states no obligation of
# its own and no polarity, so absorbing it into the whole-clause anchor cannot
# import a duty, a negation or an exclusion. Sized from the shapes real extracted
# conditions actually end with (obligation copulas `여야/이어야 함`, obligation
# verbs `해야/하여야/되어야 함`, and a bare subject noun such as `업체`/`자`).
# There is deliberately no leading-affix allowlist: observed conditions begin at
# the clause itself.
_INERT_CLAUSE_TAIL = (
    r"(?:하는|되는)?\s*(?:업체|자|기관|법인|단체|사업자)?\s*"
    r"(?:여야|이어야)?\s*(?:해야|하여야|되어야)?\s*(?:함|한다|합니다)?\s*\.?"
)
_SMALL_BUSINESS_HOLDER_OR = (
    rf"{_SMALL_BUSINESS_CERT_PATTERN}\s*(?:을|를)?\s*(?:소지|보유)(?:한|하는)?\s*"
    r"(?:업체|자|기관|법인|단체|사업자)\s*또는\s*"
)


def _has_explicit_nonprofit_small_business_alternative(text: str) -> bool:
    """Recognize one complete SME/nonprofit OR, without borrowing another duty.

    Full-clause matching keeps exclusions, negation, AND conditions and extra
    qualification duties outside this alternative. The optional parenthesis is
    limited to the stated incorporation-permit evidence submission, and the only
    tolerated tail is the inert declarative ending in `_INERT_CLAUSE_TAIL`.
    """

    return re.fullmatch(
        _SMALL_BUSINESS_HOLDER_OR
        + r"비영리법인(?:\s*\(\s*법인\s*설립\s*허가서\s*(?:등\s*)?증빙\s*제출\s*\))?"
        rf"\s*중\s*(?:어느\s*)?하나에\s*해당{_INERT_CLAUSE_TAIL}",
        text,
    ) is not None


def _has_unresolved_nonprofit_small_business_subset(text: str) -> bool:
    """Keep a bounded legal-subset OR unresolved, never infer subset membership.

    These complete clause shapes identify a second alternative whose extra
    legal qualification has no bound company fact. A generic nonprofit fact
    cannot satisfy it, and absence of the SME certificate cannot refute it.
    """

    subset = (
        r"(?:우선\s*조달\s*계약\s*예외\s*규정에\s*따른\s*비영리법인|"
        r"관계\s*법령상\s*비영리법인|"
        r"비영리법인\s*\(\s*우선\s*조달\s*예외\s*대상\s*\)|"
        r"시행령\s*제\s*2\s*조의\s*3\s*(?:제\s*1\s*항\s*)?"
        r"제\s*2\s*호\s*(?:해당|에\s*따른)\s*비영리법인)"
    )
    return re.fullmatch(
        _SMALL_BUSINESS_HOLDER_OR + subset
        + rf"\s*(?:중\s*(?:어느\s*)?하나에|에)\s*해당{_INERT_CLAUSE_TAIL}",
        text,
    ) is not None


def _is_unqualified_small_business_certificate_clause(text: str) -> bool:
    """Identify only a standalone possession/validity clause with unknown scope.

    An explicit nonprofit exclusion, an independent-duty marker, or another
    AND qualification cannot match this complete clause and keeps its own gate.
    """

    return re.fullmatch(
        rf"{_SMALL_BUSINESS_CERT_PATTERN}\s*(?:을|를|은|는)?\s*"
        r"(?:(?:보유|소지)(?:해야|하여야)|유효기간\s*내(?:에)?\s*있어야)"
        r"\s*(?:함|한다|합니다)?\s*\.?",
        text,
    ) is not None


def _has_explicit_nonprofit_small_business_exception(text: str, *, category: str) -> bool:
    """Accept an SME exception only when its scope is proven in this clause."""

    alternative_participation = re.search(
        r"(?:소기업\s*또는\s*소상공인)(?:이면서)?\s*확인서(?:를)?\s*"
        r"보유해야\s*하며,?\s*"
        r"(?:일정\s*요건의\s*)?비영리법인(?:은|는)?\s*(?:참여|참가)\s*가능",
        text,
    )
    return (
        _is_small_business_eligibility(text, category=category)
        and "비영리법인" in text
        and (
            alternative_participation is not None
            or _has_explicit_nonprofit_small_business_alternative(text)
            or _has_scoped_nonprofit_exception(
                text,
                subject_pattern=_SMALL_BUSINESS_CERT_PATTERN,
            )
        )
    )


# A nonprofit alternative is decided by the structure that binds it, never by
# the presence of the word. The sentence-shaped `fullmatch` gates above stay
# authoritative for the shapes they already prove; this classifier only decides
# the clauses that reach them unmatched, which previously fell through to a
# confirmed-absence FAIL even when the notice offered the company a route.
_NONPROFIT_SUBJECT = r"비영리\s*법인"
_NONPROFIT_NEAR = r"[^.。]{0,45}?"

# The notice names the nonprofit to exclude it. This must outrank every
# allowance below: "일부 비영리법인 및 부실기업은 참가 불가" both allows and
# forbids in one sentence, and the forbidding half governs.
_NONPROFIT_EXCLUDED_RE = re.compile(
    rf"{_NONPROFIT_SUBJECT}\s*(?:은|는|도|의\s*경우)?\s*(?:제외|배제)(?!\s*가능)"
    rf"|(?:일부\s*)?{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}"
    rf"(?:참가|참여|입찰)\s*(?:불가|불허|할\s*수\s*없)"
)

# The notice names the nonprofit to bind it to the duty, not to release it:
# "중소기업확인서는 비영리법인도 보유해야 함" extends the requirement rather
# than excepting it. Reading this as an allowance would invert the clause, so
# it ranks with exclusion and keeps the confirmed-absence FAIL.
_NONPROFIT_DUTY_RE = re.compile(
    rf"{_NONPROFIT_SUBJECT}\s*(?:도|은|는|에게도|의\s*경우에도)\s*"
    rf"(?![^.。]{{0,25}}(?:없이|없어도|면제|무관|제외))"
    rf"[^.。]{{0,25}}?(?:보유|소지|제출|갖추)(?:해야|하여야|되어야|할)"
)

# A different legal form qualifies the subject. 사회복지법인(비영리법인) demands
# the welfare form itself, so no generic nonprofit fact and no company-wide
# determination can settle it. This outranks the statutory reading below,
# which narrows the contract rather than the bidder.
_NONPROFIT_LEGAL_FORM_RE = re.compile(
    rf"[가-힣]{{2,8}}법인\s*\(\s*{_NONPROFIT_SUBJECT}\s*\)"
)

# The narrowing cites the priority-procurement exception. That provision turns
# on the contract the buyer is letting, and the buyer must state the ground in
# the notice to rely on it. Whether this company stands inside it is a single
# legal determination, so it is carried as a company fact rather than guessed
# from the sentence.
_NONPROFIT_STATUTORY_RE = re.compile(
    rf"(?:제\s*2\s*조의\s*3|우선\s*조달\s*계약\s*예외|우선조달계약\s*예외|우선\s*조달\s*예외"
    rf"|관계\s*법령상|법령상\s*참여\s*가능한|시행령상|시행령에\s*따(?:른|라)|시행령\s*제"
    rf"|간주되는)\s*{_NONPROFIT_NEAR}{_NONPROFIT_SUBJECT}"
    rf"|{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}(?:제\s*2\s*조의\s*3|우선\s*조달\s*예외"
    rf"|우선조달계약\s*예외|관련\s*시행령)"
)

# The narrowing names no statute and no form: 일부, 특정 요건, 특례 대상.
# Nothing in the clause says which nonprofits, so nothing can resolve it.
_NONPROFIT_VAGUE_RE = re.compile(
    rf"(?:일부|일정|특정|특례|소정의|명시된\s*요건의)\s*[^.。]{{0,20}}?{_NONPROFIT_SUBJECT}"
    rf"|(?:특정|일정|관련)\s*요건[^.。]{{0,14}}?{_NONPROFIT_SUBJECT}"
    rf"|(?:예외\s*(?:규정|조항)|관련)\s*{_NONPROFIT_NEAR}{_NONPROFIT_SUBJECT}"
    rf"|{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}예외\s*규정\s*있음"
    rf"|{_NONPROFIT_SUBJECT}\s*(?:특례\s*대상|예외\s*규정에\s*해당|특례)"
    rf"|{_NONPROFIT_SUBJECT}\s*\(\s*(?:예외|우선)"
    rf"|해당\s*{_NONPROFIT_SUBJECT}"
    # "예외 비영리법인 해당", "예외 특별법인·비영리법인 포함": the exception is
    # named but its scope is not.
    rf"|예외\s*[^.。]{{0,10}}?{_NONPROFIT_SUBJECT}"
)

# The nonprofit's own founding purpose is narrowed, so eligibility depends on
# the entity, not on this notice's subject matter.
_NONPROFIT_PURPOSE_RE = re.compile(
    rf"(?:학술|연구|조사|검사|평가|개발)[^.。]{{0,14}}(?:등을\s*)?위한\s*{_NONPROFIT_SUBJECT}"
    rf"|{_NONPROFIT_SUBJECT}[^.。]{{0,20}}학술\s*연구"
    rf"|(?:정책연구|학술연구)\s*용역[^.。]{{0,10}}{_NONPROFIT_SUBJECT}"
    rf"|(?:학술|연구|조사)[^.。]{{0,30}}용역의\s*경우[^.。]{{0,10}}{_NONPROFIT_SUBJECT}"
)

# Participation is open but conditioned on filing proof of nonprofit status.
_NONPROFIT_EVIDENCE_RE = re.compile(
    # "비영리법인 확인서" is a document to file; "비영리법인은 확인서 없이도" is
    # the opposite claim about the same noun, so the waiver must not match here.
    rf"{_NONPROFIT_SUBJECT}\s*(?:은|는|도)?\s*(?:확인서|확인서류|증명서류|증빙)"
    rf"(?!\s*(?:없이|없어도|면제|요건\s*없이|소지\s*여부와\s*무관))"
    rf"|(?:증빙|증명서류|입증자료|증빙자료|설립\s*허가(?:증|서))"
    rf"{_NONPROFIT_NEAR}{_NONPROFIT_SUBJECT}"
    rf"|{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}(?:별도\s*증빙|설립\s*허가(?:증|서)"
    rf"|증명서류|입증자료|증빙자료|증명하는|법인설립\s*허가)"
    rf"|비영리\s*(?:입증|증빙)자료"
)

# The certificate duty is waived, or the nonprofit stands as an unnarrowed
# alternative subject of the same OR.
_NONPROFIT_UNCONDITIONAL_RE = re.compile(
    rf"{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}"
    rf"(?:확인서\s*(?:없이도|없이|면제)|확인서가\s*없어도|제출\s*면제"
    rf"|(?:자격|요건)과\s*무관|확인서\s*요건\s*없이|소지\s*여부와\s*무관|소지와\s*무관"
    rf"|자격\s*제한\s*없이)"
    rf"|{_NONPROFIT_SUBJECT}\s*(?:은|는|도|의\s*경우)?\s*(?:예외|면제)"
    rf"|{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}예외\s*(?:적용|가능|있음|대상|허용|인정)"
    rf"|예외\s*{_NONPROFIT_SUBJECT}\s*허용"
    rf"|{_NONPROFIT_SUBJECT}\s*(?:은|는|도|의\s*경우)?\s*(?:예외적으로\s*)?"
    rf"(?:경쟁\s*)?(?:입찰\s*)?(?:참가|참여)\s*(?:가능|허용)"
    rf"|(?:이거나|또는|,)\s*{_NONPROFIT_SUBJECT}(?:에\s*해당|이어야|이면|,|/|\s*중\s*하나|\s*\))"
    rf"|{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}별도로\s*(?:참가|참여)\s*가능"
    rf"|{_NONPROFIT_SUBJECT}\s*(?:은|는|도)?\s*참가\s*자격\s*(?:부여|인정)"
    rf"|{_NONPROFIT_SUBJECT}\s*(?:은|는)?\s*제외\s*가능"
)

# "일부 비영리법인 및 부실기업은 참가 불가" forbids some of the group, so it
# proves neither a route nor its absence. It must not read as a blanket
# exclusion, which is why it is tested ahead of one.
_NONPROFIT_PARTIAL_EXCLUSION_RE = re.compile(
    rf"(?:일부|일정|특정)\s*{_NONPROFIT_SUBJECT}{_NONPROFIT_NEAR}"
    rf"(?:참가|참여|입찰)\s*(?:불가|불허|할\s*수\s*없)"
)

NONPROFIT_ALTERNATIVE_STRUCTURES = (
    ("QUALIFIED_VAGUE", _NONPROFIT_PARTIAL_EXCLUSION_RE),
    ("EXCLUDED", _NONPROFIT_EXCLUDED_RE),
    ("DUTY_EXTENDED", _NONPROFIT_DUTY_RE),
    ("QUALIFIED_FORM", _NONPROFIT_LEGAL_FORM_RE),
    ("QUALIFIED_STATUTE", _NONPROFIT_STATUTORY_RE),
    ("QUALIFIED_VAGUE", _NONPROFIT_VAGUE_RE),
    ("PURPOSE", _NONPROFIT_PURPOSE_RE),
    ("EVIDENCE", _NONPROFIT_EVIDENCE_RE),
    ("UNCONDITIONAL", _NONPROFIT_UNCONDITIONAL_RE),
)

# The one legal determination this company can record once and reuse: whether
# it stands inside the priority-procurement exception a notice may cite.
NONPROFIT_STATUTORY_EXCEPTION_FACT = "nonprofit_priority_procurement_exception"


# Markers that void the whole alternative: the clause negates it, conjoins it
# with a second mandatory duty, or gates it behind a further approval. A
# structural reading must not survive any of these, because each one changes
# what the sentence requires rather than which nonprofits it admits.
_NONPROFIT_ROUTE_VOID_RE = re.compile(
    r"해당하지\s*않|하지\s*않아야|하지\s*않는|아니어야\s*함"
    r"|모두\s*(?:보유|소지)|도\s*(?:보유|소지)(?:해야|하여야)"
    r"|별도\s*(?:승인|심사|허가)"
    r"|및\s*비영리\s*법인"
)


def classify_nonprofit_alternative(text: str) -> str | None:
    """Name the structure that binds a nonprofit mention, or None if absent.

    Order is the contract. An exclusion outranks every allowance, and every
    narrowing outranks the unconditional reading, so a clause that both allows
    and narrows can never be read as an open door. A voided clause returns
    None, leaving the certificate decision exactly as the earlier gates made it.
    """

    if not re.search(_NONPROFIT_SUBJECT, text):
        return None
    # A clause that closes the door is named before the void check, so the
    # reason survives in the explanation instead of collapsing to silence.
    if _NONPROFIT_PARTIAL_EXCLUSION_RE.search(text):
        return "QUALIFIED_VAGUE"
    if _NONPROFIT_EXCLUDED_RE.search(text):
        return "EXCLUDED"
    if _NONPROFIT_DUTY_RE.search(text):
        return "DUTY_EXTENDED"
    if _NONPROFIT_ROUTE_VOID_RE.search(text):
        return None
    for name, pattern in NONPROFIT_ALTERNATIVE_STRUCTURES:
        if pattern.search(text):
            return name
    return "UNRECOGNISED"


def _nonprofit_route_for_absent_certificate(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    structure: str | None,
    deadline: "date | None",
    today: "date | None",
) -> dict[str, Any] | None:
    """Replace a confirmed-absence FAIL when the notice still offers a route.

    Only a confirmed-absence outcome is replaced. Every earlier gate keeps its
    own decision, so no clause that already passed or reviewed can be pulled
    backwards by this fallback.
    """

    if structure in {None, "EXCLUDED", "DUTY_EXTENDED"}:
        return None
    if structure in {"UNCONDITIONAL", "EVIDENCE"}:
        message = (
            "공고가 비영리법인에 한정 없이 확인서 의무를 면제하므로 회사의 "
            "비영리법인 사실이 이 조건을 대체합니다."
            if structure == "UNCONDITIONAL"
            else
            # Participation is open; the only extra step is filing proof of the
            # nonprofit status this company already holds verified.
            "공고가 비영리법인에게 증빙 제출을 조건으로 참가를 허용하고 회사의 "
            "비영리법인 사실이 확인되므로 이 조건을 대체합니다. 입찰 시 법인설립허가증 "
            "등 비영리법인 증빙을 반드시 제출해야 합니다."
        )
        return _eligibility_item(
            requirement,
            profile=profile,
            fact_key="nonprofit_entity",
            deadline=deadline,
            today=today,
            pass_outcome="PASS_EXCEPTION",
            message=message,
        )
    if structure == "QUALIFIED_STATUTE":
        # A confirmed statutory exception does not prove an unnamed subset or
        # a founding-purpose requirement. Those retain their own review below.
        # Registered determination passes; its absence reviews rather than fails.
        return _eligibility_item(
            requirement,
            profile=profile,
            fact_key=NONPROFIT_STATUTORY_EXCEPTION_FACT,
            deadline=deadline,
            today=today,
            pass_outcome="PASS_EXCEPTION",
            message=(
                "공고가 인용한 우선조달계약 예외에 회사가 해당한다는 확정 사실이 "
                "연결되어 확인서 조건을 대체합니다."
            ),
        )
    return _unmapped_eligibility_item(
        requirement,
        fact_key=f"nonprofit_alternative_{structure.lower()}",
        deadline=deadline,
        message={
            "QUALIFIED_FORM": (
                "공고는 특정 법인격(예: 사회복지법인)에 해당하는 비영리법인만 허용합니다. "
                "일반 비영리법인 사실로는 충족을 확정할 수 없어 검토가 필요합니다."
            ),
            "QUALIFIED_VAGUE": (
                "공고는 일부 비영리법인만 대안으로 허용하면서 그 범위를 특정하지 "
                "않았습니다. 확인서 미보유만으로 미충족을 단정하지 않고 원문 검토로 남깁니다."
            ),
            "PURPOSE": (
                "공고는 설립목적이 한정된 비영리법인만 대안으로 허용합니다. 회사의 "
                "목적이 그 범위인지 근거가 연결되지 않아 검토가 필요합니다."
            ),
            "UNRECOGNISED": (
                "이 조건에 비영리법인 대안 문구가 있으나 허용 범위를 구조적으로 확정할 "
                "수 없습니다. 확인서 미보유만으로 미충족을 단정하지 않고 검토로 남깁니다."
            ),
        }[structure],
    )


def _named_permit_fact_keys(text: str, *, category: str) -> list[str]:
    """Return every explicit permit family backed by the curated collection."""

    if category not in {"CERTIFICATION", "INDUSTRY_CODE"} and not _has_explicit_bidder_gate(text):
        return []
    matches: list[str] = []
    if "종합여행업" in text:
        matches.append("general_travel_business")
    if "국제회의기획업" in text:
        matches.append("international_conference_planning")
    if "컴퓨터관련서비스사업" in text:
        matches.append("software_computer_related_services")
    if "패키지소프트웨어" in text:
        matches.append("software_package_development_supply")
    if "디지털콘텐츠" in text:
        matches.append("software_digital_content_development")
    if "데이터베이스" in text and _contains(text, "검색서비스", "검색 서비스"):
        matches.append("software_database_production_search")
    if "출판사" in text:
        matches.append("publisher")
    if _contains(text, "원격평생교육", "원격 평생교육"):
        matches.append("remote_lifelong_education")
    if _contains(text, "비디오물제작", "비디오물 제작"):
        matches.append("video_production")
    if _contains(text, "무료직업소개", "무료 직업소개") and not _contains(text, "유료", "국외"):
        matches.append("free_job_placement")
    if _contains(text, "지정직업훈련시설", "직업능력개발훈련시설"):
        if _vocational_training_scope_requirement(text, category=category) is None:
            matches.append("designated_vocational_training")
    if "기타이러닝" in text:
        matches.append("other_elearning")
    if _contains(text, "이러닝서비스", "이러닝 서비스"):
        matches.append("elearning_service")
    return list(dict.fromkeys(matches))


def _has_additional_permit_condition(text: str) -> bool:
    """Fail closed when one atomic clause still contains another permit gate."""

    return bool(
        re.search(
            r"(?:과|와|및|또는|혹은|이며|이고|하고).{0,80}"
            r"(?:등록|허가|신고|면허|인증|자격|확인서|증명서)",
            text,
        )
    )


def _named_permit_fact_key(text: str, *, category: str) -> str | None:
    """Map only one complete permit gate; compound clauses require review."""

    matches = _named_permit_fact_keys(text, category=category)
    if len(matches) != 1 or _has_additional_permit_condition(text):
        return None
    return matches[0]


_NAMED_PERMIT_INDUSTRY_CODES = {
    "general_travel_business": "1261",
    "software_package_development_supply": "1426",
    "software_computer_related_services": "1468",
    "software_digital_content_development": "1469",
    "software_database_production_search": "1470",
    "publisher": "1517",
    "remote_lifelong_education": "3156",
    "video_production": "3244",
    "free_job_placement": "5601",
    "designated_vocational_training": "5609",
    "international_conference_planning": "5720",
    "elearning_service": "6529",
    "other_elearning": "6530",
}


def _unrepresented_named_permit_fact_keys(
    fact_keys: list[str],
    *,
    industry_codes: list[str],
) -> list[str]:
    """Do not count the same named permit and its code as two gates."""

    return [
        fact_key
        for fact_key in fact_keys
        if _NAMED_PERMIT_INDUSTRY_CODES.get(fact_key) not in industry_codes
    ]


def _small_business_fact_key(text: str) -> str:
    if "소상공인" in text or re.search(r"(?<!중)소기업", text):
        return "small_business_certificate"
    return "sme_certificate"


def _is_seoul_head_office_gate(text: str, *, category: str) -> bool:
    if category != "REGION" or not _is_region_eligibility(text):
        return False
    head_office = _contains(text, "법인등기부상 본점", "본점 소재지", "주된 영업소")
    seoul = _contains(text, "서울특별시", "서울시", "서울 소재")
    return head_office and seoul


def _is_two_person_attendee_limit(text: str, *, category: str) -> bool:
    """Match an attendee head-count clause, never a vehicle seat count.

    A plain ``"2인" in text`` also matches ``22인승``.  Live PPS data
    demonstrated that this silently mapped a 160-vehicle capacity condition to
    the company's two-attendee presentation capability.  Category plus context
    and digit/seat boundaries keep this deterministic rule narrow.
    """

    if category != "PERSONNEL" or not _contains(
        text,
        "참석자",
        "발표자",
        "참여자",
        "배석자",
    ):
        return False
    return bool(
        re.search(
            r"(?<!\d)2\s*(?:인|명)(?!\s*승)",
            text,
        )
    )


def _is_small_business_eligibility(text: str, *, category: str) -> bool:
    """Distinguish an SME participation condition from an SME lookback rule."""

    # The purchasing statute / competitive-product label alone is not an SME gate.
    sme_text = re.sub(r"중소기업\s*(?:제품|자\s*간)", "", text)
    if not _contains(sme_text, "중소기업", "소기업", "소상공인"):
        return False
    if category in {"ENTITY", "CERTIFICATION", "DIRECT_PRODUCTION"}:
        return True
    return _contains(
        text,
        "소기업확인서",
        "소상공인확인서",
        "중소기업확인서",
        "소기업·소상공인 확인서",
        "소기업 또는 소상공인 확인서",
        "입찰참가자격",
        "참가자격",
    )


def _has_explicit_bidder_gate(text: str) -> bool:
    exact_terms = _contains(
        text,
        "입찰참가자격",
        "참가자격",
        "자격요건",
        "업체에 한함",
        "업체로 한정",
        "업체이어야",
        "업체여야",
        "참가할 수 없음",
        "입찰할 수 없음",
        "참가 불가",
        "입찰 불가",
        "공고일 현재",
        "마감일 현재",
        "제출마감일 현재",
        "보유한 업체",
        "등록한 업체",
        "인증 업체",
        "판정받은 업체",
        "국내 소재 업체",
        "국내 사업자",
        "국내 법인",
        "국내에 본점",
        "내국인만",
        "참가대상",
    )
    actor_gate = bool(
        re.search(
            r"(?:업체|사업자|법인|보유자|입찰자|참가자).{0,20}"
            r"(?:만\s*(?:참가|입찰|참여)?\s*가능|제한|제외|결격|참가\s*가능|참여\s*가능)",
            text,
        )
    )
    participation_gate = bool(
        re.search(
            r"(?:참가|입찰|참여).{0,16}(?:가능|제한|제외|결격|불가|금지)",
            text,
        )
    )
    capability_gate = bool(
        re.search(
            r"(?:공급|제조|납품|수행)\s*가능한\s*(?:업체|사업자|법인)",
            text,
        )
    )
    return exact_terms or actor_gate or participation_gate or capability_gate


def _is_explicit_performance_eligibility(text: str) -> bool:
    """Recognize complete bidder restrictions with direct performance ownership.

    The finite subject grammar cannot borrow possession of a registration or
    certificate from an adjacent scoring description. Complete clause endings
    keep negated/historical participation statements out of the positive gate.
    No threshold, recognition scope, or company value is inferred here.
    """

    if len(text) > 2_000 or "실적" not in text:
        return False
    subject = (
        r"실적\s*(?:(?:금액|건수|합계|누계)\s*)?"
        r"(?:\d+(?:[.,]\d+)*\s*(?:건|천만원|백만원|억원|만원|천원|원)"
        r"\s*(?:이상|초과|이하|미만)?\s*)?(?:을|를|이)?\s*"
    )
    actor = r"(?:업체|사업자|법인|자)"
    possessed = subject + r"(?:보유한|갖춘|보유하고\s*있는|있는)\s*" + actor
    duty = subject + r"(?:보유해야|보유하여야|갖추어야|있어야)\s*(?:한다|합니다|함)"
    allowed = r"(?:할\s*수\s*있(?:다|습니다|음)|\s*가능(?:하다|합니다|함)?)"
    forbidden = r"(?:할\s*수\s*없(?:다|습니다|음)|\s*(?:불가|금지)(?:하다|합니다|함)?)"
    for clause in re.split(r"[!?。;]|\.(?!\d)", text):
        clause = clause.strip()
        if "실적" not in clause:
            continue
        if re.fullmatch(
            r"(?:(?:경쟁\s*)?입찰\s*)?참가\s*자격(?:\s*요건)?\s*(?:은|는|:|으로)?"
            r".{0,180}?" + rf"(?:{possessed}|{duty})", clause,
        ):
            return True
        if re.search(
            possessed + r"\s*만\s*(?:입찰(?:에)?\s*)?"
            r"(?:참가|참여|입찰)" + allowed + r"\s*$", clause,
        ):
            return True
        if re.search(
            r"(?:입찰(?:에)?\s*참가|입찰)\s*(?:하려는|하고자\s*하는)\s*"
            + actor + r"(?:은|는)?\s*.{0,120}?" + duty + r"\s*$", clause,
        ):
            return True
        if re.search(
            subject + r"(?:없는|미보유|미충족)\s*" + actor
            + r"(?:은|는)?\s*(?:입찰(?:에)?\s*)?(?:참가|참여|입찰)"
            + forbidden + r"\s*$", clause,
        ):
            return True
    return False


def _is_descriptive_entity_clause(text: str, *, category: str) -> bool:
    """Return true for subject/contractor descriptions, not bidder criteria."""

    if category != "ENTITY":
        return False
    if _is_explicit_entity_eligibility(text) or _has_explicit_bidder_gate(text):
        return False
    named_subject = bool(
        re.fullmatch(
            r"(?:(?:용역\s*입찰의\s*)?대상\s*사업|용역명|사업명|과업명|"
            r"계약\s*범위|사업\s*범위)\s*(?:은|는|:)\s*.+"
            r"(?:이다|입니다|임)\.?",
            text,
        )
    )
    contract_scope = _contains(text, "에 관한 계약이어야", "을 위한 계약이어야")
    return (
        named_subject
        or contract_scope
        or "대상으로 하는 입찰" in text
        or bool(
            re.search(
                r"계약\s*업체(?:로|로서)\s*(?:참여|선정|수행)",
                text,
            )
        )
        or (
            "계약업체" in text
            and _contains(text, "기재되어", "로 기재", "이라고 기재")
        )
    )


def _is_product_registration_eligibility(text: str) -> bool:
    """Keep notice-specific item registration separate from generic bidder registration."""

    return "등록" in text and (
        _contains(
            text,
            "제조물품",
            "공급물품",
            "세부품명",
            "세부 품명",
            "물품분류번호",
            "물품 분류번호",
        )
        or ("나라장터" in text and "제조" in text)
    )


def _is_direct_production_certificate_eligibility(text: str) -> bool:
    return bool(re.search(r"직접\s*생산\s*확인\s*(?:증명서|서)", text))


def _is_bidder_identity_consistency(text: str) -> bool:
    return (
        _contains(text, "입찰참가등록증", "입찰참가자격등록증", "등록증상")
        and "상호" in text and "대표자" in text
        and _contains(text, "법인등기", "사업자등록증")
        and _contains(text, "변경등록", "변경 등록", "일치")
    )


def _is_domestic_bid(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(re.fullmatch(
        r"(?:본(?:건|공고|입찰)은?)?국내입찰(?:로(?:구분|진행)(?:됨|함|한다|됩니다)|이다|임)?[.]?",
        compact,
    ))


def _is_integrity_conduct_clause(text: str) -> bool:
    """Match bid-conduct obligations, not a bidder's current sanction state."""

    if _has_explicit_bidder_gate(text) or _contains(
        text,
        "참가할 수 없",
        "입찰할 수 없",
        "참가 불가",
        "입찰 불가",
        "업체에 한함",
        "업체여야",
        "자격 제한",
    ):
        return False
    integrity_terms = _contains(
        text,
        "금품·향응",
        "금품 향응",
        "금품수수",
        "취업 제공",
        "취업제공",
        "입찰가격 사전 협의",
        "담합",
        "공정한 경쟁을 방해",
        "공정경쟁을 방해",
    )
    integrity_acknowledgement = "청렴계약" in text and _contains(
        text,
        "동의",
        "승낙",
        "숙지",
        "준수",
        "서약",
    )
    return integrity_terms or integrity_acknowledgement


def _is_origin_marking_clause(text: str) -> bool:
    if _contains(
        text,
        "참가자격",
        "자격요건",
        "참가할 수 있",
        "업체에 한함",
        "업체여야",
    ):
        return False
    return _contains(text, "원산지", "제조국") and _contains(
        text,
        "표시",
        "표기",
        "기재",
    )


def _is_region_eligibility(text: str) -> bool:
    return _contains(
        text,
        "지역제한",
        "지역 제한",
        "주된 영업소",
        "법인등기부상 본점",
        "본점 소재지",
        "사업장 소재지",
    )


def _is_execution_region_information(text: str, *, category: str) -> bool:
    if (
        category != "REGION"
        or _is_region_eligibility(text)
        or _has_explicit_bidder_gate(text)
    ):
        return False
    delivery_location = _contains(
        text,
        "납품 장소",
        "납품장소",
        "설치 장소",
        "설치장소",
        "인도 장소",
        "인도장소",
        "과업 장소",
        "과업장소",
        "수행 장소",
        "수행장소",
    )
    domestic_bid = "국내입찰" in text and _contains(text, "진행", "방식", "해당")
    return delivery_location or domestic_bid


def _is_post_award_certification_action(text: str, *, category: str) -> bool:
    if category != "CERTIFICATION" or _has_explicit_bidder_gate(text):
        return False
    post_award_subject = _contains(text, "계약업체", "계약 업체", "계약상대자", "낙찰자")
    post_award_timing = _contains(
        text,
        "납품 전",
        "계약 후",
        "계약 체결 후",
        "계약체결 후",
    )
    strategic_review = "전략물자" in text and _contains(
        text,
        "전문판정",
        "자가판정",
        "판정서",
    )
    action = _contains(text, "의뢰", "제출", "신청", "받아야", "발급")
    return post_award_subject and post_award_timing and strategic_review and action


def _is_product_specification_clause(text: str, *, category: str) -> bool:
    if category != "CERTIFICATION" or _has_explicit_bidder_gate(text):
        return False
    if _contains(
        text,
        "참가자격",
        "자격요건",
        "업체에 한함",
        "업체여야",
        "업체로 한정",
        "보유한 업체",
    ):
        return False
    office_product = _contains(
        text,
        "ms 오피스",
        "microsoft office",
        "오피스 소프트웨어",
    )
    return office_product and _contains(text, "라이센스", "라이선스") and _contains(
        text,
        "정품",
        "활성화",
        "호환",
        "설치",
        "구매",
    )


def _is_explicit_entity_eligibility(text: str) -> bool:
    return _contains(
        text,
        "참가자격",
        "자격요건",
        "사업자등록",
        "법인사업자",
        "비영리법인",
        "업체에 한함",
        "업체로 한정",
        "업체이어야",
        "업체여야",
    )


def _is_bid_bond_clause(text: str) -> bool:
    """Keep bid-bond consequences out of the bidder-sanction fact.

    Notices often mention a future 부정당업자 제재 in the same sentence as
    bid-bond forfeiture or payment. That is a conditional bid rule, not proof
    that the bidder is currently free of sanctions.
    """

    return bool(re.search(r"입찰\s*보증금", text))


_STATUTORY_QUALIFICATION_AND_SANCTION = re.compile(
    r"(?P<registration>(?:지방\s*계약\s*법|지방자치단체를\s*당사자로\s*하는\s*계약에\s*관한\s*법률)"
    r"\s*시행령\s*제?\s*13조\s*[·,및]\s*시행규칙\s*제?\s*14조(?:의|에\s*따른)\s*"
    r"자격\s*요건\s*을\s*(?:구비하고|갖추고))\s*,?\s*"
    r"(?P<sanction>시행령\s*제?\s*92조\s*\(\s*부정당업자\s*(?:제재|입찰참가자격\s*제한)\s*\)\s*"
    r"(?:해당사항이\s*없는|에\s*해당하지\s*않는|에\s*해당되지\s*않는)\s*"
    r"(?:업체|자)(?:여야\s*함|이어야\s*함|여야\s*한다|이어야\s*한다))\s*[.]?"
)


def _clause_depths(text: str) -> list[int] | None:
    """Locate top-level connectives without treating parenthetical OR as AND."""
    pairs = {"(": ")", "[": "]", "{": "}", "（": "）"}
    stack: list[str] = []
    depths: list[int] = []
    for char in text:
        depths.append(len(stack))
        if char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or stack.pop() != char:
                return None
    return None if stack else depths


def _known_bidder_and_prefix(text: str) -> tuple[str, str] | None:
    """Retain an existing qualification only with directly owned positive duty.

    The established helpers select its company-fact family. A bounded positive
    predicate must immediately follow that family's actual source subject; an
    adjacent registration, scoring description, negation or submission cannot
    supply possession of a different certificate.
    """
    if "실적" in text or re.search(
        r"아니|않|없|미보유|미등록|과거|종전|이전에는|예외|면제|제출|첨부|평가|가점|배점|참고", text,
    ):
        return None
    families = [
        ("registration", "ENTITY",
         _is_bidder_registration_eligibility(text) and not _registration_mapping_is_only_a_performance_heading(text),
         r"(?:경쟁\s*)?입찰\s*참가\s*자격\s*등록(?:증)?", r"(?:완료|보유|등록)"),
        ("direct-production", "CERTIFICATION", _is_direct_production_certificate_eligibility(text),
         _DIRECT_PRODUCTION_CERT_PATTERN, r"보유"),
        ("business-certificate", "CERTIFICATION", _is_small_business_eligibility(text, category="CERTIFICATION"),
         r"(?:중소기업|소기업(?:\s*(?:또는|[·ㆍ])\s*소상공인)?|소상공인)\s*확인서", r"보유"),
    ]
    matched = [(name, category, subject, verb)
               for name, category, recognized, subject, verb in families if recognized]
    if len(matched) != 1:
        return None
    name, category, subject, verb = matched[0]
    # This grammatical tail is independent of the certificate's spelling and
    # of notice/provider identifiers. It does not infer a new company fact.
    tail = (
        r"\s*(?:[（(][^()（）]{1,160}[)）]\s*)?(?:을|를)?\s*"
        + r"(?:(?:반드시|모두|각각)\s*)*" + verb
        + r"(?:하여야|해야)?(?:\s*(?:한다|합니다|함))?"
        + r"(?:한\s*(?:업체|사업자|법인|자))?\s*$"
    )
    owned = list(re.finditer(subject, text))
    if len(owned) != 1 or not re.fullmatch(tail, text[owned[0].end():]):
        return None
    return name, category


def _performance_qualification_and_parts(
    text: str,
) -> list[tuple[str, str, str]] | None:
    """Split a proven top-level AND; preserve exact source parts and anchors."""
    depths = _clause_depths(text)
    if depths is None:
        return None
    # `소기업 또는 소상공인 확인서` names one existing fact family. An
    # alternative between complete bidder paths must never become an AND.
    certificate_alternatives = [match.span() for match in re.finditer(
        r"소기업\s*또는\s*소상공인\s*확인서", text,
    )]
    for alternative in re.finditer(r"또는|혹은|하거나|이거나|중\s*(?:하나|어느)", text):
        if depths[alternative.start()] == 0 and not any(
            start <= alternative.start() < end for start, end in certificate_alternatives
        ):
            return None
    connector_pattern = (
        r"(?:하고|하며)\s+(?!있는(?:\s|업체|사업자|법인))"
        r"|[.!?。;]\s*또한\s+"
        r"|(?P<actor>업체|사업자|법인|자)로서\s+"
        r"|\s+및\s+"
    )
    found: list[list[tuple[str, str, str]]] = []
    for connector in re.finditer(connector_pattern, text):
        if any(depths[index] != 0 for index in range(connector.start(), connector.end())):
            continue
        boundary = connector.start() + len(connector.group("actor") or "")
        before = text[:boundary].strip()
        after = text[connector.end():].strip()
        known = _known_bidder_and_prefix(before)
        if known is None or not _is_explicit_performance_eligibility(after):
            continue
        name, category = known
        found.append([(name, category, before), ("performance", "PERFORMANCE", after)])
    return found[0] if len(found) == 1 else None


def expand_statutory_qualification_requirements(
    requirements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Split only a complete recognized AND clause, keeping original evidence.

    Unknown tails, OR alternatives and ambiguous extra requirements are not
    discarded. Part conditions are exact substrings of normalized source prose.
    """
    expanded: list[dict[str, Any]] = []
    for requirement in requirements:
        text = _normalise(requirement.get("normalized_condition"))
        if (
            requirement.get("ambiguity_reason")
            or str(requirement.get("logic") or "SINGLE") not in {"SINGLE", "AND"}
        ):
            expanded.append(requirement)
            continue
        match = _STATUTORY_QUALIFICATION_AND_SANCTION.fullmatch(text)
        parts = (
            [(name, category, match.group(name))
             for name, category in (("registration", "ENTITY"), ("sanction", "SANCTION"))]
            if match is not None else
            _performance_qualification_and_parts(text)
            if bool(requirement.get("mandatory", True)) else None
        )
        if parts is None and bool(requirement.get("mandatory", True)):
            parts = _registration_and_sanction_parts(text)
        if parts is None:
            expanded.append(requirement)
            continue
        original_id = str(requirement.get("requirement_id") or "requirement")
        for part, category, condition in parts:
            digest = hashlib.sha256((original_id + "\n" + part + "\n" + condition).encode()).hexdigest()[:32]
            expanded.append({
                **requirement,
                "requirement_id": f"ai-{digest}",
                "category": category,
                "logic": "SINGLE",
                "normalized_condition": condition,
            })
    return expanded


def _is_bidder_registration_eligibility(text: str) -> bool:
    """Match registration and narrow statutory bidder-qualification clauses."""

    # A restriction clause is evidence about sanctions, not registration.
    if re.search(r"입찰\s*참가(?:\s*자격)?\s*제한", text) or "부정당" in text:
        return False

    # A requested copy of a registration certificate is a submission task.
    # Remove only that document name before looking for a separate actual
    # registration obligation in the same clause.
    if re.search(r"제출|첨부|사본|\d+\s*부", text):
        text = re.sub(r"(?:경쟁\s*)?입찰\s*참가\s*자격\s*등록증", "", text)

    # Allow particles and spacing used in live notices.
    if re.search(
        r"입찰\s*참가\s*자격\s*(?:을|를)?[^.!?]{0,24}?등록",
        text,
    ):
        return True

    if re.search(r"입찰\s*참가\s*자격\s*요건", text):
        return True
    if "경쟁입찰참가자격" in text:
        return True

    # A general statutory qualification clause is mapped only when it also says
    # that the bidder must possess the relevant qualifications. Merely citing
    # the Act (for a bond, contract term, etc.) is intentionally insufficient.
    contract_qualification_law = bool(
        re.search(
            r"(?:국가\s*를\s*당사자로\s*하는\s*계약"
            r"(?:\s*에\s*관한\s*법률)?|국가\s*계약\s*법|지방\s*계약\s*법|"
            r"지방자치단체를\s*당사자로\s*하는\s*계약에\s*관한\s*법률)",
            text,
        )
    )
    qualification_possession = bool(
        re.search(
            r"(?:입찰\s*참가\s*)?(?:자격|요건)(?:\s*요건)?\s*(?:을|를)?\s*"
            r"(?:갖춘|갖출|갖추어야|갖추고|구비한|구비하고|구비해야|구비\s*및|충족한|보유한)"
            r"|유\s*자격\s*(?:자|업체)"
            r"|(?:입찰|참가)\s*등록을\s*(?:마친|완료한|필한)",
            text,
        )
    )
    return contract_qualification_law and qualification_possession


def _registration_mapping_is_only_a_performance_heading(text: str) -> bool:
    """A generic eligibility heading cannot prove bidder registration."""
    remainder, removed = re.subn(
        r"^(?:(?:경쟁\s*)?입찰\s*)?참가\s*자격(?:\s*요건)?\s*(?:은|는|:|으로)?\s*",
        "", text, count=1,
    )
    return bool(
        removed
        and not _is_bidder_registration_eligibility(remainder)
        and not re.search(r"등록|(?:국가|지방)\s*계약|법률|시행령|시행규칙", remainder)
    )


# These are company declarations, not certificates or forecasts of performance.
_DECLARED_CLEARANCE_MESSAGES = {
    "public_dues_arrears_clear": "회사 확인값상 국세·지방세·과태료 등 체납이 없어 충족합니다.",
    "court_receivership_clear": "회사 확인값상 법정관리 이력이 없어 충족합니다.",
    "contract_nonperformance_clear": "회사 확인값상 계약 불이행 이력이 없어 충족합니다.",
}


def _declared_clearance_fact_key(text: str) -> str | None:
    """Bind only a complete, single clearance clause; never swallow other gates."""
    compact = re.sub(r"\s+", "", text).rstrip(".")
    prefix = r"(?:(?:입찰)?공고일(?:현재|기준)|입찰일(?:현재|기준)|제안서제출일기준|현재)?"
    party = r"(?:업체|사업자|자|법인)"
    ending = rf"(?:{party}(?:이어야함|여야함|일것|이어야한다)?)?"
    absent = rf"(?:이|사실이|내역이|이력이)?(?:없는{ending}|없어야(?:함|한다)|없음|없을것)"
    dues = r"(?:(?:국세|지방세|과태료|세금|제세공과금|공과금)(?:및|와|과|등|[,·ㆍ/])?)*"
    if re.fullmatch(prefix + dues + r"체납" + absent, compact):
        return "public_dues_arrears_clear"
    if re.fullmatch(prefix + "법정관리" + absent, compact) or re.fullmatch(
        prefix + r"법정관리(?:이력이없는" + ending
        + r"|중이(?:아닌" + ending + r"|아니어야함)|상태가아닌" + ending + r"|가아닌" + ending + r")",
        compact,
    ):
        return "court_receivership_clear"
    if re.fullmatch(prefix + r"계약불이행" + absent, compact):
        return "contract_nonperformance_clear"
    # A never-breached declaration covers a narrower historical exclusion, even
    # when the notice limits the customer, lookback, or means of establishing it.
    history = prefix + r"(?:최근\d+년이내)?(?:협회와(?:의|체결한)|발주기관과(?:의|체결한))?계약을"
    breach = r"(?:정당한이유없이)?(?:불이행한|이행하지않은)사실"
    proof = r"(?:(?:법원)?확정판결|중재판정|(?:협회의공식계약)?해지통보)(?:(?:[,·ㆍ]|또는)(?:(?:법원)?확정판결|중재판정|(?:협회의공식계약)?해지통보))*등으로"
    exclusion = rf"이(?:{proof})?확인된{party}는(?:입찰)?참가(?:불가|불가능|할수없음)"
    if re.fullmatch(history + breach + r"(?:이없는" + ending + r"|" + exclusion + r")", compact):
        return "contract_nonperformance_clear"
    return None


def _is_current_sanction_clearance(text: str) -> bool:
    """Match a present no-restriction condition, not a future penalty."""

    if _is_bid_bond_clause(text) or re.search(
        r"자격(?:\s*요건)?\s*(?:을|를)?\s*(?:갖추|갖춘|구비|보유|충족)", text
    ):
        return False
    sanction_context = "부정당" in text or bool(
        re.search(r"입찰\s*참가(?:\s*자격)?\s*제한", text)
    )
    clear_condition = _contains(
        text,
        "받고 있지 않",
        "받지 않",
        "해당되지 않",
        "해당하지 않",
        "해당사항이 없는",
        "지정되지 않",
        "제한되지 않",
        "제재 중이지 않",
        "부정당업자가 아닌",
        "부정당업자가 아니어야",
        "제한 대상이 아니",
        "제한 대상이 아닐",
        "제재 대상이 아니",
        "제재 대상이 아닐",
        "제한된 업체가 아니",
        "제한을 받지 아니",
        "제한 처분을 받지 아니",
        "제재를 받지 아니",
        "제한) 상태가 아닌",
        "사유가 없",
        "받은 사실이 없",
        "제재 중이 아닌",
        "제한 중이 아닌",
        "제한기간 중이 아",
        "제한기간에 있지 아니",
        "기간 중에 있지 않",
        "지정되지 아니",
    )
    return sanction_context and clear_condition


def _is_current_disqualification_clearance(text: str) -> bool:
    if not _contains(text, "결격사유", "결격 사유", "결격사항", "결격 사항"):
        return False
    return _contains(
        text,
        "해당되지 않",
        "해당하지 않",
        "없는 자",
        "없는 업체",
        "없어야",
    ) or bool(re.search(r"결격\s*(?:사유|사항)(?:이|가)?\s*있는\s*(?:업체|자)[^.]{0,30}(?:탈락|제외|불가)", text))


# --- 2026-09-30 reviewer generalizations ------------------------------------
# A reviewer decided the REVIEW conditions shown on 58 live notices. The shapes
# below turn the repeatable decisions into rules. Each one only replaces a result
# that previously fell through to an unmapped REVIEW; every mapped PASS/FAIL path
# earlier in classify_requirements keeps its decision.

# A penalty for a future breach ("적발 시 ... 제한", "처분일로부터 2년") is a
# conduct or contract duty, not the bidder's present sanction state.
_FUTURE_TRIGGER_RE = re.compile(
    r"\S\s*(?:시|경우|때)(?:에는|에도|에)?(?=[\s,(]|$)"
    r"|(?:되면|이면|하면)(?=[\s,])"
    r"|처분일\s*(?:로)?부터|위반\s*정도에\s*따라"
)
_FUTURE_CONSEQUENCE_RE = re.compile(
    r"제한|제재|취소|해지|해제|배상|위약금|조치|불이익|실격|무효|탈락|처분|부당업체|책임"
)
_PRESENT_SANCTION_MARKERS = (
    "받고 있", "받은 사실이 없", "받지 않", "받지 아니", "기간 중", "중이 아닌", "중이 아니",
    "중에 있지", "중인 업체", "중인 자", "중인 사업자", "경과한", "지정되지 아니", "지정되지 않",
    "해당하지 않", "해당되지 않", "사유가 없", "아닌 자", "아닌 업체", "아니어야", "아닐 것", "현재",
)


def _is_future_sanction_consequence(text: str) -> bool:
    if any(marker in text for marker in _PRESENT_SANCTION_MARKERS):
        return False
    return bool(_FUTURE_TRIGGER_RE.search(text) and _FUTURE_CONSEQUENCE_RE.search(text))


_AWARD_PROCEDURE_RE = re.compile(
    r"계약\s*보증금|하자\s*보수|지체\s*상금|재\s*공고|협상\s*(?:적격자|대상)|기술능력\s*평가\s*점수"
    r"|0\s*점\s*처리|확약|이의를?\s*제기|민\s*[·ㆍ]?\s*형사|임의\s*교체|교체할\s*수\s*없"
    r"|입찰\s*(?:은|의)?\s*무효|무효\s*(?:및|사유)|허위로?\s*(?:확인|작성|판명)|입증\s*(?:요구|하지)"
    r"|수의\s*계약|타인\s*명의|중복\s*배치|투입\s*인력으로\s*참여할\s*수\s*없"
)
# A present company-status exclusion still names who may not bid now.
_PRESENT_EXCLUSION_RE = re.compile(
    r"(?:업체|업자|사업자|자|기업|회사)(?:는|은)\s*(?:입찰\s*)?(?:참가|참여)\s*(?:불가|할\s*수\s*없)"
)


def _is_award_procedure_or_contract_term(text: str) -> bool:
    """Bid procedure, award, contract and post-award terms filed under SANCTION."""
    if not _AWARD_PROCEDURE_RE.search(text):
        return False
    # "부정당업자 ... 참가 불가" or a company-status exclusion is a real gate even
    # when it also mentions a contract consequence.
    return not (
        re.search(r"부정당|체납|파산|부도|법정\s*관리|화의|청산", text) and _PRESENT_EXCLUSION_RE.search(text)
    )


def _is_sanction_penalty_schedule(text: str) -> bool:
    """Penalty lengths for collusion/bribery (e.g. 주도 2년, 가담 1년), not a present gate."""
    if any(marker in text for marker in _PRESENT_SANCTION_MARKERS):
        return False
    duration = re.search(r"\d+\s*(?:개월|년)\s*(?:간|~|이상|이하|\)|$)|금액에\s*따라|정도에\s*따라", text)
    restriction = re.search(r"입찰\s*참가(?:\s*자격)?\s*제한|계약\s*제재", text)
    cause = re.search(r"담합|뇌물|금품|알선|청탁|사망|중대\s*재해|위반", text)
    return bool(duration and restriction and cause)


def _is_present_sanction_exclusion(text: str) -> bool:
    """A present exclusion of sanctioned bidders, satisfied by a clear record."""

    # "국가계약법, 지방계약법 또는 공공기관운영법" lists the statutes that may
    # have imposed the restriction; it is not an alternative qualification.
    guard_text = re.sub(r"(법률|법|규정|세칙|규칙)\s*」?\s*(?:,|또는|혹은)\s*「?", r"\1 및 ", text)
    if _is_future_sanction_consequence(text) or re.search(
        r"또는|혹은|하거나|이거나|허가|면허|인증|확인서|증명서|실적|인력|별도|추가"
        r"|자격\s*(?:요건)?\s*(?:을|를)?\s*(?:갖추|갖춘|구비|보유|충족)", guard_text,
    ):
        return False
    subject = re.search(
        r"부정당\s*업(?:자|체)|입찰\s*참가(?:\s*자격)?\s*제한(?:을|이)?\s*(?:받|중|기간|사유|대상)"
        r"|입찰\s*참가\s*자격을\s*제한\s*받|제한\s*처분(?:을|이)?\s*(?:받|중)"
        r"|제한\s*기간|제\s*76\s*조[^.]{0,20}해당하는\s*(?:기업|업체|자)|제\s*76\s*조의?\s*(?:제한|제재)",
        text,
    )
    exclusion = re.search(
        r"불가|제외|수\s*없|없는|없어야|아니|아닐|아닌|않|경과한\s*자|만료되어야|따름|의함"
        r"|제한\s*(?:됨|된다|한다)?\s*\.?$",
        text,
    )
    return bool(subject and exclusion)


def _is_contract_conduct_duty(text: str) -> bool:
    """Wage, ethics and similar duties the contractor must keep after award."""

    return "최저임금" in text or bool(
        re.search(r"(?:연구\s*윤리|보안\s*(?:정책|규정)|안전\s*수칙)[^.]{0,30}준수", text)
    )


def _is_evaluation_or_reference_rule(text: str) -> bool:
    """Evaluation thresholds and statutory pointers carry no bidder gate."""

    if re.search(r"보유|소지|등록|허가|면허|인증|증명서|확인서|실적|인력", text):
        return False
    return bool(
        re.search(r"협상\s*대상(?:자)?에서\s*제외|평가\s*점수가[^.]{0,30}미만", text)
        or re.search(r"입찰\s*무효는[^.]{0,40}(?:의함|따름|따른다)", text)
        or re.search(r"가점|우대", text)
    )


def _is_large_enterprise_software_restriction(text: str) -> bool:
    restricted = re.search(r"제한|불가|할\s*수\s*없|만\s*(?:참여|참가|입찰)\s*가능|지정되지\s*않", text)
    # Membership of a 상호출자제한기업집단 restricts on its own; plain 대기업/중견
    # limits stay software-specific because the company fact only covers those.
    return bool(restricted and (
        re.search(r"상호\s*출자\s*제한\s*기업\s*집단", text)
        or (re.search(r"대기업|중견\s*기업", text) and re.search(r"소프트웨어|\bSW\b", text))
    ))


def _is_business_registration_possession(text: str) -> bool:
    return bool(
        re.search(r"사업자\s*등록(?:증)?|고유\s*번호", text)
        and re.search(r"(?:교부|부여|발급)\s*받|보유|소지|등록한", text)
        and not re.search(r"제출|사본|첨부", text)
    )


def _is_non_restrictive_entity_statement(text: str) -> bool:
    """Wording that widens participation instead of narrowing it."""

    if re.search(r"보유|소지|등록|허가|면허|인증|실적|인력|자본금|매출|일부|특정|일정", text):
        return False
    performers = re.findall(r"기업|대학|학술\s*단체|협회|연구\s*기관|연구소", text)
    return bool(
        re.search(r"(?:기업\s*규모|자격)\s*제한\s*없이|제한\s*없이\s*(?:참가|참여)", text)
        or (len(set(performers)) >= 3 and "등" in text)
        or re.search(r"(?:법|규정)[^.]{0,20}계약\s*체결이\s*가능한\s*(?:업체|자)", text)
        or re.search(rf"{_NONPROFIT_SUBJECT}\s*(?:도|은|는)\s*(?:입찰\s*)?(?:참여|참가)\s*가능", text)
    )


# Submission of a document or a contract-time step is a checklist task. An
# earlier "…업체여야 하며" keeps the clause a qualification that only asks for
# its proof ("파트너사여야 하며 확인서 제출").
_DOCUMENT_NOUN_RE = re.compile(
    r"등록증|등본|증명서|확인서(?:류)?|서약서|확약서|동의서|인감계|신청서|제안서|현황|사본|평가서|증권|회보서|서류"
)


def _is_document_or_contract_task(text: str) -> bool:
    if re.search(r"보유|소지|취득한|등록한|인증받은", text):
        return False
    if re.search(r"자격\s*(?:이\s*)?없|참가\s*불가|입찰\s*불가|무효|실격", text):
        return False
    if re.search(r"(?<!제출하)(?<!첨부하)(?:여야|이어야)\s*(?:하며|함|하고|한다)", text):
        return False
    contract_time = re.search(r"계약\s*체결\s*(?:시|전|후)|영업\s*개시|가입\s*대상|낙찰\s*(?:후|시)", text)
    submission = re.search(r"제출|첨부", text) and _DOCUMENT_NOUN_RE.search(text)
    return bool(contract_time or submission or re.search(r"확약서를?\s*제출", text))


def _is_document_submission_list(text: str) -> bool:
    """A list of documents to file, which also names a certificate."""

    if re.search(r"자격\s*(?:이\s*)?없|참가\s*불가|입찰\s*불가|직접\s*생산", text):
        return False
    return bool(re.search(r"제출|첨부|구비\s*서류", text)) and len(
        set(_DOCUMENT_NOUN_RE.findall(text))
    ) >= 3


def _is_capability_prose(text: str) -> bool:
    """Expertise the proposal evaluation judges; no licence is named."""

    return bool(
        re.search(r"전문성을?\s*보유", text)
        and re.search(r"수행이?\s*가능한\s*(?:업체|자)", text)
        and not re.search(r"인증|자격증|등록|허가|면허|신고|지정|확인서|증명서", text)
    )


# Company-status compounds ("청산·합병·매각 ... 화의·법정관리 ... 부정당") pass
# only when every named predicate has its own confirmed company fact. One
# clearance never stands in for another.
_STATUS_COMPONENT_PATTERNS = (
    ("court_receivership_clear", r"법정\s*관리"),
    (
        "business_continuity_clear",
        r"청산|합병|매각|정리\s*절차|워크\s*아웃|work\s*-?\s*out|부도|파산|회생|화의"
        r"|금융\s*(?:신용\s*)?(?:불량|부실)|휴\s*[·ㆍ]?\s*폐업|휴업|폐업|영업\s*정지|업무\s*정지"
        r"|인허가\s*취소|등록\s*취소|행정\s*처분|당좌\s*거래\s*정지",
    ),
    ("sanction_clear", r"부정당|입찰\s*참가(?:\s*자격)?\s*(?:을|이)?\s*제한|자격이\s*제한"),
    ("public_dues_arrears_clear", r"체납"),
    ("contract_nonperformance_clear", r"계약\s*(?:을\s*)?(?:불이행|이행하지)"),
    ("conviction_clear", r"유죄\s*판결|조세\s*포탈"),
)
# Predicates no company fact represents keep the whole clause in REVIEW.
_STATUS_UNCOVERED_RE = re.compile(
    r"소송|채무\s*불이행|신용\s*등급|신용\s*평가|실적|인력|기재|허가증|면허|인증|보유|소지"
)


def _company_status_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    text: str,
    deadline: date | None,
    today: date,
) -> dict[str, Any] | None:
    if _STATUS_UNCOVERED_RE.search(text) or _is_future_sanction_consequence(text):
        return None
    # A negated predicate offered as an alternative is not an AND of
    # clearances. Positive status predicates joined to another requirement
    # likewise cannot be established by a declaration of absence.
    if re.search(r"(?:아니|없|않)[^.;。]{0,24}(?:거나|또는|혹은)", text) or re.search(
        r"(?:법정\s*관리|화의|회생|청산|파산)\s*중"
        r"(?:이며|이고|인\s*(?:업체|기업|법인)(?:이면서|이며|이고|로서))",
        text,
    ):
        return _unmapped_eligibility_item(
            requirement, fact_key="compound_company_status_qualification", deadline=deadline,
            message="선택 조건 또는 서로 다른 방향의 회사 상태 조건이 섞여 있어 개별 경로 확인이 필요합니다.",
        )
    if not re.search(r"아닌|아니|없|않|제외|불가|불허|제한|금지", text):
        return None
    keys = [key for key, pattern in _STATUS_COMPONENT_PATTERNS if re.search(pattern, text, re.I)]
    if len(keys) < 2 and keys != ["business_continuity_clear"]:
        return None
    facts = profile.get("facts", {})
    items = [
        _eligibility_item(
            requirement,
            profile=profile,
            fact_key=key,
            deadline=deadline,
            today=today,
            message="",
            fail_on_confirmed_absence=(facts.get(key) or {}).get("value") is False,
            failure_message="회사 확인값상 함께 적힌 회사 상태 조건 중 하나를 충족하지 않습니다.",
        )
        for key in keys
    ]
    failed = next((item for item in items if item["outcome"] == "FAIL_CONFIRMED"), None)
    if failed is not None:
        return failed
    if not all(item["outcome"].startswith("PASS") for item in items):
        return _unmapped_eligibility_item(
            requirement, fact_key=(
                "compound_sanction_and_financial_qualification"
                if "부정당" in text and _contains(text, "부도", "파산", "금융신용", "금융 신용")
                and not re.search(r"법정\s*관리", text)
                else "compound_company_status_qualification"
            ), deadline=deadline,
            message="함께 적힌 회사 상태 조건 중 확인되지 않은 사실이 있어 전체 조건을 충족으로 판정할 수 없습니다.",
        )
    lead = items[keys.index("business_continuity_clear") if "business_continuity_clear" in keys else 0]
    lead["message"] = (
        "프로토타입 회사 기준: " if lead.get("assessment_basis") == "PROTOTYPE_CURRENT_FACTS" else ""
    ) + (
        "함께 적힌 회사 상태 조건(" + ", ".join(keys) + ")마다 회사 확인값이 있어 모두 충족합니다."
    )
    lead["component_fact_keys"] = keys
    return lead


_REGION_CODES = (
    ("SEOUL", r"서울"), ("BUSAN", r"부산"), ("DAEGU", r"대구"), ("INCHEON", r"인천"),
    ("GWANGJU", r"광주\s*광역시|(?<!경기도\s)(?<!경기\s)광주(?!시)"), ("DAEJEON", r"대전"),
    ("ULSAN", r"울산"), ("SEJONG", r"세종"), ("GYEONGGI", r"경기"), ("GANGWON", r"강원"),
    ("CHUNGBUK", r"충청\s*북도|충북"), ("CHUNGNAM", r"충청\s*남도|충남"),
    ("JEONBUK", r"전라\s*북도|전북"), ("JEONNAM", r"전라\s*남도|전남"),
    ("GYEONGBUK", r"경상\s*북도|경북"), ("GYEONGNAM", r"경상\s*남도|경남"), ("JEJU", r"제주"),
)
_COMPANY_LOCATION_RE = re.compile(
    r"본점|본사|주된\s*영업\s*소|사업장\s*소재지|소재지가|소재한\s*(?:업체|여행사|기업|사업자|법인)"
    r"|소재\s*본점"
)
_NOT_COMPANY_LOCATION_RE = re.compile(r"시설|연수원|교육장|숙소|호텔|전국|도서|산간|납품|설치|인력\s*활용")


def _region_gate_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    text: str,
    deadline: date | None,
    today: date,
) -> dict[str, Any] | None:
    """Compare a named company-location restriction with verified locations."""

    if re.search(r"지역\s*제한\s*(?:이\s*)?없", text):
        return _information_item(
            requirement, profile=profile, capability_key=None, message="지역 제한이 없다는 안내입니다.",
        )
    if not _COMPANY_LOCATION_RE.search(text) or _NOT_COMPANY_LOCATION_RE.search(text):
        return None
    allowed = [code for code, pattern in _REGION_CODES if re.search(pattern, text)]
    if not allowed:
        return None
    branch_allowed = bool(re.search(r"지점", text)) and not re.search(r"지점\s*(?:은|는)?\s*(?:제외|불가|불인정)", text)
    head = _eligibility_item(
        requirement,
        profile=profile,
        fact_key="head_office_region_codes",
        deadline=deadline,
        today=today,
        message="공식 입찰등록증의 본점 소재지가 공고가 허용한 지역에 포함됩니다.",
        fail_on_confirmed_absence=True,
        failure_message=(
            "공식 입찰등록증의 본점 소재지가 공고가 허용한 지역에 없습니다. "
            "회사가 선언한 지점은 본점 요건에 쓰이지 않습니다."
        ),
        operator="contains_any",
        required_value=allowed,
    )
    head["required_region_codes"] = allowed
    if head["outcome"].startswith("PASS") or not branch_allowed:
        return head
    branch = _eligibility_item(
        requirement,
        profile=profile,
        fact_key="registered_bidder_branch_region_codes",
        deadline=deadline,
        today=today,
        message="공식 입찰등록증에 등록된 지점이 공고가 허용한 지역에 있습니다.",
        fail_on_confirmed_absence=True,
        failure_message=(
            "본점과 공식 등록 지점 모두 공고가 허용한 지역에 없습니다. 회사가 선언한 지점은 "
            "법인등기·입찰등록에 오르기 전까지 쓰이지 않습니다."
        ),
        operator="contains_any",
        required_value=allowed,
    )
    branch["required_region_codes"] = allowed
    if branch["outcome"].startswith("PASS"):
        return branch
    if head["outcome"] == "REVIEW":
        return head
    return branch


_TRAVEL_OR_RE = re.compile(
    r"종합\s*여행업\s*(?:또는|이나|혹은|,|/)\s*(?:국내\s*외|국외|국내외|국내)\s*여행업"
)


def _general_travel_or_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    text: str,
    deadline: date | None,
    today: date,
) -> dict[str, Any] | None:
    """종합여행업 offered as one OR alternative is met by the 종합여행업 permit."""

    if not _TRAVEL_OR_RE.search(text):
        return None
    remainder = _TRAVEL_OR_RE.sub("", text)
    if re.search(r"허가|면허|인증|확인서|증명서|직접\s*생산|중소기업|소상공인|등록증|실적|인력|부정당|제재|본점|소재|자본금|매출", remainder):
        return None
    registration_conjunct = _is_bidder_registration_eligibility(remainder) or bool(
        re.search(r"입찰\s*등록을?\s*(?:필한|완료|마친)", remainder)
    )
    if registration_conjunct:
        registration = _eligibility_item(
            requirement, profile=profile, fact_key="bidder_registration",
            deadline=deadline, today=today, message="함께 적힌 입찰등록 조건을 충족합니다.",
        )
        if not registration["outcome"].startswith("PASS"):
            return registration
    return _eligibility_item(
        requirement,
        profile=profile,
        fact_key="general_travel_business",
        deadline=deadline,
        today=today,
        message=(
            "공고는 종합여행업 또는 국내외·국외여행업 중 하나를 요구하며, 검증된 인허가 모음의 "
            "종합여행업 등록이 이를 충족합니다."
            + (" 함께 적힌 입찰참가자격 등록도 회사 확인값으로 충족합니다." if registration_conjunct else "")
        ),
    )


def _named_industry_codes(text: str, profile: dict[str, Any]) -> list[str]:
    """Registered industry names the clause lists as alternatives."""

    def key(name: str) -> str:
        return re.sub(r"[\s·ㆍ,]|(?:용역|업|서비스)$", "", name)

    inventory = (profile.get("facts", {}).get("industry_code_name_inventory") or {}).get("value") or []
    compact = re.sub(r"[\s·ㆍ]", "", text)
    return [
        str(entry.get("code"))
        for entry in inventory
        if isinstance(entry, dict) and len(key(str(entry.get("name") or ""))) >= 3
        and key(str(entry.get("name") or "")) in compact
    ]


def _industry_code_fallback_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    text: str,
    deadline: date | None,
    today: date,
) -> dict[str, Any] | None:
    codes = [
        code for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", text)
        if not re.fullmatch(r"(?:19|20)\d\d", code)
    ]
    if len(set(codes)) == 1 and "등록" in text:
        return _eligibility_item(
            requirement,
            profile=profile,
            fact_key="industry_code_inventory",
            deadline=deadline,
            today=today,
            message="공고 요구 업종코드가 공식 입찰등록 업종에 있습니다.",
            fail_on_confirmed_absence=True,
            failure_message="공식 입찰등록 업종에 공고 요구 업종코드가 없습니다.",
            operator="contains",
            required_value=codes[0],
        )
    named = _named_industry_codes(text, profile)
    if named and not re.search(r"및|모두|각각|동시|추가|별도", text) and re.search(r"등\s*관련\s*종목|중\s*하나|또는|등록된\s*업체", text):
        return _eligibility_item(
            requirement,
            profile=profile,
            fact_key="industry_code_inventory",
            deadline=deadline,
            today=today,
            message="공고가 나열한 종목 중 하나가 공식 입찰등록 업종명과 일치합니다.",
            operator="contains_any",
            required_value=sorted(set(named)),
        )
    return None


def _registration_and_sanction_parts(text: str) -> list[tuple[str, str, str]] | None:
    """Split "자격을 갖추고 … 부정당업자가 아닐 것" at the conjunction.

    Both halves are exact substrings of the clause, and each must be recognized
    on its own (a bidder-registration clause and a present sanction clearance),
    otherwise the clause stays whole.
    """

    if not ("부정당" in text or re.search(r"입찰\s*참가(?:\s*자격)?\s*제한", text)):
        return None
    if re.search(r"또는|혹은|하거나|이거나|허가|면허|인증|확인서|증명서|실적|인력|별도|추가", text):
        return None
    depths = _clause_depths(text)
    if depths is None:
        return None
    for match in re.finditer(
        r"(?:갖추고|갖춘\s*(?:자|업체)로서|구비하고|구비\s*및|(?:하|이|여|이어)며|이고)\s*,?\s*",
        text,
    ):
        if depths[match.start()] != 0:
            continue
        left, right = text[:match.end()].rstrip(" ,"), text[match.end():]
        if (
            right
            and _is_bidder_registration_eligibility(left)
            and (_is_current_sanction_clearance(right) or _is_present_sanction_exclusion(right))
        ):
            return [("registration", "ENTITY", left), ("sanction", "SANCTION", right)]
    return None


def _reviewed_fallback_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    text: str,
    category: str,
    deadline: date | None,
    today: date,
) -> dict[str, Any] | None:
    """Resolve the repeatable shapes that used to reach an unmapped REVIEW."""

    if category not in {"ENTITY", "CERTIFICATION", "INDUSTRY_CODE", "SANCTION", "REGION"}:
        return None
    facts = profile.get("facts", {})
    if _is_contract_conduct_duty(text):
        return _checklist_item(
            requirement,
            profile=profile,
            capability_key="standard_pledges",
            message="계약 후 지켜야 할 임금·윤리·보안 의무입니다. 입찰 시점 참가자격이 아니라 준수 체크리스트로 관리합니다.",
        )
    if _is_evaluation_or_reference_rule(text):
        return _information_item(
            requirement,
            profile=profile,
            capability_key=None,
            message="평가 기준·가점 또는 법령 안내이며 참가자격 조건이 아닙니다.",
        )
    if category == "REGION":
        if re.search(r"지역\s*제한\s*(?:이\s*)?없", text):
            return _information_item(
                requirement, profile=profile, capability_key=None, message="지역 제한이 없다는 안내입니다.",
            )
        return _region_gate_item(requirement, profile=profile, text=text, deadline=deadline, today=today)
    if category == "SANCTION" and _is_award_procedure_or_contract_term(text):
        return _information_item(
            requirement,
            profile=profile,
            capability_key=None,
            message="입찰·평가 절차 또는 계약 조건 안내이며 입찰 시점 회사 참가자격이 아닙니다.",
        )
    if category == "SANCTION" and _is_sanction_penalty_schedule(text):
        return _checklist_item(
            requirement,
            profile=profile,
            capability_key="integrity_pledge",
            message="담합·금품 등 위반 시 제한 기간을 정한 조항입니다. 참가자격이 아니라 준수 체크리스트로 관리합니다.",
        )
    if category == "SANCTION" and _is_future_sanction_consequence(text):
        capability = (
            "safety_health_pledge" if re.search(r"안전|보건", text)
            else "integrity_pledge" if re.search(r"청렴|금품|향응|담합|부정", text)
            else "standard_pledges"
        )
        return _checklist_item(
            requirement,
            profile=profile,
            capability_key=capability,
            message=(
                "위반·적발 시 제재나 계약 조치를 정한 조항입니다. 입찰 시점 참가자격이 아니라 "
                "준수 체크리스트로 관리합니다."
            ),
        )
    if category in {"SANCTION", "ENTITY"}:
        if _is_large_enterprise_software_restriction(text):
            return _eligibility_item(
                requirement,
                profile=profile,
                fact_key="large_enterprise_software_clear",
                deadline=deadline,
                today=today,
                message=(
                    "회사는 대기업·중견기업 소프트웨어사업자나 상호출자제한기업집단 소속이 아니어서 "
                    "이 참여 제한에 해당하지 않습니다."
                ),
                fail_on_confirmed_absence=(facts.get("large_enterprise_software_clear") or {}).get("value") is False,
                failure_message="회사 확인값상 대기업·중견기업 소프트웨어사업자 참여 제한에 해당합니다.",
            )
        status = _company_status_item(requirement, profile=profile, text=text, deadline=deadline, today=today)
        if status is not None:
            return status
        if _is_current_disqualification_clearance(text):
            return _eligibility_item(
                requirement,
                profile=profile,
                fact_key="disqualification_clear",
                deadline=deadline,
                today=today,
                message="회사 확인값상 결격사유가 없어 충족합니다.",
            )
        if _is_present_sanction_exclusion(text):
            return _eligibility_item(
                requirement,
                profile=profile,
                fact_key="sanction_clear",
                deadline=deadline,
                today=today,
                message="회사 확인값상 부정당 제재·입찰참가자격 제한 이력이 없어 충족합니다.",
            )
    if category == "ENTITY":
        if _is_business_registration_possession(text):
            return _eligibility_item(
                requirement,
                profile=profile,
                fact_key="bidder_registration",
                deadline=deadline,
                today=today,
                message="나라장터 입찰참가자격 등록(사업자등록번호 또는 고유번호 기반) 근거가 연결되어 충족합니다.",
            )
        if _is_non_restrictive_entity_statement(text):
            return _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message="참가 범위를 넓히는 안내이며 제한 조건이 아닙니다.",
            )
        if re.search(r"확약서를?\s*제출", text):
            return _checklist_item(
                requirement,
                profile=profile,
                capability_key="proposal_submission",
                message="확약서 제출은 제안 제출 체크리스트로 관리합니다.",
            )
    if category == "CERTIFICATION":
        if _is_document_or_contract_task(text):
            return _checklist_item(
                requirement,
                profile=profile,
                capability_key="proposal_submission",
                message=(
                    "증빙 서류 제출 또는 계약 시점 조치입니다. 입찰 시점 보유 자격과 분리해 "
                    "체크리스트로 관리합니다."
                ),
            )
        if _is_capability_prose(text):
            return _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message="제안 평가에서 판단하는 전문성 서술이며 보유 자격 조건이 아닙니다.",
            )
    if category == "INDUSTRY_CODE":
        return _industry_code_fallback_item(
            requirement, profile=profile, text=text, deadline=deadline, today=today,
        )
    return None


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


_CURRENT_COPY_MARKERS = (
    "유효기간 내",
    "유효기간이 남",
    "현재 유효",
    "유효한 증",
    "유효해야",
    "마감일 기준 유효",
    "제출일 기준 유효",
    "공고일 현재",
    "마감일 현재",
    "입찰마감일 현재",
    "제출마감일 현재",
    "공고일 이후 발급",
    "최근 발급",
)


def _deadline_freshness_recheck_required(
    requirement: dict[str, Any],
    *,
    fact: dict[str, Any],
    deadline: date | None,
    today: date,
) -> bool:
    """Honor each fact's explicit deadline freshness policy.

    RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY is a narrow, condition-gated
    policy: it only forces a recheck when the notice text itself demands
    current/recent documentary proof (see ``_CURRENT_COPY_MARKERS``).

    RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE and RECONFIRM_BEFORE_EACH_SUBMISSION
    are reminders to double-check the fact before submission -- they are not
    proof the fact is currently wrong. A notice's own deadline is always in
    the future relative to a fixed ``last_verified_at`` snapshot (a closed
    notice would not be evaluated at all), so comparing the deadline against
    ``last_verified_at`` treated every such fact as unconditionally stale and
    forced REVIEW on essentially every open notice. This flipped the intent
    of the policy: it should flag an *aging* verification, not a notice that
    merely closes in the future.

    We now compare ``last_verified_at`` against the evaluation clock
    (``today``) using ``_FRESHNESS_STALENESS_DAYS`` as the staleness horizon.
    ``deadline_check_required`` (see ``_eligibility_item``) still exposes a
    "recheck before submission" reminder to the UI independent of this gate.
    """

    if deadline is None:
        return False
    raw_policy = fact.get("deadline_policy")
    if not raw_policy:
        return False
    policy = str(raw_policy).upper()
    if "RECHECK" not in policy and "RECONFIRM" not in policy:
        return False
    if policy == "RECONFIRM_IF_NOTICE_REQUIRES_A_CURRENT_COPY":
        condition = _normalise(requirement.get("normalized_condition"))
        return _contains(condition, *_CURRENT_COPY_MARKERS)
    last_verified = _as_date(fact.get("last_verified_at"))
    if last_verified is None:
        return True
    return (today - last_verified).days > _FRESHNESS_STALENESS_DAYS


def _evidence_index(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["evidence_key"]): item
        for item in profile.get("evidence", [])
        if isinstance(item, dict) and item.get("evidence_key")
    }


def _public_evidence(profile: dict[str, Any], evidence_key: str | None) -> dict[str, Any] | None:
    if not evidence_key:
        return None
    item = _evidence_index(profile).get(evidence_key)
    if item is None:
        return None
    return {
        "evidence_key": item.get("evidence_key"),
        "display_name": item.get("display_name"),
        "source_file_name": item.get("source_file_name"),
        "sha256": item.get("sha256"),
        "valid_from": item.get("valid_from"),
        "last_observed_at": item.get("last_observed_at"),
        "valid_until": item.get("valid_until"),
        "validity_policy": item.get("validity_policy"),
    }


def _base_item(requirement: dict[str, Any], policy_class: PolicyClass) -> dict[str, Any]:
    return {
        "requirement_id": requirement.get("requirement_id"),
        "source_category": str(requirement.get("category") or "OTHER").upper(),
        "condition": requirement.get("normalized_condition"),
        "mandatory": bool(requirement.get("mandatory", True)),
        "policy_class": policy_class,
    }


def _eligibility_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    fact_key: str,
    deadline: date | None,
    today: date | None = None,
    pass_outcome: str = "PASS_CURRENT",
    message: str,
    fail_on_confirmed_absence: bool = False,
    failure_message: str | None = None,
    operator: str = "eq",
    required_value: Any = True,
) -> dict[str, Any]:
    item = _base_item(requirement, "ELIGIBILITY")
    fact = dict(profile.get("facts", {}).get(fact_key) or {})
    evidence = _public_evidence(profile, fact.get("evidence_key"))
    prototype = prototype_fact_enabled(profile, fact_key)
    start = _as_date(fact.get("effective_from"))
    end = _as_date(fact.get("effective_to"))
    evidence_start = _as_date(evidence.get("valid_from")) if evidence else None
    evidence_end = _as_date(evidence.get("valid_until")) if evidence else None
    deadline_policy = str(fact.get("deadline_policy") or "RECHECK_AT_DEADLINE")
    recheck_required = "RECHECK" in deadline_policy or "RECONFIRM" in deadline_policy
    freshness_recheck = _deadline_freshness_recheck_required(
        requirement,
        fact=fact,
        deadline=deadline,
        today=today or date.today(),
    )
    in_effect = deadline is None or (
        (start is None or start <= deadline)
        and (end is None or deadline <= end)
        and (evidence_start is None or evidence_start <= deadline)
        and (evidence_end is None or deadline <= evidence_end)
    )
    if prototype:
        in_effect = True
        freshness_recheck = False
    effective = (
        _policy_value_matches(fact.get("value"), operator, required_value)
        and in_effect
        and not freshness_recheck
    )
    confirmed_absence = (
        "value" in fact
        and not _policy_value_matches(fact.get("value"), operator, required_value)
        and in_effect
        and not freshness_recheck
        and fail_on_confirmed_absence
        and str(fact.get("evidence_state") or "").startswith(("COMPANY_CONFIRMED", "VERIFIED"))
    )
    item.update(
        {
            "outcome": pass_outcome if effective else "FAIL_CONFIRMED" if confirmed_absence else "REVIEW",
            "blocking": not effective,
            "company_fact_key": fact_key,
            "operator": operator,
            "required_value": required_value,
            "review_trigger_value": "__MISSING__",
            "evaluation_fact_key": (
                f"prototype.{fact_key}" if prototype and (effective or confirmed_absence)
                else f"reconfirm.{fact_key}" if prototype
                else f"reconfirm.{fact_key}" if freshness_recheck else fact_key
            ),
            "assessment_basis": "PROTOTYPE_CURRENT_FACTS" if prototype else "DEADLINE_EVIDENCE",
            "evidence_state": fact.get("evidence_state") or "MISSING",
            "evidence": evidence,
            "deadline_as_of": deadline.isoformat() if deadline else None,
            "deadline_check_required": recheck_required and not prototype,
            "message": (
                message
                if effective
                else (failure_message or "회사 확인 결과 해당 자격을 보유하지 않습니다.")
                if confirmed_absence
                else "공고 마감일 기준 유효 범위를 확인할 수 없어 검토가 필요합니다."
            ),
        }
    )
    if prototype and (effective or confirmed_absence):
        item["message"] = (
            "프로토타입 회사 기준: "
            + (message if effective else failure_message or "회사 확인 결과 해당 자격을 보유하지 않습니다.")
        )
    return item


def _unmapped_eligibility_item(
    requirement: dict[str, Any],
    *,
    fact_key: str,
    deadline: date | None,
    message: str,
) -> dict[str, Any]:
    """Represent a real bidder gate without inventing a company PASS fact."""

    item = _base_item(requirement, "ELIGIBILITY")
    item.update(
        {
            "outcome": "REVIEW",
            "blocking": True,
            "company_fact_key": fact_key,
            "evidence_state": "MISSING_OR_UNMAPPED",
            "evidence": None,
            "deadline_as_of": deadline.isoformat() if deadline else None,
            "deadline_check_required": True,
            "message": message,
        }
    )
    return item


def _independent_small_business_certificate_pass(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    text: str,
    deadline: date | None,
    today: date,
) -> dict[str, Any] | None:
    """Return the certificate PASS item when it completes an SME OR by itself.

    Within OR alternatives a complete PASS path wins, so a company certificate
    that is verified and valid at the notice deadline satisfies the clause on
    its own and the nonprofit alternative never has to be decided. Evaluation
    reuses ``_eligibility_item`` unchanged, so the fact value, effective range,
    evidence validity window and freshness recheck all keep their existing
    meaning. Anything short of that PASS -- a missing fact, an unverified one,
    a confirmed absence, or one outside the deadline window -- returns ``None``
    so the caller's nonprofit-scope handling stays exactly as before. This
    never turns a certificate absence into a FAIL: the nonprofit alternative
    may still apply, so ``fail_on_confirmed_absence`` stays off here.
    """

    item = _eligibility_item(
        requirement,
        profile=profile,
        fact_key=_small_business_fact_key(text),
        deadline=deadline,
        today=today,
        message=(
            "공고가 요구한 기업 확인서를 마감일 기준 유효하게 보유하고 있어 "
            "비영리법인 대안을 판단하지 않고도 이 조건을 충족합니다."
        ),
    )
    if item["outcome"] != "PASS_CURRENT" or item["blocking"]:
        return None
    return item


def _checklist_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    capability_key: str,
    message: str,
) -> dict[str, Any]:
    ready = bool(profile.get("capabilities", {}).get(capability_key))
    item = _base_item(requirement, "CHECKLIST")
    item.update(
        {
            "outcome": "READY" if ready else "CHECK_REQUIRED",
            "blocking": False,
            "company_fact_key": capability_key,
            "evidence_state": profile.get("capability_basis", {}).get("state", "UNCONFIRMED"),
            "evidence": None,
            "deadline_as_of": None,
            "deadline_check_required": True,
            "message": message if ready else "수행 가능 여부와 담당자를 확인해야 합니다.",
        }
    )
    return item


def _information_item(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    capability_key: str | None,
    message: str,
) -> dict[str, Any]:
    acknowledged = capability_key is None or bool(profile.get("capabilities", {}).get(capability_key))
    item = _base_item(requirement, "INFORMATION")
    item.update(
        {
            "outcome": "ACKNOWLEDGED" if acknowledged else "INFORMATION",
            "blocking": False,
            "company_fact_key": capability_key,
            "evidence_state": "NOT_REQUIRED",
            "evidence": None,
            "deadline_as_of": None,
            "deadline_check_required": False,
            "message": message,
        }
    )
    return item


def _notice_small_business_nonprofit_route(
    requirements: list[dict[str, Any]],
    normalized: list[str],
) -> str | None:
    """Return the nonprofit route the notice states for its SME-certificate duty.

    A notice often restates one certificate duty in several clauses and puts the
    nonprofit exception in only one of them. The stated route governs the
    restatements. Submission lists are not duties. Any exclusion, duty
    extension, legal-form narrowing, partial exclusion or unreadable nonprofit
    wording among the duty clauses returns None, so every restatement keeps its
    earlier REVIEW.
    """

    structures: list[str | None] = []
    for requirement, text in zip(requirements, normalized, strict=True):
        category = str(requirement.get("category") or "OTHER").upper()
        if (
            not bool(requirement.get("mandatory", True))
            or requirement.get("ambiguity_reason")
            or not _is_small_business_eligibility(text, category=category)
            or _is_document_submission_list(text)
            or not re.search(_NONPROFIT_SUBJECT, text)
        ):
            continue
        if _has_explicit_nonprofit_small_business_alternative(text):
            structures.append("UNCONDITIONAL")
        elif _has_unresolved_nonprofit_small_business_subset(text):
            # This complete legal-subset OR still has its own unresolved gate.
            # It cannot supply a stronger result to a sibling than to itself.
            structures.append(None)
        elif _NONPROFIT_PARTIAL_EXCLUSION_RE.search(text):
            structures.append(None)
        else:
            structures.append(classify_nonprofit_alternative(text))
    resolvable = {"UNCONDITIONAL", "EVIDENCE", "QUALIFIED_STATUTE"}
    if not structures or any(structure not in resolvable for structure in structures):
        return None
    if "QUALIFIED_STATUTE" in structures:
        return "QUALIFIED_STATUTE"
    return "EVIDENCE" if "EVIDENCE" in structures else "UNCONDITIONAL"


def _inherited_nonprofit_route(
    requirement: dict[str, Any],
    *,
    profile: dict[str, Any],
    route: str | None,
    deadline: date | None,
    today: date,
) -> dict[str, Any] | None:
    text = _normalise(requirement.get("normalized_condition"))
    if route is None or not _is_repeated_sme_certificate_clause(text):
        return None
    item = _nonprofit_route_for_absent_certificate(
        requirement, profile=profile, structure=route, deadline=deadline, today=today,
    )
    if item is not None:
        item["message"] = "같은 공고의 다른 조항에 적힌 비영리법인 예외를 이 확인서 조건에 적용합니다. " + item["message"]
        item["nonprofit_route_source"] = "NOTICE_SIBLING_CLAUSE"
    return item


def _is_repeated_sme_certificate_clause(text: str) -> bool:
    """Only restate a certificate duty, never waive an additional qualification."""
    if _is_unqualified_small_business_certificate_clause(text):
        return True
    if re.search(
        r"비영리|예외\s*없|반드시|별도|추가|등록|허가|면허|인증|직접\s*생산|실적|인력"
        r"|대기업|중견|소프트웨어|특별\s*법인|협동\s*조합|벤처|창업|자본금|매출|소재|시설",
        text,
    ):
        return False
    return bool(
        re.search(_SMALL_BUSINESS_CERT_PATTERN, text)
        and re.search(r"소지|보유|유효\s*기간|발급", text)
        and not re.search(r"외에|이외|그\s*밖|아울러|또한|그리고", text)
    )


_OVERALL_SEVERITY = {"PASS": 0, "PASS_CURRENT": 1, "PASS_EXCEPTION": 2, "REVIEW": 3, "FAIL": 4}


def _eligibility_card_status(item: dict[str, Any]) -> str:
    outcome = str(item.get("outcome") or "").upper()
    if outcome in {"PASS_CURRENT", "PASS_EXCEPTION"}:
        return outcome
    if outcome == "FAIL_CONFIRMED":
        return "FAIL"
    return "REVIEW"


def reconcile_eligibility_overall(
    persisted: str | None,
    display_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine the stored eligibility verdict with current-policy cards.

    Opening a notice's detail loads the current policy cards and the list badge
    is redrawn from this result, so the cards may only improve the stored
    verdict: a stored PASS is not demoted to 조건부/현재 충족 or 확인 필요 by
    policy nuance. A confirmed FAIL, stored or on a card, always wins.
    """

    stored = str(persisted or "").upper()
    stored = stored if stored in _OVERALL_SEVERITY else None
    cards = [
        _eligibility_card_status(item)
        for item in display_items
        if item.get("policy_class") == "ELIGIBILITY" and item.get("mandatory", True) is not False
    ]
    cards_status = max(cards, key=_OVERALL_SEVERITY.__getitem__) if cards else None
    if stored == "FAIL" or cards_status == "FAIL":
        status = "FAIL"
    elif cards_status is None:
        status = stored or "REVIEW"
    elif stored is not None and _OVERALL_SEVERITY[stored] < _OVERALL_SEVERITY[cards_status]:
        status = stored
    else:
        status = cards_status
    return {"status": status, "cards_status": cards_status}


def classify_requirements(
    requirements: list[dict[str, Any]],
    *,
    profile: dict[str, Any],
    deadline: date | datetime | str | None,
    evaluation_date: date | datetime | str | None = None,
) -> dict[str, Any]:
    """Classify extracted conditions without turning every clause into eligibility.

    Eligibility uses only curated public facts and retains its deadline-as-of
    recheck policy. One-off participation is a blocking action. Procedural work
    and contract facts remain checklist/information even when mandatory.

    ``evaluation_date`` is the freshness clock used to judge whether a
    RECHECK/RECONFIRM-tagged company fact is stale (see
    ``_deadline_freshness_recheck_required``). It defaults to the real
    evaluation date; tests and audits may pin it for determinism.
    """

    _assert_public_safe(profile)
    as_of = _as_date(deadline)
    today = _as_date(evaluation_date) or date.today()
    # Bind ``today`` into every eligibility-item call below without threading
    # it through each of the 13 call sites individually.
    _eligibility_item_now = functools.partial(_eligibility_item, today=today)
    requirements = expand_statutory_qualification_requirements(requirements)
    normalized = [_normalise(item.get("normalized_condition")) for item in requirements]
    # A complete SME/nonprofit OR proves no exception for another certificate
    # family. Legal-subset ORs also stay SME-scoped, even when their qualifier
    # contains the word "예외". Their company subset membership is unresolved.
    sme_alternative_clauses = {
        text for text in normalized
        if _has_explicit_nonprofit_small_business_alternative(text)
        or _has_unresolved_nonprofit_small_business_subset(text)
    }
    sme_alternative_scope_present = any(
        text in sme_alternative_clauses and bool(requirement.get("mandatory", True))
        for requirement, text in zip(requirements, normalized, strict=True)
    )
    nonprofit_exception_present = any(
        "비영리법인" in text and _contains(text, "참여 가능", "예외", "적용하지")
        and text not in sme_alternative_clauses
        for text in normalized
    )
    # The absence claim in the SME failure message is about 공고 원문 - the notice -
    # so it must be guarded by notice-wide presence, not by one clause. A
    # clause-scoped guard leaves a separate requirement denying a nonprofit
    # exception that another requirement in the same notice states verbatim.
    nonprofit_text_present_in_notice = any("비영리법인" in text for text in normalized)
    notice_sme_route = _notice_small_business_nonprofit_route(requirements, normalized)
    notice_sme_source_ids = [
        requirement.get("requirement_id")
        for requirement, text in zip(requirements, normalized, strict=True)
        if bool(requirement.get("mandatory", True))
        and not requirement.get("ambiguity_reason")
        and _is_small_business_eligibility(text, category=str(requirement.get("category") or "OTHER").upper())
        and not _is_document_submission_list(text)
        and re.search(_NONPROFIT_SUBJECT, text)
    ]
    items: list[dict[str, Any]] = []

    for requirement, text in zip(requirements, normalized, strict=True):
        category = str(requirement.get("category") or "OTHER").upper()
        declared_clearance_key = _declared_clearance_fact_key(text)
        product_registration = _is_product_registration_eligibility(text)
        direct_production_certificate = _is_direct_production_certificate_eligibility(text)
        small_business = _is_small_business_eligibility(text, category=category)
        industry_codes = _required_industry_codes(text, category=category)
        vocational_training_scope = _vocational_training_scope_requirement(
            text,
            category=category,
        )
        named_permit_fact_keys = _named_permit_fact_keys(text, category=category)
        named_permit_fact_key = _named_permit_fact_key(text, category=category)
        ambiguous_named_permit = bool(named_permit_fact_keys) and named_permit_fact_key is None
        unrepresented_named_permits = _unrepresented_named_permit_fact_keys(
            named_permit_fact_keys,
            industry_codes=industry_codes,
        )
        notice_specific_families = sum(
            (
                product_registration,
                direct_production_certificate,
                small_business,
                bool(industry_codes),
                vocational_training_scope is not None,
                bool(unrepresented_named_permits),
            )
        )

        if notice_specific_families > 1 and _has_explicit_nonprofit_compound_exception(text):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="nonprofit_entity",
                deadline=as_of,
                pass_outcome="PASS_EXCEPTION",
                message=(
                    "공고가 직접생산·기업확인 조건 모두에 명시한 비영리법인 예외와 "
                    "설립허가 근거가 연결되었습니다."
                ),
            )
        elif notice_specific_families > 1:
            item = _unmapped_eligibility_item(
                requirement,
                fact_key="compound_notice_specific_qualification",
                deadline=as_of,
                message=(
                    "물품 등록·직접생산·기업 확인이 한 조건에 함께 있어 각각의 최신 증빙을 "
                    "분리 확인해야 합니다. 일반 입찰자격등록 사실로 대신 PASS하지 않습니다."
                ),
            )
        elif product_registration:
            item = _unmapped_eligibility_item(
                requirement,
                fact_key="notice_specific_product_registration",
                deadline=as_of,
                message=(
                    "공고가 지정한 세부품명·제조물품 등록 여부를 별도 확인해야 합니다. "
                    "일반 경쟁입찰참가자격등록증만으로 충족 처리하지 않습니다."
                ),
            )
        elif direct_production_certificate:
            if _has_explicit_nonprofit_direct_production_exception(text):
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key="nonprofit_entity",
                    deadline=as_of,
                    pass_outcome="PASS_EXCEPTION",
                    message="직접생산 조건의 명시적 비영리법인 예외와 설립허가 근거가 연결되었습니다.",
                )
            elif nonprofit_exception_present:
                item = _unmapped_eligibility_item(
                    requirement,
                    fact_key="direct_production_nonprofit_exception_scope",
                    deadline=as_of,
                    message=(
                        "비영리법인 예외 문구는 있으나 직접생산 조건에도 적용되는지 범위를 확정할 수 없어 "
                        "원문 검토가 필요합니다."
                    ),
                )
            else:
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key="direct_production_certificate",
                    deadline=as_of,
                    message="공고 지정 품목의 직접생산확인증명서 보유 사실이 연결되었습니다.",
                    fail_on_confirmed_absence=True,
                    failure_message=(
                        "회사 확인값상 직접생산확인증명서를 보유하지 않아 이 필수조건은 충족할 수 없습니다. "
                        "취득 시 회사 사실을 갱신한 뒤 재분석해야 합니다."
                    ),
                )
        elif industry_codes:
            operator = _industry_code_operator(text, industry_codes)
            if operator is None:
                item = _unmapped_eligibility_item(
                    requirement,
                    fact_key="ambiguous_industry_code_logic",
                    deadline=as_of,
                    message="슬래시로 연결된 복수 업종코드의 AND/OR 관계를 확정할 수 없어 원문 검토가 필요합니다.",
                )
            else:
                required_value: Any = industry_codes[0] if operator == "contains" else industry_codes
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key="industry_code_inventory",
                    deadline=as_of,
                    message="공고 요구 업종코드와 공식 입찰등록 전체 스냅샷이 일치합니다.",
                    fail_on_confirmed_absence=True,
                    failure_message=(
                        "공식 입찰등록 전체 스냅샷에 공고 필수 업종코드가 없어 참가자격을 충족할 수 없습니다. "
                        "업종 추가 등록 후 회사 사실을 갱신해야 합니다."
                    ),
                    operator=operator,
                    required_value=required_value,
                )
            item["required_industry_codes"] = industry_codes
        elif vocational_training_scope is not None:
            fact_key, operator, required_value = vocational_training_scope
            if fact_key is None or operator is None:
                item = _unmapped_eligibility_item(
                    requirement,
                    fact_key="vocational_training_scope_unparsed",
                    deadline=as_of,
                    message="지정직업훈련시설의 요구 NCS 직종 범위를 확정할 수 없어 원문 검토가 필요합니다.",
                )
            else:
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key=fact_key,
                    deadline=as_of,
                    message="공고 요구 NCS 훈련직종과 지정직업훈련시설 증빙 범위가 일치합니다.",
                    fail_on_confirmed_absence=True,
                    failure_message="지정직업훈련시설 증빙의 12개 NCS 세분류에 공고 요구 직종이 없습니다.",
                    operator=operator,
                    required_value=required_value,
                )
        elif (
            travel_item := _general_travel_or_item(
                requirement, profile=profile, text=text, deadline=as_of, today=today,
            )
        ) is not None:
            item = travel_item
        elif ambiguous_named_permit:
            item = _unmapped_eligibility_item(
                requirement,
                fact_key="compound_named_permit_qualification",
                deadline=as_of,
                message=(
                    "복수 인허가 또는 서로 다른 등록 조건이 한 문장에 함께 있어 각각의 충족 여부를 "
                    "분리 확인해야 합니다. 한 건의 인허가 사실로 전체 조건을 PASS하지 않습니다."
                ),
            )
        elif named_permit_fact_key is not None:
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key=named_permit_fact_key,
                deadline=as_of,
                message="공고가 요구한 인허가와 검증된 인허가 모음의 회사 사실이 일치합니다.",
            )
        elif _contains(text, "제안설명회") and _contains(text, "참여", "불참"):
            item = _base_item(requirement, "ACTION_REQUIRED")
            item.update(
                {
                    "outcome": "BLOCK_UNTIL_CONFIRMED",
                    "blocking": True,
                    "company_fact_key": None,
                    "evidence_state": "ATTENDANCE_UNCONFIRMED",
                    "evidence": None,
                    "deadline_as_of": as_of.isoformat() if as_of else None,
                    "deadline_check_required": True,
                    "message": "제안설명회 참석 기록이 확인되기 전에는 행동 필요·BLOCK이며 불참이면 입찰 진행을 중단합니다.",
                }
            )
        elif _is_bid_bond_clause(text):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message=(
                    "입찰보증금 면제·납부·귀속 및 조건부 제재에 관한 안내입니다. "
                    "현재 부정당 제재 여부의 PASS 근거로 사용하지 않습니다."
                ),
            )
        elif declared_clearance_key:
            item = _eligibility_item_now(
                requirement, profile=profile, fact_key=declared_clearance_key,
                deadline=as_of,
                fail_on_confirmed_absence=(
                    (profile.get("facts", {}).get(declared_clearance_key) or {}).get("value") is False
                ),
                message=_DECLARED_CLEARANCE_MESSAGES[declared_clearance_key],
                failure_message="회사 확인값상 해당 제한 사유 없음 조건을 충족하지 않습니다.",
            )
        elif sum(bool(re.search(pattern, text, re.I)) for _, pattern in _STATUS_COMPONENT_PATTERNS) >= 2:
            item = _company_status_item(
                requirement, profile=profile, text=text, deadline=as_of, today=today,
            ) or _unmapped_eligibility_item(
                requirement, fact_key="compound_company_status_qualification", deadline=as_of,
                message="체납·법정관리·계약 불이행 없음 확인만으로 복합 조건 전체를 충족할 수 없어, 함께 적힌 조건의 관계를 추가 확인해야 합니다.",
            )
        elif prototype_fact_enabled(profile, "bidder_identity_consistent") and _is_bidder_identity_consistency(text):
            item = _eligibility_item_now(
                requirement, profile=profile, fact_key="bidder_identity_consistent",
                deadline=as_of, fail_on_confirmed_absence=True,
                message="상호·대표자 등록정보를 일치 상태로 유지한다는 회사 확인값에 따라 충족합니다.",
            )
        elif prototype_fact_enabled(profile, "domestic_entity") and _is_domestic_bid(text):
            item = _eligibility_item_now(
                requirement, profile=profile, fact_key="domestic_entity",
                deadline=as_of, fail_on_confirmed_absence=True,
                message="국내 법인이라는 회사 확인값에 따라 국내입찰 조건을 충족합니다.",
            )
        elif _is_bidder_registration_eligibility(text):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="bidder_registration",
                deadline=as_of,
                message="경쟁입찰참가자격 등록 보유 근거가 연결되어 충족합니다.",
            )
        elif _is_descriptive_entity_clause(text, category=category):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message="입찰 대상 또는 기재된 계약업체에 대한 설명이며 회사 참가자격 조건으로 사용하지 않습니다.",
            )
        elif _is_execution_region_information(text, category=category):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message="납품·설치 장소 또는 입찰 범위 정보이며 업체 소재지 참가제한으로 사용하지 않습니다.",
            )
        elif "부정당" in text and _contains(text, "부도", "파산", "금융신용", "금융 신용"):
            item = _company_status_item(
                requirement, profile=profile, text=text, deadline=as_of, today=today,
            ) or _unmapped_eligibility_item(
                requirement,
                fact_key="compound_sanction_and_financial_qualification",
                deadline=as_of,
                message=(
                    "부정당 제재 여부와 부도·파산·금융신용 조건을 각각 확인해야 합니다. "
                    "제재가 없다는 회사 사실만으로 재무 관련 조건까지 충족한 것으로 판단하지 않습니다."
                ),
            )
        elif _is_current_sanction_clearance(text):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="sanction_clear",
                deadline=as_of,
                message="회사 확인값상 부정당 제재 이력이 없어 충족합니다.",
            )
        elif _is_current_disqualification_clearance(text):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="disqualification_clear",
                deadline=as_of,
                message="회사 확인값상 결격사유가 없어 충족합니다.",
            )
        elif _contains(text, "유죄판결", "조세포탈") and not _contains(text, "서약서"):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="conviction_clear",
                deadline=as_of,
                message="회사 확인값상 조세포탈 등 유죄판결 이력이 없어 충족합니다.",
            )
        elif _is_integrity_conduct_clause(text):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="integrity_pledge",
                message=(
                    "금품수수·담합 금지 등 입찰 행동규범입니다. 현재 참가자격 증빙과 "
                    "분리하고 청렴 준수 체크리스트로 관리합니다."
                ),
            )
        elif _is_origin_marking_clause(text):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message="제품 원산지 표시 의무이며 업체 소재지 참가제한으로 사용하지 않습니다.",
            )
        elif _is_post_award_certification_action(text, category=category):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="proposal_submission",
                message="계약 후 전문판정·증명 신청 절차이며 입찰 전 보유 자격과 분리해 일정 체크리스트로 관리합니다.",
            )
        elif _is_product_specification_clause(text, category=category):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="proposal_submission",
                message="납품할 소프트웨어의 정품·활성화·호환 사양이며 회사 보유 자격으로 사용하지 않습니다.",
            )
        elif small_business and _is_document_submission_list(text):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="proposal_submission",
                message=(
                    "기업 확인서가 포함된 제출서류 목록입니다. 확인서 보유 요건은 같은 공고의 "
                    "자격 조항에서 따로 판정합니다."
                ),
            )
        elif small_business:
            # Both SME/nonprofit OR shapes below offer the certificate as their
            # own first alternative, so an independently satisfied certificate
            # completes the clause and the nonprofit branch is never reached.
            # Without this the generic alternative fell to REVIEW on a false
            # nonprofit fact and the legal-subset alternative stayed
            # unconditionally unresolved, even though the same certificate
            # passes the plain SME clause. Scope stays on these two OR shapes:
            # the unresolved-scope branches further down keep their REVIEW.
            generic_nonprofit_alternative = _has_explicit_nonprofit_small_business_exception(
                text,
                category=category,
            )
            legal_subset_alternative = _has_unresolved_nonprofit_small_business_subset(text)
            independent_certificate_pass = (
                _independent_small_business_certificate_pass(
                    requirement,
                    profile=profile,
                    text=text,
                    deadline=as_of,
                    today=today,
                )
                if generic_nonprofit_alternative or legal_subset_alternative
                or (notice_sme_route is not None and _is_repeated_sme_certificate_clause(text))
                else None
            )
            if independent_certificate_pass is not None:
                item = independent_certificate_pass
            elif generic_nonprofit_alternative:
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key="nonprofit_entity",
                    deadline=as_of,
                    pass_outcome="PASS_EXCEPTION",
                    message="공고의 비영리법인 예외 경로와 설립허가 근거가 연결되어 소기업 확인서 조건을 대체합니다.",
                )
            elif legal_subset_alternative:
                item = _unmapped_eligibility_item(
                    requirement,
                    fact_key="small_business_nonprofit_legal_subset",
                    deadline=as_of,
                    message=(
                        "이 조건은 법령상 일부 비영리법인을 대안으로 허용하지만 회사가 그 범위에 "
                        "해당하는지 근거가 연결되지 않았습니다. 일반 비영리법인 사실이나 기업확인서 "
                        "미보유만으로 충족·미충족을 확정할 수 없어 검토가 필요합니다."
                    ),
                )
            elif sme_alternative_scope_present and _is_unqualified_small_business_certificate_clause(text):
                item = _inherited_nonprofit_route(
                    requirement, profile=profile, route=notice_sme_route, deadline=as_of, today=today,
                ) or _unmapped_eligibility_item(
                    requirement,
                    fact_key="small_business_nonprofit_exception_scope",
                    deadline=as_of,
                    message=(
                        "같은 기업확인서 계열에 비영리법인 대안이 있으나 이 보유·유효기간 조건에도 "
                        "적용되는지 범위가 연결되지 않아 원문 검토가 필요합니다."
                    ),
                )
            elif nonprofit_exception_present and _is_repeated_sme_certificate_clause(text):
                # A clause that names the nonprofit itself is decided by its own
                # structure below; only a restatement looks at the notice.
                item = _inherited_nonprofit_route(
                    requirement, profile=profile, route=notice_sme_route, deadline=as_of, today=today,
                ) or _unmapped_eligibility_item(
                    requirement,
                    fact_key="small_business_nonprofit_exception_scope",
                    deadline=as_of,
                    message=(
                        "비영리법인 예외 문구는 있으나 이 중소·소기업 확인서 조건에도 적용되는지 "
                        "범위를 확정할 수 없어 원문 검토가 필요합니다."
                    ),
                )
            else:
                certificate_fact_key = _small_business_fact_key(text)
                nonprofit_structure = classify_nonprofit_alternative(text)
                # Do not assert that the notice contains no nonprofit exception
                # when the clause mentions one. The outcome stays the same
                # confirmed-absence FAIL; only the stated reason changes, so the
                # explanation never claims a fact about the source text that the
                # text contradicts.
                if nonprofit_structure == "EXCLUDED":
                    certificate_failure_message = (
                        "회사 확인값상 공고가 요구한 중소·소기업 또는 소상공인 확인서를 보유하지 "
                        "않으며, 공고가 비영리법인을 명시적으로 제외하거나 참가 불가로 규정하여 "
                        "예외 경로가 없습니다."
                    )
                elif nonprofit_structure == "DUTY_EXTENDED":
                    certificate_failure_message = (
                        "회사 확인값상 공고가 요구한 중소·소기업 또는 소상공인 확인서를 보유하지 "
                        "않으며, 공고가 비영리법인에게도 같은 확인서 보유 의무를 부과하므로 "
                        "예외 경로가 없습니다."
                    )
                elif nonprofit_text_present_in_notice:
                    certificate_failure_message = (
                        "회사 확인값상 공고가 요구한 중소·소기업 또는 소상공인 확인서를 보유하지 "
                        "않습니다. 공고의 다른 항목에 비영리법인 문구가 있으나 이 조건에 적용되는 "
                        "예외로 확정할 수 없으므로 공고 원문 검토가 필요합니다."
                    )
                else:
                    certificate_failure_message = (
                        "회사 확인값상 공고가 요구한 중소·소기업 또는 소상공인 확인서를 보유하지 않으며, "
                        "공고 원문에도 비영리법인 예외가 없어 필수조건을 충족할 수 없습니다."
                    )
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key=certificate_fact_key,
                    deadline=as_of,
                    message="공고가 요구한 기업 확인서 보유 사실이 연결되었습니다.",
                    fail_on_confirmed_absence=True,
                    failure_message=certificate_failure_message,
                )
                if item.get("outcome") == "FAIL_CONFIRMED":
                    # The certificate is confirmed absent, but the same clause
                    # may still name a nonprofit route. Decide that route by the
                    # structure that binds it; only a confirmed absence is
                    # replaced, so no earlier PASS or REVIEW is disturbed.
                    rescued = _nonprofit_route_for_absent_certificate(
                        requirement,
                        profile=profile,
                        structure=nonprofit_structure,
                        deadline=as_of,
                        today=today,
                    ) if nonprofit_structure is not None else _inherited_nonprofit_route(
                        requirement,
                        profile=profile,
                        route=notice_sme_route,
                        deadline=as_of,
                        today=today,
                    )
                    if rescued is not None:
                        item = rescued
        elif _contains(text, "하도급", "단독입찰"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="subcontracting_restriction_acknowledged",
                message="단독 수행·하도급 금지 조건을 수행계획 체크리스트로 확인합니다. 참가자격 REVIEW 항목은 아닙니다.",
            )
        elif _contains(text, "서약서"):
            capability = (
                "integrity_pledge"
                if "청렴" in text
                else "safety_health_pledge"
                if "안전보건" in text
                else "standard_pledges"
            )
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key=capability,
                message="표준 서약서 제출 가능 상태입니다. 실제 제출·서명 완료 여부만 일정에 맞춰 확인합니다.",
            )
        elif _contains(text, "직접 방문", "방문접수"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="direct_visit_submission",
                message="직접 방문 제출이 가능하며 담당자·방문시간·접수완료만 체크합니다.",
            )
        elif _contains(text, "날인된 공문"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="sealed_cover_letter",
                message="날인 공문 준비가 가능하며 제출본 완성 여부만 체크합니다.",
            )
        elif _contains(text, "제출기한 내 제안서"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="proposal_submission",
                message="제안서 제출은 수행 가능한 기본 절차이며 마감 전 접수 완료만 체크합니다.",
            )
        elif _contains(text, "질의") and _contains(text, "문서"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="written_question_submission",
                message="문서 질의 방식과 질의기한을 체크합니다.",
            )
        elif _contains(text, "사업책임자") and _contains(text, "발표"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="pm_presentation",
                message="PM 직접 발표가 가능하며 발표자 지정과 참석 가능 여부를 체크합니다.",
            )
        elif _is_two_person_attendee_limit(text, category=category):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="attendee_limit_two",
                message="발표자 포함 참석자 2인 제한을 참석계획 체크리스트에 반영합니다.",
            )
        elif _contains(text, "재직증명서", "고용보험가입", "신분증명자료"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="attendee_documents",
                message="발표자·참석자 증빙서류 구비 여부를 체크합니다.",
            )
        elif _contains(text, "산출내역"):
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="cost_breakdown",
                message="세부 산출내역서와 산출근거 작성·제출 시점을 체크합니다.",
            )
        elif _contains(text, "전자입찰", "전자입찰서", "나라장터 전자"):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key="electronic_bidding",
                message="나라장터 전자입찰 수행이 가능한 기본 절차로 정보 표시합니다.",
            )
        elif _contains(text, "총액"):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key="total_price_submission",
                message="부가가치세 포함 총액 제출 방식으로 정보 표시합니다.",
            )
        elif _contains(text, "용역기간", "계약체결일부터", "계약 체결일"):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key="contract_schedule_review",
                message="계약일과 용역 종료일을 일정 정보로 표시합니다.",
            )
        elif category == "REGION" and _is_region_eligibility(text):
            if _is_seoul_head_office_gate(text, category=category):
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key="head_office_region_seoul",
                    deadline=as_of,
                    message="공식 입찰등록증의 서울 본점 지역 사실이 공고 제한과 일치합니다.",
                )
            else:
                item = _region_gate_item(
                    requirement, profile=profile, text=text, deadline=as_of, today=today,
                ) or _unmapped_eligibility_item(
                    requirement,
                    fact_key="notice_region_eligibility",
                    deadline=as_of,
                    message=(
                        "본점·사업장 소재지 등 공고별 지역제한 근거를 추가로 연결해야 합니다. "
                        "회사 선언 지점은 공식 지사 증빙 전까지 자동 PASS에 사용하지 않습니다."
                    ),
                )
        elif (
            fallback_item := _reviewed_fallback_item(
                requirement, profile=profile, text=text, category=category, deadline=as_of, today=today,
            )
        ) is not None:
            item = fallback_item
        elif category == "ENTITY" and _is_explicit_entity_eligibility(text):
            item = _unmapped_eligibility_item(
                requirement,
                fact_key="notice_entity_eligibility",
                deadline=as_of,
                message="공고가 요구한 법인·사업자 유형에 대응하는 공개 프로필 근거를 연결해야 합니다.",
            )
        elif category in {"CERTIFICATION", "INDUSTRY_CODE"}:
            item = _unmapped_eligibility_item(
                requirement,
                fact_key=f"notice_{category.casefold()}_eligibility",
                deadline=as_of,
                message="공고별 자격 조건에 대응하는 공개 프로필 근거를 추가로 연결해야 합니다.",
            )
        elif category == "SANCTION":
            item = _unmapped_eligibility_item(
                requirement,
                fact_key="notice_sanction_eligibility",
                deadline=as_of,
                message="제재·결격 조건의 현재 충족 여부를 추가 확인해야 합니다.",
            )
        elif category == "REGION":
            item = _unmapped_eligibility_item(
                requirement,
                fact_key="notice_region_eligibility",
                deadline=as_of,
                message="지역 관련 문구가 참가제한인지 추가 확인해야 합니다.",
            )
        elif category == "ENTITY":
            item = _unmapped_eligibility_item(
                requirement,
                fact_key="notice_entity_eligibility",
                deadline=as_of,
                message="법인·사업자 유형 등 참가자격 여부를 추가 확인해야 합니다.",
            )
        elif category in {"SUBMISSION", "PERSONNEL", "CONSORTIUM"}:
            item = _checklist_item(
                requirement,
                profile=profile,
                capability_key="proposal_submission",
                message="입찰 수행 체크리스트로 관리하며 완료 누락 시에만 담당자 조치가 필요합니다.",
            )
        else:
            item = _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message="참가자격과 분리된 공고 정보로 표시합니다.",
            )
        if bool(requirement.get("mandatory", True)) and _is_explicit_performance_eligibility(text):
            heading_only_registration = (
                item.get("company_fact_key") == "bidder_registration"
                and _registration_mapping_is_only_a_performance_heading(text)
            )
            if item.get("evaluation_fact_key") and not heading_only_registration:
                # An unsupported relationship must not replace an existing
                # mapped condition, including a DB-only explicit FALSE. Only
                # the proven AND expander may add a new performance atom.
                item["performance_relation_unresolved"] = True
                item["message"] += (
                    " 함께 적힌 실적 조건의 관계·인정범위는 아직 분리 검증되지 않았으며, "
                    "기존 자격 판정은 그 실적 조건의 충족을 증명하지 않습니다."
                )
            else:
                item = _unmapped_eligibility_item(
                    requirement,
                    fact_key="notice_performance_eligibility",
                    deadline=as_of,
                    message=(
                        "원문은 실적을 필수 입찰참가 조건으로 요구합니다. 인정기간·유사범위·"
                        "완료·금액/VAT·증빙 조건과 검증 실적대장의 연결이 아직 없어 검토가 "
                        "필요합니다. 등록된 실적 총수나 일반 회사 사실로 충족을 대신하지 않습니다."
                    ),
                )
                # The untrusted provider ID cannot select a company-fact key. A
                # separate source-scoped binding must exist before this pending gate
                # may consume any company fact; the marker remains explicit.
                condition_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                item["evaluation_fact_key"] = f"eligibility.performance.unbound.{condition_digest[:32]}"
                item["requires_performance_scope_binding"] = True
        if item.get("nonprofit_route_source") == "NOTICE_SIBLING_CLAUSE":
            item["nonprofit_route_requirement_ids"] = list(notice_sme_source_ids)
        items.append(item)

    display_items = group_equivalent_policy_items(items)
    counts = Counter(item["policy_class"] for item in display_items)
    groups = {
        key: [item for item in display_items if item["policy_class"] == key]
        for key in ("ELIGIBILITY", "ACTION_REQUIRED", "CHECKLIST", "INFORMATION")
    }
    return {
        "policy_version": POLICY_VERSION,
        "profile_version": profile.get("profile_version"),
        "profile_classification": profile.get("classification"),
        "assessment_mode": profile.get("eligibility_assessment_mode", "DEADLINE_EVIDENCE"),
        "deadline_as_of": as_of.isoformat() if as_of else None,
        "counts": {key: counts.get(key, 0) for key in groups},
        "blocking_actions": sum(
            item["policy_class"] == "ACTION_REQUIRED" and bool(item["blocking"])
            for item in items
        ),
        "blocking_items": sum(bool(item["blocking"]) for item in items),
        "items": items,
        "display_items": display_items,
        "duplicate_count": len(items) - len(display_items),
        "verdict_counts": dict(Counter(
            "P" if item["outcome"].startswith("PASS") else "F" if item["outcome"] == "FAIL_CONFIRMED" else "R"
            for item in display_items if item["policy_class"] == "ELIGIBILITY"
        )),
        "groups": groups,
        "decision_boundary": (
            ("현재 회사 확인값으로 점검하는 프로토타입입니다. " if profile.get("eligibility_assessment_mode") == "PROTOTYPE_CURRENT_FACTS" else "공고 마감일 기준 증빙으로 점검합니다. ")
            + "참가 자격은 P 충족 / F 미충족 / R 근거 부족·예외 범위 확인으로 표시합니다. "
            "행동필요는 완료 전 BLOCK, "
            "체크리스트와 정보는 그 자체로 참가자격 REVIEW를 만들지 않습니다."
        ),
    }


def group_equivalent_policy_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group only equivalent known gates for display; preserve every source row.

    Evaluation still consumes `items` one-to-one. Different codes, verdicts,
    exceptions, mandatory flags and unresolved compound gates never collapse.
    """
    groups: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        fact_key = item.get("company_fact_key")
        groupable = (
            item.get("policy_class") == "ELIGIBILITY"
            and fact_key in PROTOTYPE_FACT_KEYS - {"nonprofit_entity", "disqualification_clear"}
            and item.get("outcome") != "REVIEW"
            and not item.get("performance_relation_unresolved")
        )
        key = json.dumps({
            "fact": fact_key, "operator": item.get("operator"),
            "required": item.get("required_value"), "outcome": item.get("outcome"),
            "mandatory": item.get("mandatory"),
            "component_facts": item.get("component_fact_keys"),
            "products": sorted(re.findall(r"(?<!\d)\d{10}(?!\d)", str(item.get("condition")))),
        }, ensure_ascii=False, sort_keys=True) if groupable else f"row:{index}"
        if key not in groups:
            groups[key] = {**copy.deepcopy(item), "source_requirement_ids": [], "source_conditions": []}
        group = groups[key]
        group["source_requirement_ids"].append(item.get("requirement_id"))
        if item.get("condition") not in group["source_conditions"]:
            group["source_conditions"].append(item.get("condition"))
        group["duplicate_count"] = len(group["source_requirement_ids"]) - 1
    return list(groups.values())
