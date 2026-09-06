from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

import pai_loop.analysis_api as api
from pai_loop.models import IngestionJob, Notice, NoticeVersion
from pai_loop.pps_enrichment import PPS_METADATA_SCHEMA

PLAN = "/api/v1/operations/analysis-backfills/plan"
BATCH = "/api/v1/notices/analysis/batch"
KEY = "PPS-SYN-REVIEW-CAMPAIGN"


def seed(client, key=KEY, *, metadata=True):
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        notice = Notice(notice_key=key, bid_notice_no=key, title="Synthetic campaign notice", agency="Synthetic agency",
                        status="OPEN", deadline=now + timedelta(days=30))
        session.add(notice)
        session.flush()
        if metadata:
            session.add(NoticeVersion(notice_id=notice.id, version_no=1, file_sha256="a" * 64,
                source_payload={"kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA,
                                "attachment_manifest": []}))
        session.commit()


def request_body(**changes):
    body = dict(queue_name="BACKFILL", notice_keys=[KEY], retry_reviewed=True,
                review_campaign_key="SYN-CAMPAIGN-1", request_token="SYN-LEASE-0",
                execution_limit=1, max_total=10, max_continuations=30)
    body.update(changes)
    return body


def plan(client, **changes):
    response = client.post(PLAN, json=request_body(**changes))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_policy"] == "FROZEN_REVIEW_RETRY_V1"
    assert body["review_campaign_key"] == changes.get("review_campaign_key", "SYN-CAMPAIGN-1")
    return body


def batch_body(planned, **changes):
    body = dict(operation_id=planned["job_id"], segment_id=planned["segment_id"],
                chunk_index=planned["chunk_indices"][0], notice_keys=planned["chunks"][0],
                max_notices=1, max_attachments_per_notice=10, enrich_missing=True,
                dry_run=planned["dry_run"])
    body.update(changes)
    return body


def complete(client, planned):
    response = client.post(f"/api/v1/operations/analysis-backfills/{planned['job_id']}/complete",
                           json={"segment_id": planned["segment_id"]})
    assert response.status_code == 200, response.text
    return response.json()


