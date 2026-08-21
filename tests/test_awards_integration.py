from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from pai_loop.api import _award_similarity
from pai_loop.integrations import awards as awards_module
from pai_loop.integrations.awards import (
    DEFAULT_AWARD_OPERATION,
    PpsAwardClient,
    normalise_award,
)
from pai_loop.integrations.pps import DateWindow, PpsApiError
from pai_loop.main import create_app


def _award_payload(*, title: str = "승진후보자 역량 교육") -> dict[str, object]:
    return {
        "bidNtceNo": "AWARD-001",
        "bidNtceOrd": "1",
        "bidClsfcNo": "0",
        "rbidNo": "0",
        "bidNtceNm": title,
        "prtcptCnum": "4",
        "bidwinnrNm": "합성 수주기관",
        "sucsfbidAmt": "98,000,000",
        "sucsfbidRate": "88.125",
        "rlOpengDt": "202508161100",
        "dminsttNm": "합성 발주기관",
        "fnlSucsfDate": "20250817",
        # These provider fields must never cross the normalisation boundary.
        "bidwinnrBizno": "PRIVATE-ID",
        "bidwinnrCeoNm": "PRIVATE-PERSON",
        "bidwinnrAdrs": "PRIVATE-ADDRESS",
        "bidwinnrTelNo": "PRIVATE-CONTACT",
        "ofclNm": "PRIVATE-OFFICIAL",
    }


def test_award_normalisation_keeps_public_business_facts_and_drops_pii() -> None:
    item = normalise_award(_award_payload())
    assert item["identity"] == "AWARD-001|001|0|000"
    assert item["participant_count"] == 4
    assert item["award_amount"] == 98_000_000
    assert item["award_rate"] == 88.125
    assert item["opened_at"].isoformat() == "2025-08-16T11:00:00+09:00"
    assert item["awarded_at"].isoformat() == "2025-08-17T00:00:00+09:00"
    serialised = str(item)
    for private_value in (
        "PRIVATE-ID",
        "PRIVATE-PERSON",
        "PRIVATE-ADDRESS",
        "PRIVATE-CONTACT",
        "PRIVATE-OFFICIAL",
    ):
        assert private_value not in serialised


def test_award_normalisation_rejects_non_integer_participant_count() -> None:
    assert normalise_award({})["participant_count"] is None
    assert normalise_award({"prtcptCnum": "not-an-integer"})["participant_count"] is None


def test_korean_compound_title_similarity_uses_core_phrase_not_only_token_boundaries() -> None:
    target = "2026년 7급 승진후보자 역량 위탁운영 용역"
    compound = "2025년 5급승진후보자 역량평가 과제개발 및 평가운영"
    unrelated = "청사 시설물 정기 안전점검"
    assert _award_similarity(target, target) == 100
    assert _award_similarity(target, compound) > _award_similarity(target, unrelated)
    assert _award_similarity(target, compound) > 0


def test_award_client_uses_bounded_windows_keyword_and_current_array_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["inqryDiv"] == "1"
        assert request.url.params["bidNtceNm"] == "승진후보자"
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {"totalCount": 1, "items": [_award_payload()]},
                }
            },
        )

    with PpsAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        items = list(
            client.iter_awards(
                start=date(2025, 1, 1),
                end=date(2025, 2, 2),
                keyword="승진후보자",
                rows=100,
            )
        )
    assert len(requests) == 2
    assert requests[0].url.params["inqryBgnDt"] == "202501010000"
    assert requests[0].url.params["inqryEndDt"] == "202501302359"
    assert requests[1].url.params["inqryBgnDt"] == "202501310000"
    assert len(items) == 2
    assert all(item["winner_name"] == "합성 수주기관" for item in items)


def test_award_client_supports_legacy_wrapper_and_page_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["pageNo"]
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {
                        "totalCount": 9,
                        "items": {"item": [_award_payload(title=f"승진후보자 역량 교육 {page}")]},
                    },
                }
            },
        )

    client = PpsAwardClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    items = list(
        client.iter_awards(
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            keyword="승진후보자",
            rows=1,
            max_pages_per_window=2,
        )
    )
    client.close()
    assert len(items) == 2
    assert client.request_count == 2
    assert client.hit_page_limit is True


