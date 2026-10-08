"""2026-10-08: REVIEW reductions measured on production — re-issued notice labels, gaps that only
say another *read* attachment is absent, named non-eligibility quality notes, and clause wordings
whose every predicate is a confirmed company clearance."""
import pytest

from pai_loop import eligibility_policy as ep
from pai_loop.analysis_pipeline import (
    _gap_names_read_sibling_documents, _is_notice_document_label, run_analysis_pipeline,
)
from test_analysis_pipeline import _current_pps_confidence_source
from test_analysis_pipeline import _relabel_source


@pytest.mark.parametrize("label,expected", [
    ("7. 입찰 재공고(제2026-19-01호).hwp", True),
    ("재공고_온동네 PG북 제작.pdf", True),
    ("붙임1. 인덕대학교 입찰 재공고.hwp", True),
    ("(재공고) 입찰재공고(교육콘텐츠 개발).hwp", True),
    ("1. 입찰공고문.hwp", True),
    ("(재공고) 제안요청서(교육콘텐츠 개발).pdf", False),
    ("붙임3. 과업지시서(26-031호, 경영성과, 재공고).hwp", False),
    ("용역평가위원 모집공고.pdf", False),
])
def test_reissued_announcements_count_as_the_notice(label, expected):
    assert _is_notice_document_label(label) is expected


GAP = ("참가자격 관련 세부 내용은 '제안요청서 파일 참조 필수'로 안내되어 있으나, 해당 제안요청서 및 "
       "기술평가 배점표가 본 소스에 포함되어 있지 않아 확인 불가")


def test_gap_pointing_at_a_read_sibling_is_answered_only_when_that_document_was_read():
    assert _gap_names_read_sibling_documents(GAP, "1. 입찰공고문.hwp", ["2. 제안요청서.hwp", "1. 입찰공고문.pdf"])
    # Only the announcement's own twin was read: the RFP is still unread.
    assert not _gap_names_read_sibling_documents(GAP, "1. 입찰공고문.hwp", ["1. 입찰공고문.pdf"])
    # The gap's own document kind never answers it.
    own = "제안요청서(세부 과업내용)가 본 공고문 본문에 포함되어 있지 않아 상세 자격요건 확인 불가"
    assert _gap_names_read_sibling_documents(own, "입찰공고서.hwpx", ["입찰공고서.pdf", "제안요청서(수정).hwpx"])
    assert not _gap_names_read_sibling_documents(own, "입찰공고서.hwpx", ["입찰공고서.pdf"])
    # A named annex that was never attached stays closed; so does a gap naming no document.
    assert not _gap_names_read_sibling_documents(
        "별첨 실적증명서 서식 미첨부로 확인 불가", "입찰공고문.hwp", ["서식모음.hwp"])
    assert not _gap_names_read_sibling_documents("일부 내용을 읽을 수 없음", "입찰공고문.hwp", ["제안요청서.hwp"])


def test_reissued_notice_label_opens_the_notice_read_gate():
    with _current_pps_confidence_source(missing=["입찰 건명 및 날짜 미기재로 구체적 사업명 확인 불가"]) as case:
        _relabel_source(case, "7. 입찰 재공고(제2026-19-01호).hwpx")
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "PASS"
        assert "NOTICE_READ_UNRESOLVED_GAPS_GATE_APPLIED" in result.warnings


@pytest.mark.parametrize("gap", [
    "행사장(서울 엠갤러리 호텔 그랜드볼룸)이 '협의 중'으로 표시되어 최종 확정 여부가 불명확함",
    "을(수탁자)의 구체적 상호, 소재지, 대표자명이 ◯◯◯◯◯ 등으로 마스킹되어 확인 불가",
])
def test_named_quality_notes_do_not_hold_eligibility(gap):
    with _current_pps_confidence_source(missing=[gap]) as case:
        _relabel_source(case, "SYN 입찰공고문.hwpx")
        assert run_analysis_pipeline(case.session, notice_id=case.notice_id).eligibility == "PASS"


def test_rfp_reference_gap_without_the_rfp_attached_keeps_the_gate_closed():
    with _current_pps_confidence_source(missing=[GAP]) as case:
        _relabel_source(case, "SYN 입찰공고문.hwpx")
        result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert result.eligibility == "REVIEW"


def _classify(text, category):
    profile = ep.load_public_company_profile()
    return ep.classify_requirements(
        [{"requirement_id": "SYN", "normalized_condition": text, "category": category, "mandatory": True}],
        profile=profile, deadline="2026-10-20", evaluation_date="2026-10-08")["items"][0]


