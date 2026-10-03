"""Clauses filed under SANCTION in October 2026 notices that used to stay at notice_sanction_eligibility."""
import pytest

from pai_loop.eligibility_policy import classify_requirements, load_public_company_profile


def item(condition):
    return classify_requirements(
        [{"requirement_id": "SYN-SANCTION", "category": "SANCTION",
          "normalized_condition": condition, "mandatory": True}],
        profile=load_public_company_profile(), deadline="2026-10-12",
    )["items"][0]


@pytest.mark.parametrize("condition,fact", [
    ("입찰등록 마감일 현재 지방계약법 제31조 및 시행령 제92조에 따른 부정당업자 입찰참가자격 제한 대상이 아닌 사업자", "sanction_clear"),
    ("국가계약법, 지방계약법 또는 공공기관운영법에 따라 부정당업자로 제재 처분기간 중인 자는 입찰 참가 불가", "sanction_clear"),
    ("국가계약법 시행령 제76조의 제한을 받은 사실이 없는 업체여야 함", "sanction_clear"),
    ("국가계약법 시행령 제39조 제4항 및 시행규칙 제44조에 의해 입찰참가자격을 제한받은 업체는 입찰 참가에서 제외됨", "sanction_clear"),
    ("부정당업자 제한을 받은 업체는 가격 입찰참가등록마감일 전일까지 제한기간이 만료되어야 입찰 참가 가능", "sanction_clear"),
    ("입찰일 현재 국세 및 지방세 체납 사실이 없는 업체", "public_dues_arrears_clear"),
    ("상호출자제한기업집단에 속하는 기업은 입찰 참여 불가", "large_enterprise_software_clear"),
    ("공정거래법 제31조에 따른 상호출자제한기업집단 소속으로 지정되지 않은 회사여야 함", "large_enterprise_software_clear"),
])
def test_present_company_clearances_bind_existing_company_facts(condition, fact):
    result = item(condition)
    assert result["policy_class"] == "ELIGIBILITY"
    assert result["outcome"] == "PASS_CURRENT"
    assert result["company_fact_key"] == fact


@pytest.mark.parametrize("condition", [
    "적격업체 선정은 2개 이상 업체가 제안서 응찰 시 실시하며, 1개 이하 업체 응찰시 재공고함",
    "낙찰자는 낙찰일로부터 10일 이내에 낙찰금액의 10/100 이상 계약보증금을 보증서로 제출",
    "제안서 평가결과 입찰가격이 사업예산(또는 예정가격) 이하이고 기술능력평가 점수가 배점한도의 85% 이상인 자를 협상적격자로 선정",
    "계약상대자가 하자보수 의무를 제대로 이행하지 않으면 하자보수보증금이 본교에 귀속됨",
    "입찰자는 사업자 선정방식, 제안요청서 내용과 입찰 방침에 이의가 없음을 확약하는 확약서(별지 제3호 서식)를 제출해야 함",
    "구비서류(실적증명서 포함) 미제출자가 행한 입찰은 무효",
])
def test_procedure_and_contract_terms_are_not_eligibility(condition):
    result = item(condition)
    assert result["policy_class"] == "INFORMATION"


@pytest.mark.parametrize("condition", [
    "입찰담합을 주도한 자는 2년간, 담합에 가담한 자는 1년간 입찰참가 제한을 받음",
    "뇌물 제공 금액에 따라 3개월~2년간 입찰참가자격 제한",
    "대학구성원, 계약상대자 근로자 또는 시민을 사망에 이르게 한 업체는 계약제재 3년",
])
def test_penalty_schedules_are_pledge_checklists(condition):
    result = item(condition)
    assert result["policy_class"] == "CHECKLIST"


@pytest.mark.parametrize("condition", [
    "국제기구나 외국정부로부터 입찰참가자격이 제한되거나 계약부적격자로 선언된 자, 또는 이에 대해 제재를 받은 자는 입찰 참가 불가",
    "중소기업제품 구매촉진 및 판로지원에 관한 법률 제8조의2에 해당하는 자는 입찰 참여 불가",
    "업체 및 대표자가 은행연합회 불량거래처 등으로 등재되어 있지 않고, 관련 법령상 제한·저촉 및 이해상충이 없어야 함",
    "한국수출입은행 퇴직 후 2년 미경과자를 고용하여 입찰·계약 관련 업무를 담당시키는 업체는 참여 불가",
    # A real gate that also mentions a contract consequence stays a gate.
    "부정당업자 또는 부도 업체는 입찰 참가 불가하며 낙찰 후 확인 시 계약보증금이 귀속됨",
])
def test_clauses_without_a_company_fact_stay_in_review(condition):
    result = item(condition)
    assert result["policy_class"] == "ELIGIBILITY"
    if "은행연합회" in condition or "국제기구" in condition or "판로지원" in condition or "수출입은행" in condition:
        assert result["outcome"] == "REVIEW"
