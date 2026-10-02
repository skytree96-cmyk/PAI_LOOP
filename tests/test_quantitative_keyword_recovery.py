"""Synthetic production persistence boundary; no external calls."""
import json
import sys
from datetime import datetime, timezone

import httpx
import pytest

from pai_loop.integrations.openai_extraction import OpenAIExtractionClient
from pai_loop.models import Notice, NoticeVersion, IngestionJob, NoticeAnalysisPolicy, PpsNoticeAuthority
from pai_loop.pps_enrichment import enrich_notice_from_pps, has_current_accepted_pps_extraction
from pai_loop.quantitative_scoring import _current_dynamic_quantitative_profile
from test_pps_enrichment import _single_hwpx_reuse_case, _RetryableReviewClient
from test_quantitative_count_ranges import fixture, ATT
from test_quantitative_probe_client import response, make_client
from test_long_output_once import FAILURE


@pytest.mark.parametrize("first_raises", [False, True])
@pytest.mark.parametrize("retry_sibling", [False, True])
def test_failed_sibling_refresh_preserves_next_attempt_and_accounting(first_raises, retry_sibling):
    import time
    from copy import deepcopy

    _, source = fixture(inline=True)
    engine, factory, notice_id, download = _single_hwpx_reuse_case(
        notice_key="SYN-SIBLING-TRANSACTION", source_text=source)
    with factory() as session:
        metadata = session.get(Notice, notice_id).versions[0]
        saved = deepcopy(metadata.source_payload)
        sibling = deepcopy(saved["attachment_manifest"][0])
        sibling.update(attachment_id="PPS-ATT-" + "a" * 24, slot=2,
                       file_name="SYN 제안요청서2.hwpx")
        saved["attachment_manifest"].append(sibling)
        metadata.source_payload = saved
        session.commit()
    options = dict(notice_id=notice_id, openai_api_key="SYN-key", openai_model="SYN-model",
                   llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.test/v1",
                   transport=download)
    with factory() as session:
        enrich_notice_from_pps(session, **options, openai_client_factory=_RetryableReviewClient)
        failed = [v for v in session.get(Notice, notice_id).versions
                  if v.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"]
        ids = frozenset(v.id for v in failed)
    assert len(ids) == 2
    if not retry_sibling:
        ids = frozenset(v.id for v in failed if v.source_payload["attachment_id"] != sibling["attachment_id"])
    calls = []
    def handler(request):
        calls.append(request)
        if first_raises and len(calls) == 1:
            raise RuntimeError("SYN-UNEXPECTED-FAILURE")
        return httpx.Response(502, json={"error": "SYN-FAILURE"})
    def client_factory(**kwargs):
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(handler))
    with factory() as session:
        result = enrich_notice_from_pps(session, **options, openai_client_factory=client_factory,
            retry_reviewed_version_ids=ids, deadline_monotonic=time.monotonic() + 900)
        assert not session.in_transaction()
    assert len(calls) == (2 if retry_sibling else 1)
    assert result.openai_calls == len(calls) - int(first_raises)
    assert ("INTERNAL_ENRICHMENT_ERROR" in result.warnings) is first_raises
    if first_raises:
        assert not result.openai_telemetry.accounting_complete
    engine.dispose()


