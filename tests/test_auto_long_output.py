"""In-request 32k escalation after a 20k stop, SYN only, no provider or operating-service calls."""
import io
import json
import time
import zipfile

import httpx
import pytest

from pai_loop.integrations.openai_extraction import OpenAIExtractionClient
from pai_loop.models import IngestionJob, Notice
import pai_loop.pps_enrichment as enrichment
from test_failed_attachment_retry_scope import failed_case, KEY  # noqa: F401  (fixture)
from test_long_output_once import FAILURE, POLICY, claim_for, long_case  # noqa: F401  (fixture)
from test_pps_enrichment import _QUANTITATIVE_RETRY_SOURCE


def run_ordinary_retry(long_case, *, remaining=900):
    """An operator's ordinary FAILED_ATTACHMENTS retry of a 20k failure (no LONG policy chosen)."""
    client, notice_id, _, _ = long_case
    with client.app.state.session_factory() as session:
        notice = session.get(Notice, notice_id)
        scope = enrichment.failed_attachment_retry_snapshot(
            list(notice.versions), error_codes=["HTTP_ERROR"], max_attachments=1,
            notice_key=KEY, revision_no=notice.revision_no)
    budgets = []

    def download(request):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr("Contents/section0.xml", "<s>" + "".join(
                f"<p>{line}</p>" for line in _QUANTITATIVE_RETRY_SOURCE.splitlines()) + "<p>SYN 4</p></s>")
        return httpx.Response(200, content=buffer.getvalue())

    def provider(request):
        json.loads(request.content)
        return httpx.Response(500, json={"gateway_error": FAILURE})

    def factory(**kwargs):
        budgets.append((kwargs.get("budget_policy"), kwargs.get("max_output_tokens"), kwargs["timeout_seconds"]))
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(provider))

    with client.app.state.session_factory() as session:
        result = enrichment.enrich_notice_from_pps(
            session, notice_id=notice_id, openai_api_key="SYN-key", openai_model="claude-sonnet-5",
            llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.invalid/",
            transport=httpx.MockTransport(download), openai_client_factory=factory,
            deadline_monotonic=time.monotonic() + remaining,
            retry_reviewed_version_ids=frozenset(scope["version_ids"]), failed_attachment_retry=scope)
    return result, budgets


def latest_failure(client, notice_id):
    with client.app.state.session_factory() as session:
        rows = [v for v in session.get(Notice, notice_id).versions
                if v.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
                and v.source_payload.get("status") == "REVIEW"]
        return max(rows, key=lambda row: row.version_no).id


def test_20k_stop_takes_the_one_32k_call_in_the_same_request(long_case):
    client, notice_id, _, _ = long_case
    result, budgets = run_ordinary_retry(long_case)
    # The ordinary attempt stops at 20k, then exactly one LONG_OUTPUT_ONCE call follows.
    assert budgets[-1] == (POLICY, 32000, 300)
    assert all(policy is None for policy, _tokens, _timeout in budgets[:-1])
    assert result.openai_calls == len(budgets)
    with client.app.state.session_factory() as session:
        claims = session.query(IngestionJob).filter(IngestionJob.source == POLICY).all()
        assert len(claims) == 1


def test_escalation_is_spent_once_per_failure(long_case):
    client, notice_id, _, _ = long_case
    run_ordinary_retry(long_case)
    # The 32k attempt itself is not a new 20k allowance; a second retry makes no 32k call.
    _result, budgets = run_ordinary_retry(long_case)
    assert all(policy is None for policy, _tokens, _timeout in budgets)


def test_kill_switch_keeps_the_old_behaviour(long_case, monkeypatch):
    monkeypatch.setenv(enrichment.AUTO_LONG_OUTPUT_ENV, "0")
    client, _, _, _ = long_case
    _result, budgets = run_ordinary_retry(long_case)
    assert all(policy is None for policy, _tokens, _timeout in budgets)
    with client.app.state.session_factory() as session:
        assert session.query(IngestionJob).filter(IngestionJob.source == POLICY).count() == 0


def test_no_escalation_without_time_for_a_whole_32k_unit(long_case):
    client, _, _, _ = long_case
    # Enough for the ordinary 180s unit, not for download + 300s + guard afterwards.
    _result, budgets = run_ordinary_retry(long_case, remaining=400)
    assert all(policy is None for policy, _tokens, _timeout in budgets)
    with client.app.state.session_factory() as session:
        assert session.query(IngestionJob).filter(IngestionJob.source == POLICY).count() == 0
