"""A REVIEW attempt with failed anchors keeps only trusted diagnostics (2026-10-05)."""
from pai_loop.integrations.openai_extraction import ExtractionOutcome
from pai_loop.models import NoticeVersion
from pai_loop.pps_enrichment import enrich_notice_from_pps
from test_pps_enrichment import _single_hwpx_reuse_case

SAMPLE = {"hint": "LETTERS_ONLY_MATCH", "quote_chars": 31, "quote_sha256": "a" * 64,
          "source_excerpt": "「소프트웨어 진흥법」에 따른 사업자"}


class _QuoteFailureClient:
    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def extract(self, *, document_text, allowed_attachment_ids, **kwargs):
        return ExtractionOutcome(status="REVIEW", review_code="R07", error_code="UNVERIFIED_QUOTE",
                                 message="인용 불일치", model="synthetic", api_calls=2,
                                 corrective_retry_used=True, unverified_quote_samples=[SAMPLE])


def test_review_attempt_persists_quote_diagnostics():
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(notice_key="PPS-SYN-QUOTE-SAMPLES")
    try:
        with factory() as session:
            enrich_notice_from_pps(session, notice_id=notice_id, openai_api_key="synthetic-only",
                                   openai_model="synthetic", transport=transport,
                                   openai_client_factory=_QuoteFailureClient)
        with factory() as session:
            attempt = next(
                v for v in session.query(NoticeVersion).filter_by(notice_id=notice_id)
                if (v.source_payload or {}).get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
            )
            assert attempt.source_payload["error_code"] == "UNVERIFIED_QUOTE"
            assert attempt.source_payload["unverified_quote_samples"] == [SAMPLE]
            assert attempt.source_payload["result"] is None
    finally:
        engine.dispose()
