from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import zipfile

import httpx
import pytest
from sqlalchemy import select

import pai_loop.analysis_api as api
import pai_loop.pps_enrichment as enrichment
from pai_loop.integrations.openai_extraction import ExtractionOutcome
from pai_loop.models import Notice, NoticeVersion
from test_pps_enrichment import (
    G2B_DOWNLOAD, _QUANTITATIVE_RETRY_SOURCE, _PersistentQuantitativeReviewClient,
    _RetryableReviewClient,
    _append_extraction_history_version,
)
from test_reviewed_analysis_campaign import PLAN, BATCH, request_body, batch_body, complete, resume, parent_config

KEY = "PPS-SYN-FAILED-ATTACHMENTS"


@pytest.fixture
def failed_case(client, monkeypatch):
    downloads = []
    def transport(request):
        index = int(request.url.params["fileSeq"])
        downloads.append(index)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr("Contents/section0.xml", "<s>" + "".join(f"<p>{line}</p>" for line in _QUANTITATIVE_RETRY_SOURCE.splitlines()) + f"<p>SYN {index}</p></s>")
        return httpx.Response(200, headers={"Content-Type": "application/zip"}, content=buffer.getvalue())
    mock_transport = httpx.MockTransport(transport)
    with client.app.state.session_factory() as session:
        notice = Notice(notice_key=KEY, bid_notice_no="SYN-FAILED", revision_no="00", title="SYN failed scope",
                        agency="SYN agency", status="OPEN", deadline=datetime.now(timezone.utc) + timedelta(days=20))
        session.add(notice)
        session.flush()
        raw = {"bidNtceNo": "SYN-FAILED", "bidNtceOrd": "000"}
        for index in range(1, 5):
            raw[f"ntceSpecFileNm{index}"] = f"SYN-{index}.hwpx"
            raw[f"ntceSpecDocUrl{index}"] = G2B_DOWNLOAD.replace("fileSeq=1", f"fileSeq={index}")
        enrichment.persist_pps_metadata_version(session, notice, raw_item=raw, search_keywords=[], dry_run=False)
        session.commit()
        notice_id = notice.id
        manifest = next(version.source_payload["attachment_manifest"] for version in notice.versions if version.source_payload.get("kind") == enrichment.PPS_METADATA_KIND)
    fourth = manifest[3]["attachment_id"]
    class InitialClient(_PersistentQuantitativeReviewClient):
        def extract(self, **kwargs):
            if fourth in kwargs["allowed_attachment_ids"]:
                return ExtractionOutcome(status="REVIEW", review_code="R07", error_code="XLS_PARSE_FAILED", message="SYN parser failed")
            return super().extract(**kwargs)
    def run(scope=None, *, dry_run=False):
        with client.app.state.session_factory() as session:
            return enrichment.enrich_notice_from_pps(session, notice_id=notice_id, openai_api_key="SYN-test", openai_model="SYN-model",
                transport=mock_transport, openai_client_factory=_RetryableReviewClient if scope is not None else InitialClient,
                dry_run=dry_run, retry_reviewed_version_ids=frozenset(scope["version_ids"]) if scope is not None else frozenset(),
                **({"failed_attachment_retry": scope} if scope is not None else {}))
    run()
    downloads.clear()
    _RetryableReviewClient.calls = 0
    def snapshot():
        with client.app.state.session_factory() as session:
            notice = session.get(Notice, notice_id)
            return enrichment.failed_attachment_retry_snapshot(list(notice.versions), error_codes=["XLS_PARSE_FAILED"], max_attachments=3,
                                                                notice_key=notice.notice_key, revision_no=notice.revision_no)
    def worker(request, *, notice_id, payload, deadline_monotonic, retry_reviewed_version_ids=frozenset(), failed_attachment_retry=None):
        assert failed_attachment_retry is not None
        assert retry_reviewed_version_ids == frozenset(failed_attachment_retry["version_ids"])
        return run(failed_attachment_retry, dry_run=payload.dry_run)
    monkeypatch.setattr(api, "_enrich_one_notice", worker)
    return client, notice_id, downloads, run, snapshot


def narrow_body(**changes):
    return request_body(notice_keys=[KEY], retry_scope="FAILED_ATTACHMENTS", retry_error_codes=["XLS_PARSE_FAILED"], retry_max_attachments=3, **changes)


