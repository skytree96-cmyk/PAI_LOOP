from __future__ import annotations

from datetime import date

import httpx
import pytest

from pai_loop.integrations.pps import PpsApiError, PpsClient, normalise_notice, redact_url, split_date_range


def test_date_windows_are_inclusive_and_non_overlapping() -> None:
    windows = split_date_range(date(2026, 1, 1), date(2026, 3, 5), max_days=30)
    assert [(item.start.isoformat(), item.end.isoformat()) for item in windows] == [
        ("2026-01-01", "2026-01-30"),
        ("2026-01-31", "2026-03-01"),
        ("2026-03-02", "2026-03-05"),
    ]


def test_notice_normalisation_handles_pps_fields() -> None:
    item = normalise_notice(
        {
            "bidNtceNo": "20260816001",
            "bidNtceOrd": "1",
            "bidNtceNm": "가상 용역",
            "ntceInsttNm": "가상 기관",
            "bidNtceDt": "202608161000",
            "bidClseDt": "202608201700",
            "presmptPrce": "120,000,000",
        }
    )
    assert item["revision_no"] == "01"
    assert item["deadline"].isoformat() == "2026-08-20T17:00:00+09:00"
    assert item["deadline_basis"] == "BID_CLOSE"
    assert item["estimated_amount"] == 120_000_000


@pytest.mark.parametrize(
    ("title", "opening"),
    [
        (
            "2026년 의령군 공무원 역량강화교육 용역(협상에 의한 계약)",
            "2026-08-24 18:00:00",
        ),
        ("2026년 수성구청 직원 YES수성 연수 위탁 용역", "2026-08-27 18:00:00"),
    ],
)
def test_direct_bid_without_electronic_close_uses_explicit_opening_fallback(
    title: str,
    opening: str,
) -> None:
    item = normalise_notice(
        {
            "bidNtceNo": "R26BK01678610",
            "bidNtceOrd": "000",
            "bidNtceNm": title,
            "bidNtceDt": "2026-08-12 11:43:20",
            "bidClseDt": "",
            "opengDt": opening,
            "bidMethdNm": "직찰",
            "cntrctCnclsMthdNm": "일반경쟁",
        }
    )

    assert item["deadline"] is not None
    assert item["deadline"].isoformat() == opening.replace(" ", "T") + "+09:00"
    assert item["deadline_basis"] == "OPENING_FALLBACK"
    assert item["direct_contract_signal"] is False


def test_missing_close_does_not_use_opening_for_non_direct_bid() -> None:
    item = normalise_notice(
        {
            "bidNtceNo": "R26BK-NON-DIRECT",
            "bidNtceOrd": "000",
            "bidNtceNm": "전자입찰 마감 누락 합성 공고",
            "bidClseDt": "",
            "opengDt": "2026-08-27 18:00:00",
            "bidMethdNm": "전자입찰",
            "cntrctCnclsMthdNm": "제한경쟁",
        }
    )

    assert item["deadline"] is None
    assert item["deadline_basis"] is None


def test_notice_normalisation_keeps_contract_signals_without_filtering_direct_work() -> None:
    direct = normalise_notice(
        {
            "bidNtceNo": "DIRECT-1",
            "bidNtceOrd": "0",
            "bidNtceNm": "교육 프로그램 소액수의 안내",
            "bidClseDt": "202608201700",
            "bidMethdNm": "전자견적",
            "cntrctCnclsMthdNm": "수의계약",
            "sucsfbidMthdNm": "최저가",
        }
    )

    assert direct["bid_method"] == "전자견적"
    assert direct["contract_method"] == "수의계약"
    assert direct["award_method"] == "최저가"
    assert direct["direct_contract_signal"] is True

    authoritative_competitive = normalise_notice(
        {
            "bidNtceNo": "COMPETITIVE-1",
            "bidNtceOrd": "0",
            "bidNtceNm": "수의계약 사례 교육",
            "bidClseDt": "202608201700",
            "cntrctCnclsMthdNm": "일반경쟁",
        }
    )
    assert authoritative_competitive["direct_contract_signal"] is False


def test_notice_normalisation_canonicalises_cancellation_variants() -> None:
    cancelled = normalise_notice(
        {
            "bidNtceNo": "CANCEL-1",
            "bidNtceOrd": "1",
            "bidNtceNm": "취소 대상 공고",
            "bidClseDt": "202608201700",
            "ntceKindNm": "등록공고(취소)",
        }
    )

    assert cancelled["notice_kind"] == "취소공고"


