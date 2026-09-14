from datetime import date

import httpx
import pytest

from pai_loop.integrations.awards import PpsAwardClient, is_pps_rate_limit_error, normalise_award
from pai_loop.integrations.pps import PpsApiError


def envelope(items, total=None):
    return {"response": {"header": {"resultCode": "00"}, "body": {
        "totalCount": len(items) if total is None else total, "items": items,
    }}}


def award(key="A", *, name="SYN 수요기관", code=None):
    return {"bidNtceNo": f"SYN-AWARD-{key}", "bidNtceOrd": "000",
            "bidNtceNm": "SYN 교육", "bidwinnrNm": "SYN 업체",
            "dminsttNm": name, "dminsttCd": code}


def provider(handler):
    return PpsAwardClient(service_key="SYN-unused", base_url="https://example.test",
                          max_retries=0, transport=httpx.MockTransport(handler))


def collect(client, **kwargs):
    return list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 2, 1),
                                  keyword="SYN", continue_on_window_error=True, **kwargs))


def test_demand_name_applies_to_every_primary_fallback_and_page():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["dminsttNm"] == "SYN 수요기관"
        assert "dminsttCd" not in request.url.params
        assert "ntceInsttNm" not in request.url.params
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=envelope([award(str(len(requests)))], 2))

    with provider(handler) as client:
        rows = collect(client, demand_agency_name="SYN 수요기관", rows=1, max_pages_per_window=2)
        assert len(requests) == client.request_count == 11
        assert len(rows) == 10 and client.fallback_window_count == 1
        assert not client.hit_rate_limit


def test_name_filter_is_normalized_exact_and_never_uses_publishing_agency():
    candidates = [award("MATCH", name=" ＳＹＮ\t수요 기관 "),
                  award("CHILD", name="SYN 수요기관 지부"),
                  award("OTHER", name="SYN 다른기관"),
                  {**award("PUBLISHER", name=None), "ntceInsttNm": "SYN 수요기관"}]
    with provider(lambda _: httpx.Response(200, json=envelope(candidates))) as client:
        rows = list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 1, 1),
                                      keyword="SYN", demand_agency_name="syn 수요기관"))
    assert [row["bid_notice_no"] for row in rows] == ["SYN-AWARD-MATCH"]


@pytest.mark.parametrize("with_name,expected", [(True, ["CODE", "NAME_FALLBACK"]), (False, ["CODE"])])
def test_code_precedes_name_and_name_fallback_requires_missing_returned_code(with_name, expected):
    candidates = [award("CODE", name="SYN 다른 표기", code=" ＳＹＮ-A "),
                  award("CONFLICT", code="SYN-B"), award("NAME_FALLBACK"),
                  award("UNKNOWN", name=None)]

    def handler(request):
        assert request.url.params["dminsttCd"] == "SYN-A"
        assert "dminsttNm" not in request.url.params
        return httpx.Response(200, json=envelope(candidates))

    with provider(handler) as client:
        rows = list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 1, 1), keyword="SYN",
            demand_agency_code="SYN-A", demand_agency_name="SYN 수요기관" if with_name else None))
    assert [row["bid_notice_no"] for row in rows] == [f"SYN-AWARD-{key}" for key in expected]


def test_no_agency_arguments_preserve_existing_query_and_results():
    candidates = [award("A"), award("B", name="SYN 다른기관"), award("C", name=None)]

    def handler(request):
        assert not any(key in request.url.params for key in ("dminsttCd", "dminsttNm", "ntceInsttNm"))
        return httpx.Response(200, json=envelope(candidates))

    with provider(handler) as client:
        rows = list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 1, 1), keyword="SYN"))
    assert len(rows) == 3
    assert normalise_award(award(code="SYN-A"))["demand_agency_code"] == "SYN-A"


@pytest.mark.parametrize("returned_name,expected_incomplete", [(None, True), ("SYN 다른기관", False)])
def test_unverifiable_agency_is_incomplete_but_a_known_different_agency_is_excluded(returned_name, expected_incomplete):
    with provider(lambda _: httpx.Response(200, json=envelope([award(name=returned_name)]))) as client:
        assert list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 1, 1),
            keyword="SYN", demand_agency_name="SYN 수요기관")) == []
        assert client.hit_incomplete_response is expected_incomplete
        assert client.request_count == 1 and client.window_errors == []


