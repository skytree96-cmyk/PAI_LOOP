from __future__ import annotations

from datetime import date
from dataclasses import replace
import json
from unittest.mock import patch

import httpx
import pytest
from award_scope_helpers import attach_award_scope
from sqlalchemy import select

from pai_loop.integrations.awards import PpsAwardClient
from pai_loop.integrations.pps import PpsApiError, parse_paged_response
from pai_loop.models import IngestionJob

CANARY = "SYN_PRIVATE_PROVIDER_CANARY"


def _empty():
    return {"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 0, "items": []}}}


def _client(handler, **kwargs):
    return PpsAwardClient(service_key="SYN-unused", base_url="https://example.test",
        max_retries=kwargs.pop("max_retries", 0), transport=httpx.MockTransport(handler), **kwargs)


def _read(client, days=1, **kwargs):
    return list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, days),
        keyword="SYN", continue_on_window_error=True, max_window_days=30, **kwargs))


def test_short_failed_window_is_not_requested_twice():
    requested = []
    def handler(request):
        requested.append((request.url.params["inqryBgnDt"], request.url.params["inqryEndDt"]))
        return httpx.Response(401, json={"synthetic": "SYN-denied"})
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", max_retries=0,
                        transport=httpx.MockTransport(handler)) as client:
        rows = list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, 1),
            keyword="SYN", continue_on_window_error=True))
        assert len(requested) == client.request_count == 1
        assert rows == [] and len(client.window_errors) == 1
        assert client.fallback_window_count == 0
        assert client.window_error_counts == [{"phase": "PRIMARY", "error_type": "HTTP_ERROR", "http_status": 401, "provider_code": None, "count": 1}]


def test_deadline_preserves_successful_fallback_without_another_request():
    calls = []
    retained = {"identity": "SYN-AWARD|000|1|000", "title": "SYN-success"}
    def fetch(**kwargs):
        calls.append(kwargs["window"])
        if len(calls) == 1:
            raise PpsApiError("SYN-nonstandard envelope")
        return [retained]
    with PpsAwardClient(service_key="SYN-unused", base_url="https://example.test", max_retries=0,
                        transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with patch.object(client, "_fetch_window", side_effect=fetch), patch(
            "pai_loop.integrations.awards.time.monotonic", side_effect=[0, 0, 2]):
            rows = list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, 30),
                keyword="SYN", continue_on_window_error=True, deadline_monotonic=1))
        assert rows == [retained]
        assert len(calls) == 2 and client.hit_time_limit is True


@pytest.mark.parametrize("days,requests,fallback,failed", [(7, 1, 0, 1), (8, 3, 1, 2), (31, 7, 1, 6)])
def test_short_boundary_and_final_tail_do_not_repeat_identical_intervals(days, requests, fallback, failed):
    windows = []
    def handler(request):
        windows.append((request.url.params["inqryBgnDt"], request.url.params["inqryEndDt"]))
        return httpx.Response(200, json={"unexpected": CANARY})
    with _client(handler) as client:
        assert _read(client, days) == []
        assert len(windows) == len(set(windows)) == client.request_count == requests
        assert client.fallback_window_count == fallback and len(client.window_errors) == failed
        assert sum(row["count"] for row in client.window_error_counts) == requests


def test_strict_short_failure_still_raises_instead_of_becoming_empty_success():
    with _client(lambda _: httpx.Response(403)) as client:
        with pytest.raises(PpsApiError) as captured:
            list(client.iter_awards(start=date(2025, 1, 1), end=date(2025, 1, 7), keyword="SYN"))
        assert captured.value.http_status == 403
        assert client.request_count == 1 and len(client.window_errors) == 1


def test_existing_http_retry_budget_is_not_multiplied_by_an_identical_fallback():
    sleeps = []
    with _client(lambda _: httpx.Response(503), max_retries=2, sleep=sleeps.append) as client:
        assert _read(client) == []
        assert client.request_count == 3
        assert sleeps == [0.25, 0.5]
        assert client.window_error_counts[0]["count"] == 1  # Final failed window, not each HTTP attempt.