def resume(client, planned, ordinal=1, **changes):
    body = dict(queue_name="ANY", resume_only=True, resume_job_id=planned["job_id"],
                request_token=f"SYN-RESUME-{ordinal}", execution_limit=1,
                max_continuations=30, dry_run=planned["dry_run"])
    body.update(changes)
    response = client.post(PLAN, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def fake_execution(monkeypatch, *, continuation=False, fail=False):
    calls = []
    def execute(payload, request, *, job_id, retry_reviewed_version_ids=frozenset()):
        calls.append(retry_reviewed_version_ids)
        if fail:
            raise RuntimeError("synthetic interrupted response")
        response = api._review_stopped_response(payload, job_id, "SYNTHETIC_REVIEW")
        if continuation:
            response.enrichment.warnings.append("ATTACHMENT_CONTINUATION_REQUIRED")
        api._store_batch_response(request, job_id=job_id, response=response)
        return response
    monkeypatch.setattr(api, "_execute_notice_analysis_batch", execute)
    return calls


def parent_config(client, planned):
    with client.app.state.session_factory() as session:
        return session.get(IngestionJob, planned["job_id"]).request_json


def test_frozen_ids_and_empty_snapshot_are_not_recomputed_on_continuation(client, monkeypatch):
    seed(client)
    snapshots = []
    def capture(versions):
        snapshots.append(len(versions))
        return frozenset() if len(snapshots) == 1 else frozenset({"SYN-NEW-REVIEW"})
    monkeypatch.setattr(api, "current_retryable_review_version_ids", capture)
    calls = fake_execution(monkeypatch, continuation=True)
    current = plan(client)
    for index in range(2):
        result = client.post(BATCH, json=batch_body(current))
        assert result.status_code == 200
        complete(client, current)
        current = resume(client, current, index)
    assert calls == [frozenset(), frozenset()]
    assert len(snapshots) == 1
    assert parent_config(client, current)["review_snapshots"][KEY]["version_ids"] == []


def test_exact_http_replay_does_not_consume_an_execution(client, monkeypatch):
    seed(client)
    monkeypatch.setattr(api, "current_retryable_review_version_ids", lambda versions: frozenset({"SYN-OLD"}))
    calls = fake_execution(monkeypatch)
    current = plan(client)
    first = client.post(BATCH, json=batch_body(current))
    replay = client.post(BATCH, json=batch_body(current))
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert calls == [frozenset({"SYN-OLD"})]
    assert parent_config(client, current)["review_execution_counts"] == {KEY: 1}


def test_tenth_continuation_is_terminal_failure_and_denominator_is_preserved(client, monkeypatch):
    seed(client)
    calls = fake_execution(monkeypatch, continuation=True)
    current = plan(client)
    for ordinal in range(1, 11):
        result = client.post(BATCH, json=batch_body(current))
        assert result.status_code == 200, result.text
        after = complete(client, current)
        assert after["planned"] == after["attempted"] + after["remaining"] == 1
        if ordinal < 10:
            assert after["remaining"] == 1
            current = resume(client, current, ordinal)
        else:
            assert after["remaining"] == 0
            assert after["failed"] == 1
            assert "REVIEW_RETRY_CONTINUATION_LIMIT" in result.json()["warnings"]
    assert len(calls) == 10
    assert resume(client, current, 11)["offered"] == 0


def test_failed_exact_reopen_also_consumes_budget_and_stops_at_ten(client, monkeypatch):
    seed(client)
    calls = fake_execution(monkeypatch, fail=True)
    current = plan(client)
    for _ in range(10):
        with pytest.raises(RuntimeError, match="synthetic interrupted"):
            client.post(BATCH, json=batch_body(current))
    stopped = client.post(BATCH, json=batch_body(current))
    assert stopped.status_code == 200
    assert stopped.json()["warnings"] == ["REVIEW_RETRY_CONTINUATION_LIMIT"]
    assert len(calls) == 10
    assert parent_config(client, current)["review_execution_counts"] == {KEY: 10}
    assert complete(client, current)["remaining"] == 0


def test_terminal_campaign_replay_is_permanent_even_after_source_changed(client, monkeypatch):
    seed(client)
    calls = fake_execution(monkeypatch)
    current = plan(client)
    client.post(BATCH, json=batch_body(current))
    terminal = complete(client, current)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == KEY))
        notice.status = "CLOSED"
        session.commit()
    replay = plan(client)
    assert replay["job_id"] == current["job_id"]
    assert replay["planned"] == terminal["planned"] == 1
    assert replay["offered"] == 0
    assert len(calls) == 1
    with client.app.state.session_factory() as session:
        assert len(session.scalars(select(IngestionJob).where(IngestionJob.source == "ANALYSIS_BACKFILL")).all()) == 1


@pytest.mark.parametrize("changes", [
    {"notice_keys": ["PPS-OTHER"]}, {"dry_run": True}, {"max_total": 20},
    {"max_continuations": 31}, {"execution_limit": 2},
])
def test_campaign_same_key_changed_scope_or_flags_conflicts(client, changes):
    seed(client)
    plan(client)
    response = client.post(PLAN, json=request_body(**changes))
    assert response.status_code == 409


@pytest.mark.parametrize("changes", [
    {"enrich_missing": False}, {"dry_run": True},
])
def test_child_mode_changes_are_rejected_even_before_cached_replay(client, monkeypatch, changes):
    seed(client)
    calls = fake_execution(monkeypatch)
    current = plan(client)
    client.post(BATCH, json=batch_body(current))
    response = client.post(BATCH, json=batch_body(current, **changes))
    assert response.status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize("body", [
    {"retry_reviewed": True}, {"review_campaign_key": "SYN-ORPHAN"},
    {"retry_reviewed": True, "review_campaign_key": "SYN-DAILY", "queue_name": "DAILY", "notice_keys": [KEY]},
    {"retry_reviewed": True, "review_campaign_key": "SYN-ANY", "queue_name": "ANY", "resume_only": True},
    {"notice_keys": [KEY], "retry_reviewd": True},
    {"notice_keys": [KEY], "retry_reviewed_version_ids": ["SYN-INJECTED"]},
])
def test_invalid_or_unknown_optins_are_rejected(client, body):
    assert client.post(PLAN, json=body).status_code == 422


def test_unknown_batch_retry_injection_is_rejected(client):
    response = client.post(BATCH, json={"notice_keys": [KEY], "retry_reviewed": True})
    assert response.status_code == 422