def test_only_failed_attachment_runs_and_accepted_quantitative_review_is_preserved(failed_case):
    client, notice_id, downloads, run, snapshot = failed_case
    scope = snapshot()
    assert len(scope["targets"]) == 1 and enrichment.valid_failed_attachment_retry_scope(scope)
    with client.app.state.session_factory() as session:
        originals = {row.id: deepcopy(row.source_payload) for row in session.get(Notice, notice_id).versions if row.source_payload.get("status") == "ACCEPTED"}
        assert len(originals) == 3
        assert all(row["quantitative_validation_record"]["status"] in {"INCOMPLETE", "REVIEW"} for row in originals.values())
    result = run(scope)
    assert downloads == [4] and result.openai_calls == 1 and _RetryableReviewClient.calls == 1
    with client.app.state.session_factory() as session:
        for row_id, original in originals.items():
            assert session.get(NoticeVersion, row_id).source_payload == original
        latest = max(session.get(Notice, notice_id).versions, key=lambda row: row.version_no)
        assert latest.id not in scope["version_ids"] and latest.source_payload["status"] == "REVIEW"
        latest.created_at = datetime.now(timezone.utc) - timedelta(days=3)
        session.commit()
    continued = run(scope)
    assert continued.openai_calls == 0 and downloads == [4] and _RetryableReviewClient.calls == 1


@pytest.mark.parametrize("mutation", ["manifest", "deleted_target", "scope_hash", "empty", "cancelled", "revision", "attempt_mutated"])
def test_narrow_retry_stale_empty_and_cancelled_stop_before_download(failed_case, mutation):
    client, notice_id, downloads, run, snapshot = failed_case
    scope = snapshot()
    if mutation == "scope_hash":
        scope["targets"][0]["error_code"] = "INCOMPLETE_RESPONSE"
    elif mutation == "empty":
        scope["targets"] = []
    else:
        with client.app.state.session_factory() as session:
            notice = session.get(Notice, notice_id)
            if mutation == "cancelled": notice.status = "CANCELLED"
            elif mutation == "revision": notice.revision_no = "01"
            elif mutation == "attempt_mutated":
                attempt = session.get(NoticeVersion, scope["targets"][0]["version_id"])
                data = deepcopy(attempt.source_payload)
                data["status"] = "ACCEPTED"
                attempt.source_payload = data
            elif mutation == "deleted_target": session.delete(session.get(NoticeVersion, scope["targets"][0]["version_id"]))
            else:
                metadata = next(row for row in notice.versions if row.source_payload.get("kind") == enrichment.PPS_METADATA_KIND)
                data = deepcopy(metadata.source_payload)
                data["attachment_manifest"][0]["file_name"] = "SYN-changed.hwpx"
                metadata.source_payload = data
            session.commit()
    result = run(scope)
    assert result.status in {"REVIEW", "SKIPPED"} and result.openai_calls == 0 and downloads == []


def test_expired_unselected_failure_and_missing_sibling_never_expand_scope(failed_case):
    client, notice_id, downloads, run, snapshot = failed_case
    with client.app.state.session_factory() as session:
        notice = session.get(Notice, notice_id)
        accepted = [row for row in notice.versions if row.source_payload.get("status") == "ACCEPTED"]
        payload = deepcopy(accepted[0].source_payload)
        payload.update(status="REVIEW", error_code="NETWORK_ERROR", result=None, quantitative_validation_record=None)
        accepted[0].source_payload = payload
        accepted[0].created_at = datetime.now(timezone.utc) - timedelta(days=3)
        session.delete(accepted[1])
        session.commit()
    result = run(snapshot())
    assert downloads == [4] and result.openai_calls == 1
    assert "ATTACHMENT_CONTINUATION_REQUIRED" not in result.warnings


def test_campaign_replay_immutable_scope_and_worker_inheritance(failed_case):
    client, notice_id, downloads, run, snapshot = failed_case
    planned = client.post(PLAN, json=narrow_body())
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    assert plan["retry_scope"] == "FAILED_ATTACHMENTS" and plan["retry_target_count"] == 1
    frozen = parent_config(client, plan)["review_snapshots"][KEY]
    changed = narrow_body()
    changed["retry_error_codes"] = ["XLS_PARSE_FAILED", "INCOMPLETE_RESPONSE"]
    assert client.post(PLAN, json=changed).status_code == 409
    changed = narrow_body()
    changed["retry_max_attachments"] = 1
    assert client.post(PLAN, json=changed).status_code == 409
    first = client.post(BATCH, json=batch_body(plan))
    assert first.status_code == 200, first.text
    assert downloads == [4]
    assert client.post(BATCH, json=batch_body(plan)).json() == first.json()
    assert downloads == [4] and parent_config(client, plan)["review_snapshots"][KEY] == frozen
    complete(client, plan)
    again = resume(client, plan)
    assert parent_config(client, again)["review_snapshots"][KEY] == frozen


