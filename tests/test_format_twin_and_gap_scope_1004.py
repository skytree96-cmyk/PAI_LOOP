"""A gated PASS whose only unread attachment is another format of a read one (2026-10-04),
and gaps where a non-announcement document says it does not itself carry eligibility."""
import io
import json
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from pai_loop.analysis_pipeline import (
    _notice_read_unrelated_gaps, _unread_format_twins, run_analysis_pipeline,
)
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.integrations.openai_extraction import OpenAIExtractionClient, OpenAITelemetry
from pai_loop.models import CompanyFact, Evaluation, Notice
from pai_loop.notice_freshness import (
    has_current_independent_pass, latest_current_analysis_run, latest_current_evaluation,
)
from pai_loop.pps_enrichment import enrich_notice_from_pps, persist_pps_metadata_version
from test_analysis_pipeline import _verified_boolean_fact

ELIGIBILITY = "경쟁입찰참가자격 등록을 완료한 업체여야 함"
NAMED_GAP = ["입찰 건명 및 날짜 미기재로 구체적 사업명 확인 불가"]


def _url(seq):
    return ("https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
            f"?bidPbancNo=R26BK00000001&bidPbancOrd=000&fileSeq={seq}&fileType=1&prcmBsneSeCd=01")


def _hwpx(text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", f"<s><p>{text}</p></s>")
    return buffer.getvalue()


def _client(missing):
    class SyntheticClient:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract(self, *, document_text, allowed_attachment_ids):
            type(self).calls += 1
            attachment_id = next(iter(allowed_attachment_ids))
            data = {
                "document_type": "NOTICE",
                "requirements": [{
                    "requirement_id": "SYN-TWIN-0", "category": "ENTITY", "logic": "SINGLE",
                    "normalized_condition": ELIGIBILITY, "mandatory": True,
                    "deadline_basis": "입찰 마감일",
                    "evidence": [{"attachment_id": attachment_id, "page": 1, "section": "합성 공고",
                                  "quote": ELIGIBILITY, "confidence": 0.98}],
                    "ambiguity_reason": None,
                }],
                "quantitative_tables": [], "quantitative_table_not_applicable": None,
                "missing_or_unreadable": missing, "summary": "합성 추출 검증",
            }

            def forbidden_network(request):
                raise AssertionError("Provider transport must never execute")

            with OpenAIExtractionClient(api_key="synthetic-only", provider="openai",
                                        transport=httpx.MockTransport(forbidden_network)) as boundary:
                return boundary._validate_response(
                    {"status": "completed", "output_text": json.dumps(data, ensure_ascii=False)},
                    document_text=document_text, allowed_attachment_ids=allowed_attachment_ids,
                    api_calls=1, openai_telemetry=OpenAITelemetry(),
                )
    return SyntheticClient


def _case(*, second_name, company_fact=True, missing=NAMED_GAP):
    """Announcement .hwpx is read; the second attachment's bytes never parse."""
    good = _hwpx(ELIGIBILITY)

    def respond(request):
        if "fileSeq=1" in str(request.url):
            return httpx.Response(200, headers={"Content-Type": "application/zip"}, content=good)
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4 broken")

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        notice = Notice(notice_key="PPS-SYN-FORMAT-TWIN", bid_notice_no="R26BK00000001", revision_no="000",
                        title="교육 컨설팅 용역", agency="공공기관",
                        deadline=datetime(2027, 1, 1, tzinfo=timezone.utc), status="OPEN")
        session.add(notice)
        session.flush()
        persist_pps_metadata_version(
            session, notice,
            raw_item={"bidNtceNo": "R26BK00000001", "bidNtceOrd": "000",
                      "ntceSpecFileNm1": "SYN 입찰공고문.hwpx", "ntceSpecDocUrl1": _url(1),
                      "ntceSpecFileNm2": second_name, "ntceSpecDocUrl2": _url(2)},
            search_keywords=["교육", "컨설팅"], dry_run=False,
        )
        _verified_boolean_fact(session, "bidder_registration")
        session.flush()
        session.scalar(select(CompanyFact).where(CompanyFact.fact_key == "bidder_registration")).value = company_fact
        session.commit()
        notice_id = notice.id
    client = _client(missing)
    with factory() as session:
        enrich_notice_from_pps(session, notice_id=notice_id, openai_api_key="synthetic-only",
                               openai_model="synthetic", transport=httpx.MockTransport(respond),
                               openai_client_factory=client)
    session = factory()
    return engine, session, notice_id, client


@pytest.mark.parametrize("second_name,shown", [
    ("SYN 입찰공고문.pdf", True),       # the same announcement in another format
    ("SYN 제안요청서.pdf", False),      # a different document was never read
])
def test_gated_pass_is_shown_only_when_every_unread_attachment_is_a_format_twin(second_name, shown):
    engine, session, notice_id, client = _case(second_name=second_name)
    try:
        result = run_analysis_pipeline(session, notice_id=notice_id)
        session.commit()
        notice = session.get(Notice, notice_id)
        evaluation = session.get(Evaluation, result.evaluation_id)
        assert result.eligibility == ("PASS" if shown else "REVIEW")   # an unread RFP keeps R07
        assert ("UNREAD_ATTACHMENTS_ARE_FORMAT_TWINS" in result.warnings) is shown
        assert has_current_independent_pass(notice, evaluation) is shown
        assert (latest_current_evaluation(notice) is evaluation) is shown
        assert latest_current_analysis_run(notice) is None   # no score or run from a twin proof
        from pai_loop.api import _dashboard_qualification
        assert _dashboard_qualification(notice, latest_current_evaluation(notice)) == (
            "PASS" if shown else "NOT_EVALUATED")
        assert client.calls == 1
        session.commit()
        assert run_analysis_pipeline(session, notice_id=notice_id).reused
    finally:
        session.close()
        engine.dispose()


def test_a_twin_never_turns_a_review_into_a_shown_pass():
    engine, session, notice_id, _client_cls = _case(second_name="SYN 입찰공고문.pdf", company_fact=None)
    try:
        result = run_analysis_pipeline(session, notice_id=notice_id)
        session.commit()
        notice = session.get(Notice, notice_id)
        assert result.eligibility == "REVIEW"
        assert "UNREAD_ATTACHMENTS_ARE_FORMAT_TWINS" not in result.warnings
        assert latest_current_evaluation(notice) is None
    finally:
        session.close()
        engine.dispose()


def test_twin_proof_expires_with_the_policy(monkeypatch):
    engine, session, notice_id, _client_cls = _case(second_name="SYN 입찰공고문.pdf")
    try:
        result = run_analysis_pipeline(session, notice_id=notice_id)
        session.commit()
        notice = session.get(Notice, notice_id)
        evaluation = session.get(Evaluation, result.evaluation_id)
        assert has_current_independent_pass(notice, evaluation)
        monkeypatch.setattr("pai_loop.eligibility_policy.POLICY_VERSION", "pai-loop-requirement-policy-next")
        assert not has_current_independent_pass(notice, evaluation)
        assert latest_current_evaluation(notice) is None
    finally:
        session.close()
        engine.dispose()


def _source(label, gaps, complete=True):
    version = SimpleNamespace(document_complete=complete, source_payload={"source_label": label})
    return SimpleNamespace(version=version, materializable=True, attachment_id=label,
                           data=SimpleNamespace(missing_or_unreadable=gaps))


@pytest.mark.parametrize("file_names,read,expected", [
    ({"A": "공고문.hwp", "B": "공고문.pdf"}, {"A"}, ["B"]),
    ({"A": "공고문.hwp", "B": "공고문 .PDF"}, {"A"}, ["B"]),
    ({"A": "공고문.hwp", "B": "공고문(수정).pdf"}, {"A"}, []),
    ({"A": "공고문.hwp", "B": "공고문.zip"}, {"A"}, []),
    ({"A": "공고문.hwp", "B": "공고문.hwp"}, {"A"}, []),          # same format is not a twin
    ({"A": "공고문.hwp", "B": "공고문.pdf", "C": "제안요청서.pdf"}, {"A"}, []),
    ({"A": "공고문.hwp", "B": "공고문.pdf"}, {"A", "B"}, []),     # nothing unread
])
def test_format_twin_matching(file_names, read, expected):
    sources = [SimpleNamespace(attachment_id=key, materializable=True, data=SimpleNamespace(),
                               version=SimpleNamespace(document_complete=True)) for key in sorted(read)]
    basis = {"coverage_complete": True, "expected_attachment_ids": sorted(file_names),
             "attachment_file_names": file_names}
    assert _unread_format_twins(sources, basis) == expected
    assert _unread_format_twins(sources, {**basis, "coverage_complete": False}) == []


BASIS = {"coverage_complete": True}


@pytest.fixture
def plain_gaps(monkeypatch):
    from pai_loop import analysis_pipeline as pipeline
    monkeypatch.setattr(pipeline, "_aggregate_source_gaps", lambda sources: (
        [pipeline._normalise_text(gap) for source in sources for gap in source.data.missing_or_unreadable], []))
SCOPE_GAP = "입찰참가자격, 제안서 제출 방식·마감일 등 공고 관련 세부사항이 본 과업지시서에 포함되어 있지 않음"


@pytest.mark.parametrize("sources,expected", [
    # A scope document saying it does not carry the eligibility section.
    ([_source("SYN 입찰공고문.hwpx", []), _source("SYN 과업지시서.hwpx", [SCOPE_GAP])], True),
    ([_source("SYN 입찰공고문.hwpx", []),
      _source("붙임 서식.hwp", ["본 문서는 입찰 관련 서식(붙임1~6)만 포함되어 있으며, 자격요건 상세는 제공된 source에 없음"])], True),
    # The same words from the announcement itself keep the gate closed.
    ([_source("SYN 입찰공고문.hwpx", [SCOPE_GAP])], False),
    # An annex named but never attached may hold eligibility detail.
    ([_source("SYN 입찰공고문.hwpx", []),
      _source("SYN 제안요청서.hwpx", ["본 문서의 별첨 참가자격 세부기준이 첨부되지 않아 확인 불가"])], False),
    # A plainly missing eligibility source is not a scope note.
    ([_source("SYN 입찰공고문.hwpx", []),
      _source("SYN 제안요청서.hwpx", ["제안요청서 원문이 첨부되지 않아 상세 자격요건을 확인할 수 없음"])], False),
    # Newly named non-eligibility subjects.
    ([_source("SYN 입찰공고문.hwpx", ["입찰공고번호가 공란으로 표기되어 확인 불가",
                                      "사업대상지 지도 이미지는 판독 불가"])], True),
    # Unread announcement: closed whatever the gaps say.
    ([_source("SYN 입찰공고문.hwpx", [], complete=False), _source("SYN 과업지시서.hwpx", [SCOPE_GAP])], False),
])
def test_other_document_scope_gaps(sources, expected, plain_gaps):
    assert _notice_read_unrelated_gaps(sources, BASIS) is expected