def test_existing_normal_parent_cannot_be_upgraded(client):
    seed(client)
    normal = client.post(PLAN, json={"notice_keys": [KEY]}).json()
    upgraded = client.post(PLAN, json=request_body(resume_job_id=normal["job_id"]))
    assert upgraded.status_code == 409
    assert not parent_config(client, normal).get("review_policy")


def test_w11_any_resume_inherits_but_explicit_false_or_append_conflicts(client, monkeypatch):
    seed(client)
    fake_execution(monkeypatch, continuation=True)
    current = plan(client)
    client.post(BATCH, json=batch_body(current))
    complete(client, current)
    inherited = resume(client, current)
    assert inherited["job_id"] == current["job_id"]
    assert inherited["review_policy"] == "FROZEN_REVIEW_RETRY_V1"
    assert client.post(PLAN, json={"queue_name": "ANY", "resume_only": True,
        "resume_job_id": current["job_id"], "retry_reviewed": False}).status_code == 409
    assert client.post(PLAN, json={"resume_job_id": current["job_id"], "notice_keys": [KEY]}).status_code == 409


@pytest.mark.parametrize("change", ["metadata", "metadata-in-place", "deadline", "contract"])
def test_changed_source_or_contract_is_terminal_without_provider(client, monkeypatch, change):
    seed(client)
    calls = fake_execution(monkeypatch)
    current = plan(client)
    if change == "contract":
        monkeypatch.setattr(api, "CURRENT_EXTRACTION_CONTRACT", ("NEW", "NEW", "NEW", "NEW"))
    else:
        with client.app.state.session_factory() as session:
            notice = session.scalar(select(Notice).where(Notice.notice_key == KEY))
            if change == "deadline":
                notice.deadline += timedelta(days=1)
            elif change == "metadata":
                session.add(NoticeVersion(notice_id=notice.id, version_no=2, file_sha256="b"*64,
                    source_payload={"kind": "PPS_NOTICE_METADATA", "schema_version": PPS_METADATA_SCHEMA,
                                    "attachment_manifest": []}))
            else:
                version = notice.versions[0]
                version.source_payload = {**version.source_payload, "synthetic_change": True}
            session.commit()
    response = client.post(BATCH, json=batch_body(current))
    assert response.status_code == 200
    expected = "REVIEW_CAMPAIGN_CONTRACT_CHANGED" if change == "contract" else "REVIEW_CAMPAIGN_SOURCE_CHANGED"
    assert response.json()["warnings"] == [expected]
    assert response.json()["openai_calls"] == 0
    assert calls == []
    assert complete(client, current)["failed"] == 1


def test_analysis_output_versions_do_not_change_frozen_source(client, monkeypatch):
    seed(client)
    calls = fake_execution(monkeypatch)
    current = plan(client)
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == KEY))
        notice.updated_at = datetime.now(timezone.utc)
        session.add(NoticeVersion(notice_id=notice.id, version_no=2, file_sha256="b"*64,
            source_payload={"kind": "OPENAI_REQUIREMENT_EXTRACTION", "status": "REVIEW"}))
        session.commit()
    assert client.post(BATCH, json=batch_body(current)).status_code == 200
    assert len(calls) == 1


def test_unavailable_reserved_and_missing_source_scope_never_silently_shrinks(client):
    seed(client)
    assert client.post(PLAN, json=request_body(notice_keys=[KEY, "PPS-NOT-FOUND"])).status_code == 409
    seed(client, "MANUAL-SYN")
    assert client.post(PLAN, json=request_body(notice_keys=["MANUAL-SYN"])).status_code == 409
    seed(client, "PPS-NO-METADATA", metadata=False)
    assert client.post(PLAN, json=request_body(notice_keys=["PPS-NO-METADATA"])).status_code == 409
    normal = client.post(PLAN, json={"notice_keys": [KEY]}).json()
    assert client.post(PLAN, json=request_body()).status_code == 409
    assert not parent_config(client, normal).get("review_policy")