@pytest.mark.parametrize("text,category,outcome", [
    ("조세포탈 등으로 유죄판결 확정 후 2년이 지나지 않은 자 또는 입찰참가자격 제한기간이 경과되지 않은 자는 입찰 참여 불가",
     "SANCTION", "PASS_CURRENT"),
    ("입찰일 현재 국세, 지방세, 부가세 체납 사실이 없는 업체여야 함", "SANCTION", "PASS_CURRENT"),
    ("입찰참가일 기준 국세, 지방세, 사업장 4대 사회보험료 체납이 없을 것", "ENTITY", "PASS_CURRENT"),
    ("부가가치세법 제8조에 따라 사업자등록을 완료한 자", "ENTITY", "PASS_CURRENT"),
    ("국가계약법 시행령 제12조 및 시행규칙 제14조에 따른 자격요건을 갖춘 사업자로서 부정당업자에 해당하지 않을 것",
     "ENTITY", "PASS_CURRENT"),
    ("입찰등록사항과 법인등기부등본(법인) 또는 사업자등록증(개인)상 대표자, 주소, 상호 등이 일치하여야 함",
     "ENTITY", "PASS_CURRENT"),
    ("국가·지자체·정부투자기관·본교의 부정당업자 제재 중이 아니며, 법정관리·면허취소 등 영업정지 상태가 아니어야 함",
     "SANCTION", "PASS_CURRENT"),
    ("입찰참가자(대리인·임직원)는 금품·향응 제공 금지, 입찰가격 사전 협의·담합 금지 등 청렴계약 조건을 준수해야 함",
     "SANCTION", "ACKNOWLEDGED"),
    ("낙찰자가 특별한 사유 없이 계약을 이행하지 않을 경우 부정당사업자로서 입찰참가자격제한을 받을 수 있음",
     "SANCTION", "ACKNOWLEDGED"),
])
def test_confirmed_company_clearances_settle_these_wordings(text, category, outcome):
    assert _classify(text, category)["outcome"] == outcome


@pytest.mark.parametrize("text,category", [
    # No company fact covers litigation with the agency or its own conduct judgement.
    ("입찰공고일 현재 본교 상대 소송(소송 중 포함), 당좌거래정지, 청산 등 정리절차 중인 사업자는 입찰참가 불가", "SANCTION"),
    ("입찰공고일 기준 2년 이내 본교 입찰·계약·계약이행 등에서 물의를 일으킨 업체는 입찰 참가 불가", "SANCTION"),
    # A clearance joined to a performance condition is not settled by the clearance.
    ("조세포탈 유죄판결 이력이 없고 최근 3년 유사 실적을 보유한 자, 입찰참가자격 제한 시 참여 불가", "SANCTION"),
])
def test_uncovered_predicates_stay_in_review(text, category):
    assert _classify(text, category)["outcome"] == "REVIEW"


def test_trailing_subcontracting_note_does_not_swallow_a_permit_gate():
    gate = _classify("공고일 현재 당해 사업에 대한 사업자등록증을 교부받고 건축사법에 따른 건축사사무소 신고(등록)한 "
                     "업체여야 하며 공동도급 및 하도급 불가", "ENTITY")
    assert (gate["policy_class"], gate["outcome"], gate["company_fact_key"]) == (
        "ELIGIBILITY", "FAIL_CONFIRMED", "architect_office")
    plain = _classify("본 입찰은 공동도급 및 하도급을 허용하지 않음", "CONSORTIUM")
    assert plain["policy_class"] == "CHECKLIST"


@pytest.mark.parametrize("text", [
    "공고일 현재 최근 3년간 대학·공공기관·기업 등에 단일건으로 IDC 또는 LMS 개발 및 유지보수, 서버 관리 관련 "
    "5천만원 이상(VAT포함) 실적이 있는 업체(공동도급계약 및 하도급 제외)",
    "최근 2년 이내(2024.9.1. 이후) 고등교육기관(전문대학 이상)에서 단일건 3천만원 이상의 취업프로그램 관련 "
    "유사 실적 보유 업체여야 함",
])
def test_a_bidder_description_that_owns_performance_is_a_performance_gate(text):
    item = _classify(text, "PERFORMANCE")
    assert (item["policy_class"], item["outcome"]) == ("ELIGIBILITY", "REVIEW")


def test_performance_evidence_instructions_stay_information():
    item = _classify("수행실적은 최근 3년간 완료된 유사용역만 인정, 실적증명서·계약서·세금계산서 제출 필수", "PERFORMANCE")
    assert item["policy_class"] == "INFORMATION"
