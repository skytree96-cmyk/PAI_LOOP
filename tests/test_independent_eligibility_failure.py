"""Verified prototype mismatches survive unrelated incomplete documents."""
import copy
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from pai_loop.analysis_pipeline import run_analysis_pipeline
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import CompanyFact, Evaluation, Notice, NoticeVersion
from pai_loop.notice_freshness import (
    has_current_independent_failure, latest_current_analysis_run, latest_current_evaluation,
)
from pai_loop.reference_registry import sync_public_company_profile
from test_analysis_pipeline import _current_pps_confidence_source, _notice, _requirement, _source_version

DP = "직접생산확인증명서(8014199001)를 소지해야 함"

@pytest.mark.parametrize("change,expected", [
    ("none", "FAIL"), ("weak", "REVIEW"), ("ambiguous", "REVIEW"),
    ("optional", "REVIEW"), ("fact_true", "REVIEW"), ("fact_missing", "REVIEW"),
    ("nonprofit", "REVIEW"), ("or", "REVIEW"), ("strict", "REVIEW"),
])
def test_only_verified_mandatory_actual_failure_crosses_document_gate(change, expected, monkeypatch):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with build_session_factory(engine)() as session:
        sync_public_company_profile(session)
        notice = _notice(session, notice_key="SYN-INDEPENDENT-FAILURE")
        condition = {
            "nonprofit": "직접생산확인증명서는 비영리법인에 적용하지 않으며 참여 가능함.",
            "or": "나라장터 업종코드 9901 또는 9998로 등록해야 함",
        }.get(change, DP)
        row = _requirement("SYN-DP", condition, attachment_id="SYN-GOOD", category="CERTIFICATION",
                           mandatory=change != "optional", confidence=0.5 if change == "weak" else 0.98)
        if change == "ambiguous":
            row["ambiguity_reason"] = "적용 대상 불명확"
        _source_version(notice, version_no=1, attachment_id="SYN-GOOD", digest_char="a", requirements=[row])
        _source_version(notice, version_no=2, attachment_id="SYN-BAD", digest_char="b",
                        requirements=None, status="REVIEW", document_complete=False)
        if change == "fact_true":
            fact = session.scalar(select(CompanyFact).where(CompanyFact.fact_key == "direct_production_certificate"))
            fact.value = True
        if change == "fact_missing":
            session.execute(delete(CompanyFact).where(CompanyFact.fact_key == "direct_production_certificate"))
        if change == "strict":
            from pai_loop.eligibility_policy import load_public_company_profile
            profile = load_public_company_profile()
            profile["eligibility_assessment_mode"] = "DEADLINE_EVIDENCE"
            monkeypatch.setattr("pai_loop.analysis_pipeline.load_public_company_profile", lambda: profile)
        session.commit()
        result = run_analysis_pipeline(session, notice_id=notice.id)
        assert result.status == "PARTIAL"
        assert result.eligibility == expected
        evaluation = session.get(Evaluation, result.evaluation_id)
        basis = session.get(NoticeVersion, evaluation.notice_version_id)
        assert not basis.document_complete
        assert basis.source_payload["review_code"] == "R07"
        assert bool(basis.source_payload.get("independent_failure")) == (expected == "FAIL")
        if expected == "FAIL":
            failed = next(r for r in evaluation.atomic_results if r["result"] == "FAIL")
            assert failed["reason_code"] == "DF-000"
            assert failed["actual_value"] is False
            session.expire(notice, ["versions"])
            assert has_current_independent_failure(notice, evaluation)
        session.commit()
        assert run_analysis_pipeline(session, notice_id=notice.id).reused
    engine.dispose()


def _pps_run(case):
    case.session.get(Notice, case.notice_id).notice_key = "PPS-SYN_INDEPENDENT_FAILURE"
    source = case.session.get(NoticeVersion, case.source_id)
    payload = copy.deepcopy(source.source_payload)
    payload["document_processing"]["source_read_complete"] = False
    payload["document_processing"]["analysis_input_complete"] = False
    source.source_payload = payload
    source.document_complete = False
    sync_public_company_profile(case.session)
    case.session.commit()
    result = run_analysis_pipeline(case.session, notice_id=case.notice_id)
    case.session.commit()
    return result, case.session.get(Notice, case.notice_id), case.session.get(Evaluation, result.evaluation_id)


def test_current_pps_failure_exposed_without_completing_documents_or_scores():
    from pai_loop.api import _dashboard_qualification, _load_dashboard_notice_batch, _summary
    with _current_pps_confidence_source(eligibility_condition=DP, missing=["일부 내용을 읽을 수 없음"]) as case:
        result, notice, evaluation = _pps_run(case)
        assert result.eligibility == "FAIL"
        assert result.status == "PARTIAL"
        assert latest_current_evaluation(notice) is evaluation
        assert latest_current_analysis_run(notice) is None
        assert _dashboard_qualification(notice, evaluation) == "FAIL"
        public = _summary(notice, public_view=True)
        assert public.qualification_status == "FAIL"
        assert public.eligibility_independent_failure is True
        assert public.analysis_state == "REVIEW"
        assert public.latest_evaluation.eligibility == "FAIL"
        assert not public.latest_evaluation.atomic_results
        assert set(public.latest_evaluation.explanation) == {"public_view", "note"}
        assert case.client.calls == 1
        case.session.expunge_all()
        compact = _load_dashboard_notice_batch(case.session, [case.notice_id])[0]
        assert _dashboard_qualification(compact, latest_current_evaluation(compact)) == "FAIL"


@pytest.mark.parametrize("change", ["deadline", "manifest", "new_attempt", "policy", "new_review"])
def test_new_basis_invalidates_independent_failure_without_reviving_old_result(change):
    with _current_pps_confidence_source(eligibility_condition=DP, missing=["일부 내용을 읽을 수 없음"]) as case:
        result, notice, evaluation = _pps_run(case)
        assert result.eligibility == "FAIL"
        assert has_current_independent_failure(notice, evaluation)
        if change == "deadline":
            notice.deadline += timedelta(days=1)
        elif change == "policy":
            basis = case.session.get(NoticeVersion, evaluation.notice_version_id)
            payload = copy.deepcopy(basis.source_payload)
            payload["independent_failure"]["policy_version"] = "SYN-OBSOLETE"
            basis.source_payload = payload
        elif change == "new_review":
            newer = Evaluation(notice_id=notice.id, notice_version_id=evaluation.notice_version_id,
                               evaluated_at=evaluation.evaluated_at + timedelta(seconds=1),
                               deadline_snapshot_at=notice.deadline, eligibility="REVIEW", reason_code="R07",
                               readiness_status=evaluation.readiness_status, readiness_score=0,
                               evidence_coverage=0, risk_score=None, risk_band="UNKNOWN",
                               atomic_results=[], explanation={})
            case.session.add(newer)
        else:
            source = (next(v for v in notice.versions if v.source_payload.get("kind") == "PPS_NOTICE_METADATA")
                      if change == "manifest" else case.session.get(NoticeVersion, case.source_id))
            newer = NoticeVersion(notice_id=notice.id, version_no=max(v.version_no for v in notice.versions)+1,
                                  file_sha256=source.file_sha256, document_complete=source.document_complete,
                                  extraction_status=source.extraction_status,
                                  extraction_confidence=source.extraction_confidence,
                                  source_payload=copy.deepcopy(source.source_payload))
            case.session.add(newer)
        case.session.commit()
        case.session.expire_all()
        assert latest_current_evaluation(notice) is None
        assert latest_current_analysis_run(notice) is None