def test_primary_and_fallback_failure_categories_are_distinct_without_raw_payloads():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, text=CANARY)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "30", "resultMsg": CANARY}}})
    with _client(handler) as client:
        assert _read(client, 30) == []
        counts = client.window_error_counts
        assert counts == [
            {"phase": "FALLBACK", "error_type": "PROVIDER_RESULT_ERROR", "http_status": None, "provider_code": "30", "count": 5},
            {"phase": "PRIMARY", "error_type": "HTTP_ERROR", "http_status": 401, "provider_code": None, "count": 1},
        ]
        assert client.request_count == 6 and len(client.window_errors) == 5
        assert CANARY not in json.dumps(counts) and "example.test" not in json.dumps(counts)


@pytest.mark.parametrize("payload,kind,code", [
    ({"unexpected": CANARY}, "MISSING_RESPONSE", None),
    ({"response": {"header": {}}}, "MISSING_HEADER", None),
    ({"response": {"header": {"resultCode": "00"}}}, "MISSING_BODY", None),
    ({"response": {"header": {"resultCode": "00"}, "body": {}}}, "MISSING_TOTAL_COUNT", None),
    ({"response": {"header": {"resultCode": "22", "resultMsg": CANARY}}}, "PROVIDER_RESULT_ERROR", "22"),
    ({"response": {"header": {"resultCode": CANARY, "resultMsg": CANARY}}}, "PROVIDER_RESULT_ERROR", None),
    ({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "30", "returnAuthMsg": CANARY}}}, "SERVICE_ERROR", "30"),
    ({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": CANARY, "errMsg": CANARY}}}, "SERVICE_ERROR", None),
    ({"response": {"header": {"resultCode": "00"}, "body": {"totalCount": CANARY, "items": []}}}, "INVALID_TOTAL_COUNT", None),
    ({"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 1, "items": {"item": CANARY}}}}, "INVALID_ITEMS", None),
])
def test_common_parser_reports_safe_typed_failure_without_reinterpreting_it_as_zero(payload, kind, code):
    with pytest.raises(PpsApiError) as captured:
        parse_paged_response(payload)
    metadata = captured.value.safe_metadata()
    assert metadata == {"error_type": kind, "http_status": None, "provider_code": code}
    assert CANARY not in json.dumps(metadata)


@pytest.mark.parametrize("mode,kind,status", [("network", "NETWORK_ERROR", None), ("non_json", "INVALID_JSON", None),
    ("non_object", "INVALID_PAYLOAD", None), ("http", "HTTP_ERROR", 429), ("bad_rows", "AWARD_PAGE_INVALID", None)])
def test_transport_and_award_shape_failures_have_fixed_classification(mode, kind, status):
    def handler(request):
        if mode == "network":
            raise httpx.ReadTimeout(CANARY, request=request)
        if mode == "non_json":
            return httpx.Response(200, text=CANARY)
        if mode == "non_object":
            return httpx.Response(200, json=[CANARY])
        if mode == "http":
            return httpx.Response(429, text=CANARY)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 1, "items": [CANARY]}}})
    with _client(handler) as client:
        assert _read(client) == []
        assert client.window_error_counts == [{"phase": "PRIMARY", "error_type": kind, "http_status": status, "provider_code": None, "count": 1}]
        assert client.request_count == 1
        assert CANARY not in json.dumps(client.window_error_counts)


def test_counter_resets_between_sweeps_and_successful_empty_is_not_a_failure():
    responses = iter((httpx.Response(403), httpx.Response(200, json=_empty())))
    with _client(lambda _: next(responses)) as client:
        _read(client)
        assert len(client.window_error_counts) == 1
        assert _read(client) == []
        assert client.window_error_counts == client.window_errors == []
        assert client.request_count == 2 and not client.hit_time_limit


