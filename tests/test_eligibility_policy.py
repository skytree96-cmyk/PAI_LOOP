from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from pai_loop.eligibility_policy import (
    _assert_public_safe,
    classify_requirements,
    load_public_company_profile,
)


def requirement(
    key: str,
    category: str,
    condition: str,
    *,
    mandatory: bool = True,
) -> dict[str, object]:
    return {
        "requirement_id": key,
        "category": category,
        "normalized_condition": condition,
        "mandatory": mandatory,
    }


INCHON_REQUIREMENTS = [
    requirement("REQ-001", "ENTITY", "지방계약법령상 입찰참가 자격요건을 갖춘 업체여야 함."),
    requirement("REQ-002", "SANCTION", "부정당업자 입찰참가자격 제한을 받고 있지 않아야 함."),
    requirement("REQ-003", "CONSORTIUM", "단독입찰만 허용되며 하도급은 허용되지 않음."),
    requirement(
        "REQ-004",
        "ENTITY",
        "소기업 또는 소상공인이면서 확인서를 보유해야 하며, 일정 요건의 비영리법인은 참여 가능함.",
    ),
    requirement("REQ-005", "CERTIFICATION", "소기업·소상공인 확인서는 유효기간 내에 있어야 함."),
    requirement("REQ-006", "SANCTION", "조세포탈 등으로 유죄판결이 확정된 날부터 2년이 지나지 않은 자는 참여할 수 없음."),
    requirement("REQ-007", "SUBMISSION", "입찰서 제출 시 조세포탈 등 해당 없음 서약서를 제출해야 함."),
    requirement("REQ-008", "SUBMISSION", "가격제안서는 나라장터 전자입찰로 제출해야 함."),
    requirement("REQ-009", "SUBMISSION", "나라장터 입찰참가자격 등록을 마감일까지 완료해야 함."),
    requirement("REQ-010", "PERFORMANCE", "입찰가격은 부가가치세를 포함한 총액으로 제출해야 함."),
    requirement("REQ-011", "SUBMISSION", "제안서는 직접 방문하여 제출해야 함."),
    requirement("REQ-014", "SUBMISSION", "제안요청서에 대한 질의는 반드시 문서로 해야 함."),
    requirement("REQ-015", "PERSONNEL", "제안서 발표는 사업책임자가 직접 해야 함."),
    requirement("REQ-016", "PERSONNEL", "발표자와 참석자는 합계 2인 이내로 제한됨."),
    requirement("REQ-017", "PERSONNEL", "참석자는 재직증명서와 신분증명자료를 제시해야 함."),
    requirement("REQ-018", "SUBMISSION", "제안설명회에 참여해야 하며 불참 시 사업신청 포기로 간주됨."),
    requirement("REQ-019", "SUBMISSION", "계약 체결 시 청렴계약이행서약서를 제출해야 함."),
    requirement("REQ-020", "SUBMISSION", "계약 체결 시 안전보건관리 준수 서약서를 제출해야 함."),
    requirement("REQ-022", "PERFORMANCE", "용역기간은 계약체결일부터 2026년 12월 31일까지임."),
    requirement("REQ-023", "SUBMISSION", "세부 산출내역서는 산출근거를 포함하여 작성해야 함."),
]


def test_public_profile_contains_only_safe_evidence_metadata() -> None:
    profile = load_public_company_profile()
    serialized = json.dumps(profile, ensure_ascii=False)

    assert profile["classification"] == "PUBLIC_SAFE_COMPANY_PROFILE"
    assert {item["display_name"] for item in profile["evidence"]} == {
        "경쟁입찰참가자격등록증",
        "비영리법인 설립허가증",
        "나라장터 인허가 증명서 모음",
    }
    assert all(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in profile["evidence"])
    assert not re.search(r"\b\d{3}-\d{2}-\d{5}\b", serialized)
    assert '"address":' not in serialized.casefold()
    assert '"person_name":' not in serialized.casefold()


def test_public_profile_guard_fails_closed_on_sensitive_fields_and_values() -> None:
    with pytest.raises(ValueError, match="forbidden key"):
        _assert_public_safe({"address": "비공개"})
    with pytest.raises(ValueError, match="sensitive value"):
        _assert_public_safe({"note": "test" + "@" + "example.com"})
    with pytest.raises(ValueError, match="sensitive value"):
        _assert_public_safe({"note": "123-45" + "-67890"})


