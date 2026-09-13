from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Lock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from pai_loop.integrations.awards import PpsAwardClient
from pai_loop.integrations.pps import PpsApiCallBudget, PpsApiCallBudgetExceeded, PpsClient
from pai_loop.models import AwardHistoryItem, IngestionJob, Notice
from pai_loop.schemas import AwardHistoryRefreshRequest


def envelope(items, total=None):
    return {"response": {"header": {"resultCode": "00"}, "body": {
        "totalCount": len(items) if total is None else total, "items": items,
    }}}


def award():
    return {"bidNtceNo": "SYN-AWARD", "bidNtceOrd": "000", "bidClsfcNo": "0",
            "rbidNo": "000", "bidNtceNm": "SYN 교육", "bidwinnrNm": "SYN winner"}


@pytest.mark.parametrize("value", [0, 2001, -1, True, 1.5, "10"])
def test_refresh_rejects_invalid_physical_budget(value):
    with pytest.raises(ValidationError):
        AwardHistoryRefreshRequest(max_api_calls=value)


def test_budget_is_optional_and_bounded():
    assert AwardHistoryRefreshRequest().max_api_calls is None
    assert AwardHistoryRefreshRequest(max_api_calls=2000).max_api_calls == 2000


@pytest.mark.parametrize("failure", ["http", "network"])
def test_budget_counts_retries_and_is_not_a_provider_failure(failure):
    calls = []
    def handler(request):
        calls.append(1)
        if failure == "network":
            raise httpx.ReadTimeout("SYN timeout", request=request)
        return httpx.Response(503)
    budget = PpsApiCallBudget(2)
    with PpsAwardClient(service_key="SYN-key", request_budget=budget, max_retries=5,
                        sleep=lambda _: None, transport=httpx.MockTransport(handler)) as provider:
        assert list(provider.iter_awards(start=date(2026, 1, 1), end=date(2026, 3, 1),
            keyword="SYN", continue_on_window_error=True)) == []
        assert len(calls) == provider.request_count == budget.consumed == 2
        assert provider.hit_api_call_limit is True
        assert provider.window_error_counts == []
        assert provider.fallback_window_count == 0


def test_budget_is_shared_across_concurrent_clients_before_http():
    budget, calls, lock = PpsApiCallBudget(7), [], Lock()
    def handler(request):
        with lock:
            calls.append(1)
        return httpx.Response(200, json=envelope([]))
    clients = [PpsClient(service_key="SYN-key", request_budget=budget,
               transport=httpx.MockTransport(handler)) for _ in range(3)]
    def attempt(index):
        try:
            clients[index % len(clients)]._request("SYN-operation", {})
            return True
        except PpsApiCallBudgetExceeded:
            return False
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            successes = list(pool.map(attempt, range(50)))
        assert sum(successes) == len(calls) == budget.consumed == 7
        assert sum(client.request_count for client in clients) == 7
    finally:
        for client in clients:
            client.close()


def test_history_cap_keeps_completed_page_and_stops_unresolved_next_page():
    budget = PpsApiCallBudget(1)
    with PpsAwardClient(service_key="SYN-key", request_budget=budget,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=envelope([award()], 2)))) as provider:
        rows = list(provider.iter_awards(start=date(2026, 1, 1), end=date(2026, 2, 1),
            keyword="SYN", rows=1, max_pages_per_window=3))
        assert len(rows) == 1
        assert provider.request_count == 1 and provider.hit_api_call_limit
        assert provider.window_errors == [] and provider.window_error_counts == []


def test_history_cap_covers_fallback_and_retains_successful_subwindow():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=envelope([award()]))
    with PpsAwardClient(service_key="SYN-key", request_budget=PpsApiCallBudget(2),
            max_retries=0, transport=httpx.MockTransport(handler)) as provider:
        rows = list(provider.iter_awards(start=date(2026, 1, 1), end=date(2026, 2, 1),
            keyword="SYN", continue_on_window_error=True))
        assert len(rows) == 1 and len(calls) == provider.request_count == 2
        assert provider.hit_api_call_limit and provider.fallback_window_count == 1
        assert provider.window_error_counts == [{"phase": "PRIMARY", "error_type": "HTTP_ERROR",
            "http_status": 503, "provider_code": None, "count": 1}]


@pytest.fixture()
def target(client):
    key = "SYN-BUDGET-TARGET"
    response = client.post("/api/v1/notices", json={"notice_key": key, "bid_notice_no": key,
        "title": "SYN 교육", "agency": "SYN agency", "deadline": "2099-01-01T00:00:00Z"})
    assert response.status_code == 201
    client.app.state.settings = replace(client.app.state.settings, pps_api_key="SYN-key")
    with client.app.state.session_factory() as session:
        notice = session.scalar(select(Notice).where(Notice.notice_key == key))
        session.add(AwardHistoryItem(target_notice_id=notice.id, external_identity="SYN-AWARD|000|0|000",
            bid_notice_no="SYN-AWARD", title="SYN 교육", winner_name="SYN winner", similarity_score=1,
            opening_results=[{"company_name": "SYN stored", "bid_amount": 7000}],
            opening_results_status="COLLECTED"))
        session.commit()
    return key


