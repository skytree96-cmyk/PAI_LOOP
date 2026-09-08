from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from pai_loop.integrations.awards import PpsAwardClient
from pai_loop.models import AwardHistoryItem, IngestionJob, Notice, UserDecision


CANARY = "SYN-private-provider-probe-content"
PROBE = {"keyword": "SYN", "years": 3, "page_size": 2, "max_pages_per_window": 3,
         "dry_run": True, "include_opening_results": False, "diagnostic_probe": True}
REFRESH_KEYS = {"job_id", "notice_key", "status", "keyword", "window", "api_calls",
                "fetched", "created", "updated", "duplicates", "records", "dry_run",
                "warnings", "window_error_counts"}


def envelope(body):
    return {"response": {"header": {"resultCode": "00", "resultMsg": CANARY}, "body": body}}


def provider_response(kind, request):
    if kind == "timeout":
        raise httpx.ReadTimeout(CANARY, request=request)
    if kind == "http_error":
        return httpx.Response(503, text=CANARY)
    if kind == "redirect":
        return httpx.Response(302, headers={"Location": "https://example.test/" + CANARY})
    if kind == "invalid_json":
        return httpx.Response(200, text=CANARY)
    if kind == "missing_response":
        return httpx.Response(200, json={CANARY: CANARY})
    if kind == "provider_error":
        return httpx.Response(200, json={"response": {"header": {"resultCode": "30", "resultMsg": CANARY}}})
    if kind == "invalid_page":
        body = {"totalCount": 1, CANARY: CANARY}
    elif kind in {"missing_items_empty", "missing_items_empty_string"}:
        body = {"totalCount": "0" if kind.endswith("_string") else 0, CANARY: CANARY}
    elif kind == "empty":
        body = {"totalCount": 0, "items": []}
    else:
        # One matching existing award and one new award; total requires another
        # page under the ordinary path, then hundreds of additional date windows.
        body = {"totalCount": 4, "items": [
            {"bidNtceNo": key, "bidNtceOrd": "000", "bidClsfcNo": "0", "rbidNo": "000",
             "bidNtceNm": "SYN " + CANARY, "bidwinnrNm": CANARY, "sucsfbidAmt": "95000000"}
            for key in ("SYN-EXISTING", "SYN-NEW")
        ]}
    return httpx.Response(200, json=envelope(body))


def read(client, **kwargs):
    return list(client.iter_awards(start=date(2024, 1, 1), end=date(2026, 9, 8),
        keyword="SYN", rows=2, max_pages_per_window=3, continue_on_window_error=True, **kwargs))