def test_inchon_policy_separates_four_classes_and_keeps_one_blocking_action() -> None:
    result = classify_requirements(
        INCHON_REQUIREMENTS,
        profile=load_public_company_profile(),
        deadline="2026-09-03",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert set(result["groups"]) == {
        "ELIGIBILITY",
        "ACTION_REQUIRED",
        "CHECKLIST",
        "INFORMATION",
    }
    assert result["blocking_actions"] == 1
    # bidder_registration carries RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE, last
    # verified 2026-08-05. A 2026-09-03 deadline is well inside the staleness
    # window, so this is a confirmed PASS, not an unconditional REVIEW (see
    # the 2026-09-03 eligibility freshness fix changelog).
    assert by_id["REQ-001"]["outcome"] == "PASS_CURRENT"
    assert by_id["REQ-001"]["deadline_check_required"] is True
    assert by_id["REQ-001"]["evidence"]["display_name"] == "경쟁입찰참가자격등록증"
    # sanction_clear was last verified 2026-09-03 (same day as this notice's
    # deadline), well inside the staleness window, so this is a confirmed
    # PASS (see the 2026-09-03 eligibility freshness fix changelog).
    assert by_id["REQ-002"]["outcome"] == "PASS_CURRENT"
    assert by_id["REQ-002"]["deadline_check_required"] is True
    assert by_id["REQ-003"]["policy_class"] == "CHECKLIST"
    assert by_id["REQ-003"]["outcome"] == "READY"
    assert by_id["REQ-004"]["outcome"] == "PASS_EXCEPTION"
    assert by_id["REQ-005"]["outcome"] == "REVIEW"
    assert by_id["REQ-005"]["company_fact_key"] == (
        "small_business_nonprofit_exception_scope"
    )
    assert by_id["REQ-006"]["outcome"] == "PASS_CURRENT"
    assert by_id["REQ-008"]["policy_class"] == "INFORMATION"
    assert by_id["REQ-010"]["policy_class"] == "INFORMATION"
    assert by_id["REQ-018"]["policy_class"] == "ACTION_REQUIRED"
    assert by_id["REQ-018"]["outcome"] == "BLOCK_UNTIL_CONFIRMED"
    assert by_id["REQ-018"]["blocking"] is True
    assert "REVIEW" not in by_id["REQ-018"]["message"]
    assert by_id["REQ-019"]["policy_class"] == "CHECKLIST"
    assert by_id["REQ-022"]["policy_class"] == "INFORMATION"


def test_confirmed_missing_small_business_certificate_fails_without_notice_exception() -> None:
    result = classify_requirements(
        [requirement("SMALL-1", "CERTIFICATION", "소기업·소상공인 확인서를 보유해야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-09-03",
    )

    assert result["items"][0]["policy_class"] == "ELIGIBILITY"
    assert result["items"][0]["outcome"] == "FAIL_CONFIRMED"
    assert result["items"][0]["blocking"] is True

    future = classify_requirements(
        [requirement("SMALL-FUTURE", "CERTIFICATION", "소기업·소상공인 확인서를 보유해야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-09-04",
    )["items"][0]
    # A one-day-later deadline no longer forces REVIEW on its own: the company
    # fact (small_business_certificate=False) is still fresh (last verified
    # 2026-09-03), so this remains a confirmed FAIL, not an unconditional
    # REVIEW (see the 2026-09-03 eligibility freshness fix changelog).
    assert future["outcome"] == "FAIL_CONFIRMED"
    assert future["evaluation_fact_key"] == "small_business_certificate"

    historical = classify_requirements(
        [requirement("SMALL-HISTORICAL", "CERTIFICATION", "소기업 확인서를 보유해야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )["items"][0]
    assert historical["outcome"] == "REVIEW"


def test_future_conviction_check_does_not_overextend_current_declaration() -> None:
    # A distant notice deadline no longer drives staleness by itself (see the
    # 2026-09-03 eligibility freshness fix changelog) -- deadline is when the
    # bid closes, not a proxy for "time since we last checked". Freshness is
    # now judged against the evaluation clock, so we pin ``evaluation_date``
    # to actually exercise both the fresh and the stale branch.
    result = classify_requirements(
        [requirement("SANCTION-1", "SANCTION", "조세포탈 유죄판결이 없어야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-12-31",
        evaluation_date="2026-09-03",
    )

    item = result["items"][0]
    assert item["outcome"] == "PASS_CURRENT"
    assert item["blocking"] is False
    assert item["deadline_check_required"] is True
    assert result["blocking_actions"] == 0
    assert result["blocking_items"] == 0

    stale = classify_requirements(
        [requirement("SANCTION-STALE", "SANCTION", "조세포탈 유죄판결이 없어야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-12-31",
        # conviction_clear was last verified 2026-09-03; push well past the
        # 180-day staleness horizon so the declaration must be overextended.
        evaluation_date="2027-06-01",
    )["items"][0]
    assert stale["outcome"] == "REVIEW"
    assert stale["evaluation_fact_key"] == "reconfirm.conviction_clear"
    assert stale["blocking"] is True


def test_profile_structures_official_and_declared_company_facts_separately() -> None:
    facts = load_public_company_profile()["facts"]

    assert len(facts["industry_code_inventory"]["value"]) == 18
    assert facts["industry_code_name_inventory"]["value"] == [
        {"code": "1169", "name": "학술·연구용역"},
        {"code": "1261", "name": "종합여행업"},
        {"code": "1426", "name": "소프트웨어사업자(패키지소프트웨어개발·공급사업)"},
        {"code": "1468", "name": "소프트웨어사업자(컴퓨터관련서비스사업)"},
        {"code": "1469", "name": "소프트웨어사업자(디지털콘텐츠개발서비스사업)"},
        {"code": "1470", "name": "소프트웨어사업자(데이터베이스제작및검색서비스사업)"},
        {"code": "1517", "name": "출판사"},
        {"code": "3156", "name": "평생교육시설(원격)"},
        {"code": "3244", "name": "비디오물제작업"},
        {"code": "5601", "name": "국내무료직업소개사업"},
        {"code": "5608", "name": "직업능력개발훈련수탁기관"},
        {"code": "5609", "name": "직업능력개발훈련시설(지정직업훈련시설)"},
        {"code": "5720", "name": "국제회의기획업"},
        {"code": "6529", "name": "이러닝서비스업"},
        {"code": "6530", "name": "기타이러닝업"},
        {"code": "6639", "name": "가족친화기업"},
        {"code": "9901", "name": "기타자유업(행사대행업)"},
        {"code": "9999", "name": "기타자유업종"},
    ]
    assert facts["direct_production_certificate"]["value"] is False
    assert facts["small_business_certificate"]["value"] is False
    assert facts["sme_certificate"]["value"] is False
    assert facts["head_office_region_seoul"]["evidence_state"].startswith("VERIFIED")
    assert facts["head_office_region_codes"]["value"] == [
        "SEOUL",
        "SEOUL_YEONGDEUNGPO",
    ]
    assert facts["registered_bidder_branch_region_codes"]["value"] == []
    assert facts["declared_branch_region_codes"]["value"] == [
        "DAEGU",
        "BUSAN",
        "DAEJEON",
        "GWANGJU",
        "JEJU",
    ]
    assert facts["declared_branch_region_codes"]["evidence_state"] == "COMPANY_DECLARATION"
    assert facts["public_software_bid_ceiling_krw"]["value"] == 4_000_000_000
    assert len(facts["designated_vocational_training_ncs_subclasses"]["value"]) == 12


def test_industry_inventory_supports_single_or_and_and_confirmed_fail() -> None:
    result = classify_requirements(
        [
            requirement(
                "INDUSTRY-OR",
                "INDUSTRY_CODE",
                "종합여행업(업종코드 1261) 또는 국내여행업(업종코드 1263) 등록업체",
            ),
            requirement(
                "INDUSTRY-AND",
                "INDUSTRY_CODE",
                "학술연구용역(업종코드 1169) 및 종합여행업(업종코드 1261) 모두 등록",
            ),
            requirement(
                "INDUSTRY-MISSING",
                "INDUSTRY_CODE",
                "국내여행업(업종코드 1263) 등록업체",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-08-05",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["INDUSTRY-OR"]["outcome"] == "PASS_CURRENT"
    assert by_id["INDUSTRY-OR"]["operator"] == "contains_any"
    assert by_id["INDUSTRY-AND"]["outcome"] == "PASS_CURRENT"
    assert by_id["INDUSTRY-AND"]["operator"] == "contains_all"
    assert by_id["INDUSTRY-MISSING"]["outcome"] == "FAIL_CONFIRMED"
    assert by_id["INDUSTRY-MISSING"]["company_fact_key"] == "industry_code_inventory"


def test_nonprofit_direct_production_exception_is_scoped_fail_closed() -> None:
    profile = load_public_company_profile()
    same_clause = classify_requirements(
        [
            requirement(
                "COMPOUND-EXCEPTION",
                "CERTIFICATION",
                (
                    "직접생산확인증명서와 중소기업확인서 조건은 비영리법인에는 적용하지 않으며 "
                    "비영리법인은 참여 가능함."
                ),
            )
        ],
        profile=profile,
        deadline="2026-09-10",
    )["items"][0]
    assert same_clause["outcome"] == "PASS_EXCEPTION"
    assert same_clause["company_fact_key"] == "nonprofit_entity"

    misleading_scope = classify_requirements(
        [
            requirement(
                "COMPOUND-NON-EXCEPTION",
                "CERTIFICATION",
                (
                    "직접생산확인증명서와 중소기업확인서는 비영리법인도 보유해야 하며, "
                    "지역제한은 적용하지 않아 참여 가능하다."
                ),
            )
        ],
        profile=profile,
        deadline="2026-09-10",
    )["items"][0]
    assert misleading_scope["outcome"] == "REVIEW"
    assert misleading_scope["company_fact_key"] == "compound_notice_specific_qualification"

    for condition in (
        "직접생산확인증명서와 중소기업확인서는 비영리법인도 면제 대상이 아니며 모두 보유해야 한다.",
        "직접생산확인증명서와 중소기업확인서는 비영리법인도 예외 대상이 아니며 모두 보유해야 한다.",
        "직접생산확인증명서와 중소기업확인서를 비영리법인에 적용하지 않는 것은 아니며 모두 보유해야 한다.",
    ):
        negated = classify_requirements(
            [requirement("NEGATED-EXCEPTION", "CERTIFICATION", condition)],
            profile=profile,
            deadline="2026-09-10",
        )["items"][0]
        assert negated["outcome"] == "REVIEW"
        assert negated["company_fact_key"] == "compound_notice_specific_qualification"

    for key, category, condition in (
        (
            "DIRECT-UNRELATED-EXCEPTION",
            "DIRECT_PRODUCTION",
            "직접생산확인증명서를 보유해야 하며, 지역제한은 비영리법인에 적용하지 않아 참여 가능하다.",
        ),
        (
            "SME-UNRELATED-EXCEPTION",
            "CERTIFICATION",
            "중소기업확인서를 보유해야 하며, 지역제한은 비영리법인에 적용하지 않아 참여 가능하다.",
        ),
        (
            "DIRECT-NEGATED-EXCEPTION",
            "DIRECT_PRODUCTION",
            "직접생산확인증명서는 비영리법인도 면제 대상이 아니며 보유해야 한다.",
        ),
    ):
        single_family = classify_requirements(
            [requirement(key, category, condition)],
            profile=profile,
            deadline="2026-09-03",
        )["items"][0]
        assert single_family["outcome"] != "PASS_EXCEPTION"
        assert single_family["company_fact_key"] != "nonprofit_entity"

    separate_scoped = classify_requirements(
        [
            requirement(
                "DIRECT",
                "DIRECT_PRODUCTION",
                "직접생산확인증명서를 보유해야 함.",
            ),
            requirement(
                "DIRECT-EXCEPTION",
                "CERTIFICATION",
                "직접생산확인증명서는 비영리법인에 적용하지 않으며 참여 가능함.",
            ),
        ],
        profile=profile,
        deadline="2026-09-10",
    )
    separate_by_id = {item["requirement_id"]: item for item in separate_scoped["items"]}
    assert separate_by_id["DIRECT"]["outcome"] == "REVIEW"
    assert separate_by_id["DIRECT-EXCEPTION"]["outcome"] == "PASS_EXCEPTION"

    separate_ambiguous = classify_requirements(
        [
            requirement(
                "DIRECT",
                "DIRECT_PRODUCTION",
                "직접생산확인증명서를 보유해야 함.",
            ),
            requirement(
                "GENERIC-EXCEPTION",
                "ENTITY",
                "일정 요건의 비영리법인은 참여 가능함.",
            ),
        ],
        profile=profile,
        deadline="2026-09-10",
    )
    direct = next(item for item in separate_ambiguous["items"] if item["requirement_id"] == "DIRECT")
    assert direct["outcome"] == "REVIEW"
    assert direct["company_fact_key"] == "direct_production_nonprofit_exception_scope"


def test_nonprofit_small_business_exception_is_scoped_fail_closed() -> None:
    profile = load_public_company_profile()
    result = classify_requirements(
        [
            requirement(
                "SME-SAME-CLAUSE",
                "CERTIFICATION",
                "중소기업확인서 조건은 비영리법인에 적용하지 않으며 비영리법인은 참여 가능함.",
            ),
            requirement(
                "SME-SEPARATE",
                "CERTIFICATION",
                "중소기업확인서를 보유해야 함.",
            ),
        ],
        profile=profile,
        deadline="2026-09-10",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["SME-SAME-CLAUSE"]["outcome"] == "PASS_EXCEPTION"
    assert by_id["SME-SAME-CLAUSE"]["company_fact_key"] == "nonprofit_entity"
    assert by_id["SME-SEPARATE"]["outcome"] == "REVIEW"
    assert by_id["SME-SEPARATE"]["company_fact_key"] == (
        "small_business_nonprofit_exception_scope"
    )


_NONPROFIT_SMALL_BUSINESS_OR = (
    "소기업·소상공인 확인서 소지 업체 또는 "
    "비영리법인(법인설립허가서 등 증빙 제출) 중 하나에 해당"
)


# Tails an extracted `normalized_condition` carries after the clause. Sized from
# the shapes real extracted conditions end with; each is inert (no obligation of
# its own, no polarity), so the whole-clause anchor still owns the meaning.
_NONPROFIT_SMALL_BUSINESS_OR_TAILS = [
    "",
    "함.",
    "해야 함.",
    "하여야 함",
    "되어야 함.",
    "하는 업체여야 함.",
    "하는 업체이어야 함",
    "하는 자",
    "한다.",
]


@pytest.mark.parametrize("tail", _NONPROFIT_SMALL_BUSINESS_OR_TAILS)
def test_nonprofit_small_business_or_absorbs_inert_declarative_tail(tail: str) -> None:
    item = classify_requirements(
        [requirement("SYN-SME-OR-TAIL", "CERTIFICATION", _NONPROFIT_SMALL_BUSINESS_OR + tail)],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "PASS_EXCEPTION"
    assert item["company_fact_key"] == "nonprofit_entity"


@pytest.mark.parametrize("condition", [
    _NONPROFIT_SMALL_BUSINESS_OR.replace("확인서 소지", "확인서를 소지한"),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("중 하나에", "중 어느 하나에"),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("소지", "보유"),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("소기업·소상공인", "중소기업"),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("소기업·소상공인", "소상공인"),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("(법인설립허가서 등 증빙 제출)", ""),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("업체 또는", "자 또는"),
])
def test_nonprofit_small_business_or_accepts_equivalent_complete_clause(condition: str) -> None:
    item = classify_requirements(
        [requirement("SYN-SME-OR-EQUIV", "CERTIFICATION", condition + "해야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "PASS_EXCEPTION"
    assert item["company_fact_key"] == "nonprofit_entity"


@pytest.mark.parametrize("category", ["ENTITY", "CERTIFICATION", "OTHER"])
def test_nonprofit_small_business_exact_or_uses_existing_evidence(category: str) -> None:
    item = classify_requirements(
        [requirement("SYN-SME-OR", category, _NONPROFIT_SMALL_BUSINESS_OR)],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "PASS_EXCEPTION"
    assert item["blocking"] is False
    assert item["company_fact_key"] == "nonprofit_entity"
    assert item["evidence_state"] == "VERIFIED"
    assert item["evidence"] is not None
    assert item["deadline_as_of"] == "2026-09-10"
    assert "비영리법인 예외가 없어" not in item["message"]


@pytest.mark.parametrize("unavailable", ["missing_fact", "false_fact", "expired_fact", "expired_evidence"])
def test_nonprofit_small_business_or_preserves_fact_and_deadline_checks(unavailable: str) -> None:
    profile = load_public_company_profile()
    fact = profile["facts"]["nonprofit_entity"]
    if unavailable == "missing_fact":
        del profile["facts"]["nonprofit_entity"]
    elif unavailable == "false_fact":
        fact["value"] = False
    elif unavailable == "expired_fact":
        fact["effective_to"] = "2026-09-09"
    else:
        evidence = next(
            item for item in profile["evidence"]
            if item["evidence_key"] == fact["evidence_key"]
        )
        evidence["valid_until"] = "2026-09-09"

    item = classify_requirements(
        [requirement("SYN-SME-OR", "CERTIFICATION", _NONPROFIT_SMALL_BUSINESS_OR)],
        profile=profile,
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "REVIEW"
    assert item["blocking"] is True
    assert item["company_fact_key"] == "nonprofit_entity"


@pytest.mark.parametrize("condition", [
    _NONPROFIT_SMALL_BUSINESS_OR.replace(" 또는 ", " 및 "),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("해당", "해당하지 않음"),
    _NONPROFIT_SMALL_BUSINESS_OR.replace("비영리법인(", "비영리법인 제외 업체("),
    _NONPROFIT_SMALL_BUSINESS_OR + ". 단, 비영리법인은 제외한다.",
    _NONPROFIT_SMALL_BUSINESS_OR.replace("증빙 제출", "증빙 제출 및 직접생산확인증명서 보유"),
    "직접생산확인증명서와 " + _NONPROFIT_SMALL_BUSINESS_OR,
    _NONPROFIT_SMALL_BUSINESS_OR + "하며 직접생산확인증명서도 보유해야 함.",
    _NONPROFIT_SMALL_BUSINESS_OR + "하며 중소기업확인서는 모두 보유해야 함.",
    _NONPROFIT_SMALL_BUSINESS_OR + "하지 않아야 함.",
    _NONPROFIT_SMALL_BUSINESS_OR + "하지 않는 업체여야 함.",
    _NONPROFIT_SMALL_BUSINESS_OR + ". 다만 비영리법인은 참여할 수 없음.",
    _NONPROFIT_SMALL_BUSINESS_OR + "하는 업체는 참가할 수 없음.",
    # A nonprofit alternative restricted to a legal subset does not prove the
    # company belongs to that subset, so it must never open the PASS path.
    _NONPROFIT_SMALL_BUSINESS_OR.replace("비영리법인", "특정 비영리법인"),
    _NONPROFIT_SMALL_BUSINESS_OR.replace(
        "비영리법인(법인설립허가서 등 증빙 제출)", "비영리법인(우선조달 예외 대상)"
    ),
    _NONPROFIT_SMALL_BUSINESS_OR.replace(
        "비영리법인(법인설립허가서 등 증빙 제출)", "시행령 제2조의3 제2호 해당 비영리법인"
    ),
    _NONPROFIT_SMALL_BUSINESS_OR.replace(
        "(법인설립허가서 등 증빙 제출)", "(법인설립허가서 등 증빙 제출 후 별도 승인)"
    ),
])
def test_nonprofit_small_business_or_rejects_negation_and_extra_gates(condition: str) -> None:
    items = classify_requirements(
        [requirement("SYN-SME-OR-CLOSED", "CERTIFICATION", condition)],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]

    assert all(not item["outcome"].startswith("PASS") for item in items)
    assert all(item["company_fact_key"] != "nonprofit_entity" for item in items)


def test_nonprofit_small_business_or_does_not_extend_to_another_requirement() -> None:
    result = classify_requirements(
        [
            requirement("SYN-SME-OR", "CERTIFICATION", _NONPROFIT_SMALL_BUSINESS_OR),
            requirement("SYN-SME-SEPARATE", "CERTIFICATION", "중소기업확인서를 보유해야 함."),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["SYN-SME-OR"]["outcome"] == "PASS_EXCEPTION"
    # A separate row does not establish independent applicability: this remains
    # the SME certificate family, and its relation to the OR clause is unbound.
    # Match the existing same-family scope REVIEW contract, without granting PASS.
    assert by_id["SYN-SME-SEPARATE"]["outcome"] == "REVIEW"
    assert by_id["SYN-SME-SEPARATE"]["blocking"] is True
    assert by_id["SYN-SME-SEPARATE"]["company_fact_key"] == "small_business_nonprofit_exception_scope"


def test_nonprofit_small_business_or_never_downgrades_direct_production_fail() -> None:
    """An SME/nonprofit OR clause says nothing about direct production."""

    result = classify_requirements(
        [
            requirement("SYN-SME-OR", "CERTIFICATION", _NONPROFIT_SMALL_BUSINESS_OR),
            requirement(
                "SYN-DP-SEPARATE",
                "CERTIFICATION",
                "직접생산확인증명서를 보유해야 함.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["SYN-SME-OR"]["outcome"] == "PASS_EXCEPTION"
    assert by_id["SYN-DP-SEPARATE"]["outcome"] == "FAIL_CONFIRMED"
    assert by_id["SYN-DP-SEPARATE"]["company_fact_key"] != "nonprofit_entity"
    assert by_id["SYN-DP-SEPARATE"]["company_fact_key"] != (
        "direct_production_nonprofit_exception_scope"
    )


def test_separate_certificate_review_does_not_deny_a_nonprofit_clause_elsewhere() -> None:
    """The same-family scope review must not deny the notice's stated alternative."""

    result = classify_requirements(
        [
            requirement("SYN-SME-OR", "CERTIFICATION", _NONPROFIT_SMALL_BUSINESS_OR),
            requirement("SYN-SME-SEPARATE", "CERTIFICATION", "중소기업확인서를 보유해야 함."),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )
    separate = {item["requirement_id"]: item for item in result["items"]}[
        "SYN-SME-SEPARATE"
    ]

    assert separate["outcome"] == "REVIEW"
    assert separate["blocking"] is True
    assert separate["company_fact_key"] != "nonprofit_entity"
    assert "비영리법인 예외가 없어" not in separate["message"]
    assert "원문 검토" in separate["message"]


def test_absence_claim_survives_when_no_requirement_mentions_a_nonprofit() -> None:
    """The claim is truthful for a notice with no nonprofit text, so keep it."""

    item = classify_requirements(
        [requirement("SYN-SME-ONLY", "CERTIFICATION", "중소기업확인서를 보유해야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "FAIL_CONFIRMED"
    assert "비영리법인 예외가 없어" in item["message"]


def test_legal_subset_nonprofit_alternative_needs_bound_membership() -> None:
    """A false SME branch cannot refute an explicitly stated unbound OR branch."""

    condition = "소기업·소상공인 확인서 소지 업체 또는 관계법령상 비영리법인에 해당해야 함."
    item = classify_requirements(
        [requirement("SYN-SME-OR-UNBOUND", "CERTIFICATION", condition)],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "REVIEW"
    assert item["blocking"] is True
    assert item["company_fact_key"] == "small_business_nonprofit_legal_subset"
    assert "비영리법인 예외가 없어" not in item["message"]
    assert "검토" in item["message"]


# PUBLIC_POLICY_PROJECTION: observed UI wording, not an ACCEPTED attachment
# extraction or a verified quote from its raw source. This tests only policy.
_PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR = (
    "소기업·소상공인 확인서를 소지한 업체 또는 "
    "우선조달계약 예외 규정에 따른 비영리법인 중 하나에 해당"
)


@pytest.mark.parametrize("condition", [
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR,
    "소기업·소상공인 확인서를 소지한 업체 또는 "
    "비영리법인(우선조달 예외 대상) 중 하나에 해당해야 함.",
    "소기업·소상공인 확인서를 소지한 업체 또는 "
    "시행령 제2조의3 제1항 제2호에 따른 비영리법인 중 하나에 해당하는 업체여야 함.",
])
def test_public_policy_projection_legal_subset_never_uses_generic_nonprofit_pass(condition: str) -> None:
    profile = load_public_company_profile()
    assert profile["facts"]["nonprofit_entity"]["value"] is True
    item = classify_requirements(
        [requirement("SYN-SME-LEGAL-SUBSET", "CERTIFICATION", condition)],
        profile=profile,
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "REVIEW"
    assert item["blocking"] is True
    assert item["company_fact_key"] == "small_business_nonprofit_legal_subset"
    assert item["evidence"] is None


@pytest.mark.parametrize("condition", [
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR.replace(" 또는 ", " 및 "),
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR.replace("해당", "해당하지 않음"),
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR + ". 단, 비영리법인은 제외한다.",
    "참고사항: " + _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR,
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR + "하며 직접생산확인증명서도 보유해야 함.",
])
def test_legal_subset_review_never_borrows_a_partial_or_negated_alternative(condition: str) -> None:
    items = classify_requirements(
        [requirement("SYN-SME-SUBSET-UNSUPPORTED", "CERTIFICATION", condition)],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]

    assert all(item["company_fact_key"] != "small_business_nonprofit_legal_subset" for item in items)
    assert all(not item["outcome"].startswith("PASS") for item in items)


def _synthetic_sme_holder_profile() -> dict[str, object]:
    """Synthetic certificate holder that is not a nonprofit; no real company data.

    The curated public profile pins the opposite pair (no certificate, nonprofit
    true), so the independently satisfied certificate branch of an SME/nonprofit
    OR needs its own fixture. Every identifier is a `SYN-` placeholder.
    """

    def fact(value: bool, evidence_key: str) -> dict[str, object]:
        return {
            "value": value,
            "evidence_key": evidence_key,
            "evidence_state": "VERIFIED",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "last_verified_at": "2026-09-07",
            "deadline_policy": "RECHECK_ONLINE_AT_EACH_NOTICE_DEADLINE",
        }

    return {
        "classification": "PUBLIC_SAFE_COMPANY_PROFILE",
        "facts": {
            "small_business_certificate": fact(True, "SYN-SME-CERT"),
            "sme_certificate": fact(True, "SYN-SME-CERT"),
            "nonprofit_entity": fact(False, "SYN-NONPROFIT-STATE"),
            "direct_production_certificate": {
                "value": False,
                "evidence_key": None,
                "evidence_state": "COMPANY_CONFIRMED_ABSENT",
                "effective_from": "2026-01-01",
                "effective_to": None,
                "last_verified_at": "2026-09-07",
                "deadline_policy": "RECONFIRM_BEFORE_EACH_SUBMISSION",
            },
        },
        "evidence": [
            {
                "evidence_key": key,
                "display_name": "Synthetic eligibility evidence",
                "sha256": "a" * 64,
                "valid_from": "2026-01-01",
                "valid_until": "2026-12-31",
                "last_observed_at": "2026-09-07",
            }
            for key in ("SYN-SME-CERT", "SYN-NONPROFIT-STATE")
        ],
    }


_SME_CERTIFICATE_FACT_KEYS = {"small_business_certificate", "sme_certificate"}

# Both OR shapes name the certificate as their own first alternative: the
# generic nonprofit alternative and the legal-subset one.
_INDEPENDENT_SME_OR_CONDITIONS = [
    _NONPROFIT_SMALL_BUSINESS_OR,
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR,
    "소기업·소상공인 확인서 소지 업체 또는 관계법령상 비영리법인에 해당해야 함.",
]


@pytest.mark.parametrize("condition", _INDEPENDENT_SME_OR_CONDITIONS)
def test_sme_or_accepts_the_independently_satisfied_certificate_branch(condition: str) -> None:
    """A verified, deadline-valid certificate completes the OR on its own.

    Within OR alternatives a complete PASS path wins, so neither a false generic
    nonprofit fact nor an unresolved legal-subset alternative may drag the whole
    clause to REVIEW when the certificate branch is already satisfied.
    """

    profile = _synthetic_sme_holder_profile()
    assert profile["facts"]["nonprofit_entity"]["value"] is False

    item = classify_requirements(
        [requirement("SYN-SME-OR-HOLDER", "CERTIFICATION", condition)],
        profile=profile,
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] == "PASS_CURRENT"
    assert item["blocking"] is False
    assert item["company_fact_key"] in _SME_CERTIFICATE_FACT_KEYS
    assert item["evidence_state"] == "VERIFIED"
    assert item["evidence"] is not None
    assert item["deadline_as_of"] == "2026-09-10"


@pytest.mark.parametrize("condition", _INDEPENDENT_SME_OR_CONDITIONS)
def test_sme_or_certificate_branch_matches_the_plain_certificate_clause(condition: str) -> None:
    """The OR's certificate branch decides exactly as the plain clause does."""

    items = classify_requirements(
        [
            requirement("SYN-SME-PLAIN", "CERTIFICATION", "소기업·소상공인 확인서 소지 업체여야 함."),
            requirement("SYN-SME-OR-HOLDER", "CERTIFICATION", condition),
        ],
        profile=_synthetic_sme_holder_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]
    by_id = {item["requirement_id"]: item for item in items}

    for key in ("outcome", "blocking", "company_fact_key", "evidence_state"):
        assert by_id["SYN-SME-OR-HOLDER"][key] == by_id["SYN-SME-PLAIN"][key]


@pytest.mark.parametrize("condition", _INDEPENDENT_SME_OR_CONDITIONS)
@pytest.mark.parametrize("unavailable", [
    "missing_fact",
    "false_fact",
    "expired_fact",
    "expired_evidence",
    "stale_verification",
])
def test_sme_or_certificate_branch_keeps_evidence_and_deadline_checks(
    condition: str, unavailable: str,
) -> None:
    """Only a fact that survives every existing validity check may open PASS."""

    profile = _synthetic_sme_holder_profile()
    for key in sorted(_SME_CERTIFICATE_FACT_KEYS):
        if unavailable == "missing_fact":
            del profile["facts"][key]
            continue
        fact = profile["facts"][key]
        if unavailable == "false_fact":
            fact["value"] = False
        elif unavailable == "expired_fact":
            fact["effective_to"] = "2026-09-09"
        elif unavailable == "stale_verification":
            fact["last_verified_at"] = "2020-01-01"
    if unavailable == "expired_evidence":
        for evidence in profile["evidence"]:
            if evidence["evidence_key"] == "SYN-SME-CERT":
                evidence["valid_until"] = "2026-09-09"

    item = classify_requirements(
        [requirement("SYN-SME-OR-HOLDER", "CERTIFICATION", condition)],
        profile=profile,
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"][0]

    assert item["outcome"] != "PASS_CURRENT"
    assert item["blocking"] is True
    assert item["company_fact_key"] not in _SME_CERTIFICATE_FACT_KEYS


@pytest.mark.parametrize("condition", _INDEPENDENT_SME_OR_CONDITIONS)
def test_satisfied_sme_or_never_masks_an_independent_direct_production_fail(condition: str) -> None:
    """A satisfied certificate branch says nothing about direct production."""

    items = classify_requirements(
        [
            requirement("SYN-SME-OR-HOLDER", "CERTIFICATION", condition),
            requirement("SYN-DP-GATE", "CERTIFICATION", "직접생산확인증명서를 보유해야 함."),
        ],
        profile=_synthetic_sme_holder_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]
    gate = next(item for item in items if item["requirement_id"] == "SYN-DP-GATE")

    assert gate["outcome"] == "FAIL_CONFIRMED"
    assert gate["blocking"] is True
    assert gate["company_fact_key"] == "direct_production_certificate"


@pytest.mark.parametrize("related", [
    "소기업·소상공인 확인서를 보유해야 함.",
    "소기업·소상공인 확인서는 유효기간 내에 있어야 함.",
])
def test_satisfied_sme_or_keeps_the_separate_scope_review(related: str) -> None:
    """A satisfied OR clause does not resolve another clause's nonprofit scope."""

    items = classify_requirements(
        [
            requirement("SYN-SME-OR-HOLDER", "CERTIFICATION", _NONPROFIT_SMALL_BUSINESS_OR),
            requirement("SYN-SME-RELATED", "CERTIFICATION", related),
        ],
        profile=_synthetic_sme_holder_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]
    by_id = {item["requirement_id"]: item for item in items}

    assert by_id["SYN-SME-OR-HOLDER"]["outcome"] == "PASS_CURRENT"
    assert by_id["SYN-SME-RELATED"]["outcome"] == "REVIEW"
    assert by_id["SYN-SME-RELATED"]["company_fact_key"] == (
        "small_business_nonprofit_exception_scope"
    )


@pytest.mark.parametrize("condition", [
    "소기업·소상공인 확인서를 보유해야 함.",
    "소기업·소상공인 확인서는 유효기간 내에 있어야 함.",
])
def test_separate_sme_possession_or_validity_keeps_scope_review(condition: str) -> None:
    items = classify_requirements(
        [
            requirement("SYN-SME-OR", "CERTIFICATION", _NONPROFIT_SMALL_BUSINESS_OR),
            requirement("SYN-SME-RELATED", "CERTIFICATION", condition),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]
    related = next(item for item in items if item["requirement_id"] == "SYN-SME-RELATED")

    assert related["outcome"] == "REVIEW"
    assert related["company_fact_key"] == "small_business_nonprofit_exception_scope"


@pytest.mark.parametrize("alternative", [
    _NONPROFIT_SMALL_BUSINESS_OR,
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR,
])
@pytest.mark.parametrize("independent", [
    "직접생산확인증명서를 보유해야 함.",
    "중소기업확인서는 비영리법인도 보유해야 함.",
    "중소기업확인서를 보유해야 하며 비영리법인은 제외한다.",
    "중소기업확인서를 보유해야 하며 별도 등록도 완료해야 함.",
    "중소기업확인서를 예외 없이 반드시 보유해야 함.",
])
def test_sme_alternative_review_never_masks_independent_or_excluded_failure(
    alternative: str, independent: str,
) -> None:
    items = classify_requirements(
        [
            requirement("SYN-SME-ALTERNATIVE", "CERTIFICATION", alternative),
            requirement("SYN-INDEPENDENT-GATE", "CERTIFICATION", independent),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]
    gate = next(item for item in items if item["requirement_id"] == "SYN-INDEPENDENT-GATE")

    assert gate["outcome"] == "FAIL_CONFIRMED"
    assert gate["blocking"] is True
    assert gate["company_fact_key"] in {"direct_production_certificate", "sme_certificate"}


@pytest.mark.parametrize("alternative", [
    _NONPROFIT_SMALL_BUSINESS_OR,
    _PUBLIC_POLICY_PROJECTION_NONPROFIT_SUBSET_OR,
])
def test_optional_alternative_does_not_reclassify_a_mandatory_sme_gate(alternative: str) -> None:
    items = classify_requirements(
        [
            requirement("SYN-OPTIONAL-OR", "CERTIFICATION", alternative, mandatory=False),
            requirement("SYN-REQUIRED-SME", "CERTIFICATION", "중소기업확인서를 보유해야 함."),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
        evaluation_date="2026-09-07",
    )["items"]
    gate = next(item for item in items if item["requirement_id"] == "SYN-REQUIRED-SME")

    assert gate["outcome"] == "FAIL_CONFIRMED"
    assert gate["company_fact_key"] == "sme_certificate"


def test_slash_industry_logic_and_training_scope_stay_fail_closed() -> None:
    result = classify_requirements(
        [
            requirement(
                "INDUSTRY-SLASH",
                "INDUSTRY_CODE",
                "학술연구용역(업종코드 1169) / 국내여행업(업종코드 1263) 등록",
            ),
            requirement(
                "NCS-HELD",
                "CERTIFICATION",
                "지정직업훈련시설의 NCS 세분류 04030102 기업교육을 보유한 업체",
            ),
            requirement(
                "NCS-MISSING",
                "CERTIFICATION",
                "지정직업훈련시설의 NCS 세분류 08020101 디자인 직종을 보유한 업체",
            ),
            requirement(
                "NCS-SLASH",
                "CERTIFICATION",
                "지정직업훈련시설 NCS 세분류 04030102 / 08020101 보유 업체",
            ),
            requirement(
                "NCS-MIXED",
                "CERTIFICATION",
                "지정직업훈련시설 NCS 세분류 04030102 또는 08020101 및 02010101 보유 업체",
            ),
            requirement(
                "NCS-UNRELATED-AND",
                "CERTIFICATION",
                "지정직업훈련시설 등록 및 NCS 세분류 04030102, 08020101 보유 업체",
            ),
            requirement(
                "NCS-SUFFIX-ANY",
                "CERTIFICATION",
                "지정직업훈련시설 NCS 세분류 04030102, 08020101 중 하나 보유 업체",
            ),
            requirement(
                "NCS-NAME-PREFIX",
                "CERTIFICATION",
                "지정직업훈련시설 NCS 훈련직종 인사관리 보유 업체",
            ),
            requirement(
                "FACILITY-DATE-NOT-NCS",
                "CERTIFICATION",
                "지정직업훈련시설 지정일은 20260903이며 해당 시설을 보유한 업체",
            ),
            requirement(
                "FACILITY-PERMIT-NOT-NCS",
                "CERTIFICATION",
                "지정직업훈련시설 허가번호 12345678을 보유한 업체",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["INDUSTRY-SLASH"]["outcome"] == "REVIEW"
    assert by_id["INDUSTRY-SLASH"]["company_fact_key"] == "ambiguous_industry_code_logic"
    assert by_id["NCS-HELD"]["outcome"] == "PASS_CURRENT"
    assert by_id["NCS-HELD"]["company_fact_key"] == "designated_vocational_training_ncs_codes"
    assert by_id["NCS-MISSING"]["outcome"] == "FAIL_CONFIRMED"
    assert by_id["NCS-SLASH"]["outcome"] == "REVIEW"
    assert by_id["NCS-MIXED"]["outcome"] == "REVIEW"
    assert by_id["NCS-MIXED"]["company_fact_key"] == "vocational_training_scope_unparsed"
    assert by_id["NCS-UNRELATED-AND"]["outcome"] == "REVIEW"
    assert by_id["NCS-UNRELATED-AND"]["company_fact_key"] == "vocational_training_scope_unparsed"
    assert by_id["NCS-SUFFIX-ANY"]["outcome"] == "PASS_CURRENT"
    assert by_id["NCS-SUFFIX-ANY"]["operator"] == "contains_any"
    assert by_id["NCS-NAME-PREFIX"]["outcome"] == "REVIEW"
    assert by_id["NCS-NAME-PREFIX"]["company_fact_key"] == "vocational_training_scope_unparsed"
    assert by_id["FACILITY-DATE-NOT-NCS"]["outcome"] == "PASS_CURRENT"
    assert by_id["FACILITY-DATE-NOT-NCS"]["company_fact_key"] == "designated_vocational_training"
    assert by_id["FACILITY-PERMIT-NOT-NCS"]["outcome"] == "PASS_CURRENT"
    assert by_id["FACILITY-PERMIT-NOT-NCS"]["company_fact_key"] == "designated_vocational_training"


def test_industry_code_parser_ignores_amounts_and_rejects_mixed_logic() -> None:
    result = classify_requirements(
        [
            requirement(
                "INDUSTRY-AMOUNT",
                "INDUSTRY_CODE",
                "업종코드 1261 등록 및 5000만원 이상 실적을 보유한 업체",
            ),
            requirement(
                "INDUSTRY-MIXED",
                "INDUSTRY_CODE",
                "업종코드 1261 및 업종코드 1263 또는 업종코드 7777 등록 업체",
            ),
            requirement(
                "INDUSTRY-NO-OPERATOR",
                "INDUSTRY_CODE",
                "업종코드 1169, 1261 등록 업체",
            ),
            requirement(
                "INDUSTRY-UNRELATED-AND",
                "INDUSTRY_CODE",
                "종합여행업 등록 및 업종코드 1261, 7777 등록 업체",
            ),
            requirement(
                "INDUSTRY-SUFFIX-ALL",
                "INDUSTRY_CODE",
                "업종코드 1169, 1261 모두 등록 업체",
            ),
            requirement(
                "INDUSTRY-SUFFIX-ANY",
                "INDUSTRY_CODE",
                "업종코드 1169, 1263 중 하나를 등록한 업체",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-08-05",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["INDUSTRY-AMOUNT"]["outcome"] == "PASS_CURRENT"
    assert by_id["INDUSTRY-AMOUNT"]["required_industry_codes"] == ["1261"]
    assert by_id["INDUSTRY-MIXED"]["outcome"] == "REVIEW"
    assert by_id["INDUSTRY-MIXED"]["company_fact_key"] == "ambiguous_industry_code_logic"
    assert by_id["INDUSTRY-NO-OPERATOR"]["outcome"] == "REVIEW"
    assert by_id["INDUSTRY-UNRELATED-AND"]["outcome"] == "REVIEW"
    assert by_id["INDUSTRY-UNRELATED-AND"]["company_fact_key"] == "ambiguous_industry_code_logic"
    assert by_id["INDUSTRY-SUFFIX-ALL"]["outcome"] == "PASS_CURRENT"
    assert by_id["INDUSTRY-SUFFIX-ALL"]["operator"] == "contains_all"
    assert by_id["INDUSTRY-SUFFIX-ANY"]["outcome"] == "PASS_CURRENT"
    assert by_id["INDUSTRY-SUFFIX-ANY"]["operator"] == "contains_any"


def test_compound_and_multiple_permit_gates_never_reuse_one_fact() -> None:
    result = classify_requirements(
        [
            requirement(
                "TWO-NAMED-PERMITS",
                "CERTIFICATION",
                "종합여행업 및 국제회의기획업을 모두 등록한 업체",
            ),
            requirement(
                "KNOWN-AND-OTHER-PERMIT",
                "CERTIFICATION",
                "종합여행업 등록업체이며 관광사업자 등록증을 보유해야 함",
            ),
            requirement(
                "PERMIT-AND-INDUSTRY",
                "INDUSTRY_CODE",
                "종합여행업 등록 및 학술연구용역(업종코드 1169) 등록을 모두 충족한 업체",
            ),
            requirement(
                "SAME-PERMIT-AND-CODE",
                "INDUSTRY_CODE",
                "종합여행업(업종코드 1261) 등록업체",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-08-05",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["TWO-NAMED-PERMITS"]["outcome"] == "REVIEW"
    assert by_id["TWO-NAMED-PERMITS"]["company_fact_key"] == "compound_named_permit_qualification"
    assert by_id["KNOWN-AND-OTHER-PERMIT"]["outcome"] == "REVIEW"
    assert by_id["KNOWN-AND-OTHER-PERMIT"]["company_fact_key"] == "compound_named_permit_qualification"
    assert by_id["PERMIT-AND-INDUSTRY"]["outcome"] == "REVIEW"
    assert by_id["PERMIT-AND-INDUSTRY"]["company_fact_key"] == "compound_notice_specific_qualification"
    assert by_id["SAME-PERMIT-AND-CODE"]["outcome"] == "PASS_CURRENT"
    assert by_id["SAME-PERMIT-AND-CODE"]["company_fact_key"] == "industry_code_inventory"
    assert by_id["SAME-PERMIT-AND-CODE"]["required_industry_codes"] == ["1261"]


def test_verified_permits_and_seoul_head_office_map_but_declared_branches_do_not() -> None:
    result = classify_requirements(
        [
            requirement("VIDEO", "CERTIFICATION", "비디오물제작업 신고 업체에 한함."),
            requirement("JOB", "CERTIFICATION", "국내 무료직업소개사업 등록업체에 한함."),
            requirement("SEOUL", "REGION", "법인등기부상 본점 소재지가 서울특별시인 업체에 한함."),
            requirement("BUSAN-BRANCH", "REGION", "부산광역시 지점 소재 업체에 한함."),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-10",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["VIDEO"]["company_fact_key"] == "video_production"
    assert by_id["VIDEO"]["outcome"] == "PASS_CURRENT"
    assert by_id["JOB"]["company_fact_key"] == "free_job_placement"
    assert by_id["JOB"]["outcome"] == "PASS_CURRENT"
    assert by_id["SEOUL"]["company_fact_key"] == "head_office_region_seoul"
    # head_office_region_seoul was last verified 2026-08-05; a 2026-09-10
    # deadline is inside the staleness window, so this is a confirmed PASS
    # (see the 2026-09-03 eligibility freshness fix changelog).
    assert by_id["SEOUL"]["outcome"] == "PASS_CURRENT"
    assert by_id["SEOUL"]["evaluation_fact_key"] == "head_office_region_seoul"
    assert by_id["SEOUL"]["deadline_check_required"] is True
    assert by_id["BUSAN-BRANCH"]["outcome"] == "REVIEW"

    snapshot_day = classify_requirements(
        [requirement("SEOUL-AS-OF", "REGION", "법인등기부상 본점 소재지가 서울특별시인 업체에 한함.")],
        profile=load_public_company_profile(),
        deadline="2026-08-05",
    )["items"][0]
    assert snapshot_day["outcome"] == "PASS_CURRENT"


def test_deadline_freshness_distinguishes_recheck_and_conditional_reconfirm() -> None:
    profile = load_public_company_profile()
    bidder_clause = requirement(
        "BIDDER-FRESHNESS",
        "SUBMISSION",
        "나라장터 경쟁입찰참가자격 등록을 완료해야 함",
    )
    at_snapshot = classify_requirements(
        [bidder_clause], profile=profile, deadline="2026-08-05"
    )["items"][0]
    after_snapshot = classify_requirements(
        [bidder_clause], profile=profile, deadline="2026-08-06"
    )["items"][0]

    assert at_snapshot["outcome"] == "PASS_CURRENT"
    assert at_snapshot["evaluation_fact_key"] == "bidder_registration"
    # A notice deadline one day after ``last_verified_at`` no longer forces a
    # REVIEW on its own: every open notice's deadline is, by definition, in
    # the future relative to a fixed verification snapshot, so gating on
    # "deadline > last_verified_at" made this branch fire unconditionally
    # (see the 2026-09-03 eligibility freshness fix changelog). Freshness is
    # now judged against the evaluation clock instead.
    assert after_snapshot["outcome"] == "PASS_CURRENT"
    assert after_snapshot["evaluation_fact_key"] == "bidder_registration"
    assert after_snapshot["deadline_check_required"] is True

    stale_snapshot = classify_requirements(
        [bidder_clause],
        profile=profile,
        deadline="2026-08-06",
        # bidder_registration was last verified 2026-08-05; push evaluation
        # well past the staleness horizon so the recheck gate must fire.
        evaluation_date="2027-06-01",
    )["items"][0]
    assert stale_snapshot["outcome"] == "REVIEW"
    assert stale_snapshot["evaluation_fact_key"] == "reconfirm.bidder_registration"

    ordinary_permit = classify_requirements(
        [requirement("PERMIT-ORDINARY", "CERTIFICATION", "종합여행업 등록 업체여야 함.")],
        profile=profile,
        deadline="2026-09-10",
    )["items"][0]
    current_copy_permit = classify_requirements(
        [
            requirement(
                "PERMIT-CURRENT-COPY",
                "CERTIFICATION",
                "유효기간 내 종합여행업 등록증을 보유한 업체여야 함.",
            )
        ],
        profile=profile,
        deadline="2026-09-10",
    )["items"][0]
    as_of_permit = classify_requirements(
        [
            requirement(
                "PERMIT-AS-OF",
                "CERTIFICATION",
                "공고일 현재 종합여행업 등록 업체여야 함.",
            )
        ],
        profile=profile,
        deadline="2026-09-10",
    )["items"][0]

    assert ordinary_permit["outcome"] == "PASS_CURRENT"
    assert current_copy_permit["outcome"] == "REVIEW"
    assert current_copy_permit["evaluation_fact_key"] == "reconfirm.general_travel_business"
    assert as_of_permit["outcome"] == "REVIEW"
    assert as_of_permit["evaluation_fact_key"] == "reconfirm.general_travel_business"


def test_permit_collection_does_not_pass_before_observation_date() -> None:
    clause = requirement(
        "TRAVEL-PERMIT",
        "CERTIFICATION",
        "종합여행업 등록 업체여야 함.",
    )

    historical = classify_requirements(
        [clause],
        profile=load_public_company_profile(),
        deadline="2026-09-02",
    )["items"][0]
    current = classify_requirements(
        [clause],
        profile=load_public_company_profile(),
        deadline="2026-09-03",
    )["items"][0]

    assert historical["outcome"] == "REVIEW"
    assert current["outcome"] == "PASS_CURRENT"


@pytest.mark.parametrize(
    "condition",
    [
        "나라장터에 입찰서 제출 마감일 전일까지 입찰참가자격을 등록해야 함",
        "나라장터에 입찰 참가 자격을 등록하여야 함",
        "나라장터 경쟁입찰 참가자격 등록을 완료해야 함",
    ],
)
def test_live_bidder_registration_wording_allows_particles_and_spacing(
    condition: str,
) -> None:
    result = classify_requirements(
        [requirement("LIVE-REGISTRATION", "SUBMISSION", condition)],
        profile=load_public_company_profile(),
        deadline="2026-08-05",
    )

    item = result["items"][0]
    assert item["policy_class"] == "ELIGIBILITY"
    assert item["company_fact_key"] == "bidder_registration"
    assert item["outcome"] == "PASS_CURRENT"
    assert item["blocking"] is False
    assert item["evidence"]["display_name"] == "경쟁입찰참가자격등록증"


def test_live_state_contract_qualification_and_restriction_map_to_distinct_facts() -> None:
    result = classify_requirements(
        [
            requirement(
                "LIVE-STATE-CONTRACT-QUALIFICATION",
                "ENTITY",
                (
                    "국가를 당사자로 하는 계약에 관한 법률 시행령 및 "
                    "시행규칙에 따른 자격 요건을 갖춘 자"
                ),
            ),
            requirement(
                "LIVE-STATE-CONTRACT-RESTRICTION",
                "SANCTION",
                (
                    "국가를 당사자로 하는 계약에 관한 법률 시행령 상 "
                    "입찰참가 제한 각호에 해당되지 않는 업체여야 함"
                ),
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-08-05",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    qualification = by_id["LIVE-STATE-CONTRACT-QUALIFICATION"]
    assert qualification["company_fact_key"] == "bidder_registration"
    assert qualification["outcome"] == "PASS_CURRENT"
    restriction = by_id["LIVE-STATE-CONTRACT-RESTRICTION"]
    assert restriction["company_fact_key"] == "sanction_clear"
    assert restriction["outcome"] == "PASS_CURRENT"


def test_current_sanction_snapshot_does_not_pass_before_observation_date() -> None:
    clause = requirement(
        "HISTORICAL-SANCTION",
        "SANCTION",
        "부정당업자 입찰참가자격 제한을 받고 있지 않아야 함.",
    )

    historical = classify_requirements(
        [clause],
        profile=load_public_company_profile(),
        deadline="2026-08-04",
    )["items"][0]
    current = classify_requirements(
        [clause],
        profile=load_public_company_profile(),
        deadline="2026-08-05",
    )["items"][0]

    assert historical["outcome"] == "REVIEW"
    assert current["outcome"] == "PASS_CURRENT"


def test_live_bid_bond_penalty_clause_is_not_sanction_clearance_pass() -> None:
    result = classify_requirements(
        [
            requirement(
                "LIVE-BID-BOND",
                "SANCTION",
                (
                    "입찰보증금은 원칙적으로 면제되나, 특정 사유(국고귀속 미수납, "
                    "채무불이행, 부정당업자 제재 등) 해당 시 입찰금액의 일정 "
                    "비율을 납부해야 함"
                ),
            )
        ],
        profile=load_public_company_profile(),
        deadline="2026-08-27",
    )

    item = result["items"][0]
    assert item["policy_class"] == "INFORMATION"
    assert item["company_fact_key"] is None
    assert item["outcome"] != "PASS_CURRENT"
    assert item["blocking"] is False


def test_checklist_and_information_items_remain_nonblocking() -> None:
    result = classify_requirements(
        [
            requirement(
                "NONBLOCKING-CHECKLIST",
                "SUBMISSION",
                "계약 체결 시 청렴계약이행서약서를 제출해야 함.",
            ),
            requirement(
                "NONBLOCKING-INFORMATION",
                "PERFORMANCE",
                "입찰가격은 부가가치세를 포함한 총액으로 제출해야 함.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-08-27",
    )

    assert [item["policy_class"] for item in result["items"]] == [
        "CHECKLIST",
        "INFORMATION",
    ]
    assert all(item["blocking"] is False for item in result["items"])


def test_vehicle_seat_count_is_not_misread_as_two_person_attendee_limit() -> None:
    result = classify_requirements(
        [
            requirement(
                "FACILITY-1",
                "FACILITY",
                (
                    "40인승 이상 차량 154대와 22인승 이상 중형버스 6대를 운행할 수 "
                    "있어야 하며 세부 좌석 기준은 45석 및 25석 이상임."
                ),
            ),
            requirement(
                "PERSONNEL-1",
                "PERSONNEL",
                "발표자와 참석자는 합계 2인 이내로 제한됨.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["FACILITY-1"]["company_fact_key"] is None
    assert by_id["FACILITY-1"]["policy_class"] != "CHECKLIST"
    assert by_id["PERSONNEL-1"]["company_fact_key"] == "attendee_limit_two"
    assert by_id["PERSONNEL-1"]["policy_class"] == "CHECKLIST"


def test_descriptive_entity_and_sme_performance_lookback_do_not_block_eligibility() -> None:
    result = classify_requirements(
        [
            requirement(
                "ENTITY-SUBJECT",
                "ENTITY",
                "경기도 신청사 인터넷전화서비스 용역을 대상으로 하는 입찰이다.",
            ),
            requirement(
                "ENTITY-CONTRACTOR",
                "ENTITY",
                "계약업체는 공개회사로 기재되어 있다.",
            ),
            requirement(
                "PERFORMANCE-LOOKBACK",
                "PERFORMANCE",
                (
                    "최근 5년간 실적을 평가하며 창업기업·소기업·소상공인은 "
                    "최근 7년간 실적을 적용한다."
                ),
            ),
            requirement(
                "SME-CERTIFICATE",
                "CERTIFICATION",
                "소기업·소상공인 확인서는 유효기간 내에 있어야 한다.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["ENTITY-SUBJECT"]["policy_class"] == "INFORMATION"
    assert by_id["ENTITY-SUBJECT"]["blocking"] is False
    assert by_id["ENTITY-CONTRACTOR"]["policy_class"] == "INFORMATION"
    assert by_id["PERFORMANCE-LOOKBACK"]["policy_class"] == "INFORMATION"
    assert by_id["PERFORMANCE-LOOKBACK"]["company_fact_key"] is None
    assert by_id["SME-CERTIFICATE"]["policy_class"] == "ELIGIBILITY"
    assert by_id["SME-CERTIFICATE"]["blocking"] is True


def test_notice_specific_goods_qualifications_and_contract_conduct_are_separated() -> None:
    result = classify_requirements(
        [
            requirement(
                "PRODUCT-REGISTRATION",
                "INDUSTRY_CODE",
                "나라장터 세부품명번호 5종을 제조물품으로 등록한 업체여야 함.",
            ),
            requirement(
                "DIRECT-PRODUCTION",
                "DIRECT_PRODUCTION",
                "공고 지정 품목의 직접생산확인증명서를 보유해야 함.",
            ),
            requirement(
                "CONTRACT-SUBJECT",
                "ENTITY",
                "전국기능경기대회 경기용 재료 구매 계약업체로 참여해야 함.",
            ),
            requirement(
                "ANTI-BRIBERY",
                "SANCTION",
                "입찰·계약 과정에서 금품·향응·취업 제공을 요구하거나 수수하지 않아야 함.",
            ),
            requirement(
                "ANTI-COLLUSION",
                "SANCTION",
                "입찰가격 사전 협의 또는 특정인 낙찰을 위한 담합을 하지 않아야 함.",
            ),
            requirement(
                "ORIGIN-MARKING",
                "REGION",
                "대한민국 외에서 제조된 계약물품은 원산지를 표시해야 함.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-03",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["PRODUCT-REGISTRATION"]["policy_class"] == "ELIGIBILITY"
    assert by_id["PRODUCT-REGISTRATION"]["outcome"] == "REVIEW"
    assert by_id["PRODUCT-REGISTRATION"]["company_fact_key"] == (
        "notice_specific_product_registration"
    )
    assert by_id["DIRECT-PRODUCTION"]["policy_class"] == "ELIGIBILITY"
    assert by_id["DIRECT-PRODUCTION"]["outcome"] == "FAIL_CONFIRMED"
    assert by_id["DIRECT-PRODUCTION"]["company_fact_key"] == (
        "direct_production_certificate"
    )
    assert by_id["CONTRACT-SUBJECT"]["policy_class"] == "INFORMATION"
    assert by_id["ANTI-BRIBERY"]["policy_class"] == "CHECKLIST"
    assert by_id["ANTI-COLLUSION"]["policy_class"] == "CHECKLIST"
    assert by_id["ORIGIN-MARKING"]["policy_class"] == "INFORMATION"
    assert result["blocking_items"] == 2


def test_compound_goods_qualification_never_uses_generic_registration_pass() -> None:
    result = classify_requirements(
        [
            requirement(
                "COMPOUND-GOODS",
                "INDUSTRY_CODE",
                (
                    "나라장터 세부품명번호를 제조물품으로 등록하고 "
                    "직접생산확인증명서와 소기업·소상공인 확인서를 보유해야 함."
                ),
            )
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )

    item = result["items"][0]
    assert item["policy_class"] == "ELIGIBILITY"
    assert item["outcome"] == "REVIEW"
    assert item["blocking"] is True
    assert item["company_fact_key"] == "compound_notice_specific_qualification"


def test_unknown_entity_region_and_sanction_clauses_remain_fail_closed() -> None:
    result = classify_requirements(
        [
            requirement(
                "UNKNOWN-ENTITY",
                "ENTITY",
                "법인 또는 개인사업자만 참여할 수 있음.",
            ),
            requirement(
                "UNKNOWN-REGION",
                "REGION",
                "서울시에 소재한 업체에 한함.",
            ),
            requirement(
                "UNKNOWN-SANCTION",
                "SANCTION",
                "최근 영업정지 이력이 없는 업체여야 함.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )

    assert {item["policy_class"] for item in result["items"]} == {"ELIGIBILITY"}
    assert {item["outcome"] for item in result["items"]} == {"REVIEW"}
    assert result["blocking_items"] == 3


def test_known_information_guards_do_not_override_embedded_eligibility() -> None:
    result = classify_requirements(
        [
            requirement(
                "PRODUCT-CONTRACT",
                "ENTITY",
                "세부품명 등록을 완료한 계약업체로 참여해야 함.",
            ),
            requirement(
                "BIDDER-CONTRACT",
                "ENTITY",
                "경쟁입찰참가자격 등록을 완료한 계약업체로 참여해야 함.",
            ),
            requirement(
                "COLLUSION-EXCLUSION",
                "SANCTION",
                "담합 사실이 있는 업체는 입찰에 참가할 수 없음.",
            ),
            requirement(
                "ORIGIN-CAPABILITY",
                "REGION",
                "원산지를 표시할 수 있는 업체에 한함.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["PRODUCT-CONTRACT"]["policy_class"] == "ELIGIBILITY"
    assert by_id["PRODUCT-CONTRACT"]["company_fact_key"] == (
        "notice_specific_product_registration"
    )
    assert by_id["BIDDER-CONTRACT"]["policy_class"] == "ELIGIBILITY"
    assert by_id["BIDDER-CONTRACT"]["company_fact_key"] == "bidder_registration"
    # bidder_registration was last verified 2026-08-05; a 2026-09-01 deadline
    # is inside the staleness window, so this is a confirmed PASS (see the
    # 2026-09-03 eligibility freshness fix changelog).
    assert by_id["BIDDER-CONTRACT"]["outcome"] == "PASS_CURRENT"
    assert by_id["COLLUSION-EXCLUSION"]["policy_class"] == "ELIGIBILITY"
    assert by_id["ORIGIN-CAPABILITY"]["policy_class"] == "ELIGIBILITY"


def test_live_descriptions_and_post_award_duties_do_not_block_eligibility() -> None:
    result = classify_requirements(
        [
            requirement(
                "SERVICE-NAME",
                "ENTITY",
                "용역명은 캄보디아 교육 기자재 공급 용역이다.",
            ),
            requirement(
                "TARGET-BUSINESS",
                "ENTITY",
                "용역 입찰의 대상 사업은 캄보디아 교육 기자재 공급 용역임.",
            ),
            requirement(
                "CONTRACT-SCOPE",
                "ENTITY",
                "캄보디아 교육 기자재 공급 용역에 관한 계약이어야 한다.",
            ),
            requirement(
                "DELIVERY-PLACE",
                "REGION",
                "최종 납품 장소는 캄보디아 프놈펜 National Employment Agency이다.",
            ),
            requirement(
                "DOMESTIC-BID",
                "REGION",
                "국내입찰로 진행됨.",
            ),
            requirement(
                "STRATEGIC-GOODS",
                "CERTIFICATION",
                "계약업체는 납품 전 무역안보관리원에 전략물자 전문판정을 의뢰하여 받아야 한다.",
            ),
            requirement(
                "OFFICE-LICENSE",
                "CERTIFICATION",
                "MS 오피스 라이센스는 정품 구매·활성화를 포함하고 Windows 11과 호환되어야 함.",
            ),
            requirement(
                "INTEGRITY-ACK",
                "SANCTION",
                "입찰참여자는 청렴계약서 제출에 동의한 것으로 간주되며 조건을 준수해야 함.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    for key in (
        "SERVICE-NAME",
        "TARGET-BUSINESS",
        "CONTRACT-SCOPE",
        "DELIVERY-PLACE",
        "DOMESTIC-BID",
    ):
        assert by_id[key]["policy_class"] == "INFORMATION"
        assert by_id[key]["blocking"] is False
    assert by_id["STRATEGIC-GOODS"]["policy_class"] == "CHECKLIST"
    assert by_id["OFFICE-LICENSE"]["policy_class"] == "CHECKLIST"
    assert by_id["INTEGRITY-ACK"]["policy_class"] == "CHECKLIST"
    assert result["blocking_items"] == 0


def test_description_guards_keep_embedded_bidder_gates_fail_closed() -> None:
    result = classify_requirements(
        [
            requirement(
                "NAMED-ENTITY-GATE",
                "ENTITY",
                "사업명은 교육 기자재 공급이며 법인 또는 개인사업자 업체에 한함.",
            ),
            requirement(
                "DELIVERY-REGION-GATE",
                "REGION",
                "최종 납품장소는 서울이며 서울시에 소재한 업체에 한함.",
            ),
            requirement(
                "LICENSE-GATE",
                "CERTIFICATION",
                "정품 소프트웨어 라이선스를 보유한 업체에 한함.",
            ),
            requirement(
                "PRE-BID-CERTIFICATE",
                "CERTIFICATION",
                "입찰마감일까지 전략물자 판정서를 보유한 업체여야 함.",
            ),
            requirement(
                "INTEGRITY-EXCLUSION",
                "SANCTION",
                "청렴계약 위반 업체는 입찰에 참가할 수 없음.",
            ),
            requirement(
                "NAMED-PARTICIPANT-GATE",
                "ENTITY",
                "사업명은 교육 기자재 공급이며 참가대상은 관련 실적 보유자임.",
            ),
            requirement(
                "REGION-PARTICIPATION-GATE",
                "REGION",
                "납품장소는 제주이며 제주 소재 업체만 참여 가능.",
            ),
            requirement(
                "OFFICE-SUPPLIER-GATE",
                "CERTIFICATION",
                "정품 MS Office를 공급 가능한 업체만 참여 가능.",
            ),
            requirement(
                "STRATEGIC-REGISTRATION-GATE",
                "CERTIFICATION",
                "계약 후 납품 전 전략물자 전문판정을 받아야 하며 판정기관으로 등록한 업체만 가능.",
            ),
            requirement(
                "INTEGRITY-TARGET-EXCLUSION",
                "SANCTION",
                "청렴계약 준수 의무 위반 업체는 참가대상에서 제외한다.",
            ),
        ],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )

    assert {item["policy_class"] for item in result["items"]} == {"ELIGIBILITY"}
    assert {item["outcome"] for item in result["items"]} == {"REVIEW"}
    assert result["blocking_items"] == 10


def test_profile_and_policy_api_use_repository_data(client: TestClient) -> None:
    profile_response = client.get("/api/v1/company-profile")
    assert profile_response.status_code == 200
    assert profile_response.json()["classification"] == "PUBLIC_SAFE_COMPANY_PROFILE"

    notice_response = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "PUBLIC-POLICY-001",
            "bid_notice_no": "PUBLIC-POLICY-001",
            "title": "공개 프로필 판단 기준 시험",
            "deadline": "2026-01-14T09:00:00Z",
        },
    )
    assert notice_response.status_code == 201
    version_response = client.post(
        "/api/v1/notices/PUBLIC-POLICY-001/versions",
        json={
            "version_no": 1,
            "file_sha256": "a" * 64,
            "source_payload": {
                "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                "status": "ACCEPTED",
                "result": {"requirements": INCHON_REQUIREMENTS},
            },
        },
    )
    assert version_response.status_code == 201

    policy_response = client.get(
        "/api/v1/notices/PUBLIC-POLICY-001/analysis/requirement-policy"
    )
    assert policy_response.status_code == 200
    payload = policy_response.json()
    assert payload["profile_classification"] == "PUBLIC_SAFE_COMPANY_PROFILE"
    assert payload["counts"]["ACTION_REQUIRED"] == 1
    assert payload["blocking_actions"] == 1
    assert payload["groups"]["ELIGIBILITY"][0]["evidence"]["sha256"]


def test_policy_api_combines_latest_requirements_from_every_attachment(
    client: TestClient,
) -> None:
    notice_key = "PUBLIC-POLICY-MULTI-001"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": notice_key,
            "title": "다중 첨부 정책 결합 시험",
            "deadline": "2026-09-01T09:00:00Z",
        },
    ).status_code == 201
    for version_no, label, digest, extracted_requirement in (
        (
            1,
            "입찰공고문.pdf",
            "b" * 64,
            requirement(
                "REQ-001",
                "ENTITY",
                "경쟁입찰참가자격 등록을 완료한 업체여야 함.",
            ),
        ),
        (
            2,
            "계약예규.zip",
            "c" * 64,
            requirement(
                "REQ-001",
                "SANCTION",
                "입찰가격 사전 협의 또는 특정인 낙찰을 위한 담합을 하지 않아야 함.",
            ),
        ),
    ):
        response = client.post(
            f"/api/v1/notices/{notice_key}/versions",
            json={
                "version_no": version_no,
                "file_sha256": digest,
                "source_payload": {
                    "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                    "status": "ACCEPTED",
                    "source_label": label,
                    "result": {"requirements": [extracted_requirement]},
                },
            },
        )
        assert response.status_code == 201, response.text

    response = client.get(
        f"/api/v1/notices/{notice_key}/analysis/requirement-policy"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["analysis_source_count"] == 2
    assert len(payload["analysis_version_ids"]) == 2
    assert payload["counts"] == {
        "ELIGIBILITY": 1,
        "ACTION_REQUIRED": 0,
        "CHECKLIST": 1,
        "INFORMATION": 0,
    }
    assert len({item["requirement_id"] for item in payload["items"]}) == 2


def test_policy_api_does_not_revive_stale_pps_source_as_legacy(
    client: TestClient,
) -> None:
    notice_key = "STALE-PPS-POLICY-001"
    assert client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": notice_key,
            "title": "교체된 과거 첨부 정책 시험",
            "deadline": "2026-09-01T09:00:00Z",
        },
    ).status_code == 201
    assert client.post(
        f"/api/v1/notices/{notice_key}/versions",
        json={
            "version_no": 1,
            "file_sha256": "d" * 64,
            "document_complete": True,
            "extraction_status": "ACCEPTED",
            "extraction_confidence": 0.99,
            "source_payload": {
                "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                "source_kind": "PPS_PUBLIC_ATTACHMENT",
                "status": "ACCEPTED",
                "prompt_version": "stale-prompt-version",
                "processing_version": "stale-processing-version",
                "attachment_id": "PPS-ATT-stale",
                "result": {
                    "document_type": "NOTICE",
                    "requirements": [
                        requirement(
                            "STALE-REQ",
                            "ENTITY",
                            "경쟁입찰참가자격 등록을 완료한 업체여야 함.",
                        )
                    ],
                },
            },
        },
    ).status_code == 201

    response = client.get(
        f"/api/v1/notices/{notice_key}/analysis/requirement-policy"
    )
    assert response.status_code == 422


def test_policy_api_requires_accepted_extraction(client: TestClient) -> None:
    client.post(
        "/api/v1/notices",
        json={
            "notice_key": "NO-EXTRACTION",
            "bid_notice_no": "NO-EXTRACTION",
            "title": "추출 전 공고",
            "deadline": "2026-09-01T09:00:00Z",
        },
    )

    response = client.get("/api/v1/notices/NO-EXTRACTION/analysis/requirement-policy")
    assert response.status_code == 422


_STATUTORY_AND_CLAUSE = (
    "지방계약법 시행령 제13조·시행규칙 제14조의 자격요건을 구비하고 "
    "시행령 제92조(부정당업자 제재) 해당사항이 없는 업체여야 함"
)


@pytest.mark.parametrize("missing", (None, "bidder_registration", "sanction_clear"))
def test_statutory_and_clause_checks_both_independent_facts(missing) -> None:
    profile = load_public_company_profile()
    if missing:
        profile["facts"].pop(missing)
    original = requirement("SYN-STATUTORY-AND", "ENTITY", _STATUTORY_AND_CLAUSE)
    original["evidence"] = [{"quote": _STATUTORY_AND_CLAUSE}]
    result = classify_requirements([original], profile=profile, deadline="2026-09-06", evaluation_date="2026-09-06")
    items = result["items"]
    assert len(items) == 2
    assert {item["company_fact_key"] for item in items} == {"bidder_registration", "sanction_clear"}
    assert all(item["outcome"] == "PASS_CURRENT" for item in items) is (missing is None)
    assert result["blocking_items"] == (1 if missing else 0)
    assert original["normalized_condition"] == _STATUTORY_AND_CLAUSE
    assert len({item["requirement_id"] for item in items}) == 2


@pytest.mark.parametrize("mutation", ("extra-qualification", "or", "future-penalty"))
def test_unrecognized_statutory_compounds_never_pass_on_one_fact(mutation) -> None:
    clause = _STATUTORY_AND_CLAUSE
    if mutation == "extra-qualification":
        clause += ". 별도 허가증을 보유해야 함"
    elif mutation == "or":
        clause = clause.replace("구비하고", "구비하거나")
    else:
        clause = clause.replace("해당사항이 없는 업체여야 함", "대상이 될 수 있음")
    items = classify_requirements(
        [requirement("SYN-STATUTORY-MUTATION", "ENTITY", clause)],
        profile=load_public_company_profile(), deadline="2026-09-06", evaluation_date="2026-09-06",
    )["items"]
    assert len(items) == 1
    assert not items[0]["outcome"].startswith("PASS")


def test_statutory_split_preserves_original_source_evidence_and_is_idempotent() -> None:
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    original = requirement("SYN-STATUTORY-EVIDENCE", "ENTITY", _STATUTORY_AND_CLAUSE)
    original["evidence"] = [{"quote": _STATUTORY_AND_CLAUSE, "page": 2}]
    split = expand_statutory_qualification_requirements([original])
    assert len(split) == 2
    assert all(item["evidence"] == original["evidence"] for item in split)
    assert all(item["normalized_condition"] in _STATUTORY_AND_CLAUSE for item in split)
    assert expand_statutory_qualification_requirements(split) == split



def test_sanction_fact_does_not_prove_financial_solvency() -> None:
    clause = "입찰등록마감일 현재 부정당업자로 지정되지 않았으며, 부도·파산·금융신용 부실로 영업활동에 지장이 없는 업체"
    item = classify_requirements(
        [requirement("SYN-FINANCIAL-COMPOUND", "SANCTION", clause)],
        profile=load_public_company_profile(), deadline="2026-09-06", evaluation_date="2026-09-06",
    )["items"][0]
    assert item["outcome"] == "REVIEW"
    assert item["company_fact_key"] == "compound_sanction_and_financial_qualification"
    assert item["blocking"] is True


def test_ambiguous_statutory_clause_remains_unsplit() -> None:
    from pai_loop.eligibility_policy import expand_statutory_qualification_requirements
    item = requirement("SYN-AMBIGUOUS-STATUTORY", "ENTITY", _STATUTORY_AND_CLAUSE)
    item["ambiguity_reason"] = "추가 자격조건 적용 대상 확인 필요"
    assert expand_statutory_qualification_requirements([item]) == [item]


@pytest.mark.parametrize("law", ("국가계약법 시행령 제12조 및 시행규칙 제14조", "지방계약법 시행령 제13조 및 시행규칙 제14조"))
def test_statutory_qualification_future_possession_form_uses_registration(law) -> None:
    clause = law + "에 의한 입찰참가자격을 갖출 것"
    item = classify_requirements(
        [requirement("SYN-STATUTORY-POSSESSION", "ENTITY", clause)],
        profile=load_public_company_profile(), deadline="2026-09-06", evaluation_date="2026-09-06",
    )["items"][0]
    assert item["company_fact_key"] == "bidder_registration"
    assert item["outcome"] == "PASS_CURRENT"


def test_statutory_possession_form_does_not_imply_registration_evidence() -> None:
    profile = load_public_company_profile()
    profile["facts"].pop("bidder_registration")
    item = classify_requirements(
        [requirement("SYN-STATUTORY-NO-FACT", "ENTITY", "국가계약법에 의한 입찰참가자격을 갖출 것")],
        profile=profile, deadline="2026-09-06", evaluation_date="2026-09-06",
    )["items"][0]
    assert item["outcome"] == "REVIEW"


@pytest.mark.parametrize("condition", [
    "경쟁입찰참가자격등록증 사본 1부를 제출해야 한다.",
    "입찰참가자격등록증과 사업자등록증을 첨부해야 한다.",
    "제출서류: 경쟁 입찰 참가 자격 등록증, 인감증명서 각 1부",
    "참가신청서, 경쟁입찰참가자격등록증 등을 봉투에 넣어 제출한다.",
])
def test_registration_certificate_copy_is_a_submission_checklist(condition):
    item = classify_requirements(
        [requirement("SYN-REGISTRATION-COPY", "SUBMISSION", condition)],
        profile=load_public_company_profile(), deadline="2026-09-10", evaluation_date="2026-09-06",
    )["items"][0]
    assert item["company_fact_key"] != "bidder_registration"
    assert item["policy_class"] == "CHECKLIST"


def test_certificate_copy_does_not_erase_independent_registration_obligation():
    item = classify_requirements(
        [requirement("SYN-REGISTRATION-AND-COPY", "SUBMISSION",
          "나라장터 입찰참가자격 등록을 마감일까지 완료하고 경쟁입찰참가자격등록증 사본 1부를 제출한다.")],
        profile=load_public_company_profile(), deadline="2026-09-10", evaluation_date="2026-09-06",
    )["items"][0]
    assert item["company_fact_key"] == "bidder_registration"
