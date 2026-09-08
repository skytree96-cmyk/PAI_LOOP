from __future__ import annotations

from datetime import date

import httpx
import pytest

from pai_loop.integrations.awards import OpeningResultsIncomplete, PpsAwardClient
from pai_loop.integrations.pps import PpsApiError


def envelope(body):
    return {"response": {"header": {"resultCode": "00"}, "body": body}}


def provider(handler):
    return PpsAwardClient(service_key="SYN-unused", base_url="https://example.test",
        max_retries=0, transport=httpx.MockTransport(handler))


def read(client, *, end=date(2025, 1, 1)):
    return list(client.iter_awards(start=date(2025, 1, 1), end=end, keyword="SYN",
        rows=1, max_pages_per_window=3))


@pytest.mark.parametrize("total", [0, "0"])
def test_success_envelopes_without_items_and_exact_zero_need_no_fallback(total):
    calls = []
    def handler(request):
        calls.append((request.url.params["inqryBgnDt"], request.url.params["pageNo"]))
        return httpx.Response(200, json=envelope({"totalCount": total}))
    with provider(handler) as client:
        assert read(client, end=date(2025, 2, 1)) == []
        assert len(calls) == client.request_count == 2
        assert len(set(calls)) == 2 and all(page == "1" for _, page in calls)
        assert client.fallback_window_count == 0
        assert not client.window_errors and not client.window_error_counts
        assert not client.hit_incomplete_response and not client.hit_page_limit
        assert client.page_shape_diagnostics == {"counts": [], "suppressed_count": 0}


@pytest.mark.parametrize("total", [False, True, 0.0, 1.0, None, "", " ", " 0", "0 ",
                                  "00", "+0", "-0", "0.0", 1, "1", -1])
def test_missing_items_with_false_or_nonzero_counts_still_rejects(total):
    with provider(lambda _: httpx.Response(200, json=envelope({"totalCount": total}))) as client:
        with pytest.raises(PpsApiError):
            read(client)
        assert client.request_count == 1
        expected_error = "INVALID_TOTAL_COUNT" if total in (" ", "0.0") else "AWARD_PAGE_INVALID"
        assert client.window_error_counts[0]["error_type"] == expected_error
        assert client.page_shape_diagnostics["counts"][0]["shape"]["items_type"] == "MISSING"


@pytest.mark.parametrize("payload,error", [
    (envelope({}), "MISSING_TOTAL_COUNT"),
    ({"response": {"body": {"totalCount": 0}}}, "MISSING_HEADER"),
    ({"response": {"header": {}, "body": {"totalCount": 0}}}, "MISSING_HEADER"),
    ({"response": {"header": {"resultCode": "30"}, "body": {"totalCount": 0}}}, "PROVIDER_RESULT_ERROR"),
    ({"response": {"header": {"resultCode": False}, "body": {"totalCount": 0}}}, "PROVIDER_RESULT_ERROR"),
    ({"response": {"header": {"resultCode": "00"}, "body": None}}, "MISSING_BODY"),
    ({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "30"}},
      **envelope({"totalCount": 0})}, "SERVICE_ERROR"),
])
def test_explicit_zero_does_not_bypass_common_envelope_validation(payload, error):
    with provider(lambda _: httpx.Response(200, json=payload)) as client:
        with pytest.raises(PpsApiError):
            read(client)
        assert client.request_count == 1 and client.window_error_counts[0]["error_type"] == error


def award_page(total):
    return envelope({"totalCount": total, "items": [{"bidNtceNo": "SYN-LATER-AWARD",
        "bidNtceNm": "SYN 교육", "bidwinnrNm": "SYN 업체"}]})


def test_empty_first_window_continues_to_later_award_without_fallback():
    calls = []
    def handler(request):
        calls.append(request.url.params["inqryBgnDt"])
        return httpx.Response(200, json=envelope({"totalCount": 0}) if len(calls) == 1 else award_page(1))
    with provider(handler) as client:
        result = read(client, end=date(2025, 2, 1))
        assert [row["bid_notice_no"] for row in result] == ["SYN-LATER-AWARD"]
        assert len(calls) == len(set(calls)) == client.request_count == 2
        assert client.fallback_window_count == 0 and not client.window_errors
        assert not client.hit_incomplete_response and not client.hit_page_limit


@pytest.mark.parametrize("total", [0, "0"])
def test_later_page_changing_total_to_zero_still_marks_incomplete(total):
    pages = []
    def handler(request):
        pages.append(request.url.params["pageNo"])
        return httpx.Response(200, json=award_page(2) if len(pages) == 1 else envelope({"totalCount": total}))
    with provider(handler) as client:
        result = read(client)
        assert len(result) == 1 and pages == ["1", "2"]
        assert client.hit_incomplete_response is True
        assert client.fallback_window_count == 0 and client.request_count == 2


@pytest.mark.parametrize("total", [0, "0"])
def test_opening_missing_items_remains_incomplete(total):
    with provider(lambda _: httpx.Response(200, json=envelope({"totalCount": total}))) as client:
        with pytest.raises(OpeningResultsIncomplete):
            client.fetch_opening_results(bid_notice_no="SYN-OPENING", max_pages=1)
        assert client.request_count == 1
