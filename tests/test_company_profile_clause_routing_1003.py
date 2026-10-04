"""Notice clauses resolved by the company facts confirmed on 2026-10-03 (or set aside as non-eligibility)."""
import pytest

from pai_loop.eligibility_policy import classify_requirements, load_public_company_profile


def item(condition, category):
    return classify_requirements(
        [{"requirement_id": "SYN-PROFILE", "category": category,
          "normalized_condition": condition, "mandatory": True}],
        profile=load_public_company_profile(), deadline="2026-10-12",
    )["items"][0]


@pytest.mark.parametrize("condition,category,fact", [
    ("식품위생법상 식품접객업 중 위탁급식영업으로 신고된 업체여야 함", "CERTIFICATION", "catering_business"),
    ("전기통신사업법 제6조에 따라 과학기술정보통신부장관으로부터 허가받은 기간통신사업자여야 함", "CERTIFICATION", "telecom_carrier"),
    ("공고일 현재 행정안전부에 정보시스템 감리법인으로 등록된 업체만 입찰 참가 가능", "ENTITY", "information_system_audit_firm"),
    ("건설엔지니어링업(종합) 또는 건설엔지니어링업(설계·사업관리-일반) 등록 필요", "CERTIFICATION", "construction_engineering_business"),
    ("ISO 13485(의료기기 품질경영시스템) 인증 보유", "CERTIFICATION", "iso_13485"),
    ("AWS 클라우드가 인정한 파트너사여야 하며 파트너 확인서 제출(실적증명서로 대체 가능)", "CERTIFICATION", "aws_partner"),
    ("입찰공고일 현재 국제항공운송협회(IATA)의 BSP 가입 여행사로서 직접발권이 가능한 업체여야 함", "CERTIFICATION", "iata_bsp_member"),
])
def test_permits_the_company_does_not_hold_fail_confirmed(condition, category, fact):
    result = item(condition, category)
    assert result["outcome"] == "FAIL_CONFIRMED"
    assert result["company_fact_key"] == fact


@pytest.mark.parametrize("condition,category,fact", [
    ("관광진흥법 제4조 및 동법시행령 제2조에 따른 일반여행업 또는 국내여행업 등록을 필한 업체여야 함", "ENTITY", "general_travel_business"),
    ("관광진흥법 제9조 및 동법시행규칙 제18조에 의거 보험가입을 필한 여행업체여야 함", "CERTIFICATION", "travel_business_guarantee_insurance"),
    ("국가종합전자조달시스템(G2B, 나라장터)에 입찰참가 등록한 업체여야 함", "ENTITY", "bidder_registration"),
    ("국가계약법 시행령 제12조 및 시행규칙 제14조에 따른 경쟁입찰 참가자격이 있는 자", "ENTITY", "bidder_registration"),
    ("소프트웨어산업진흥법 제24조 및 동법시행령 제17조에 따른 소프트웨어사업자여야 함", "INDUSTRY_CODE", "software_computer_related_services"),
    ("입찰공고일 현재 사업자등록증상 교육서비스업으로 등록되어 있어야 함", "INDUSTRY_CODE", "business_registration_types"),
])
def test_clauses_backed_by_company_facts_pass(condition, category, fact):
    result = item(condition, category)
    assert result["outcome"] == "PASS_CURRENT"
    assert result["company_fact_key"] == fact


@pytest.mark.parametrize("condition", [
    "종합여행업 또는 국외여행업 등록 및 실적을 보유해야 함",              # another gate: performance
    "종합여행업 및 국제회의기획업을 모두 등록한 업체",                  # another named permit
    "종합여행업 등록 및 학술연구용역(업종코드 1169) 등록을 모두 충족한 업체",  # an industry code
])
def test_travel_family_never_absorbs_another_gate(condition):
    assert item(condition, "CERTIFICATION")["company_fact_key"] != "general_travel_business" or \
        item(condition, "CERTIFICATION")["outcome"] != "PASS_CURRENT"


@pytest.mark.parametrize("condition,outcome,required", [
    ("G2B에 교육연수원(3193)·교육행정연수원(3194)·종합교육연수원(3195) 중 하나와 기타자유업[행사대행업](9901)을 등록해야 함",
     "FAIL_CONFIRMED", ["3193", "9901"]),
    ("나라장터에 소프트웨어사업자(컴퓨터관련서비스사업)(1468)와 학술·연구용역(1169) 또는 기타자유업종(9999) 업종을 모두 등록해야 함",
     "PASS_CURRENT", ["1468", "1169"]),
    ("나라장터에 소프트웨어사업자(컴퓨터관련서비스사업)(1468)와 정보통신공사업(0036) 업종을 모두 등록해야 함",
     "FAIL_CONFIRMED", ["1468", "0036"]),
    ("나라장터(G2B) 소프트웨어사업자로 컴퓨터관련서비스사업(1468) 또는 데이터베이스제작 및 검색서비스사업(1470) 중 하나 이상 등록되어 있어야 함",
     "PASS_CURRENT", ["1468"]),
])
def test_industry_code_and_or_templates(condition, outcome, required):
    result = item(condition, "INDUSTRY_CODE")
    assert result["outcome"] == outcome
    assert result["required_value"] == required


def test_training_facility_outside_seoul_fails_and_location_notes_are_information():
    assert item("경기도 지역에 소재한 교육(연수) 시설을 보유하고 있어야 함", "REGION")["outcome"] == "FAIL_CONFIRMED"
    assert item("교육 지역은 부산광역시 일대로 지정됨", "REGION")["policy_class"] == "INFORMATION"


@pytest.mark.parametrize("condition,policy_class", [
    ("노후 네트워크 스위치 장비는 CC인증 또는 보안기능 확인서(안전성 검증)를 획득한 제품이어야 함", "INFORMATION"),
    ("손해배상책임보험증서 또는 공제증서를 수요기관에 제출하여야 함", "INFORMATION"),
    ("현장대리인, 품질관리자, 안전관리자는 각 관계법령에 따른 자격을 갖추어야 함", "CHECKLIST"),
])
def test_product_procedure_and_personnel_clauses_are_not_company_gates(condition, policy_class):
    assert item(condition, "CERTIFICATION")["policy_class"] == policy_class


@pytest.mark.parametrize("condition,category", [
    ("입찰공고일 기준 2년 이내 영남대학교 발주 계약의 계약대상자로서 계약체결 이후부터 준공(완료)까지 계약불이행 또는 지체 등으로 부과금을 납부한 사업자는 입찰참가 불가", "ENTITY"),
    ("입찰공고일 기준 2년 이내 우리대학교 발주 계약의 계약대상자로서 계약체결 이후부터 준공까지 계약불이행 또는 지체 등으로 부과금을 납부한 적이 있는 사업자는 입찰 참가 불가", "SANCTION"),
])
def test_agency_contract_penalty_history_uses_the_confirmed_clear_record(condition, category):
    # Company confirmed no non-performance or penalty history on 2026-10-04.
    result = item(condition, category)
    assert result["outcome"] == "PASS_CURRENT"
    assert result["company_fact_key"] == "contract_nonperformance_clear"


@pytest.mark.parametrize("condition", [
    "본교를 상대로 소송 이력(소송 중 포함)이 있거나 계약불이행으로 부과금을 납부한 사업자는 입찰 참가 불가",
    "입찰공고일 기준 2년 이내 본교 입찰·계약·계약이행 등에서 물의를 일으킨 업체는 입찰 참가 불가",
])
def test_lawsuit_and_vague_misconduct_histories_stay_in_review(condition):
    assert item(condition, "SANCTION")["outcome"] == "REVIEW"