def test_paging_and_secret_redaction() -> None:
    seen_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_pages.append(request.url.params["pageNo"])
        assert request.url.params["inqryDiv"] == "1"
        page = int(request.url.params["pageNo"])
        item = {
            "bidNtceNo": f"N-{page}",
            "bidNtceOrd": "0",
            "bidNtceNm": f"notice {page}",
            "bidClseDt": "202608201700",
        }
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {"totalCount": 2, "items": {"item": [item]}},
                }
            },
        )

    with PpsClient(service_key="SUPER-SECRET", base_url="https://example.test", transport=httpx.MockTransport(handler)) as client:
        items = list(
            client.iter_notices(
                operation_path="BidPublicInfoService02/example",
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
                rows=1,
            )
        )
    assert seen_pages == ["1", "2"]
    assert [item["bid_notice_no"] for item in items] == ["N-1", "N-2"]
    assert "SUPER-SECRET" not in redact_url("https://x.test?q=1&serviceKey=SUPER-SECRET")


def test_http_error_never_leaks_service_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = PpsClient(
        service_key="DO-NOT-LEAK",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    with pytest.raises(PpsApiError) as captured:
        list(
            client.iter_notices(
                operation_path="example",
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
            )
        )
    client.close()
    assert "DO-NOT-LEAK" not in str(captured.value)
    assert "serviceKey=%2A%2A%2A" in str(captured.value)


def test_encoded_data_go_kr_key_is_decoded_once_before_http_encoding() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["serviceKey"] == "abc+/="
        return httpx.Response(
            200,
            json={"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 0, "items": []}}},
        )

    client = PpsClient(
        service_key="abc%2B%2F%3D",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    assert list(
        client.iter_notices(operation_path="example", start=date(2026, 8, 1), end=date(2026, 8, 1))
    ) == []
    client.close()


def test_current_pps_direct_items_array_shape_is_supported() -> None:
    payload = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {
                "totalCount": 1,
                "items": [
                    {
                        "bidNtceNo": "LIVE-SHAPE-1",
                        "bidNtceOrd": "00",
                        "bidNtceNm": "합성 현재 응답 형식",
                        "bidClseDt": "202608201700",
                    }
                ],
            },
        }
    }
    client = PpsClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )
    items = list(
        client.iter_notices(operation_path="example", start=date(2026, 8, 1), end=date(2026, 8, 1))
    )
    client.close()
    assert len(items) == 1
    assert items[0]["bid_notice_no"] == "LIVE-SHAPE-1"


def test_page_limit_bounds_live_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["pageNo"]
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {
                        "totalCount": 99,
                        "items": [
                            {
                                "bidNtceNo": f"LIMIT-{page}",
                                "bidNtceOrd": "00",
                                "bidNtceNm": "bounded notice",
                                "bidClseDt": "202608201700",
                            }
                        ],
                    },
                }
            },
        )

    client = PpsClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    items = list(
        client.iter_notices(
            operation_path="example",
            start=date(2026, 8, 1),
            end=date(2026, 8, 1),
            rows=1,
            max_pages=2,
        )
    )
    client.close()
    assert len(items) == 2
    assert client.request_count == 2
    assert client.hit_page_limit is True


@pytest.mark.parametrize(
    "payload",
    [
        {
            "OpenAPI_ServiceResponse": {
                "cmmMsgHeader": {
                    "errMsg": "SERVICE ERROR",
                    "returnAuthMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
                }
            }
        },
        {"unexpected": {"totalCount": 0}},
        {"response": {"header": {}, "body": {"totalCount": 0, "items": []}}},
        {"response": {"header": {"resultCode": "00"}, "body": {"items": []}}},
    ],
)
def test_nonstandard_or_incomplete_envelope_never_silently_becomes_zero_results(payload: dict) -> None:
    client = PpsClient(
        service_key="key",
        base_url="https://example.test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(PpsApiError):
        list(
            client.iter_notices(
                operation_path="getBidPblancListInfoServcPPSSrch",
                start=date(2026, 8, 1),
                end=date(2026, 8, 1),
            )
        )
    client.close()
