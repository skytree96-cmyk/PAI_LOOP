"""Industry codes with a required prefix and paired alternatives (2026-10-05)."""
import pytest

from pai_loop.eligibility_policy import classify_requirements, load_public_company_profile


def item(condition):
    return classify_requirements(
        [{"requirement_id": "SYN-NESTED", "category": "INDUSTRY_CODE",
          "normalized_condition": condition, "mandatory": True}],
        profile=load_public_company_profile(), deadline="2026-10-12",
    )["items"][0]


@pytest.mark.parametrize("condition", [
    "전자입찰서 제출마감일 전일까지 학술연구용역(업종코드 1169) 및 이러닝사업자 신고 업체로서, 디지털콘텐츠개발서비스사업(1469)"
    "+이러닝콘텐츠업(6527) 또는 디지털콘텐츠개발서비스사업(1469)+이러닝서비스업(6529) 또는 기타용역(9999)으로 등록된 업체여야 함",
    "전자입찰서 제출마감일 전일까지 학술연구용역(업종코드 1169) 및 이러닝사업자로 신고한 업체로서, 디지털콘텐츠개발서비스사업(1469)"
    "과 이러닝콘텐츠업(6527) 또는 디지털콘텐츠개발서비스사업(1469)과 이러닝서비스업(6529) 또는 기타용역(9999)으로 등록된 업체여야 함",
])
def test_required_prefix_and_one_held_pair_pass(condition):
    result = item(condition)
    assert result["outcome"] == "PASS_CURRENT"
    assert result["required_value"] == ["1169", "1469", "6529"]
    assert result["component_fact_keys"] == ["industry_code_inventory", "elearning_service"]


def test_no_held_alternative_is_a_confirmed_absence():
    result = item("회계법인(1200) 업체로서, 정보보호(1542)+보안관제(6526) 또는 감리(6146)로 등록된 업체여야 함")
    assert result["outcome"] == "FAIL_CONFIRMED"


@pytest.mark.parametrize("condition", [
    # another permit in the prefix
    "학술연구용역(1169) 및 직접생산확인서 소지 업체로서, 1469+6527 또는 1469+6529로 등록된 업체",
    # no paired alternative: the existing simple templates decide
    "학술연구용역(1169) 업체로서, 6527 또는 6529로 등록된 업체",
    # a pair written without + / 와 / 과 is not read as a pair
    "학술연구용역(1169) 업체로서, 1469 6527 또는 1469 6529로 등록된 업체",
])
def test_other_shapes_are_not_read_as_nested_pairs(condition):
    result = item(condition)
    assert result.get("component_fact_keys") != ["industry_code_inventory", "elearning_service"]
    assert result.get("message", "").find("중첩 업종 조합") < 0
