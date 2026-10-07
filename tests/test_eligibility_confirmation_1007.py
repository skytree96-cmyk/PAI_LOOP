"""A person's answer to the nonprofit-exception question decides the clause; nothing passes on its own (2026-10-07)."""
import copy

import pytest
from sqlalchemy import select

from pai_loop.analysis_pipeline import run_analysis_pipeline
from pai_loop.eligibility_confirmations import current_confirmations
from pai_loop.eligibility_policy import (
    NONPROFIT_CONFIRMATION_QUESTION_KEY, classify_requirements, load_public_company_profile,
)
from pai_loop.models import EligibilityConfirmation, Notice, NoticeVersion
from pai_loop.reference_registry import sync_public_company_profile
from test_analysis_pipeline import _current_pps_confidence_source

PURPOSE = ("중소기업기본법상 소기업 또는 소상공인 보호 및 지원에 관한 법률상 소상공인으로서 소기업·소상공인확인서를 "
           "소지한 업체여야 함(비영리법인은 학술연구 용역의 경우 참여 가능)")


def _items(conditions, confirmations=None):
    return classify_requirements(
        [{"requirement_id": f"R{i}", "category": "ENTITY", "normalized_condition": c, "mandatory": True}
         for i, c in enumerate(conditions)],
        profile=load_public_company_profile(), deadline="2026-10-12", confirmations=confirmations,
    )


def test_without_an_answer_the_clause_stays_review_with_one_question_and_an_action():
    result = _items([PURPOSE])
    item = result["items"][0]
    assert item["outcome"] == "REVIEW"
    assert item["action"].startswith("담당자 판단 필요")
    [question] = result["confirmation_questions"]
    assert question["key"] == NONPROFIT_CONFIRMATION_QUESTION_KEY
    assert "학술·연구" in question["question"]
    assert question["answer"] is None


@pytest.mark.parametrize("answer,outcome,fact", [
    ("YES", "PASS_EXCEPTION", "nonprofit_entity"),
    ("NO", "FAIL_CONFIRMED", "small_business_certificate"),
])
def test_an_answer_maps_the_clause_onto_confirmed_company_facts(answer, outcome, fact):
    item = _items([PURPOSE], {NONPROFIT_CONFIRMATION_QUESTION_KEY: answer})["items"][0]
    assert item["outcome"] == outcome
    assert item["company_fact_key"] == fact
    assert item["confirmation_answer"] == answer


def test_unrelated_clauses_ignore_the_answer_and_get_an_action_line():
    result = _items(["국방과학연구소 계약요령 제18조에 따른 자격을 구비한 자"], {NONPROFIT_CONFIRMATION_QUESTION_KEY: "YES"})
    item = result["items"][0]
    assert item["outcome"] == "REVIEW"
    assert item["action"] == "공고 원문에서 법인·사업자 유형 조건을 회사가 충족하는지 확인하세요."
    assert result["confirmation_questions"] == []


def _confirm(session, notice, answer, basis=None):
    session.add(EligibilityConfirmation(
        notice_id=notice.id, notice_key=notice.notice_key, question_key=NONPROFIT_CONFIRMATION_QUESTION_KEY,
        answer=answer, basis_version_id=basis, actor_label="SYN 부서 KMA1"))
    session.commit()


@pytest.mark.parametrize("answer,expected", [("YES", "PASS"), ("NO", "FAIL")])
def test_pipeline_applies_a_current_answer_and_reruns_for_free(answer, expected):
    with _current_pps_confidence_source(eligibility_condition=PURPOSE) as case:
        sync_public_company_profile(case.session)
        case.session.commit()
        before = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert before.eligibility == "REVIEW"
        case.session.commit()
        notice = case.session.get(Notice, case.notice_id)
        metadata = max((v for v in notice.versions if v.source_payload.get("kind") == "PPS_NOTICE_METADATA"),
                       key=lambda v: v.version_no)
        _confirm(case.session, notice, answer, basis=metadata.id)
        after = run_analysis_pipeline(case.session, notice_id=case.notice_id)
        assert after.eligibility == expected
        assert not after.reused
        assert case.client.calls == 1                       # no provider call for the answer


def test_an_amended_notice_asks_again():
    with _current_pps_confidence_source(eligibility_condition=PURPOSE) as case:
        notice = case.session.get(Notice, case.notice_id)
        metadata = max((v for v in notice.versions if v.source_payload.get("kind") == "PPS_NOTICE_METADATA"),
                       key=lambda v: v.version_no)
        _confirm(case.session, notice, "YES", basis=metadata.id)
        assert current_confirmations(case.session, notice) == {NONPROFIT_CONFIRMATION_QUESTION_KEY: "YES"}
        newer = NoticeVersion(notice_id=notice.id, version_no=max(v.version_no for v in notice.versions) + 1,
                              file_sha256="b" * 64, document_complete=False, extraction_status="COMPLETE",
                              extraction_confidence=1, source_payload=copy.deepcopy(metadata.source_payload))
        case.session.add(newer)
        case.session.commit()
        case.session.expire_all()
        notice = case.session.get(Notice, case.notice_id)
        assert current_confirmations(case.session, notice) == {}


def test_revoked_answers_do_not_apply():
    with _current_pps_confidence_source(eligibility_condition=PURPOSE) as case:
        notice = case.session.get(Notice, case.notice_id)
        metadata = max((v for v in notice.versions if v.source_payload.get("kind") == "PPS_NOTICE_METADATA"),
                       key=lambda v: v.version_no)
        _confirm(case.session, notice, "NO", basis=metadata.id)
        row = case.session.scalar(select(EligibilityConfirmation))
        from datetime import datetime, timezone
        row.revoked_at = datetime.now(timezone.utc)
        case.session.commit()
        assert current_confirmations(case.session, notice) == {}


def test_confirmation_endpoints_reject_unknown_notice_question_and_answer():
    from conftest import internal_server_client
    from pai_loop.main import create_app
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with internal_server_client(app) as client:
        path = "/api/v1/notices/PPS-NOPE/eligibility-confirmations"
        assert client.post(path, json={"question_key": NONPROFIT_CONFIRMATION_QUESTION_KEY, "answer": "YES"}).status_code == 404
        assert client.post(path, json={"question_key": "OTHER", "answer": "YES"}).status_code == 422
        assert client.post(path, json={"question_key": NONPROFIT_CONFIRMATION_QUESTION_KEY, "answer": "MAYBE"}).status_code == 422
        assert client.delete(path + "/OTHER").status_code == 422
    from fastapi.testclient import TestClient
    with TestClient(app) as anonymous:
        response = anonymous.post("/api/v1/notices/PPS-NOPE/eligibility-confirmations",
                                  json={"question_key": NONPROFIT_CONFIRMATION_QUESTION_KEY, "answer": "YES"})
        assert response.status_code in {401, 403}


def test_ui_renders_the_question_and_posts_answers():
    from pathlib import Path
    script = Path(__file__).resolve().parents[1].joinpath("src", "pai_loop", "static", "app.js").read_text(encoding="utf-8")
    assert "function renderEligibilityQuestions" in script
    assert "/eligibility-confirmations" in script
    assert 'data-confirm-answer="YES"' in script and 'data-confirm-answer="NO"' in script
    assert "할 일 · " in script
