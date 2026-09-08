"""Bounded SYN recovery, no provider or operating-service calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import io
import json
import time
import zipfile
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select, event

from pai_loop.models import IngestionJob, Notice, NoticeVersion
from test_failed_attachment_retry_scope import failed_case, KEY
from test_reviewed_analysis_campaign import PLAN, BATCH, request_body, batch_body
from pai_loop.long_output_policy import consume_long_output, mark_long_output_dispatch, long_output_claim_id
from pai_loop.integrations.openai_extraction import OpenAIExtractionClient
import pai_loop.pps_enrichment as enrichment
import pai_loop.analysis_api as api
from test_pps_enrichment import _QUANTITATIVE_RETRY_SOURCE
from test_openai_extraction import valid_output, response_payload

POLICY = "LONG_OUTPUT_ONCE"
FAILURE = dict(version="gateway-failure-v1", stage="OUTPUT_NORMALIZATION", code="OUTPUT_REJECTED",
               upstream_http_status=None, detail_code="NATIVE_STOP_MAX_TOKENS", stop_reason="max_tokens",
               usage=dict(input_tokens=100, output_tokens=20000, total_tokens=20100))


@pytest.fixture
def long_case(failed_case):
    client, notice_id, downloads, _, _ = failed_case
    with client.app.state.session_factory() as session:
        row = next(v for v in session.get(Notice, notice_id).versions if v.source_payload.get("error_code") == "XLS_PARSE_FAILED")
        payload = deepcopy(row.source_payload)
        payload.update(error_code="HTTP_ERROR", message="모델 API가 HTTP 500를 반환했습니다.", gateway_failure=deepcopy(FAILURE))
        row.source_payload = payload
        session.commit()
        original_id = row.id
    return client, notice_id, original_id, downloads


def long_body(**changes):
    return request_body(notice_keys=[KEY], retry_scope="FAILED_ATTACHMENTS", retry_error_codes=["HTTP_ERROR"],
                        retry_max_attachments=1, retry_budget_policy=POLICY, max_total=1,
                        execution_limit=1, max_continuations=1, resume_active=False, **changes)


def test_long_plan_freezes_policy_without_consumption(long_case):
    client, _, _, downloads = long_case
    response = client.post(PLAN, json=long_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retry_budget_policy"] == POLICY and body["retry_target_count"] == 1
    with client.app.state.session_factory() as session:
        assert not session.scalars(select(IngestionJob).where(IngestionJob.source == POLICY)).all()
    assert downloads == []


@pytest.mark.parametrize("patch", [
    {"retry_budget_policy": "SYN-UNKNOWN"}, {"retry_scope": None}, {"retry_max_attachments": 2},
    {"max_total": 2}, {"max_continuations": 2}, {"retry_error_codes": ["NETWORK_ERROR"]},
    {"max_output_tokens": 32000}, {"retry_budget_policy": POLICY, "timeout_seconds": 300},
])
def test_long_plan_rejects_unbounded_knobs(long_case, patch):
    client, _, _, downloads = long_case
    body = long_body()
    body.update(patch)
    assert client.post(PLAN, json=body).status_code == 422
    assert downloads == []


@pytest.mark.parametrize("mutation", ["detail", "missing", "raw_extra", "http", "input", "source", "legacy", "already32k"])
def test_long_plan_requires_exact_current_max_tokens_failure(long_case, mutation):
    client, _, row_id, downloads = long_case
    with client.app.state.session_factory() as session:
        row = session.get(NoticeVersion, row_id)
        payload = deepcopy(row.source_payload)
        if mutation == "detail": payload["gateway_failure"]["detail_code"] = "OUTPUT_JSON_INVALID"
        elif mutation == "missing": payload.pop("gateway_failure")
        elif mutation == "raw_extra": payload["gateway_failure"]["raw"] = "SYN-PRIVATE-CANARY"
        elif mutation == "http": payload["message"] = "SYN HTTP 500 freeform"
        elif mutation == "input": payload["document_processing"]["analysis_input_complete"] = False
        elif mutation == "source": payload["document_processing"]["source_read_complete"] = False
        elif mutation == "legacy": payload["prompt_version"] = "SYN-old"
        elif mutation == "already32k":
            payload["gateway_failure"]["usage"].update(output_tokens=32000, total_tokens=32100)
        row.source_payload = payload
        session.commit()
    response = client.post(PLAN, json=long_body())
    assert response.status_code == 409
    assert "SYN-PRIVATE-CANARY" not in response.text and downloads == []


def claim_for(client, row_id):
    with client.app.state.session_factory() as session:
        return long_output_claim_id(session.get(NoticeVersion, row_id))


def consume(client, row_id):
    with client.app.state.session_factory() as session:
        return consume_long_output(session, version_id=row_id, validate_current=lambda _: True)


@pytest.mark.parametrize("new_failure_version", [False, True])
def test_consumption_is_atomic_across_campaigns_and_survives_retention(long_case, new_failure_version):
    client, _, row_id, _ = long_case
    row_ids = [row_id, row_id]
    if new_failure_version:
        with client.app.state.session_factory() as session:
            old = session.get(NoticeVersion, row_id)
            new = NoticeVersion(notice_id=old.notice_id, version_no=old.version_no + 1,
                                file_sha256=old.file_sha256, source_payload=deepcopy(old.source_payload))
            session.add(new)
            session.commit()
            row_ids[1] = new.id
        assert claim_for(client, row_ids[1]) == claim_for(client, row_id)
    barrier = Barrier(2)
    def worker(version_id):
        barrier.wait()
        try: return consume(client, version_id)
        except ValueError as exc: return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, row_ids))
    assert results.count(claim_for(client, row_id)) == 1
    assert results.count("LONG_OUTPUT_ALREADY_CONSUMED") == 1
    with client.app.state.session_factory() as session:
        claim = session.get(IngestionJob, claim_for(client, row_id))
        assert claim.request_json["state"] == "CONSUMED_NOT_DISPATCHED"
        claim.completed_at = datetime.now(timezone.utc) - timedelta(days=100)
        session.add(IngestionJob(source="SYN-ORDINARY", mode="LIVE", status="COMPLETED",
                                window_json={}, request_json={}, completed_at=claim.completed_at))
        session.commit()
    retention = client.post("/api/v1/operations/retention", json={"retention_days": 7, "dry_run": False})
    assert retention.status_code == 200 and retention.json()["deleted"]["ingestion_jobs"] == 1
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_for(client, row_id)) is not None
    new = client.post(PLAN, json=long_body(review_campaign_key="SYN-NEW-CAMPAIGN"))
    assert new.status_code == 409 and "LONG_OUTPUT_ALREADY_CONSUMED" in new.text


def test_dispatch_requires_unconsumed_cas_and_tracks_pre_dispatch_crash(long_case):
    client, _, row_id, _ = long_case
    claim_id = consume(client, row_id)
    # A process crash here is known not to have begun transport; reservation stays spent.
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_id).request_json["state"] == "CONSUMED_NOT_DISPATCHED"
    with client.app.state.session_factory() as session:
        mark_long_output_dispatch(session, claim_id, row_id)
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_id).request_json["state"] == "DISPATCH_STARTED"
    with client.app.state.session_factory() as session, pytest.raises(ValueError, match="ALREADY_DISPATCHED"):
        mark_long_output_dispatch(session, claim_id, row_id)


@pytest.mark.parametrize("failure", ["schema", "quote", "max_tokens", "network"])
def test_single_call_contract_has_no_http_or_corrective_retry(failure):
    calls, intents = [], []
    def transport(request):
        calls.append(json.loads(request.content))
        if failure == "network": raise httpx.ReadTimeout("SYN", request=request)
        if failure == "max_tokens": return httpx.Response(500, json={"gateway_error": FAILURE})
        output = valid_output(quote="SYN unsupported quote")
        if failure == "schema": output["requirements"][0]["mandatory"] = "SYN-wrong"
        result = response_payload(output)
        result["model"] = "claude-sonnet-5"
        return httpx.Response(200, json=result)
    with OpenAIExtractionClient(api_key="SYN-key", model="claude-sonnet-5", provider="n8n_claude",
        base_url="https://syn-gateway.invalid/", transport=httpx.MockTransport(transport),
        budget_policy=POLICY, max_output_tokens=32000, timeout_seconds=300,
        max_total_api_calls=1, before_request=lambda: intents.append(True)) as extraction:
        result = extraction.extract(document_text="SYN complete source without the returned quote.", allowed_attachment_ids={"ATT-1"})
    assert result.status == "REVIEW" and result.api_calls == len(calls) == len(intents) == 1
    assert result.corrective_retry_used is False
    assert calls[0]["budget_policy"] == POLICY and calls[0]["max_output_tokens"] == 32000


def run_frozen(long_case, *, remaining=450, factory_failure=False, events=None, scope_override=None, mutate_download=None):
    client, notice_id, _, _ = long_case
    with client.app.state.session_factory() as session:
        notice = session.get(Notice, notice_id)
        scope = scope_override or enrichment.failed_attachment_retry_snapshot(list(notice.versions), error_codes=["HTTP_ERROR"],
            max_attachments=1, notice_key=KEY, revision_no=notice.revision_no, budget_policy=POLICY, session=session,
            source_boundary=api._review_source_boundary(notice))
    downloads, calls = [], []
    def download(request):
        downloads.append(request.url.params["fileSeq"])
        if mutate_download: mutate_download()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr("Contents/section0.xml", "<s>" + "".join(f"<p>{line}</p>" for line in _QUANTITATIVE_RETRY_SOURCE.splitlines()) + "<p>SYN 4</p></s>")
        return httpx.Response(200, content=buffer.getvalue())
    def provider(request):
        calls.append(json.loads(request.content))
        if events is not None: events.append("PROVIDER_CALLED")
        return httpx.Response(500, json={"gateway_error": FAILURE})
    def factory(**kwargs):
        assert kwargs["timeout_seconds"] == 300 and kwargs["max_total_api_calls"] == 1
        assert kwargs["budget_policy"] == POLICY and kwargs["max_output_tokens"] == 32000
        if factory_failure: raise RuntimeError("SYN before dispatch crash")
        return OpenAIExtractionClient(**kwargs, transport=httpx.MockTransport(provider))
    with client.app.state.session_factory() as session:
        result = enrichment.enrich_notice_from_pps(session, notice_id=notice_id, openai_api_key="SYN-key",
            openai_model="claude-sonnet-5", llm_provider="n8n_claude", llm_gateway_base_url="https://syn-gateway.invalid/",
            transport=httpx.MockTransport(download), openai_client_factory=factory,
            deadline_monotonic=time.monotonic() + remaining,
            retry_reviewed_version_ids=frozenset(scope["version_ids"]), failed_attachment_retry=scope)
    return result, downloads, calls


@pytest.mark.parametrize("remaining", [340, 350, 450])
def test_341_second_unit_uses_one_model_call_and_reuses_siblings(long_case, remaining):
    client, notice_id, row_id, _ = long_case
    with client.app.state.session_factory() as session:
        original = {v.id: deepcopy(v.source_payload) for v in session.get(Notice, notice_id).versions}
    result, downloads, calls = run_frozen(long_case, remaining=remaining)
    expected = int(remaining >= 341)
    assert len(downloads) == len(calls) == result.openai_calls == expected
    assert all(item.openai_calls == 0 for item in result.attachment_results if item.status == "REUSED")
    with client.app.state.session_factory() as session:
        for key, payload in original.items(): assert session.get(NoticeVersion, key).source_payload == payload
        assert (session.get(IngestionJob, claim_for(client, row_id)) is not None) == bool(expected)


def test_failure_after_consumption_before_transport_stays_spent(long_case):
    client, _, row_id, _ = long_case
    result, _, calls = run_frozen(long_case, factory_failure=True)
    assert calls == [] and result.openai_calls == 0
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_for(client, row_id)).request_json["state"] == "CONSUMED_NOT_DISPATCHED"
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        consume(client, row_id)


@pytest.mark.parametrize("new_normal_failure", [False, True])
def test_long_result_cannot_become_a_new_allowance_even_if_usage_reports_20k(long_case, new_normal_failure):
    client, notice_id, _, _ = long_case
    _, _, calls = run_frozen(long_case)
    assert len(calls) == 1
    if new_normal_failure:
        # A later ordinary attempt may append a distinct unmarked 20k failure;
        # unchanged input must still use the already consumed reservation.
        with client.app.state.session_factory() as session:
            latest = max(session.get(Notice, notice_id).versions, key=lambda row: row.version_no)
            data = deepcopy(latest.source_payload)
            assert data["document_processing"].pop("retry_budget_policy") == POLICY
            session.add(NoticeVersion(notice_id=notice_id, version_no=latest.version_no + 1,
                                      file_sha256=latest.file_sha256, source_payload=data))
            session.commit()
    response = client.post(PLAN, json=long_body(review_campaign_key="SYN-new-after-long-result"))
    assert response.status_code == 409


@pytest.mark.parametrize("elapsed, expected_calls", [(45, 1), (46, 0)])
def test_download_time_is_rechecked_before_durable_consumption(long_case, monkeypatch, elapsed, expected_calls):
    client, _, row_id, _ = long_case
    clock = [1000.0]
    monkeypatch.setattr(enrichment.time, "monotonic", lambda: clock[0])
    def advance():
        clock[0] += elapsed
    result, downloads, calls = run_frozen(long_case, remaining=350, mutate_download=advance)
    assert len(downloads) == 1
    assert len(calls) == result.openai_calls == expected_calls
    with client.app.state.session_factory() as session:
        assert (session.get(IngestionJob, claim_for(client, row_id)) is not None) == bool(expected_calls)


def test_lost_commit_acknowledgement_still_blocks_dispatch(long_case):
    client, _, row_id, _ = long_case
    def crash(_session): raise RuntimeError("SYN lost commit acknowledgement")
    with client.app.state.session_factory() as session:
        event.listen(session, "after_commit", crash)
        with pytest.raises(RuntimeError, match="lost commit"):
            consume_long_output(session, version_id=row_id, validate_current=lambda _: True)
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        consume(client, row_id)


def test_provider_result_and_fallback_persistence_failure_cannot_repeat(long_case, monkeypatch):
    client, _, row_id, _ = long_case
    events = []
    def crash(*args, **kwargs): raise RuntimeError("SYN storage unavailable")
    monkeypatch.setattr(enrichment, "_persist_extraction_version", crash)
    monkeypatch.setattr(enrichment, "record_internal_pps_enrichment_failure", crash)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        run_frozen(long_case, events=events)
    assert events == ["PROVIDER_CALLED"]
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_for(client, row_id)).request_json["state"] == "DISPATCH_STARTED"
    with pytest.raises(ValueError, match="ALREADY_CONSUMED"):
        consume(client, row_id)
    response = client.post(PLAN, json=long_body(review_campaign_key="SYN-AFTER-FAILURE"))
    assert response.status_code == 409 and "LONG_OUTPUT_ALREADY_CONSUMED" in response.text


def test_plan_auth_cannot_be_replaced_by_cookie_pin_or_browser_server_key(long_case):
    client, _, _, downloads = long_case
    client.app.state.settings = replace(client.app.state.settings, api_key="SYN-server-only")
    for headers in [{}, {"Cookie": "pai_session=SYN-forged"}, {"X-PAI-Manual-Token": "SYN-pin"},
                    {"X-PAI-LOOP-API-KEY": "SYN-server-only", "Origin": "https://syn.invalid"}]:
        assert client.post(PLAN, json=long_body(), headers=headers).status_code in {401, 403}
    assert downloads == []


@pytest.mark.parametrize("change", [{"budget_policy": None}, {"max_total_api_calls": 2},
    {"timeout_seconds": 180}, {"max_output_tokens": 32001}, {"before_request": None}, {"provider": "openai"}])
def test_client_rejects_mixed_or_unbounded_policy(change):
    kwargs = dict(api_key="SYN-key", model="claude-sonnet-5", provider="n8n_claude", base_url="https://syn.invalid/",
                  budget_policy=POLICY, max_output_tokens=32000, timeout_seconds=300,
                  max_total_api_calls=1, before_request=lambda: None)
    kwargs.update(change)
    with pytest.raises(ValueError): OpenAIExtractionClient(**kwargs)


def test_dry_run_never_consumes_or_calls_and_policy_identity_is_immutable(long_case):
    client, _, row_id, downloads = long_case
    planned = client.post(PLAN, json=long_body(dry_run=True))
    assert planned.status_code == 200
    batch = client.post(BATCH, json=batch_body(planned.json()))
    assert batch.status_code == 200 and batch.json()["openai_calls"] == 0 and downloads == []
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_for(client, row_id)) is None
    changed = long_body(dry_run=True)
    changed.pop("retry_budget_policy")
    assert client.post(PLAN, json=changed).status_code == 409


@pytest.mark.parametrize("mutation", ["revision", "metadata", "source"])
def test_change_during_download_blocks_consumption_and_provider(long_case, mutation):
    client, notice_id, row_id, _ = long_case
    def mutate():
        with client.app.state.session_factory() as session:
            notice = session.get(Notice, notice_id)
            if mutation == "revision": notice.revision_no = "SYN-new"
            elif mutation == "metadata":
                metadata = next(v for v in notice.versions if v.source_payload.get("kind") == enrichment.PPS_METADATA_KIND)
                data = deepcopy(metadata.source_payload)
                data["SYN-late-source-change"] = True
                metadata.source_payload = data
            else:
                row = session.get(NoticeVersion, row_id)
                data = deepcopy(row.source_payload)
                data["document_processing"]["analysis_input_sha256"] = "f" * 64
                row.source_payload = data
            session.commit()
    result, downloads, calls = run_frozen(long_case, mutate_download=mutate)
    assert len(downloads) == 1 and calls == [] and result.openai_calls == 0
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_for(client, row_id)) is None


@pytest.mark.parametrize("lost_response", [False, True])
def test_real_batch_inherits_policy_and_replay_never_repeats_provider(long_case, monkeypatch, lost_response):
    client, _, row_id, _ = long_case
    events = []
    def worker(request, *, notice_id, payload, deadline_monotonic,
               retry_reviewed_version_ids=frozenset(), failed_attachment_retry=None):
        assert failed_attachment_retry["budget_policy"] == POLICY
        assert retry_reviewed_version_ids == frozenset(failed_attachment_retry["version_ids"])
        return run_frozen(long_case, remaining=deadline_monotonic-time.monotonic(),
                          scope_override=failed_attachment_retry, events=events)[0]
    monkeypatch.setattr(api, "_enrich_one_notice", worker)
    plan = client.post(PLAN, json=long_body())
    assert plan.status_code == 200
    body = batch_body(plan.json())
    original = api._store_batch_response
    stored = []
    def crash_after_result(*args, **kwargs):
        stored.append(True)
        if len(stored) == 1: raise RuntimeError("SYN response lost after attachment persistence")
        return original(*args, **kwargs)
    if lost_response:
        monkeypatch.setattr(api, "_store_batch_response", crash_after_result)
        with pytest.raises(RuntimeError, match="response lost"):
            client.post(BATCH, json=body)
    first = client.post(BATCH, json=body)
    assert first.status_code == 200, first.text
    second = client.post(BATCH, json=body)
    assert second.status_code == 200 and second.json() == first.json()
    assert events == ["PROVIDER_CALLED"]
    with client.app.state.session_factory() as session:
        assert session.get(IngestionJob, claim_for(client, row_id)) is not None
