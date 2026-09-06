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
POLICY_VERSION = "pai-loop-requirement-policy-2026.09.06-v9"

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
        category == "INDUSTRY_CODE"
        and _contains(text, "등록", "업종")
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
            or _has_scoped_nonprofit_exception(
                text,
                subject_pattern=_SMALL_BUSINESS_CERT_PATTERN,
            )
        )
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

    if not _contains(text, "중소기업", "소기업", "소상공인"):
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
    return _contains(
        text,
        "직접생산확인증명서",
        "직접생산 확인증명서",
        "직접생산확인서",
        "직접생산 확인서",
    )


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
        match = _STATUTORY_QUALIFICATION_AND_SANCTION.fullmatch(text)
        if (
            match is None
            or requirement.get("ambiguity_reason")
            or str(requirement.get("logic") or "SINGLE") not in {"SINGLE", "AND"}
        ):
            expanded.append(requirement)
            continue
        original_id = str(requirement.get("requirement_id") or "requirement")
        for part, category in (("registration", "ENTITY"), ("sanction", "SANCTION")):
            condition = match.group(part)
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
            r"(?:입찰\s*참가\s*)?자격(?:\s*요건)?\s*(?:을|를)?\s*"
            r"(?:갖춘|갖출|갖추어야|갖추고|구비한|구비하고|충족한|보유한)",
            text,
        )
    )
    return contract_qualification_law and qualification_possession


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
    )
    return sanction_context and clear_condition


def _is_current_disqualification_clearance(text: str) -> bool:
    if not _contains(text, "결격사유", "결격 사유"):
        return False
    return _contains(
        text,
        "해당되지 않",
        "해당하지 않",
        "없는 자",
        "없는 업체",
        "없어야",
    )


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
            "evaluation_fact_key": f"reconfirm.{fact_key}" if freshness_recheck else fact_key,
            "evidence_state": fact.get("evidence_state") or "MISSING",
            "evidence": evidence,
            "deadline_as_of": deadline.isoformat() if deadline else None,
            "deadline_check_required": recheck_required,
            "message": (
                message
                if effective
                else (failure_message or "회사 확인 결과 해당 자격을 보유하지 않습니다.")
                if confirmed_absence
                else "공고 마감일 기준 유효 범위를 확인할 수 없어 검토가 필요합니다."
            ),
        }
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
    nonprofit_exception_present = any(
        "비영리법인" in text and _contains(text, "참여 가능", "예외", "적용하지")
        for text in normalized
    )
    items: list[dict[str, Any]] = []

    for requirement, text in zip(requirements, normalized, strict=True):
        category = str(requirement.get("category") or "OTHER").upper()
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
        elif _is_bidder_registration_eligibility(text):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="bidder_registration",
                deadline=as_of,
                message="경쟁입찰참가자격 등록 보유 근거가 연결되었습니다. 마감일에는 나라장터 상태를 다시 확인합니다.",
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
            item = _unmapped_eligibility_item(
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
                message="현재 확인된 부정당 제재 사례가 없어 PASS 상태이며 마감일 기준 동적 조회를 유지합니다.",
            )
        elif _is_current_disqualification_clearance(text):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="disqualification_clear",
                deadline=as_of,
                message="현재 회사 확인값상 결격사유가 없으며 제출 전 다시 확인합니다.",
            )
        elif _contains(text, "유죄판결", "조세포탈") and not _contains(text, "서약서"):
            item = _eligibility_item_now(
                requirement,
                profile=profile,
                fact_key="conviction_clear",
                deadline=as_of,
                message="현재 확인된 유죄판결 사례가 없어 PASS 상태이며 제출 전 재확인합니다.",
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
        elif small_business:
            if _has_explicit_nonprofit_small_business_exception(
                text,
                category=category,
            ):
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key="nonprofit_entity",
                    deadline=as_of,
                    pass_outcome="PASS_EXCEPTION",
                    message="공고의 비영리법인 예외 경로와 설립허가 근거가 연결되어 소기업 확인서 조건을 대체합니다.",
                )
            elif nonprofit_exception_present:
                item = _unmapped_eligibility_item(
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
                item = _eligibility_item_now(
                    requirement,
                    profile=profile,
                    fact_key=certificate_fact_key,
                    deadline=as_of,
                    message="공고가 요구한 기업 확인서 보유 사실이 연결되었습니다.",
                    fail_on_confirmed_absence=True,
                    failure_message=(
                        "회사 확인값상 공고가 요구한 중소·소기업 또는 소상공인 확인서를 보유하지 않으며, "
                        "공고 원문에도 비영리법인 예외가 없어 필수조건을 충족할 수 없습니다."
                    ),
                )
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
                item = _unmapped_eligibility_item(
                    requirement,
                    fact_key="notice_region_eligibility",
                    deadline=as_of,
                    message=(
                        "본점·사업장 소재지 등 공고별 지역제한 근거를 추가로 연결해야 합니다. "
                        "회사 선언 지점은 공식 지사 증빙 전까지 자동 PASS에 사용하지 않습니다."
                    ),
                )
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
        items.append(item)

    counts = Counter(item["policy_class"] for item in items)
    groups = {
        key: [item for item in items if item["policy_class"] == key]
        for key in ("ELIGIBILITY", "ACTION_REQUIRED", "CHECKLIST", "INFORMATION")
    }
    return {
        "policy_version": POLICY_VERSION,
        "profile_version": profile.get("profile_version"),
        "profile_classification": profile.get("classification"),
        "deadline_as_of": as_of.isoformat() if as_of else None,
        "counts": {key: counts.get(key, 0) for key in groups},
        "blocking_actions": sum(
            item["policy_class"] == "ACTION_REQUIRED" and bool(item["blocking"])
            for item in items
        ),
        "blocking_items": sum(bool(item["blocking"]) for item in items),
        "items": items,
        "groups": groups,
        "decision_boundary": (
            "적격성만 PASS/REVIEW에 반영합니다. 행동필요는 완료 전 BLOCK, "
            "체크리스트와 정보는 그 자체로 참가자격 REVIEW를 만들지 않습니다."
        ),
    }