def test_concurrent_same_campaign_creates_only_one_parent_and_one_lease(client):
    seed(client)
    barrier = Barrier(2)
    def send():
        barrier.wait()
        response = client.post(PLAN, json=request_body())
        assert response.status_code == 200, response.text
        return response.json()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: send(), range(2)))
    assert results[0]["job_id"] == results[1]["job_id"]
    assert results[0]["segment_id"] == results[1]["segment_id"]
    assert results[0]["chunk_indices"] == results[1]["chunk_indices"]


def test_orphan_recovery_does_not_refreeze_review_ids(client, monkeypatch):
    seed(client)
    captured = []
    def snapshot(versions):
        captured.append(1)
        return frozenset({"SYN-OLD"})
    monkeypatch.setattr(api, "current_retryable_review_version_ids", snapshot)
    current = plan(client)
    now = datetime.now(timezone.utc)
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, current["job_id"])
        config = dict(parent.request_json)
        config["lease_started_at"] = (now - timedelta(hours=7)).isoformat()
        parent.request_json = config
        session.commit()
    continued = resume(client, current)
    assert continued["segment_id"] != current["segment_id"]
    assert parent_config(client, continued)["review_snapshots"][KEY]["version_ids"] == ["SYN-OLD"]
    assert len(captured) == 1


@pytest.mark.parametrize("ids,expected_calls", [(frozenset(), 0), (frozenset({"SYN-OLD"}), 1)])
def test_real_executor_retries_accepted_only_with_frozen_review_ids(client, monkeypatch, ids, expected_calls):
    from pai_loop.pps_enrichment import PpsEnrichmentResult
    seed(client)
    monkeypatch.setattr(api, "current_retryable_review_version_ids", lambda versions: ids)
    monkeypatch.setattr(api, "_has_accepted_pps_extraction", lambda *args: True)
    calls = []
    def enrich(request, *, retry_reviewed_version_ids=frozenset(), **kwargs):
        calls.append(retry_reviewed_version_ids)
        return PpsEnrichmentResult(status="REVIEW", warnings=["SYNTHETIC_REVIEW"])
    def pipeline(*args, **kwargs):
        raise RuntimeError("synthetic no materialized extraction")
    monkeypatch.setattr(api, "_enrich_one_notice", enrich)
    monkeypatch.setattr(api, "run_analysis_pipeline", pipeline)
    current = plan(client)
    response = client.post(BATCH, json=batch_body(current))
    assert response.status_code == 200, response.text
    assert len(calls) == expected_calls
    if calls:
        assert calls == [ids]


@pytest.mark.parametrize("original", ["SKIPPED", "COMPLETED"])
def test_cap_converts_partial_success_counters_into_terminal_failure(client, monkeypatch, original):
    seed(client)
    current = plan(client)
    with client.app.state.session_factory() as session:
        parent = session.get(IngestionJob, current["job_id"])
        parent.request_json = {**parent.request_json, "review_execution_counts": {KEY: 9}}
        session.commit()
    def execute(payload, request, *, job_id, **kwargs):
        result = api._review_stopped_response(payload, job_id, "ATTACHMENT_CONTINUATION_REQUIRED")
        result.results[0].status = original
        result.failed = 0
        result.completed = int(original == "COMPLETED")
        result.skipped = int(original == "SKIPPED")
        api._store_batch_response(request, job_id=job_id, response=result)
        return result
    monkeypatch.setattr(api, "_execute_notice_analysis_batch", execute)
    result = client.post(BATCH, json=batch_body(current)).json()
    assert (result["completed"], result["skipped"], result["failed"]) == (0, 0, 1)
    assert result["results"][0]["status"] == "FAILED"
    assert "REVIEW_RETRY_CONTINUATION_LIMIT" in result["results"][0]["warnings"]
    terminal = complete(client, current)
    assert terminal["attempted"] == terminal["failed"] == 1
    assert terminal["remaining"] == 0