@pytest.mark.parametrize("change", [{"retry_error_codes": ["XLS_ANYTHING"]}, {"retry_error_codes": []},
    {"retry_max_attachments": 4}, {"notice_keys": [KEY, "PPS-SYN-OTHER"]}, {"retry_reviewed": False}])
def test_scope_request_rejects_unbounded_or_unknown_filters(failed_case, change):
    client, _, downloads, _, _ = failed_case
    body = narrow_body()
    body.update(change)
    assert client.post(PLAN, json=body).status_code == 422
    assert downloads == []


def test_empty_matching_error_scope_is_not_an_all_attachment_retry(failed_case):
    client, _, downloads, _, _ = failed_case
    body = narrow_body()
    body["retry_error_codes"] = ["NETWORK_ERROR"]
    response = client.post(PLAN, json=body)
    assert response.status_code == 409 and "FAILED_RETRY_TARGETS_EMPTY" in response.text
    assert downloads == []


@pytest.mark.parametrize("count", [3, 4])
def test_target_budget_selects_all_within_three_or_rejects_without_truncation(failed_case, count):
    client, notice_id, downloads, run, snapshot = failed_case
    with client.app.state.session_factory() as session:
        accepted = [row for row in session.get(Notice, notice_id).versions if row.source_payload.get("status") == "ACCEPTED"]
        for row in accepted[:count - 1]:
            payload = deepcopy(row.source_payload)
            payload.update(status="REVIEW", error_code="XLS_PARSE_FAILED", result=None, quantitative_validation_record=None)
            row.source_payload = payload
        session.commit()
    if count == 4:
        response = client.post(PLAN, json=narrow_body())
        assert response.status_code == 409 and "FAILED_RETRY_TARGET_LIMIT" in response.text
        assert downloads == []
    else:
        scope = snapshot()
        assert len(scope["targets"]) == 3
        result = run(scope)
        assert len(downloads) == 3 and result.openai_calls == 3
        assert result.openai_calls <= 2 * len(scope["targets"])


def test_frozen_scope_survives_failed_child_reopen_without_repeating_a_new_failure(failed_case, monkeypatch):
    client, _, downloads, _, _ = failed_case
    planned = client.post(PLAN, json=narrow_body()).json()
    original = api._store_batch_response
    executions = []
    def crash_after_persist(*args, **kwargs):
        executions.append(True)
        if len(executions) == 1:
            raise RuntimeError("SYN lost response after durable attachment result")
        return original(*args, **kwargs)
    monkeypatch.setattr(api, "_store_batch_response", crash_after_persist)
    with pytest.raises(RuntimeError, match="SYN lost response"):
        client.post(BATCH, json=batch_body(planned))
    frozen = parent_config(client, planned)["review_snapshots"][KEY]
    resumed = client.post(BATCH, json=batch_body(planned))
    assert resumed.status_code == 200, resumed.text
    assert downloads == [4]
    assert parent_config(client, planned)["review_snapshots"][KEY] == frozen
    assert parent_config(client, planned)["review_execution_counts"][KEY] == 2


def test_old_accepted_history_cannot_prevent_consuming_the_selected_failure(failed_case):
    client, notice_id, downloads, run, snapshot = failed_case
    with client.app.state.session_factory() as session:
        accepted = [row for row in session.get(Notice, notice_id).versions if row.source_payload.get("status") == "ACCEPTED"]
        old = accepted[0]
        original = deepcopy(old.source_payload)
        _append_extraction_history_version(session, old, status="REVIEW", error_code="XLS_PARSE_FAILED")
        session.commit()
        old_id = old.id
    scope = snapshot()
    assert len(scope["targets"]) == 2
    run(scope)
    once = list(downloads)
    assert sorted(once) == [1, 4]
    run(scope)
    assert downloads == once
    with client.app.state.session_factory() as session:
        assert session.get(NoticeVersion, old_id).source_payload == original


def test_authoritative_cancellation_stops_selected_failed_dispatch(failed_case, monkeypatch):
    _, _, downloads, run, snapshot = failed_case
    scope = snapshot()
    monkeypatch.setattr(enrichment, "authoritative_pps_cancelled_notice_keys", lambda session, notices: {KEY})
    result = run(scope)
    assert downloads == [] and result.openai_calls == 0
    assert "FAILED_RETRY_NOTICE_NOT_ACTIVE" in result.warnings