def provider_factory(monkeypatch, handler, *, one_window=False):
    class Provider(PpsAwardClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))
        def iter_awards(self, **kwargs):
            if one_window:
                kwargs.update(start=date(2026, 1, 1), end=date(2026, 1, 1))
            return super().iter_awards(**kwargs)
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", Provider)


@pytest.mark.parametrize("opening", [False, True])
def test_refresh_marks_history_or_opening_budget_partial_and_preserves_bidders(client, target, monkeypatch, opening):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=envelope([award()]))
    provider_factory(monkeypatch, handler, one_window=opening)
    response = client.post(f"/api/v1/notices/{target}/award-history/refresh", json={
        "keyword": "SYN", "max_api_calls": 1, "include_opening_results": opening})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PARTIAL" and body["api_calls"] == len(calls) == 1
    assert body["records"] == 1
    assert any(w.startswith("AWARD_API_CALL_BUDGET_EXHAUSTED:") for w in body["warnings"])
    assert body["window_error_counts"] == []
    with client.app.state.session_factory() as session:
        stored = session.scalar(select(AwardHistoryItem))
        assert stored.opening_results == [{"company_name": "SYN stored", "bid_amount": 7000}]
        job = session.get(IngestionJob, body["job_id"])
        assert job.api_calls == 1 and job.status == "PARTIAL"
        assert job.request_json["max_api_calls"] == 1


def test_exact_budget_that_finishes_all_windows_is_complete(client, target, monkeypatch):
    provider_factory(monkeypatch, lambda _: httpx.Response(200, json=envelope([])), one_window=True)
    response = client.post(f"/api/v1/notices/{target}/award-history/refresh", json={"keyword": "SYN", "max_api_calls": 1})
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED" and response.json()["api_calls"] == 1


def test_budget_during_opening_pagination_keeps_entire_previous_bidder_snapshot(client, target, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(200, json=envelope([award()]))
        return httpx.Response(200, json=envelope([{
            "bidNtceNo": "SYN-AWARD", "bidNtceOrd": "000", "bidClsfcNo": "0", "rbidNo": "000",
            "prcbdrNm": "SYN new partial bidder", "bidprcAmt": "8000",
        }], 2))
    provider_factory(monkeypatch, handler, one_window=True)
    response = client.post(f"/api/v1/notices/{target}/award-history/refresh", json={
        "keyword": "SYN", "max_api_calls": 2, "include_opening_results": True,
        "page_size": 1, "opening_result_max_pages": 3})
    assert response.status_code == 200
    assert response.json()["status"] == "PARTIAL" and response.json()["api_calls"] == len(calls) == 2
    with client.app.state.session_factory() as session:
        stored = session.scalar(select(AwardHistoryItem))
        assert stored.opening_results == [{"company_name": "SYN stored", "bid_amount": 7000}]
        assert stored.opening_results_status == "PARTIAL"


@pytest.mark.parametrize("cap", [None, 10])
@pytest.mark.parametrize("stage", ["history", "opening"])
def test_unexpected_failure_audits_all_physical_calls(client, target, monkeypatch, cap, stage):
    calls = []
    def handler(request):
        calls.append(1)
        if stage == "history" or len(calls) == 2:
            raise RuntimeError("SYN unexpected transport failure")
        return httpx.Response(200, json=envelope([award()]))
    provider_factory(monkeypatch, handler, one_window=True)
    with pytest.raises(RuntimeError, match="SYN unexpected transport failure"):
        client.post(f"/api/v1/notices/{target}/award-history/refresh", json={
            "keyword": "SYN", "max_api_calls": cap, "include_opening_results": True})
    with client.app.state.session_factory() as session:
        job = session.scalar(select(IngestionJob))
        assert job.status == "FAILED" and job.api_calls == len(calls) == (1 if stage == "history" else 2)


def test_diagnostic_probe_keeps_one_call_even_with_larger_budget(client, target, monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503)
    provider_factory(monkeypatch, handler)
    response = client.post(f"/api/v1/notices/{target}/award-history/refresh", json={
        "keyword": "SYN", "max_api_calls": 150, "dry_run": True, "diagnostic_probe": True})
    assert response.status_code == 200
    assert response.json()["status"] == "PARTIAL" and response.json()["api_calls"] == len(calls) == 1
