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