def business_snapshot(session):
    return {
        model.__tablename__: [
            {column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in session.scalars(select(model).order_by(model.id))
        ]
        for model in (AwardHistoryItem, UserDecision)
    }


@pytest.fixture()
def notice(client):
    key = "PPS-SYN-ONE-CALL-PROBE"
    assert client.post("/api/v1/notices", json={"notice_key": key, "bid_notice_no": key,
        "title": "SYN 교육", "agency": "SYN 기관", "deadline": "2099-01-01T00:00:00Z"}).status_code == 201
    client.app.state.settings = replace(client.app.state.settings, pps_api_key="SYN-provider")
    with client.app.state.session_factory() as session:
        row = session.scalar(select(Notice).where(Notice.notice_key == key))
        session.add(AwardHistoryItem(target_notice_id=row.id,
            external_identity="SYN-EXISTING|000|0|000", bid_notice_no="SYN-EXISTING",
            title="SYN stored title", winner_name="SYN stored winner", award_amount=90000000,
            similarity_score=1, opening_results=[{"company_name": "SYN stored company"}],
            opening_results_status="COLLECTED"))
        session.add(UserDecision(notice_id=row.id, choice="HOLD", actor_label="SYN person",
            rationale="SYN human decision", department_id="SYN department", department_revision=1,
            analysis_state_snapshot="ANALYZED", analysis_snapshot={"synthetic": True}))
        session.commit()
    return key


@pytest.mark.parametrize("kind,error", [
    ("valid", None), ("empty", None), ("invalid_page", "AWARD_PAGE_INVALID"),
    ("missing_items_empty", None), ("missing_items_empty_string", None),
    ("missing_response", "MISSING_RESPONSE"), ("timeout", "NETWORK_ERROR"),
    ("http_error", "HTTP_ERROR"), ("redirect", "INVALID_JSON"),
    ("invalid_json", "INVALID_JSON"), ("provider_error", "PROVIDER_RESULT_ERROR"),
])
def test_api_probe_one_physical_call_no_business_writes_and_safe_audit(client, notice, monkeypatch, kind, error):
    physical_calls = []
    instances = []
    class Provider(PpsAwardClient):
        def __init__(self, **kwargs):
            def handler(request):
                physical_calls.append(1)
                return provider_response(kind, request)
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))
            instances.append(self)
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", Provider)
    with client.app.state.session_factory() as session:
        before = business_snapshot(session)
        jobs_before = len(list(session.scalars(select(IngestionJob))))

    response = client.post(f"/api/v1/notices/{notice}/award-history/refresh", json=PROBE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == REFRESH_KEYS
    assert body["status"] == "PARTIAL" and body["dry_run"] is True
    assert len(physical_calls) == body["api_calls"] == 1
    assert len(instances) == 1 and instances[0].fallback_window_count == 0
    assert body["fetched"] == (2 if kind == "valid" else 0)
    assert all(body[field] == 0 for field in ("created", "updated", "duplicates", "records"))
    assert any(warning.startswith("AWARD_DIAGNOSTIC_PROBE_SINGLE_CALL:") for warning in body["warnings"])
    assert not any("7일" in warning for warning in body["warnings"])
    errors = body["window_error_counts"]
    if error:
        assert len(errors) == 1 and errors[0]["error_type"] == error
        assert errors[0]["phase"] == "PRIMARY" and errors[0]["count"] == 1
    else:
        assert errors == []  # Stopping at the budget is not a provider failure.
    diagnostic = client.get(f"/api/v1/ingestion/jobs/{body['job_id']}/award-diagnostics")
    assert diagnostic.status_code == 200
    counts = diagnostic.json()["diagnostics"]["counts"]
    if kind in {"invalid_page", "missing_response", "provider_error"}:
        assert len(counts) == 1 and counts[0]["error_type"] == error
        if kind == "invalid_page":
            assert counts[0]["shape"]["items_type"] == "MISSING"
    else:
        assert counts == []
    with client.app.state.session_factory() as session:
        assert business_snapshot(session) == before
        jobs = list(session.scalars(select(IngestionJob)))
        assert len(jobs) == jobs_before + 1
        job = session.get(IngestionJob, body["job_id"])
        assert job.mode == "DRY_RUN" and job.status == "PARTIAL" and job.api_calls == 1
        assert job.request_json["diagnostic_probe"] is True
        assert job.request_json["award_page_shape_diagnostics"] == diagnostic.json()["diagnostics"]
        safe_audit = json.dumps(job.request_json) + json.dumps(job.warnings)
    assert CANARY not in response.text + diagnostic.text + safe_audit
    assert "example.test" not in response.text + diagnostic.text + safe_audit
    assert len(physical_calls) == 1  # The diagnostic GET is read only.


@pytest.mark.parametrize("overrides", [
    {"dry_run": False}, {"include_opening_results": True},
    {"dry_run": False, "include_opening_results": True}, {"diagnostic_probe": "true"},
])
def test_probe_invalid_combinations_rejected_before_job_or_provider(client, notice, monkeypatch, overrides):
    def forbidden(**_kwargs):
        pytest.fail("invalid probe must not construct a provider")
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", forbidden)
    response = client.post(f"/api/v1/notices/{notice}/award-history/refresh", json={**PROBE, **overrides})
    assert response.status_code == 422
    with client.app.state.session_factory() as session:
        assert list(session.scalars(select(IngestionJob))) == []


@pytest.mark.parametrize("kind", ["http_error", "timeout", "invalid_page", "valid"])
def test_direct_probe_overrides_retry_budget_and_reservation_cannot_be_reused(kind):
    physical_calls, sleeps = [], []
    def handler(request):
        physical_calls.append(1)
        return provider_response(kind, request)
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test",
                       diagnostic_probe=True, max_retries=3, sleep=sleeps.append,
                       transport=httpx.MockTransport(handler)) as client:
        read(client)
        first_diagnostics = client.page_shape_diagnostics
        first_errors = client.window_error_counts
        assert read(client) == []
        assert client.page_shape_diagnostics == first_diagnostics
        assert client.window_error_counts == first_errors
        with pytest.raises(RuntimeError, match="diagnostic probe request already consumed"):
            client._request("SYN-unused-operation", {})
        assert len(physical_calls) == client.request_count == 1
        assert client.fallback_window_count == 0 and sleeps == []


@pytest.mark.parametrize("expired_before", [False, True])
def test_probe_deadline_stops_before_http_or_retains_rejected_response_shape(monkeypatch, expired_before):
    clock = [2 if expired_before else 0]
    physical_calls = []
    def handler(request):
        physical_calls.append(1)
        clock[0] = 2
        return provider_response("invalid_page", request)
    monkeypatch.setattr("pai_loop.integrations.awards.time.monotonic", lambda: clock[0])
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", diagnostic_probe=True,
                       transport=httpx.MockTransport(handler)) as client:
        assert read(client, deadline_monotonic=1) == []
        assert client.hit_time_limit is True and client.fallback_window_count == 0
        assert len(physical_calls) == client.request_count == (0 if expired_before else 1)
        counts = client.page_shape_diagnostics["counts"]
        if expired_before:
            assert counts == [] and client.window_error_counts == []
        else:
            assert len(counts) == 1 and counts[0]["error_type"] == "AWARD_PAGE_INVALID"
            assert counts[0]["shape"]["items_type"] == "MISSING"


def test_default_client_still_reads_following_windows():
    physical_calls = []
    def handler(request):
        physical_calls.append(1)
        return provider_response("empty", request)
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", max_retries=0,
                       transport=httpx.MockTransport(handler)) as client:
        assert list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 2, 1), keyword="SYN")) == []
        assert len(physical_calls) == client.request_count == 2
        assert client.window_error_counts == []
