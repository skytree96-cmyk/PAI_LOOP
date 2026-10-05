from types import SimpleNamespace

import pytest

import pai_loop.quantitative_scoring as scoring
from pai_loop.quantitative_qualitative_only import (
    QUALITATIVE_AND_PRICE_ONLY,
    qualitative_and_price_only,
)
from pai_loop.quantitative_rule_extraction import (
    QuantitativeCandidateProfile,
    QuantitativeValidationIssue,
)

QUALITATIVE_RFP = {
    "document_type": "RFP",
    "summary": (
        "협상에 의한 계약 방식이며 기술능력평가 배점한도 85%(68점) 이상 협상적격 기준을 포함한다. "
        "평가배점표는 우수/보통/미흡 등급 기준으로만 구성되어 객관적 수치조건이 없는 정성 평가이므로 "
        "정량 표는 추출하지 않았다."
    ),
    "missing_or_unreadable": ["입찰 일정의 구체 날짜가 '공고문 참조'로만 표시됨"],
}
NOTICE = {
    "document_type": "NOTICE",
    "summary": "기술평가 80%와 가격평가 20%를 종합하여 고득점 순으로 선정한다.",
    "missing_or_unreadable": ["세부 정량평가 배점표는 제안요청서에 있다고 안내되나 본 문서에는 첨부되어 있지 않음"],
}


def test_qualitative_rfp_with_price_only_is_recognized():
    assert qualitative_and_price_only([NOTICE, QUALITATIVE_RFP])


@pytest.mark.parametrize("summary", [
    "기술능력 90점(정량 20점+정성 70점)과 가격 10점으로 구성된다. 정성 항목은 평가위원 판단이다.",
    "정량평가 10점(경영상태2, 실적3, 상생협력5)과 정성평가 90점으로 구성된다.",
    "평가 배점표는 정성평가(수행계획)와 정량평가(재무구조·경영상태, 입찰가격)로 구성된다.",
    "신용평가등급 배점표(별표 8)는 원문 미첨부이며 나머지 평가항목은 정성적 판단 기준이다.",
    "평가항목 배점표(유사실적 30점 등)는 세부 계량기준이 없어 정성평가로 보이는 항목이 포함된다.",
    "적격심사 종합평점 85점 이상, 세부 배점표는 조달청 별표를 참조한다. 정성 항목 없음.",
])
def test_any_company_scored_hint_keeps_review(summary):
    rfp = {**QUALITATIVE_RFP, "summary": summary}
    assert not qualitative_and_price_only([NOTICE, rfp])


def test_requires_an_rfp_that_states_the_qualitative_evaluation():
    assert not qualitative_and_price_only([NOTICE])
    assert not qualitative_and_price_only([
        NOTICE, {**QUALITATIVE_RFP, "summary": "사업 개요와 제출 서류를 규정한다."},
    ])


def test_company_review_row_keeps_review():
    assert not qualitative_and_price_only([QUALITATIVE_RFP], ["고출력 광학시스템 설계 및 구축 실적"])
    assert qualitative_and_price_only([QUALITATIVE_RFP], ["입찰가격평가", "기술평가 및 가격평가 비율"])


def _profile(*, processed=("A1", "A2"), issues=()):
    return QuantitativeCandidateProfile(
        status="INCOMPLETE",
        expected_attachment_ids=("A1", "A2"),
        processed_attachment_ids=processed,
        tables=(),
        available_candidates=(),
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(
            QuantitativeValidationIssue(
                code="QUANTITATIVE_TABLE_NOT_ESTABLISHED",
                disposition="INCOMPLETE",
                message="정량평가표 미확보",
            ),
            *issues,
        ),
    )


def _attempt(result, status="ACCEPTED"):
    return SimpleNamespace(source_payload={"status": status, "result": result})


def _estimate(monkeypatch, profile, attempts):
    monkeypatch.setattr(scoring, "_current_dynamic_quantitative_profile", lambda notice: profile)
    monkeypatch.setattr(scoring, "_current_manifest_attempts", lambda *a, **k: ([], 0, attempts))
    return scoring.estimate_for_notice(SimpleNamespace(versions=[], deadline=None, published_at=None))


def test_complete_qualitative_manifest_becomes_not_applicable(monkeypatch):
    estimate = _estimate(
        monkeypatch, _profile(), {"A1": _attempt(NOTICE), "A2": _attempt(QUALITATIVE_RFP)},
    )
    assert estimate.activation_status == "NOT_APPLICABLE"
    assert estimate.rule_source_status == "NOT_APPLICABLE"
    assert estimate.activation_reasons == [QUALITATIVE_AND_PRICE_ONLY]
    assert estimate.lower_points is None and estimate.total_max_points is None
    assert "정성평가" in estimate.opinion


def test_unprocessed_attachment_keeps_review(monkeypatch):
    estimate = _estimate(
        monkeypatch,
        _profile(processed=("A2",), issues=(QuantitativeValidationIssue(
            code="ATTACHMENT_INCOMPLETE", disposition="INCOMPLETE", message="미처리", attachment_id="A1",
        ),)),
        {"A2": _attempt(QUALITATIVE_RFP)},
    )
    assert estimate.activation_status == "REVIEW_REQUIRED"
    assert QUALITATIVE_AND_PRICE_ONLY not in estimate.activation_reasons


def test_failed_attempt_keeps_review(monkeypatch):
    estimate = _estimate(
        monkeypatch, _profile(),
        {"A1": _attempt(NOTICE), "A2": _attempt(QUALITATIVE_RFP, status="DOCUMENT_EXTRACT_FAILED")},
    )
    assert estimate.activation_status == "REVIEW_REQUIRED"
