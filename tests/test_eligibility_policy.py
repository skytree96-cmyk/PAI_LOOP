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
        deadline="2026-01-14",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert set(result["groups"]) == {
        "ELIGIBILITY",
        "ACTION_REQUIRED",
        "CHECKLIST",
        "INFORMATION",
    }
    assert result["blocking_actions"] == 1
    assert by_id["REQ-001"]["outcome"] == "PASS_CURRENT"
    assert by_id["REQ-001"]["evidence"]["display_name"] == "경쟁입찰참가자격등록증"
    assert by_id["REQ-002"]["outcome"] == "PASS_CURRENT"
    assert by_id["REQ-002"]["deadline_check_required"] is True
    assert by_id["REQ-003"]["policy_class"] == "CHECKLIST"
    assert by_id["REQ-003"]["outcome"] == "READY"
    assert by_id["REQ-004"]["outcome"] == "PASS_EXCEPTION"
    assert by_id["REQ-005"]["outcome"] == "PASS_EXCEPTION"
    assert by_id["REQ-006"]["outcome"] == "PASS_CURRENT"
    assert by_id["REQ-008"]["policy_class"] == "INFORMATION"
    assert by_id["REQ-010"]["policy_class"] == "INFORMATION"
    assert by_id["REQ-018"]["policy_class"] == "ACTION_REQUIRED"
    assert by_id["REQ-018"]["outcome"] == "BLOCK_UNTIL_CONFIRMED"
    assert by_id["REQ-018"]["blocking"] is True
    assert "REVIEW" not in by_id["REQ-018"]["message"]
    assert by_id["REQ-019"]["policy_class"] == "CHECKLIST"
    assert by_id["REQ-022"]["policy_class"] == "INFORMATION"


def test_nonprofit_exception_is_not_inferred_when_notice_does_not_offer_it() -> None:
    result = classify_requirements(
        [requirement("SMALL-1", "CERTIFICATION", "소기업·소상공인 확인서를 보유해야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-09-01",
    )

    assert result["items"][0]["policy_class"] == "ELIGIBILITY"
    assert result["items"][0]["outcome"] == "REVIEW"
    assert result["items"][0]["blocking"] is True


def test_future_conviction_check_does_not_overextend_current_declaration() -> None:
    result = classify_requirements(
        [requirement("SANCTION-1", "SANCTION", "조세포탈 유죄판결이 없어야 함.")],
        profile=load_public_company_profile(),
        deadline="2026-12-31",
    )

    item = result["items"][0]
    assert item["outcome"] == "REVIEW"
    assert item["blocking"] is True
    assert item["deadline_check_required"] is True
    assert result["blocking_actions"] == 0
    assert result["blocking_items"] == 1


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
        deadline="2026-08-27",
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
        deadline="2026-08-27",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    qualification = by_id["LIVE-STATE-CONTRACT-QUALIFICATION"]
    assert qualification["company_fact_key"] == "bidder_registration"
    assert qualification["outcome"] == "PASS_CURRENT"
    restriction = by_id["LIVE-STATE-CONTRACT-RESTRICTION"]
    assert restriction["company_fact_key"] == "sanction_clear"
    assert restriction["outcome"] == "PASS_CURRENT"


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
        deadline="2026-09-01",
    )
    by_id = {item["requirement_id"]: item for item in result["items"]}

    assert by_id["PRODUCT-REGISTRATION"]["policy_class"] == "ELIGIBILITY"
    assert by_id["PRODUCT-REGISTRATION"]["outcome"] == "REVIEW"
    assert by_id["PRODUCT-REGISTRATION"]["company_fact_key"] == (
        "notice_specific_product_registration"
    )
    assert by_id["DIRECT-PRODUCTION"]["policy_class"] == "ELIGIBILITY"
    assert by_id["DIRECT-PRODUCTION"]["outcome"] == "REVIEW"
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
    assert by_id["BIDDER-CONTRACT"]["outcome"] == "PASS_CURRENT"
    assert by_id["COLLUSION-EXCLUSION"]["policy_class"] == "ELIGIBILITY"
    assert by_id["ORIGIN-CAPABILITY"]["policy_class"] == "ELIGIBILITY"


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