def test_exception_positional_compatibility_and_metadata_cannot_copy_arbitrary_strings():
    error = PpsApiError(CANARY, "SYN-extra", error_type=CANARY, http_status=True, provider_code=CANARY)
    assert error.args == (CANARY, "SYN-extra")
    assert CANARY in str(error)  # Legacy exception formatting is deliberately unchanged.
    assert error.safe_metadata() == {"error_type": "UNKNOWN", "http_status": None, "provider_code": None}
    assert PpsApiError().args == ()


def test_fallback_results_are_yielded_once_and_preserved_when_later_subwindow_fails():
    retained = {"identity": "SYN-AWARD|000|1|000", "title": "SYN-success"}
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs["window"])
        if len(calls) in {1, 3}:
            raise PpsApiError(CANARY)
        return [retained] if len(calls) == 2 else []
    with _client(lambda _: httpx.Response(500)) as client:
        with patch.object(client, "_fetch_window", side_effect=fetch):
            assert _read(client, 30) == [retained]
        assert len(calls) == 6 and len(client.window_errors) == 1


def test_refresh_records_safe_counts_as_partial_and_preserves_existing_awards(client, monkeypatch):
    target = "PPS-SYN-WINDOW-TARGET"
    assert client.post("/api/v1/notices", json={"notice_key": target, "bid_notice_no": target,
        "title": "SYN 합성 교육", "agency": "SYN 기관", "deadline": "2027-01-01T00:00:00Z"}).status_code == 201
    attach_award_scope(client, target, demand_agency_name="SYN 기관")
    client.app.state.settings = replace(client.app.state.settings, pps_api_key="SYN-pps-key", api_key="SYN-server-key")
    class FakeClient:
        request_count = 2
        hit_page_limit = hit_incomplete_response = False
        hit_time_limit = True
        fallback_window_count = 1
        window_errors = [CANARY]
        window_error_counts = [{"phase": "FALLBACK", **PpsApiError(CANARY, error_type="HTTP_ERROR", http_status=503).safe_metadata(), "count": 1}]
        include_row = True
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def iter_awards(self, **_kwargs):
            if self.include_row:
                yield {"identity": "SYN-AWARD|000|1|000", "bid_notice_no": "SYN-AWARD", "revision_no": "000",
                    "classification_no": "1", "rebid_no": "000", "title": "SYN 합성 교육", "winner_name": "SYN 업체",
                    "agency": "SYN 기관", "awarded_at": None, "opened_at": None}
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", FakeClient)
    headers = {"X-PAI-LOOP-API-KEY": "SYN-server-key"}
    path = f"/api/v1/notices/{target}/award-history"
    first = client.post(path + "/refresh", headers=headers, json={"keyword": "SYN", "years": 1})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "PARTIAL" and first.json()["records"] == 1
    assert first.json()["window_error_counts"] == FakeClient.window_error_counts
    history = client.get(path, headers=headers).json()
    FakeClient.include_row = False
    second = client.post(path + "/refresh", headers=headers, json={"keyword": "SYN", "years": 1})
    assert second.status_code == 200 and second.json()["status"] == "PARTIAL"
    assert second.json()["records"] == 0
    assert client.get(path, headers=headers).json() == history
    with client.app.state.session_factory() as session:
        jobs = list(session.scalars(select(IngestionJob).where(IngestionJob.source == "PPS_AWARD")))
        assert all(job.request_json["window_error_counts"] == FakeClient.window_error_counts for job in jobs)
        assert CANARY not in json.dumps([job.request_json for job in jobs])
    assert CANARY not in first.text + second.text
    # A primary error fully covered by successful fallbacks is diagnostic
    # history, not unresolved coverage. Do not derive PARTIAL from counts alone.
    FakeClient.hit_time_limit = False
    FakeClient.window_errors = []
    FakeClient.window_error_counts = [{**FakeClient.window_error_counts[0], "phase": "PRIMARY"}]
    recovered = client.post(path + "/refresh", headers=headers, json={"keyword": "SYN", "years": 1})
    assert recovered.status_code == 200 and recovered.json()["status"] == "COMPLETED"
    assert recovered.json()["window_error_counts"] == FakeClient.window_error_counts
    assert client.get(path, headers=headers).json() == history