@pytest.mark.parametrize("defer", [False, True])
@pytest.mark.parametrize("mislabeled_hwp", [False, True])
def test_ordinary_then_long_failure_automatically_recovers_once(defer, mislabeled_hwp, monkeypatch):
    payload, source = fixture(inline=True)
    source = "SYN unrelated task description\n" * 100 + source
    engine, factory, notice_id, download = _single_hwpx_reuse_case(
        notice_key="SYN-AUTO-RECOVERY", source_text=source)
    if mislabeled_hwp:
        from test_document_extraction import _fake_olefile_module, _hwp_record, _raw_deflate
        section = b"".join(_hwp_record(67, line.encode("utf-16le")) for line in source.splitlines())
        monkeypatch.setitem(sys.modules, "olefile", _fake_olefile_module([_raw_deflate(section)]))
        download = httpx.MockTransport(lambda request: httpx.Response(
            200, content=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1SYN-HWP",
            headers={"Content-Type": "application/octet-stream"}))
    calls, budgets = [], []
    clock = [1000.0]
    monkeypatch.setattr("pai_loop.pps_enrichment.time.monotonic", lambda: clock[0])

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) <= 2:
            failure = json.loads(json.dumps(FAILURE))
            tokens = 20_000 if len(calls) == 1 else 32_000
            failure["usage"].update(output_tokens=tokens, total_tokens=tokens + 100)
            if defer and len(calls) == 2:
                clock[0] = 1700.0
            return httpx.Response(500, json={"gateway_error": failure})
        if len(calls) == 3:
            return httpx.Response(200, json={"status": "completed", "output_text": '{"requirements": []}'})
        # Bind the synthetic result to the actual generated fixture attachment.
        with factory() as session:
            metadata = next(v for v in session.get(Notice, notice_id).versions
                            if v.source_payload.get("kind") == "PPS_NOTICE_METADATA")
            attachment_id = metadata.source_payload["attachment_manifest"][0]["attachment_id"]
        return response(json.loads(json.dumps(payload).replace(ATT, attachment_id)))

    def client_factory(**kwargs):
        budgets.append(kwargs.get("budget_policy"))
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(handler))

    options = dict(notice_id=notice_id, openai_api_key="SYN-key", openai_model="claude-sonnet-5",
        llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.test/v1",
        transport=download, openai_client_factory=client_factory)
    with factory() as session:
        result = enrich_notice_from_pps(session, **options, deadline_monotonic=1900.0)
    if defer:
        assert len(calls) == result.openai_calls == 2
        assert "ATTACHMENT_CONTINUATION_REQUIRED" in result.warnings
        with factory() as session:
            result = enrich_notice_from_pps(session, **options, deadline_monotonic=2600.0)
        assert result.openai_calls == 2
    else:
        assert result.openai_calls == 4
    assert len(calls) == 4
    assert budgets == [None, "LONG_OUTPUT_ONCE", None]
    assert "<source_excerpts" in calls[-1]["input"][1]["content"][0]["text"]
    with factory() as session:
        stored = session.get(NoticeVersion, result.version_id)
        assert stored.source_payload["status"] == "ACCEPTED"
        assert not stored.document_complete
        if mislabeled_hwp:
            recovery = stored.source_payload["document_processing"]["quantitative_recovery"]
            assert recovery["native_hwpx_context_status"] == "NOT_HWPX"
            assert recovery["native_hwpx_table_context"] is False
            assert recovery["xml_fallback_used"] is True
        assert session.query(IngestionJob).filter_by(source="QUANTITATIVE_RECOVERY_ONCE").count() == 1
    with factory() as session:
        reused = enrich_notice_from_pps(session, **options, deadline_monotonic=2600.0)
    assert reused.openai_calls == 0
    assert len(calls) == 4
    engine.dispose()


