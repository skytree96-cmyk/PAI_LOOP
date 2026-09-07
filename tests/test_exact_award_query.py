from datetime import date

import httpx
import pytest

from pai_loop.integrations.awards import OpeningResultsIncomplete
from pai_loop.integrations import outcome_feedback as adapter
from pai_loop.integrations.outcome_feedback import PpsOutcomeFeedbackClient
from pai_loop.integrations.pps import PpsApiError


PPS_SEARCH = "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch"
BASIC_SEARCH = "as/ScsbidInfoService/getScsbidListSttusServc"
NOTICE = "SYN-EXACT-NUMBER"
OWN = "0000000000"


def row(**changes):
    return {
        "bidNtceNo": NOTICE, "bidNtceOrd": "000", "bidClsfcNo": "001",
        "rbidNo": "002", "bidNtceNm": "SYN final award", "bidwinnrNm": "SYN winner",
        "bidwinnrBizno": OWN, "sucsfbidAmt": "100", **changes,
    }


def envelope(items, *, total=None, page=1, size=100):
    return {"response": {"header": {"resultCode": "00"}, "body": {
        "items": items, "totalCount": len(items) if total is None else total,
        "pageNo": page, "numOfRows": size,
    }}}


def fetch(client, **changes):
    return client.fetch_exact_notice_awards(
        **{"bid_notice_no": NOTICE, "revision_no": "00", "start": date(2025, 1, 1),
           "end": date(2025, 1, 2), "company_business_number": OWN,
           "require_complete": True, **changes},
    )


@pytest.mark.parametrize("operation,division", [(PPS_SEARCH, "3"), (BASIC_SEARCH, "4")])
def test_exact_number_query_uses_operation_specific_division_without_date_or_other_filters(operation, division):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope([row()]))

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        result = fetch(client, operation_path=operation)
    assert len(requests) == result.api_calls == 1
    assert requests[0].url.path.endswith(operation)
    assert dict(requests[0].url.params) == {
        "inqryDiv": division, "bidNtceNo": NOTICE, "pageNo": "1", "numOfRows": "100",
        "serviceKey": "SYN-key", "type": "json",
    }
    assert result.rows[0]["opening_identity"] == {
        "bid_notice_no": NOTICE, "revision_no": "0", "classification_no": "1", "rebid_no": "2",
    }


def test_date_mode_counterexample_returns_unrelated_first_page_until_number_mode_selected():
    def handler(request):
        if request.url.params.get("inqryDiv") == "3":
            return httpx.Response(200, json=envelope([row()]))
        unrelated = [row(bidNtceNo=f"SYN-UNRELATED-{index}") for index in range(100)]
        return httpx.Response(200, json=envelope(unrelated, total=101))

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        result = fetch(client)
    assert result.api_calls == result.fetched_count == 1
    assert len(result.rows) == 1
    assert result.mismatched_count == 0
    assert result.hit_page_limit is False


@pytest.mark.parametrize("require_complete", [False, True])
@pytest.mark.parametrize("window_days", [1, 30])
def test_month_spanning_legacy_arguments_still_make_one_number_sweep(require_complete, window_days):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope([row()]))

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        result = fetch(client, start=date(2023, 1, 1), end=date(2026, 9, 8),
                       max_window_days=window_days, require_complete=require_complete)
    assert len(requests) == result.api_calls == 1
    assert result.fetched_count == len(result.rows) == 1


@pytest.mark.parametrize("operation", [
    "SYN-unknown", "as/ScsbidInfoService/getScsbidListSttusThngPPSSrch",
    "as/ScsbidInfoService/getOpengResultListInfoOpengCompt", PPS_SEARCH + "?SYN=value",
    "https://example.invalid/" + PPS_SEARCH,
])
def test_unknown_operation_is_rejected_before_any_request(operation):
    def handler(_request):
        pytest.fail("Unknown operation must not reach the transport")

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PpsApiError, match="지원되지") as caught:
            fetch(client, operation_path=operation)
        assert operation not in str(caught.value)
        assert client.request_count == 0


@pytest.mark.parametrize("changes,description", [
    ({"start": date(2025, 2, 1), "end": date(2025, 1, 1)}, "end"),
    ({"max_window_days": 0}, "max_days"),
])
def test_legacy_date_argument_validation_remains_without_requests(changes, description):
    def handler(_request):
        pytest.fail("Invalid arguments must not reach the transport")

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match=description):
            fetch(client, **changes)
        assert client.request_count == 0


@pytest.mark.parametrize("page_limit", [1, 2])
def test_exact_number_pagination_keeps_total_budget_and_revision_filter(page_limit):
    requests = []

    def handler(request):
        requests.append(request)
        page = int(request.url.params["pageNo"])
        item = row(bidNtceOrd="001") if page == 1 else row()
        return httpx.Response(200, json=envelope([item], total=2, page=page, size=1))

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        result = fetch(client, rows=1, max_pages_per_window=page_limit,
                       start=date(2025, 1, 1), end=date(2025, 5, 1))
    assert [r.url.params["pageNo"] for r in requests] == [str(n) for n in range(1, page_limit + 1)]
    assert result.api_calls == result.fetched_count == page_limit
    assert result.hit_page_limit is (page_limit == 1)
    assert result.mismatched_count == 1
    assert len(result.rows) == page_limit - 1


def test_provider_ignoring_number_filter_stays_partial_with_no_exact_rows():
    def handler(_request):
        return httpx.Response(200, json=envelope(
            [row(bidNtceNo=f"SYN-UNRELATED-{index}") for index in range(100)], total=101,
        ))

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        result = fetch(client)
    assert result.rows == []
    assert result.api_calls == 1
    assert result.fetched_count == result.mismatched_count == 100
    assert result.hit_page_limit is True


def test_duplicate_full_opening_on_later_page_still_fails_complete_result_proof():
    def handler(request):
        return httpx.Response(200, json=envelope(
            [row()], total=2, page=int(request.url.params["pageNo"]), size=1,
        ))

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpeningResultsIncomplete, match="중복"):
            fetch(client, rows=1, max_pages_per_window=2)
        assert client.request_count == 2


def test_expired_deadline_prevents_first_request():
    def handler(_request):
        pytest.fail("Expired deadline must not reach the transport")

    with PpsOutcomeFeedbackClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        result = fetch(client, deadline_monotonic=0)
    assert result.hit_time_limit is True
    assert result.api_calls == result.fetched_count == 0


def test_deadline_between_pages_stops_without_retrying_the_number(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr("pai_loop.integrations.outcome_feedback.time.monotonic", lambda: clock[0])

    class ClockClient(PpsOutcomeFeedbackClient):
        def _request(self, operation_path, params, **kwargs):
            assert 0.1 <= kwargs["timeout_seconds"] <= 9
            return super()._request(operation_path, params, **kwargs)

    def handler(_request):
        return httpx.Response(200, json=envelope([row()], total=2, size=1))

    original_normalise = adapter._normalise_outcome_row

    def normalise_then_expire(*args, **kwargs):
        result = original_normalise(*args, **kwargs)
        clock[0] = 11.0
        return result

    monkeypatch.setattr("pai_loop.integrations.outcome_feedback._normalise_outcome_row", normalise_then_expire)
    with ClockClient(service_key="SYN-key", transport=httpx.MockTransport(handler)) as client:
        result = fetch(client, rows=1, max_pages_per_window=2, deadline_monotonic=10)
    assert result.hit_time_limit is True
    assert result.api_calls == result.fetched_count == len(result.rows) == 1