def test_source_change_during_provider_retains_calls_and_stops_pipeline_and_requeue(client, monkeypatch):
    from pai_loop.pps_enrichment import PpsEnrichmentResult
    seed(client)
    monkeypatch.setattr(api, "current_retryable_review_version_ids", lambda versions: frozenset({"SYN-OLD"}))
    monkeypatch.setattr(api, "_has_accepted_pps_extraction", lambda *args: True)
    calls = []
    def enrich(request, **kwargs):
        calls.append("provider")
        with request.app.state.session_factory() as session:
            notice = session.scalar(select(Notice).where(Notice.notice_key == KEY))
            notice.versions[0].source_payload = {**notice.versions[0].source_payload, "changed": True}
            session.commit()
        return PpsEnrichmentResult(status="REVIEW", openai_calls=1,
                                   warnings=["ATTACHMENT_CONTINUATION_REQUIRED"])
    def forbidden_pipeline(*args, **kwargs):
        calls.append("pipeline")
        raise AssertionError("pipeline must not run")
    monkeypatch.setattr(api, "_enrich_one_notice", enrich)
    monkeypatch.setattr(api, "run_analysis_pipeline", forbidden_pipeline)
    current = plan(client)
    response = client.post(BATCH, json=batch_body(current))
    assert response.status_code == 200, response.text
    result = response.json()
    assert "REVIEW_CAMPAIGN_SOURCE_CHANGED" in result["warnings"]
    assert result["openai_calls"] == 1
    assert calls == ["provider"]
    assert complete(client, current)["remaining"] == 0


def test_w11_unqualified_any_poll_resumes_campaign_without_new_optin(client, monkeypatch):
    seed(client)
    fake_execution(monkeypatch, continuation=True)
    current = plan(client)
    client.post(BATCH, json=batch_body(current))
    complete(client, current)
    polled = client.post(PLAN, json={"queue_name": "ANY", "resume_only": True,
                                   "request_token": "SYN-W11-POLL"})
    assert polled.status_code == 200, polled.text
    assert polled.json()["job_id"] == current["job_id"]
    assert polled.json()["review_policy"] == "FROZEN_REVIEW_RETRY_V1"
    assert polled.json()["offered"] == 1


def test_later_manual_only_marker_is_counted_as_terminal_failure(client, monkeypatch):
    seed(client)
    calls = fake_execution(monkeypatch)
    current = plan(client)
    monkeypatch.setattr(api, "manual_only_notice_keys", lambda session, keys: {KEY})
    stopped = client.post(BATCH, json=batch_body(current))
    assert stopped.status_code == 200
    assert stopped.json()["warnings"] == ["REVIEW_CAMPAIGN_MANUAL_ONLY"]
    assert calls == []
    assert complete(client, current)["failed"] == 1



def test_w11_poll_does_not_rewrite_campaign_policy_bounds(client, monkeypatch):
    seed(client)
    fake_execution(monkeypatch, continuation=True)
    current = plan(client, max_continuations=128, execution_limit=30)
    client.post(BATCH, json=batch_body(current))
    complete(client, current)
    response = client.post(PLAN, json={"queue_name": "ANY", "resume_only": True,
        "request_token": "SYN-LOWER-OFFER", "execution_limit": 1, "max_continuations": 768})
    assert response.status_code == 200
    assert response.json()["max_continuations"] == 128
    stored = parent_config(client, current)
    assert stored["max_continuations"] == 128
    assert stored["execution_limit"] == 30



def test_ordinary_dead_letter_explicit_resume_preserves_409(client):
    with client.app.state.session_factory() as session:
        parent = IngestionJob(source="ANALYSIS_BACKFILL", mode="LIVE", status="DEAD_LETTER",
            window_json={}, request_json={"queue_name": "BACKFILL"}, notice_keys=[],
            completed_at=datetime.now(timezone.utc))
        session.add(parent)
        session.commit()
        parent_id = parent.id
    response = client.post(PLAN, json={"queue_name": "ANY", "resume_only": True,
                                      "resume_job_id": parent_id})
    assert response.status_code == 409


def test_campaign_dead_letter_replays_same_terminal_parent_without_new_work(client, monkeypatch):
    seed(client)
    calls = fake_execution(monkeypatch, continuation=True)
    current = plan(client, max_continuations=1)
    client.post(BATCH, json=batch_body(current))
    terminal = complete(client, current)
    assert terminal["status"] == "DEAD_LETTER"
    assert terminal["remaining"] == 1
    same_request = plan(client, max_continuations=1)
    explicit_resume = resume(client, current)
    for result in (same_request, explicit_resume):
        assert result["job_id"] == current["job_id"]
        assert result["status"] == "DEAD_LETTER"
        assert result["offered"] == 0
        assert result["remaining"] == 1
    assert len(calls) == 1