@pytest.mark.parametrize("failure_kind", ["INCOMPLETE_RESPONSE", "HTTP20", "HTTP32"])
def test_explicit_failed_retry_saves_partial_scoring_proof_and_reuses_it(failure_kind):
    payload, source = fixture(inline=True)
    source = "SYN unrelated task description\n" * 100 + source
    engine, factory, notice_id, download = _single_hwpx_reuse_case(
        notice_key="SYN-KEYWORD-RECOVERY", source_text=source,
    )
    options = dict(notice_id=notice_id, openai_api_key="SYN-key", openai_model="SYN-model",
                   llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.test/v1",
                   transport=download)
    with factory() as session:
        first = enrich_notice_from_pps(session, **options, openai_client_factory=_RetryableReviewClient)
        failed = session.get(NoticeVersion, first.version_id)
        attachment_id = failed.source_payload["attachment_id"]
        if failure_kind.startswith("HTTP"):
            saved = json.loads(json.dumps(failed.source_payload))
            failure = json.loads(json.dumps(FAILURE))
            tokens = 32_000 if failure_kind == "HTTP32" else 20_000
            failure["usage"].update(output_tokens=tokens, total_tokens=tokens + 100)
            saved.update(error_code="HTTP_ERROR", gateway_failure=failure,
                         message="모델 API가 HTTP 500를 반환했습니다.")
            failed.source_payload = saved
            session.commit()
    payload = json.loads(json.dumps(payload).replace(ATT, attachment_id))
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, json={"status": "completed", "output_text": '{"requirements": []}'})
        return response(payload)

    def client_factory(**kwargs):
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(handler))

    retry = dict(retry_reviewed_version_ids=frozenset({first.version_id}),
                 openai_client_factory=client_factory)
    with factory() as session:
        result = enrich_notice_from_pps(session, **options, **retry)
    assert result.status == "REVIEW"
    assert result.openai_calls == len(calls) == 2
    assert not result.analysis_input_complete
    assert "<source_excerpts" in calls[1]["input"][1]["content"][0]["text"]
    with factory() as session:
        stored = session.get(NoticeVersion, result.version_id)
        assert stored.source_payload["status"] == "ACCEPTED"
        assert stored.document_complete is False
        assert stored.source_payload["result"]["requirements"] == []
        audit = stored.source_payload["document_processing"]
        assert audit["quantitative_recovery"]["failed_version_id"] == first.version_id
        assert audit["analysis_input_complete"] is False
        assert not has_current_accepted_pps_extraction(session, notice_id)
        notice = session.get(Notice, notice_id)
        profile = _current_dynamic_quantitative_profile(notice)
        assert profile is not None
        assert profile.status != "AVAILABLE"
        assert stored.source_payload["quantitative_validation_record"]["status"] == "AVAILABLE"
    with factory() as session:
        reused = enrich_notice_from_pps(session, **options, **retry)
    assert reused.version_id == result.version_id
    assert reused.openai_calls == 0
    assert len(calls) == 2
    engine.dispose()


def test_keyword_failure_without_heading_spends_no_recovery_call():
    engine, factory, notice_id, download = _single_hwpx_reuse_case(
        notice_key="SYN-NO-SCORING-HEADING", source_text="SYN 문서의 과업 설명만 있습니다. 분석 실패 이후에도 배정하지 않습니다.",
    )
    options = dict(notice_id=notice_id, openai_api_key="SYN-key", openai_model="SYN-model",
                   llm_provider="n8n_claude", transport=download)
    with factory() as session:
        first = enrich_notice_from_pps(session, **options, openai_client_factory=_RetryableReviewClient)
    def forbidden(**kwargs):
        raise AssertionError("No keyword, no provider call")
    with factory() as session:
        result = enrich_notice_from_pps(session, **options, openai_client_factory=forbidden,
            retry_reviewed_version_ids=frozenset({first.version_id}))
    assert result.version_id == first.version_id
    assert result.openai_calls == 0
    engine.dispose()


def test_failed_recovery_quotes_never_supply_unverified_scoring_rows():
    payload, _ = fixture(inline=True)
    calls = []
    def handler(request):
        calls.append(request)
        return response(payload)
    with make_client(handler) as client:
        outcome, audit = client.extract_quantitative_recovery(
            document_text="SYN 정량 배점표: 별도 문서의 내용입니다.", allowed_attachment_ids={ATT},
        )
    assert outcome.status == "REVIEW"
    assert outcome.error_code == "UNVERIFIED_QUOTE"
    assert outcome.unverified_quantitative_tables is None
    assert outcome.data is None
    assert outcome.api_calls == len(calls) == 2
    assert audit["attachment_coverage_complete"] is False
    assert audit["eligibility_complete"] is False