def test_missing_returned_code_without_a_target_name_cannot_report_verified_empty():
    with provider(lambda _: httpx.Response(200, json=envelope([award()]))) as client:
        assert list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 1, 1),
            keyword="SYN", demand_agency_code="SYN-A")) == []
        assert client.hit_incomplete_response


def test_empty_agency_search_still_covers_all_three_calendar_years():
    dates = []

    def handler(request):
        dates.append((request.url.params["inqryBgnDt"], request.url.params["inqryEndDt"]))
        assert request.url.params["dminsttNm"] == "SYN 수요기관"
        return httpx.Response(200, json=envelope([]))

    with provider(handler) as client:
        rows = list(client.iter_awards(start=date(2024, 1, 1), end=date(2026, 9, 13),
                                      keyword="SYN", demand_agency_name="SYN 수요기관"))
        assert rows == [] and client.request_count == 36
        assert not client.window_errors and not client.hit_rate_limit
    assert dates[0][0] == "202401010000" and dates[-1][1] == "202609132359"


def failure(kind):
    if kind == "429":
        return httpx.Response(429)
    if kind == "gateway22":
        return httpx.Response(200, json={"OpenAPI_ServiceResponse": {
            "cmmMsgHeader": {"returnReasonCode": "22", "errMsg": "SYN limit"}}})
    return httpx.Response(200, json={"response": {"header": {"resultCode": kind}}})


@pytest.mark.parametrize("kind", ["429", "22", "23", "gateway22"])
def test_first_rate_limit_stops_all_windows_without_fallback(kind):
    with provider(lambda _: failure(kind)) as client:
        assert collect(client, demand_agency_name="SYN 수요기관") == []
        assert client.request_count == 1 and client.hit_rate_limit
        assert client.fallback_window_count == 0 and len(client.window_errors) == 1
        assert sum(row["count"] for row in client.window_error_counts) == 1


@pytest.mark.parametrize("kind", ["429", "22", "23", "gateway22"])
def test_rate_limit_on_second_page_preserves_first_page_in_same_window(kind):
    responses = iter([httpx.Response(200, json=envelope([award()], 2)), failure(kind)])
    with provider(lambda _: next(responses)) as client:
        rows = collect(client, rows=1, max_pages_per_window=3, demand_agency_name="SYN 수요기관")
        assert [row["bid_notice_no"] for row in rows] == ["SYN-AWARD-A"]
        assert client.request_count == 2 and client.hit_rate_limit
        assert client.fallback_window_count == 0 and client.window_errors


def test_fallback_rate_limit_preserves_prior_window_subwindow_and_current_page_once():
    responses = iter([
        httpx.Response(200, json=envelope([award("PRIOR_WINDOW")])),
        httpx.Response(503),
        httpx.Response(200, json=envelope([award("PRIOR_FALLBACK")])),
        httpx.Response(200, json=envelope([award("CURRENT_PAGE")], 2)),
        failure("22"),
    ])
    with provider(lambda _: next(responses)) as client:
        rows = list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 3, 1),
            keyword="SYN", rows=1, max_pages_per_window=3, continue_on_window_error=True,
            demand_agency_name="SYN 수요기관"))
        assert [row["bid_notice_no"] for row in rows] == [
            "SYN-AWARD-PRIOR_WINDOW", "SYN-AWARD-PRIOR_FALLBACK", "SYN-AWARD-CURRENT_PAGE"]
        assert client.request_count == 5 and client.hit_rate_limit
        assert client.fallback_window_count == 1 and len(client.window_errors) == 1
        assert {(row["phase"], row["http_status"], row["provider_code"])
                for row in client.window_error_counts} == {("PRIMARY", 503, None), ("FALLBACK", None, "22")}


def test_rate_limit_flag_resets_for_a_separate_later_sweep():
    responses = iter([failure("22"), httpx.Response(200, json=envelope([]))])
    with provider(lambda _: next(responses)) as client:
        for expected in (True, False):
            assert list(client.iter_awards(start=date(2026, 1, 1), end=date(2026, 1, 1), keyword="SYN")) == []
            assert client.hit_rate_limit is expected
        assert client.request_count == 2 and client.window_errors == []
    assert not is_pps_rate_limit_error(PpsApiError(error_type="PROVIDER_RESULT_ERROR", provider_code="07"))
    assert not is_pps_rate_limit_error(PpsApiError(error_type="HTTP_ERROR", http_status=403))
