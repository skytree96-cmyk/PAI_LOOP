from __future__ import annotations

import copy
import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal


PolicyClass = Literal["ELIGIBILITY", "ACTION_REQUIRED", "CHECKLIST", "INFORMATION"]

PROFILE_PATH = Path(__file__).with_name("data") / "company_public_profile.json"
POLICY_VERSION = "pai-loop-requirement-policy-2026.08.17-v1"

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

    if not _contains(text, "소기업", "소상공인"):
        return False
    if category in {"ENTITY", "CERTIFICATION", "DIRECT_PRODUCTION"}:
        return True
    return _contains(
        text,
        "소기업확인서",
        "소상공인확인서",
        "소기업·소상공인 확인서",
        "소기업 또는 소상공인 확인서",
        "입찰참가자격",
        "참가자격",
    )


def _is_descriptive_entity_clause(text: str, *, category: str) -> bool:
    """Return true for subject/contractor descriptions, not bidder criteria."""

    if category != "ENTITY":
        return False
    return (
        "대상으로 하는 입찰" in text
        or (
            "계약업체" in text
            and _contains(text, "기재되어", "로 기재", "이라고 기재")
        )
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
    pass_outcome: str = "PASS_CURRENT",
    message: str,
) -> dict[str, Any]:
    item = _base_item(requirement, "ELIGIBILITY")
    fact = dict(profile.get("facts", {}).get(fact_key) or {})
    start = _as_date(fact.get("effective_from"))
    end = _as_date(fact.get("effective_to"))
    effective = bool(fact.get("value")) and (
        deadline is None
        or ((start is None or start <= deadline) and (end is None or deadline <= end))
    )
    evidence = _public_evidence(profile, fact.get("evidence_key"))
    deadline_policy = str(fact.get("deadline_policy") or "RECHECK_AT_DEADLINE")
    recheck_required = "RECHECK" in deadline_policy or "RECONFIRM" in deadline_policy
    item.update(
        {
            "outcome": pass_outcome if effective else "REVIEW",
            "blocking": not effective,
            "company_fact_key": fact_key,
            "evidence_state": fact.get("evidence_state") or "MISSING",
            "evidence": evidence,
            "deadline_as_of": deadline.isoformat() if deadline else None,
            "deadline_check_required": recheck_required,
            "message": message if effective else "공고 마감일 기준 유효 범위를 확인할 수 없어 검토가 필요합니다.",
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
) -> dict[str, Any]:
    """Classify extracted conditions without turning every clause into eligibility.

    Eligibility uses only curated public facts and retains its deadline-as-of
    recheck policy. One-off participation is a blocking action. Procedural work
    and contract facts remain checklist/information even when mandatory.
    """

    _assert_public_safe(profile)
    as_of = _as_date(deadline)
    normalized = [_normalise(item.get("normalized_condition")) for item in requirements]
    nonprofit_exception_present = any(
        "비영리법인" in text and _contains(text, "참여 가능", "예외", "적용하지")
        for text in normalized
    )
    items: list[dict[str, Any]] = []

    for requirement, text in zip(requirements, normalized, strict=True):
        category = str(requirement.get("category") or "OTHER").upper()

        if _is_descriptive_entity_clause(text, category=category):
            item = _information_item(
                requirement,
                profile=profile,
                capability_key=None,
                message="입찰 대상 또는 기재된 계약업체에 대한 설명이며 회사 참가자격 조건으로 사용하지 않습니다.",
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
        elif _contains(text, "입찰참가자격 등록", "입찰참가 자격요건", "경쟁입찰참가자격"):
            item = _eligibility_item(
                requirement,
                profile=profile,
                fact_key="bidder_registration",
                deadline=as_of,
                message="경쟁입찰참가자격 등록 보유 근거가 연결되었습니다. 마감일에는 나라장터 상태를 다시 확인합니다.",
            )
        elif _contains(text, "부정당", "입찰참가자격 제한"):
            item = _eligibility_item(
                requirement,
                profile=profile,
                fact_key="sanction_clear",
                deadline=as_of,
                message="현재 확인된 부정당 제재 사례가 없어 PASS 상태이며 마감일 기준 동적 조회를 유지합니다.",
            )
        elif _contains(text, "유죄판결", "조세포탈") and not _contains(text, "서약서"):
            item = _eligibility_item(
                requirement,
                profile=profile,
                fact_key="conviction_clear",
                deadline=as_of,
                message="현재 확인된 유죄판결 사례가 없어 PASS 상태이며 제출 전 재확인합니다.",
            )
        elif _is_small_business_eligibility(text, category=category):
            if nonprofit_exception_present:
                item = _eligibility_item(
                    requirement,
                    profile=profile,
                    fact_key="nonprofit_entity",
                    deadline=as_of,
                    pass_outcome="PASS_EXCEPTION",
                    message="공고의 비영리법인 예외 경로와 설립허가 근거가 연결되어 소기업 확인서 조건을 대체합니다.",
                )
            else:
                item = _base_item(requirement, "ELIGIBILITY")
                item.update(
                    {
                        "outcome": "REVIEW",
                        "blocking": True,
                        "company_fact_key": "small_business_certificate",
                        "evidence_state": "MISSING_OR_EXCEPTION_UNCONFIRMED",
                        "evidence": None,
                        "deadline_as_of": as_of.isoformat() if as_of else None,
                        "deadline_check_required": True,
                        "message": "비영리법인 예외가 공고 원문에 명시되었는지 확인해야 합니다.",
                    }
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
        elif category in {"ENTITY", "CERTIFICATION", "SANCTION", "INDUSTRY_CODE", "REGION"}:
            item = _base_item(requirement, "ELIGIBILITY")
            item.update(
                {
                    "outcome": "REVIEW",
                    "blocking": True,
                    "company_fact_key": None,
                    "evidence_state": "UNMAPPED",
                    "evidence": None,
                    "deadline_as_of": as_of.isoformat() if as_of else None,
                    "deadline_check_required": True,
                    "message": "적격성 조건에 대응하는 공개 프로필 근거를 추가로 연결해야 합니다.",
                }
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