def test_award_client_retries_nonstandard_large_window_in_seven_day_slices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["inqryBgnDt"] == "202501010000" and request.url.params["inqryEndDt"] == "202501302359":
            return httpx.Response(200, json={"unexpected": "shape"})
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {"totalCount": 1, "items": [_award_payload()]},
                }
            },
        )

    client = PpsAwardClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    items = list(
        client.iter_awards(
            start=date(2025, 1, 1),
            end=date(2025, 1, 30),
            keyword="승진후보자",
        )
    )
    client.close()
    assert len(items) == 5
    assert client.request_count == 6
    assert client.fallback_window_count == 1
    assert client.window_errors == []


def test_award_client_records_unrecoverable_subwindows_when_partial_is_allowed() -> None:
    client = PpsAwardClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"unexpected": "shape"})
        ),
    )
    items = list(
        client.iter_awards(
            start=date(2025, 1, 1),
            end=date(2025, 1, 30),
            keyword="승진후보자",
            continue_on_window_error=True,
        )
    )
    client.close()
    assert items == []
    assert client.fallback_window_count == 1
    assert len(client.window_errors) == 5


def test_award_client_returns_partial_without_call_after_wall_deadline() -> None:
    client = PpsAwardClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("request must not start after deadline")
        ),
    )
    items = list(
        client.iter_awards(
            start=date(2025, 1, 1),
            end=date(2025, 1, 30),
            keyword="승진후보자",
            deadline_monotonic=0,
        )
    )
    client.close()
    assert items == []
    assert client.hit_time_limit is True


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"keyword": "   "}, "keyword is required"),
        ({"rows": 0}, "rows must be between 1 and 999"),
        ({"max_pages_per_window": 0}, "max_pages_per_window must be positive"),
        (
            {"max_window_days": 30, "fallback_window_days": 30},
            "fallback_window_days must be shorter than max_window_days",
        ),
    ],
)
def test_award_client_rejects_invalid_iteration_bounds(
    overrides: dict[str, object],
    message: str,
) -> None:
    client = PpsAwardClient(service_key="key", base_url="https://example.test")
    kwargs: dict[str, object] = {
        "start": date(2025, 1, 1),
        "end": date(2025, 1, 1),
        "keyword": "valid keyword",
    }
    kwargs.update(overrides)
    with pytest.raises(ValueError, match=message):
        list(client.iter_awards(**kwargs))
    client.close()


def test_award_window_stops_before_request_when_deadline_has_elapsed() -> None:
    client = PpsAwardClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("request must not start after deadline")
        ),
    )
    items = client._fetch_window(
        window=DateWindow(start=date(2025, 1, 1), end=date(2025, 1, 1)),
        keyword="keyword",
        operation_path=DEFAULT_AWARD_OPERATION,
        rows=1,
        max_pages=1,
        deadline_monotonic=0,
    )
    client.close()
    assert items == []
    assert client.hit_time_limit is True


def test_award_client_skips_title_mismatch_and_stops_on_empty_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pageNo"])
        items = [_award_payload(title="unrelated title")] if page == 1 else []
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {"totalCount": 2, "items": items},
                }
            },
        )

    client = PpsAwardClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    items = list(
        client.iter_awards(
            start=date(2025, 1, 1),
            end=date(2025, 1, 1),
            keyword="wanted keyword",
            rows=1,
            max_pages_per_window=2,
        )
    )
    client.close()
    assert items == []
    assert client.request_count == 2
    assert client.hit_page_limit is False


def test_award_client_raises_for_unrecoverable_subwindow_by_default() -> None:
    client = PpsAwardClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"unexpected": "shape"})
        ),
    )
    with pytest.raises(PpsApiError):
        list(
            client.iter_awards(
                start=date(2025, 1, 1),
                end=date(2025, 1, 30),
                keyword="keyword",
            )
        )
    client.close()


def test_award_fallback_honours_deadline_before_first_subwindow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PpsAwardClient(service_key="key", base_url="https://example.test")

    def fail_window(**_kwargs: object) -> list[dict[str, object]]:
        raise PpsApiError("synthetic envelope failure")

    monotonic_values = iter((0.0, 2.0))
    monkeypatch.setattr(client, "_fetch_window", fail_window)
    monkeypatch.setattr(awards_module.time, "monotonic", lambda: next(monotonic_values))
    items = list(
        client.iter_awards(
            start=date(2025, 1, 1),
            end=date(2025, 1, 30),
            keyword="keyword",
            deadline_monotonic=1.0,
        )
    )
    client.close()
    assert items == []
    assert client.hit_time_limit is True


