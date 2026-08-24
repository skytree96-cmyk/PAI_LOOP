from __future__ import annotations

import re
import threading
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pai_loop.company_awards as company_awards_api
from pai_loop.company_awards import router
from pai_loop.config import Settings
from pai_loop.integrations.company_awards import (
    AWARD_SCOPE_OPERATIONS,
    PpsCompanyAwardClient,
    normalise_business_number,
)
from pai_loop.integrations.pps import PpsApiError

TOKEN = "2468"
AUTH_HEADERS = {
    "Origin": "https://testserver",
    "Sec-Fetch-Site": "same-origin",
    "X-PAI-Manual-Token": TOKEN,
}


def _raw_award(
    *,
    business_number: str = "1058201810",
    title: str = "공공기관 리더십 교육",
) -> dict[str, str]:
    return {
        "bidNtceNo": "R26BK00000001",
        "bidNtceOrd": "0",
        "bidClsfcNo": "0",
        "rbidNo": "0",
        "bidNtceNm": title,
        "prtcptCnum": "3",
        "bidwinnrNm": "사단법인 한국능률협회",
        "bidwinnrBizno": business_number,
        "sucsfbidAmt": "120000000",
        "sucsfbidRate": "89.5",
        "rlOpengDt": "202608011100",
        "dminsttNm": "합성 발주기관",
        "rgstDt": "202608011230",
        "fnlSucsfDate": "20260802",
        "bidwinnrCeoNm": "MUST-NOT-LEAK-PERSON",
        "bidwinnrAdrs": "MUST-NOT-LEAK-ADDRESS",
        "bidwinnrTelNo": "MUST-NOT-LEAK-CONTACT",
        "fnlSucsfCorpOfcl": "MUST-NOT-LEAK-OFFICIAL",
    }


def _payload(items: list[dict[str, str]], *, total: int | None = None) -> dict[str, object]:
    return {
        "response": {
            "header": {"resultCode": "00"},
            "body": {
                "totalCount": len(items) if total is None else total,
                "items": items,
            },
        }
    }


def _app() -> FastAPI:
    app = FastAPI()
    app.state.settings = Settings(
        environment="production",
        pps_api_key="server-side-pps-key",
        public_read_only=True,
        public_manual_analysis_enabled=True,
        public_manual_analysis_token=TOKEN,
    )
    app.include_router(router)
    return app


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("105\u002d82\u002d01810", "1058201810"),
        ("1058201810", "1058201810"),
        (" 105 82 01810 ", "1058201810"),
    ],
)
def test_business_number_normalisation_accepts_display_forms(
    value: str,
    expected: str,
) -> None:
    assert normalise_business_number(value) == expected


@pytest.mark.parametrize("value", ["", "123", "105-82-0181A", "10582018100"])
def test_business_number_normalisation_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalise_business_number(value)


def test_company_award_client_queries_all_explicit_scopes_and_drops_identifiers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([_raw_award()]))

    scopes = ["goods", "construction", "service", "foreign"]
    with PpsCompanyAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        records = list(
            client.iter_company_awards(
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                business_number="105\u002d82\u002d01810",
                scopes=scopes,
            )
        )

    assert {request.url.path.rsplit("/", 1)[-1] for request in requests} == {
        path.rsplit("/", 1)[-1] for path in AWARD_SCOPE_OPERATIONS.values()
    }
    assert all(request.url.params["bizno"] == "1058201810" for request in requests)
    assert all(request.url.params["inqryDiv"] == "2" for request in requests)
    assert {record["scope"] for record in records} == set(scopes)
    serialised = str(records)
    assert "1058201810" not in serialised
    assert "server-key" not in serialised
    for private_value in (
        "MUST-NOT-LEAK-PERSON",
        "MUST-NOT-LEAK-ADDRESS",
        "MUST-NOT-LEAK-CONTACT",
        "MUST-NOT-LEAK-OFFICIAL",
    ):
        assert private_value not in serialised


def test_company_award_client_exactly_post_filters_provider_rows() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json=_payload(
                [
                    _raw_award(),
                    _raw_award(
                        business_number="9999999999",
                        title="다른 회사의 낙찰 결과",
                    ),
                ]
            ),
        )
    )
    with PpsCompanyAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=transport,
    ) as client:
        records = list(
            client.iter_company_awards(
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                business_number="1058201810",
            )
        )
        assert client.provider_mismatch_count == 1
    assert [record["title"] for record in records] == ["공공기관 리더십 교육"]


