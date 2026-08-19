from __future__ import annotations

from datetime import date

import httpx

from pai_loop.integrations.prespec import (
    PpsPreSpecificationClient,
    matched_pre_specification_keywords,
    normalise_pre_specification,
)


SAFE_DOCUMENT = (
    "https://www.g2b.go.kr/pn/pnz/pnza/UntyAtchFile/downloadFile.do"
    "?bfSpecRegNo=R26BD00261492&fileType=BFDTL&fileSeq=1"
)


def _provider_item() -> dict:
    return {
        "bsnsDivNm": "일반용역",
        "refNo": "합성-1",
        "prdctClsfcNoNm": "2026년 공무원 역량강화 연수 위탁 운영",
        "orderInsttNm": "가상 발주기관",
        "rlDminsttNm": "가상 수요기관",
        "asignBdgtAmt": "118,628,232",
        "rcptDt": "2026-08-12 08:18:23",
        "opninRgstClseDt": "2026-08-18 23:59:00",
        "ofclTelNo": "010-0000-0000",
        "ofclNm": "저장 금지 담당자",
        "swBizObjYn": "N",
        "dlvrTmlmtDt": "2026-12-12 00:00:00",
        "bfSpecRgstNo": "R26BD00261492",
        "specDocFileUrl1": SAFE_DOCUMENT,
        "specDocFileUrl2": "https://evil.example/file.pdf",
        "rgstDt": "2026-08-12 08:18:21",
        "chgDt": "2026-08-12 08:18:23",
        "bidNtceNoList": "R26BK01678610, R26BK01678610 | R26BK01682487",
    }


def test_prespec_normalisation_is_separate_public_allowlist() -> None:
    record = normalise_pre_specification(_provider_item())

    assert record["source_kind"] == "PPS_PRESPEC"
    assert record["pre_specification_key"] == "PRESPEC-R26BD00261492"
    assert record["title"] == "2026년 공무원 역량강화 연수 위탁 운영"
    assert record["budget_amount"] == 118_628_232
    assert record["opinion_deadline"].isoformat() == "2026-08-18T23:59:00+09:00"
    assert record["document_urls"] == [
        "https://www.g2b.go.kr/pn/pnz/pnza/UntyAtchFile/downloadFile.do"
        "?bfSpecRegNo=R26BD00261492&fileSeq=1&fileType=BFDTL"
    ]
    assert record["linked_bid_notice_nos"] == ["R26BK01678610", "R26BK01682487"]
    assert "ofclNm" not in record
    assert "ofclTelNo" not in record
    assert "저장 금지 담당자" not in repr(record)
    assert "010-0000-0000" not in repr(record)


def test_prespec_keyword_matching_is_explainable_and_local() -> None:
    record = normalise_pre_specification(_provider_item())
    assert matched_pre_specification_keywords(
        record,
        ["교육", "연수", "포럼", "위탁 운영", "연수"],
    ) == ["연수", "위탁 운영"]


def test_prespec_client_uses_official_registration_window_and_bounded_pages() -> None:
    seen_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_pages.append(request.url.params["pageNo"])
        assert request.url.path.endswith(
            "/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc"
        )
        assert request.url.params["inqryDiv"] == "1"
        assert request.url.params["inqryBgnDt"] == "202608120000"
        assert request.url.params["inqryEndDt"] == "202608192359"
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "정상"},
                    "body": {
                        "totalCount": 2,
                        "items": [_provider_item()],
                    },
                }
            },
        )

    with PpsPreSpecificationClient(
        service_key="safe-key",
        base_url="https://example.test/1230000",
        transport=httpx.MockTransport(handler),
    ) as client:
        records = list(
            client.iter_pre_specifications(
                start=date(2026, 8, 12),
                end=date(2026, 8, 19),
                rows=1,
                max_pages=1,
            )
        )

    assert len(records) == 1
    assert seen_pages == ["1"]
    assert client.hit_page_limit is True
    assert client.request_count == 1


def test_prespec_client_quarantines_rows_without_identity_or_title() -> None:
    payload = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {
                "totalCount": 2,
                "items": [
                    {"bfSpecRgstNo": "R26BD00000001"},
                    {"prdctClsfcNoNm": "식별자 없는 사전규격"},
                ],
            },
        }
    }
    with PpsPreSpecificationClient(
        service_key="safe-key",
        base_url="https://example.test/1230000",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
    ) as client:
        records = list(
            client.iter_pre_specifications(
                start=date(2026, 8, 19),
                end=date(2026, 8, 19),
            )
        )
    assert records == []