class _FakeAwardClient:
    def __init__(self, **_kwargs: object) -> None:
        self.request_count = 2
        self.hit_page_limit = False
        self.fallback_window_count = 0
        self.window_errors: list[str] = []

    def __enter__(self) -> "_FakeAwardClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_awards(self, **kwargs: object):
        assert kwargs["keyword"] == "7급 승진후보자 역량"
        yield {
            "identity": "AWARD-001|001|0|000",
            "bid_notice_no": "AWARD-001",
            "revision_no": "001",
            "title": "7급 승진후보자 역량 강화 교육",
            "agency": "합성 발주기관",
            "winner_name": "합성 수주기관",
            "participant_count": 4,
            "award_amount": 98_000_000.0,
            "award_rate": 88.125,
            "opened_at": datetime.fromisoformat("2025-08-16T11:00:00+09:00"),
            "awarded_at": datetime.fromisoformat("2025-08-17T00:00:00+09:00"),
            "private_person": "MUST-NOT-PERSIST",
        }
        yield {
            "identity": "AWARD-QUARANTINE",
            "bid_notice_no": "AWARD-002",
            "revision_no": "001",
            "title": "7급 승진후보자 역량 교육",
            "winner_name": "",
        }


class _BrokenAwardClient(_FakeAwardClient):
    def iter_awards(self, **_kwargs: object):
        raise RuntimeError("synthetic client failure")
        yield  # pragma: no cover


def _create_award_target(client: TestClient) -> None:
    response = client.post(
        "/api/v1/notices",
        json={
            "notice_key": "PUBLIC-AWARD-TARGET",
            "bid_notice_no": "TARGET-001",
            "title": "2026년 7급 승진후보자 역량 위탁운영 용역",
            "agency": "합성 기관",
            "published_at": "2026-01-15T09:00:00+09:00",
            "deadline": "2026-02-01T17:00:00+09:00",
        },
    )
    assert response.status_code == 201


def test_award_history_refresh_is_idempotent_and_visible_in_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _FakeAwardClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        _create_award_target(client)
        first = client.post(
            "/api/v1/notices/PUBLIC-AWARD-TARGET/award-history/refresh",
            json={"years": 3},
        )
        assert first.status_code == 200
        body = first.json()
        assert body["status"] == "COMPLETED"
        assert body["keyword"] == "7급 승진후보자 역량"
        assert body["window"] == {"from": "2023-01-15", "to": "2026-01-15"}
        assert body["created"] == 1
        assert body["records"] == 1

        second = client.post(
            "/api/v1/notices/PUBLIC-AWARD-TARGET/award-history/refresh",
            json={"years": 3},
        )
        assert second.status_code == 200
        assert second.json()["created"] == 0
        assert second.json()["duplicates"] == 1

        history = client.get(
            "/api/v1/notices/PUBLIC-AWARD-TARGET/award-history"
        )
        assert history.status_code == 200
        assert len(history.json()) == 1
        assert history.json()[0]["winner_name"] == "합성 수주기관"
        assert history.json()[0]["similarity_score"] > 0

        detail = client.get("/api/v1/notices/PUBLIC-AWARD-TARGET")
        assert len(detail.json()["award_history"]) == 1
        serialised = str(detail.json())
        assert "MUST-NOT-PERSIST" not in serialised
        assert "server-side-key" not in serialised


def test_award_history_dry_run_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _FakeAwardClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        _create_award_target(client)
        response = client.post(
            "/api/v1/notices/PUBLIC-AWARD-TARGET/award-history/refresh",
            json={"years": 1, "dry_run": True},
        )
        assert response.status_code == 200
        assert response.json()["created"] == 1
        assert client.get(
            "/api/v1/notices/PUBLIC-AWARD-TARGET/award-history"
        ).json() == []


def test_award_history_requires_server_side_pps_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PPS_API_KEY", raising=False)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        _create_award_target(client)
        response = client.post(
            "/api/v1/notices/PUBLIC-AWARD-TARGET/award-history/refresh",
            json={"keyword": "승진후보자"},
        )
    assert response.status_code == 503


def test_award_history_unexpected_client_error_finishes_failed_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _BrokenAwardClient)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        _create_award_target(client)
        response = client.post(
            "/api/v1/notices/PUBLIC-AWARD-TARGET/award-history/refresh",
            json={"years": 1},
        )
        assert response.status_code == 500
        jobs = client.get("/api/v1/ingestion/jobs").json()
    award_job = next(item for item in jobs if item["source"] == "PPS_AWARD")
    assert award_job["status"] == "FAILED"
    assert award_job["error_code"] == "PPS_AWARD_CLIENT_ERROR"
    assert award_job["completed_at"] is not None