@pytest.mark.parametrize("guard", ["closed", "cancelled", "expired", "manual", "consumed", "failed_recovery"])
def test_automatic_recovery_guards_and_failed_attempt_is_terminal(guard):
    payload, source = fixture(inline=True)
    source = "SYN unrelated\n" * 100 + source
    engine, factory, notice_id, download = _single_hwpx_reuse_case(
        notice_key="PPS-SYN-AUTO-GUARD", source_text=source)
    options = dict(notice_id=notice_id, openai_api_key="SYN-key", openai_model="claude-sonnet-5",
        llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.test/v1", transport=download)
    with factory() as session:
        first = enrich_notice_from_pps(session, **options, openai_client_factory=_RetryableReviewClient)
        version = session.get(NoticeVersion, first.version_id)
        saved = json.loads(json.dumps(version.source_payload))
        saved["document_processing"]["retry_budget_policy"] = "LONG_OUTPUT_ONCE"
        version.source_payload = saved
        notice = session.get(Notice, notice_id)
        if guard == "closed": notice.status = "CLOSED"
        if guard == "cancelled":
            session.add(PpsNoticeAuthority(bid_notice_no=notice.bid_notice_no,
                revision_no=notice.revision_no, disposition="CANCELLED", authority_sha256="c" * 64))
        if guard == "expired": notice.deadline = datetime(2020, 1, 1, tzinfo=timezone.utc)
        if guard == "manual":
            session.add(NoticeAnalysisPolicy(notice_key=notice.notice_key,
                bid_notice_no=notice.bid_notice_no, analysis_policy="MANUAL_ONLY"))
        session.commit()
    if guard == "consumed":
        from pai_loop import quantitative_recovery_policy as policy
        with factory() as session:
            with session.begin(): version = session.get(NoticeVersion, first.version_id)
            assert policy.consume(session, version, lambda: True)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "output_text": '{"requirements": []}'})
    def client_factory(**kwargs):
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(handler))
    import time
    for _ in range(2):
        with factory() as session:
            result = enrich_notice_from_pps(session, **options, openai_client_factory=client_factory,
                                            deadline_monotonic=time.monotonic() + 900)
        assert result.status in {"REVIEW", "REUSED"}
    assert len(calls) == (2 if guard == "failed_recovery" else 0)
    engine.dispose()


SCHEMA_DECODE = dict(version="gateway-failure-v1", stage="OUTPUT_NORMALIZATION", code="OUTPUT_REJECTED",
                     upstream_http_status=None, detail_code="NATIVE_SCHEMA_DECODE_INVALID", stop_reason="end_turn",
                     usage=dict(input_tokens=100, output_tokens=900, total_tokens=1000))
GATEWAY_TIMEOUT = dict(version="gateway-failure-v1", stage="MODEL_EXECUTION", code="MODEL_EXECUTION_FAILED",
                       upstream_http_status=None, detail_code="MODEL_TRANSPORT_UNKNOWN")


def _first_failure_case(monkeypatch, *, first_failure, later_failures=1, notice_key="SYN-FIRST-FAILURE"):
    payload, source = fixture(inline=True)
    source = "SYN unrelated task description\n" * 100 + source
    engine, factory, notice_id, download = _single_hwpx_reuse_case(notice_key=notice_key, source_text=source)
    calls, budgets = [], []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(500, json={"gateway_error": json.loads(json.dumps(first_failure))})
        if len(calls) <= 1 + later_failures:
            return httpx.Response(200, json={"status": "completed", "output_text": '{"requirements": []}'})
        with factory() as session:
            metadata = next(v for v in session.get(Notice, notice_id).versions
                            if v.source_payload.get("kind") == "PPS_NOTICE_METADATA")
            attachment_id = metadata.source_payload["attachment_manifest"][0]["attachment_id"]
        return response(json.loads(json.dumps(payload).replace(ATT, attachment_id)))

    def client_factory(**kwargs):
        budgets.append(kwargs.get("budget_policy"))
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(handler))

    options = dict(notice_id=notice_id, openai_api_key="SYN-key", openai_model="claude-sonnet-5",
                   llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.test/v1",
                   transport=download, openai_client_factory=client_factory)
    return engine, factory, options, calls, budgets


