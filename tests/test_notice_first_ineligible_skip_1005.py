"""The announcement is read first; a confirmed, document-robust failure skips paid reads of the rest (2026-10-05)."""
import io
import json
import zipfile
from datetime import datetime, timezone

import httpx
import pytest

from pai_loop.analysis_pipeline import run_analysis_pipeline
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.integrations.openai_extraction import OpenAIExtractionClient, OpenAITelemetry
from pai_loop.models import Evaluation, Notice, NoticeVersion
from pai_loop.notice_freshness import has_current_independent_failure, latest_current_evaluation
from pai_loop.pps_enrichment import (
    NOTICE_CONFIRMED_INELIGIBLE_SKIP, enrich_notice_from_pps, persist_pps_metadata_version,
)
from pai_loop.reference_registry import sync_public_company_profile

INDUSTRY_FAIL = "입찰서 제출 마감일 전일까지 나라장터(G2B)에 회계법인(업종코드 1200)으로 입찰참가자격을 등록해야 함"
SME_FAIL = "중소기업기본법상 소기업 또는 소상공인으로서 소기업·소상공인 확인서를 소지한 자"
PASS_CLAUSE = "국가종합전자조달시스템(G2B, 나라장터)에 입찰참가 등록한 업체여야 함"


def _url(seq):
    return ("https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
            f"?bidPbancNo=R26BK00000001&bidPbancOrd=000&fileSeq={seq}&fileType=1&prcmBsneSeCd=01")


def _hwpx(text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", f"<s><p>{text}</p></s>")
    return buffer.getvalue()


def _client(clause, confidence):
    class SyntheticClient:
        labels: list[str] = []

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract(self, *, document_text, allowed_attachment_ids):
            attachment_id = next(iter(allowed_attachment_ids))
            type(self).labels.append(document_text[:30])
            quote = clause if clause in document_text else document_text.strip()[:40]
            data = {
                "document_type": "NOTICE",
                "requirements": [{
                    "requirement_id": "SYN-GATE-0", "category": "INDUSTRY_CODE" if "업종코드" in quote else "ENTITY",
                    "logic": "SINGLE", "normalized_condition": quote, "mandatory": True,
                    "deadline_basis": "입찰 마감일",
                    "evidence": [{"attachment_id": attachment_id, "page": 1, "section": "합성",
                                  "quote": quote, "confidence": confidence}],
                    "ambiguity_reason": None,
                }],
                "quantitative_tables": [], "quantitative_table_not_applicable": None,
                "missing_or_unreadable": [], "summary": "합성",
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
    SyntheticClient.labels = []
    return SyntheticClient


def _run(clause, *, confidence=0.98):
    files = {1: ("SYN 제안요청서.hwpx", "제안요청서 본문: 과업 범위와 평가 기준을 설명한다."),
             2: ("SYN 입찰공고문.hwpx", clause)}

    def respond(request):
        seq = int(str(request.url).split("fileSeq=")[1].split("&")[0])
        return httpx.Response(200, headers={"Content-Type": "application/zip"}, content=_hwpx(files[seq][1]))

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        sync_public_company_profile(session)
        notice = Notice(notice_key="PPS-SYN-NOTICE-FIRST", bid_notice_no="R26BK00000001", revision_no="000",
                        title="합성 용역", agency="공공기관",
                        deadline=datetime(2027, 1, 1, tzinfo=timezone.utc), status="OPEN")
        session.add(notice)
        session.flush()
        persist_pps_metadata_version(
            session, notice,
            raw_item={"bidNtceNo": "R26BK00000001", "bidNtceOrd": "000",
                      "ntceSpecFileNm1": files[1][0], "ntceSpecDocUrl1": _url(1),
                      "ntceSpecFileNm2": files[2][0], "ntceSpecDocUrl2": _url(2)},
            search_keywords=["합성"], dry_run=False,
        )
        session.commit()
        notice_id = notice.id
    client = _client(clause, confidence)
    with factory() as session:
        result = enrich_notice_from_pps(session, notice_id=notice_id, openai_api_key="synthetic-only",
                                        openai_model="synthetic", transport=httpx.MockTransport(respond),
                                        openai_client_factory=client)
    return engine, factory, notice_id, client, result


def test_confirmed_industry_failure_in_the_announcement_skips_the_rfp_and_is_shown():
    engine, factory, notice_id, client, result = _run(INDUSTRY_FAIL)
    try:
        assert len(client.labels) == 1                      # only the announcement was read
        assert INDUSTRY_FAIL[:20] in client.labels[0]
        assert NOTICE_CONFIRMED_INELIGIBLE_SKIP in result.warnings
        with factory() as session:
            outcome = run_analysis_pipeline(session, notice_id=notice_id)
            session.commit()
            notice = session.get(Notice, notice_id)
            evaluation = session.get(Evaluation, outcome.evaluation_id)
            assert outcome.eligibility == "FAIL"
            assert has_current_independent_failure(notice, evaluation)
            assert latest_current_evaluation(notice) is evaluation
    finally:
        engine.dispose()


def test_second_enrichment_round_still_skips_without_paid_calls():
    engine, factory, notice_id, client, _result = _run(INDUSTRY_FAIL)
    try:
        def no_download(request):
            raise AssertionError("no attachment download after the gate")
        with factory() as session:
            again = enrich_notice_from_pps(session, notice_id=notice_id, openai_api_key="synthetic-only",
                                           openai_model="synthetic", transport=httpx.MockTransport(no_download),
                                           openai_client_factory=client)
        assert len(client.labels) == 1
        assert NOTICE_CONFIRMED_INELIGIBLE_SKIP in again.warnings
    finally:
        engine.dispose()


@pytest.mark.parametrize("clause,confidence", [
    (SME_FAIL, 0.98),          # certificate gates may be lifted by a nonprofit exception elsewhere
    (INDUSTRY_FAIL, 0.85),     # weakly anchored failure
    (PASS_CLAUSE, 0.98),       # the announcement passes
])
def test_other_announcements_keep_reading_every_attachment(clause, confidence):
    engine, _factory, _notice_id, client, result = _run(clause, confidence=confidence)
    try:
        assert len(client.labels) == 2
        assert NOTICE_CONFIRMED_INELIGIBLE_SKIP not in result.warnings
    finally:
        engine.dispose()