def test_company_award_client_uses_february_safe_default_windows() -> None:
    """PPS rejects Feb 20-Mar 21 as longer than one calendar month."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([]))

    with PpsCompanyAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        records = list(
            client.iter_company_awards(
                start=date(2026, 2, 20),
                end=date(2026, 3, 21),
                business_number="1058201810",
            )
        )

    assert records == []
    # Newest windows are fetched first, but the underlying fixed windows must
    # remain at most 28 inclusive days even across February.
    assert [
        (
            request.url.params["inqryBgnDt"],
            request.url.params["inqryEndDt"],
        )
        for request in requests
    ] == [
        ("202603200000", "202603212359"),
        ("202602200000", "202603192359"),
    ]


def test_company_award_client_paginates_each_window_when_approved() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pageNo"])
        pages.append(page)
        item = _raw_award(title=f"페이지 {page} 낙찰")
        item["bidNtceNo"] = f"R26BK0000000{page}"
        return httpx.Response(200, json=_payload([item], total=101))

    with PpsCompanyAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        records = list(
            client.iter_company_awards(
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                business_number="1058201810",
                max_pages_per_window=2,
            )
        )

    assert pages == [1, 2]
    assert [record["title"] for record in records] == [
        "페이지 1 낙찰",
        "페이지 2 낙찰",
    ]
    assert client.hit_page_limit is False
    assert client.planned_window_count == 1
    assert client.attempted_window_count == 1
    assert client.successful_window_count == 1
    assert client.failed_window_count == 0


def test_company_award_client_completes_three_year_windows_with_bounded_workers() -> None:
    requests: list[httpx.Request] = []
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with state_lock:
            active -= 1
            requests.append(request)
        return httpx.Response(200, json=_payload([]))

    with PpsCompanyAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        records = list(
            client.iter_company_awards(
                start=date(2023, 8, 24),
                end=date(2026, 8, 24),
                business_number="1058201810",
                max_workers=8,
            )
        )

    assert records == []
    assert len(requests) == 40
    assert client.request_count == 40
    assert client.planned_window_count == 40
    assert client.attempted_window_count == 40
    assert client.successful_window_count == 40
    assert client.failed_window_count == 0
    assert 1 < maximum_active <= 8
    assert all(
        (
            datetime.strptime(request.url.params["inqryEndDt"][:8], "%Y%m%d").date()
            - datetime.strptime(request.url.params["inqryBgnDt"][:8], "%Y%m%d").date()
        ).days
        < 28
        for request in requests
    )


def test_company_award_client_keeps_successful_windows_when_one_window_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["inqryBgnDt"].startswith("20260801"):
            return httpx.Response(
                200,
                json={
                    "response": {
                        "header": {"resultCode": "07", "resultMsg": "temporary failure"},
                        "body": {"totalCount": 0, "items": []},
                    }
                },
            )
        return httpx.Response(200, json=_payload([_raw_award()]))

    with PpsCompanyAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        records = list(
            client.iter_company_awards(
                start=date(2026, 8, 1),
                end=date(2026, 8, 30),
                business_number="1058201810",
                max_workers=2,
            )
        )

    assert [record["title"] for record in records] == ["공공기관 리더십 교육"]
    assert client.failed_window_count == 1
    assert client.planned_window_count == 2
    assert client.attempted_window_count == 2
    assert client.successful_window_count == 1
    assert client.window_errors == ["RESULT_07"]


def test_company_award_client_clamps_request_timeout_to_remaining_wall() -> None:
    timeout_extensions: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout_extensions.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, json=_payload([]))

    deadline = time.monotonic() + 0.5
    with PpsCompanyAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        timeout_seconds=30,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert list(
            client.iter_company_awards(
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                business_number="1058201810",
                deadline_monotonic=deadline,
            )
        ) == []

    assert len(timeout_extensions) == 1
    assert all(
        0 < value <= 0.5
        for value in timeout_extensions[0].values()
    )


def test_company_award_server_budget_finishes_before_browser_timeout() -> None:
    frontend_source = (
        Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    timeout_match = re.search(
        r"const EXTERNAL_PPS_REQUEST_TIMEOUT_MS = (\d+);",
        frontend_source,
    )
    assert timeout_match is not None
    assert (
        int(timeout_match.group(1)) / 1_000
        == company_awards_api._SEARCH_CLIENT_BUDGET_SECONDS
    )
    assert company_awards_api._SEARCH_WALL_SECONDS == 55.0
    assert (
        company_awards_api._SEARCH_WALL_SECONDS
        + company_awards_api._SEARCH_HTTP_TIMEOUT_SECONDS
        + company_awards_api._SEARCH_RESPONSE_MARGIN_SECONDS
        <= company_awards_api._SEARCH_CLIENT_BUDGET_SECONDS
    )


class _FakeCompanyAwardClient:
    instances: list["_FakeCompanyAwardClient"] = []

    def __init__(self, **kwargs: object) -> None:
        assert kwargs["service_key"] == "server-side-pps-key"
        assert kwargs["timeout_seconds"] == 30.0
        assert kwargs["max_retries"] == 0
        self.request_count = 0
        self.hit_page_limit = False
        self.hit_time_limit = False
        self.provider_mismatch_count = 0
        self.__class__.instances.append(self)

    def __enter__(self) -> "_FakeCompanyAwardClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_company_awards(self, **kwargs: object):
        assert kwargs["business_number"] == "1058201810"
        assert kwargs["max_window_days"] == 28
        assert kwargs["max_workers"] == 8
        assert isinstance(kwargs["deadline_monotonic"], float)
        self.request_count += 1
        yield {
            "identity": "R26BK00000001|000|0|000",
            "scope": kwargs["scopes"][0],
            "bid_notice_no": "R26BK00000001",
            "revision_no": "000",
            "classification_no": "0",
            "rebid_no": "000",
            "title": "공공기관 리더십 교육",
            "participant_count": 3,
            "winner_name": "사단법인 한국능률협회",
            "award_amount": 120_000_000.0,
            "award_rate": 89.5,
            "opened_at": datetime.fromisoformat("2026-08-01T11:00:00+09:00"),
            "agency": "합성 발주기관",
            "registered_at": datetime.fromisoformat("2026-08-01T12:30:00+09:00"),
            "awarded_at": datetime.fromisoformat("2026-08-02T00:00:00+09:00"),
        }


def test_company_award_endpoint_requires_same_origin_and_scoped_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _FakeCompanyAwardClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        cross_origin = client.post(
            "/api/v1/company-awards/search",
            headers={**AUTH_HEADERS, "Origin": "https://attacker.test"},
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        )
        assert cross_origin.status_code == 403

        no_token = client.post(
            "/api/v1/company-awards/search",
            headers={"Origin": "https://testserver"},
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        )
        assert no_token.status_code == 401

        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["company"] == {
        "name": "사단법인 한국능률협회",
        "is_default": True,
    }
    assert body["count"] == 1
    assert body["api_calls"] == 1
    assert body["query"]["scopes"] == ["service"]
    assert "1058201810" not in response.text
    assert "server-side-pps-key" not in response.text
    assert TOKEN not in response.text


def test_company_award_endpoint_default_three_calendar_year_window_is_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _FakeCompanyAwardClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={},
        )
    assert response.status_code == 200, response.text
    assert response.json()["query"]["scopes"] == ["service"]


def test_company_award_endpoint_rejects_invalid_or_excessive_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        lambda **_kwargs: pytest.fail("PPS must not be called for rejected input"),
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        invalid_number = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={"business_number": "123"},
        )
        assert invalid_number.status_code == 422
        assert "123" not in invalid_number.text

        excessive = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={
                "start_date": "2025-08-23",
                "end_date": "2026-08-23",
                "scopes": ["goods", "construction", "service", "foreign"],
                "max_pages_per_window": 2,
            },
        )
        assert excessive.status_code == 422


def test_company_award_endpoint_requires_server_pps_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    app.state.settings = replace(app.state.settings, pps_api_key=None)
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        lambda **_kwargs: pytest.fail("client must not receive a missing key"),
    )
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        )
    assert response.status_code == 503


class _FailingCompanyAwardClient(_FakeCompanyAwardClient):
    def iter_company_awards(self, **kwargs: object):
        assert kwargs["max_workers"] == 8
        self.request_count += 2
        raise PpsApiError("provider detail that must remain server-side")
        yield  # pragma: no cover - preserve generator shape


def test_company_award_endpoint_returns_public_safe_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _FailingCompanyAwardClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "조달청 낙찰 API 응답이 지연되거나 일시적으로 실패했습니다. "
        "잠시 후 다시 시도해 주세요."
    )
    assert "provider detail" not in response.text


class _PartialCompanyAwardClient(_FakeCompanyAwardClient):
    def iter_company_awards(self, **kwargs: object):
        yield from super().iter_company_awards(**kwargs)
        self.failed_window_count = 1
        self.window_errors = ["NETWORK"]


def test_company_award_endpoint_marks_failed_window_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _PartialCompanyAwardClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={"start_date": "2026-07-01", "end_date": "2026-08-24"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["partial"] is True
    assert body["truncated"] is True
    assert any("1개 조회 기간이 실패" in warning for warning in body["warnings"])


class _PerScopeWindowStatsClient(_FakeCompanyAwardClient):
    def iter_company_awards(self, **kwargs: object):
        assert kwargs["max_workers"] == 8
        self.request_count += 1
        self.planned_window_count = 1
        self.attempted_window_count = 1
        self.window_errors = []
        scope = kwargs["scopes"][0]
        if scope == "service":
            self.successful_window_count = 0
            self.failed_window_count = 1
            self.window_errors = ["NETWORK"]
            return
        self.successful_window_count = 1
        self.failed_window_count = 0
        yield {
            "identity": "GOODS-1|000|0|000",
            "scope": scope,
            "bid_notice_no": "GOODS-1",
            "revision_no": "000",
            "classification_no": "0",
            "rebid_no": "000",
            "title": "물품 낙찰 성공 창",
            "participant_count": 1,
            "winner_name": "사단법인 한국능률협회",
            "award_amount": 1.0,
            "award_rate": 1.0,
            "opened_at": None,
            "agency": "합성 발주기관",
            "registered_at": None,
            "awarded_at": None,
        }


def test_company_award_endpoint_uses_per_scope_window_stats_without_negative_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _PerScopeWindowStatsClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
                "scopes": ["service", "goods"],
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["partial"] is True
    assert body["truncated"] is True
    assert any("service: 1개 조회 기간이 실패" in item for item in body["warnings"])
    assert all("-1개" not in item for item in body["warnings"])


def test_company_award_endpoint_returns_502_when_every_window_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _PerScopeWindowStatsClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
                "scopes": ["service"],
            },
        )

    assert response.status_code == 502


def test_company_award_endpoint_is_hidden_when_manual_feature_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    app.state.settings = replace(
        app.state.settings,
        public_manual_analysis_enabled=False,
    )
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        lambda **_kwargs: pytest.fail("disabled feature must not call PPS"),
    )
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        )
    assert response.status_code == 404


class _TimeLimitedCompanyAwardClient(_FakeCompanyAwardClient):
    def iter_company_awards(self, **kwargs: object):
        assert isinstance(kwargs["deadline_monotonic"], float)
        self.request_count += 1
        self.hit_time_limit = True
        if False:  # pragma: no cover - preserve generator shape
            yield {}


def test_company_award_endpoint_marks_wall_time_limit_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _TimeLimitedCompanyAwardClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
                "scopes": ["service", "goods"],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is True
    assert body["truncated"] is True
    assert body["api_calls"] == 1
    assert any("조회시간 제한" in warning for warning in body["warnings"])


class _ManyCompanyAwardClient(_FakeCompanyAwardClient):
    def iter_company_awards(self, **kwargs: object):
        assert isinstance(kwargs["deadline_monotonic"], float)
        self.request_count += 1
        scope = kwargs["scopes"][0]
        for index in range(501):
            yield {
                "identity": f"CAP-{index}|000|0|000",
                "scope": scope,
                "bid_notice_no": f"CAP-{index}",
                "revision_no": "000",
                "classification_no": "0",
                "rebid_no": "000",
                "title": f"응답 상한 테스트 {index}",
                "participant_count": 1,
                "winner_name": "사단법인 한국능률협회",
                "award_amount": 1.0,
                "award_rate": 1.0,
                "opened_at": None,
                "agency": "합성 발주기관",
                "registered_at": None,
                "awarded_at": None,
            }


def test_company_award_endpoint_caps_unique_response_records_at_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pai_loop.company_awards.PpsCompanyAwardClient",
        _ManyCompanyAwardClient,
    )
    with TestClient(_app(), base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/company-awards/search",
            headers=AUTH_HEADERS,
            json={"start_date": "2026-08-01", "end_date": "2026-08-01"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 500
    assert len(body["records"]) == 500
    assert body["partial"] is True
    assert body["truncated"] is True
    assert any("최대 500건" in warning for warning in body["warnings"])