def test_first_call_schema_failure_recovers_without_32k(monkeypatch):
    import time
    engine, factory, options, calls, budgets = _first_failure_case(monkeypatch, first_failure=SCHEMA_DECODE)
    with factory() as session:
        result = enrich_notice_from_pps(session, **options, deadline_monotonic=time.monotonic() + 900)
    # ordinary call, keyword text, XML fallback; no 32k stage for a completed malformed output
    assert len(calls) == result.openai_calls == 3
    assert "LONG_OUTPUT_ONCE" not in budgets
    assert "<source_excerpts" in calls[-1]["input"][1]["content"][0]["text"]
    with factory() as session:
        stored = session.get(NoticeVersion, result.version_id)
        assert stored.source_payload["status"] == "ACCEPTED"
        assert stored.source_payload["document_processing"]["quantitative_recovery"]["xml_fallback_used"] is True
        assert not stored.document_complete
        assert session.query(IngestionJob).filter_by(source="QUANTITATIVE_RECOVERY_ONCE").count() == 1
    with factory() as session:
        reused = enrich_notice_from_pps(session, **options, deadline_monotonic=time.monotonic() + 900)
    assert reused.openai_calls == 0 and len(calls) == 3
    engine.dispose()


def test_stored_first_failure_never_spends_without_explicit_retry(monkeypatch):
    """A failure stored before deployment stays put until an explicit retry asks for it."""
    import time
    engine, factory, options, calls, _ = _first_failure_case(
        monkeypatch, first_failure=SCHEMA_DECODE, notice_key="SYN-FIRST-FAILURE-STORED")
    with factory() as session:
        first = enrich_notice_from_pps(session, **options)
    assert first.status == "REVIEW" and len(calls) == 1
    with factory() as session:
        again = enrich_notice_from_pps(session, **options, deadline_monotonic=time.monotonic() + 900)
    assert again.openai_calls == 0 and len(calls) == 1
    with factory() as session:
        result = enrich_notice_from_pps(session, **options, deadline_monotonic=time.monotonic() + 900,
                                        retry_reviewed_version_ids=frozenset({first.version_id}))
    assert len(calls) == 3 and result.openai_calls == 2
    with factory() as session:
        assert session.get(NoticeVersion, result.version_id).source_payload["status"] == "ACCEPTED"
    engine.dispose()


def test_first_failure_recovery_kill_switch(monkeypatch):
    import time
    monkeypatch.setenv("PAI_FIRST_FAILURE_RECOVERY", "false")
    engine, factory, options, calls, _ = _first_failure_case(
        monkeypatch, first_failure=SCHEMA_DECODE, notice_key="SYN-FIRST-FAILURE-OFF")
    for _ in range(2):
        with factory() as session:
            result = enrich_notice_from_pps(session, **options, deadline_monotonic=time.monotonic() + 900)
        assert result.status in {"REVIEW", "REUSED"}
    assert len(calls) == 1
    with factory() as session:
        assert session.query(IngestionJob).filter_by(source="QUANTITATIVE_RECOVERY_ONCE").count() == 0
    engine.dispose()


@pytest.mark.parametrize("auto_long", [True, False])
def test_gateway_timeout_keeps_32k_first_unless_disabled(monkeypatch, auto_long):
    import time
    monkeypatch.setenv("PAI_AUTO_LONG_OUTPUT", "1" if auto_long else "0")
    # 32k (when taken) fails too, then keyword text fails, then XML succeeds.
    engine, factory, options, calls, budgets = _first_failure_case(
        monkeypatch, first_failure=GATEWAY_TIMEOUT, later_failures=2 if auto_long else 1,
        notice_key=f"SYN-FIRST-TIMEOUT-{int(auto_long)}")
    with factory() as session:
        result = enrich_notice_from_pps(session, **options, deadline_monotonic=time.monotonic() + 1800)
    if auto_long:
        assert budgets[:2] == [None, "LONG_OUTPUT_ONCE"]
    else:
        assert "LONG_OUTPUT_ONCE" not in budgets
    assert "<source_excerpts" in calls[-1]["input"][1]["content"][0]["text"]
    with factory() as session:
        assert session.get(NoticeVersion, result.version_id).source_payload["status"] == "ACCEPTED"
        assert session.query(IngestionJob).filter_by(source="QUANTITATIVE_RECOVERY_ONCE").count() == 1
    engine.dispose()
