from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

DEFAULT_BASE_URL = "https://apis.data.go.kr/1230000"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
SECRET_QUERY_KEYS = {"servicekey", "apikey", "api_key", "key"}
# Korea has no daylight-saving transition in the procurement periods handled
# by this service; a fixed UTC+09:00 offset also works on Windows without the
# optional IANA tzdata package.
KST = timezone(timedelta(hours=9), name="Asia/Seoul")


class PpsApiError(RuntimeError):
    """A public-safe PPS API error that never includes a credential value."""


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: date
    end: date


def split_date_range(start: date, end: date, *, max_days: int = 30) -> list[DateWindow]:
    if end < start:
        raise ValueError("end must be on or after start")
    if max_days < 1:
        raise ValueError("max_days must be positive")
    windows: list[DateWindow] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=max_days - 1), end)
        windows.append(DateWindow(cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    redacted = [
        (key, "***" if key.casefold() in SECRET_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = re.sub(r"[^0-9]", "", str(value))
    for pattern, size in (("%Y%m%d%H%M%S", 14), ("%Y%m%d%H%M", 12), ("%Y%m%d", 8)):
        if len(text) == size:
            # 나라장터의 숫자형 공고/마감 시각은 한국 표준시 기준이다.
            return datetime.strptime(text, pattern).replace(tzinfo=KST)
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def normalise_notice(item: dict[str, Any]) -> dict[str, Any]:
    """Map BidPublicInfoService02-style fields to the canonical notice input."""
    notice_no = str(item.get("bidNtceNo") or item.get("bid_notice_no") or "").strip()
    revision = str(item.get("bidNtceOrd") or item.get("revision_no") or "00").zfill(2)
    deadline = _parse_datetime(item.get("bidClseDt") or item.get("deadline"))
    published = _parse_datetime(item.get("bidNtceDt") or item.get("published_at"))
    identity = f"{notice_no}|{revision}|{deadline.isoformat() if deadline else ''}"
    return {
        "identity": identity,
        "bid_notice_no": notice_no,
        "revision_no": revision,
        "title": str(item.get("bidNtceNm") or item.get("title") or "").strip(),
        "agency": str(
            item.get("ntceInsttNm") or item.get("dminsttNm") or item.get("agency") or ""
        ).strip(),
        "published_at": published,
        "deadline": deadline,
        "estimated_amount": _number(
            item.get("presmptPrce") or item.get("asignBdgtAmt") or item.get("estimated_amount")
        ),
        "notice_kind": str(item.get("ntceKindNm") or item.get("notice_kind") or "").strip(),
        "source_url": (
            item.get("bidNtceDtlUrl")
            or item.get("bidNtceUrl")
            or item.get("source_url")
        ),
        "raw": item,
    }


def parse_paged_response(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Validate the common PPS envelope and return object items plus total count."""

    if "OpenAPI_ServiceResponse" in payload:
        envelope = payload.get("OpenAPI_ServiceResponse")
        header = envelope.get("cmmMsgHeader", {}) if isinstance(envelope, dict) else {}
        message = (
            header.get("errMsg")
            or header.get("returnAuthMsg")
            or "OpenAPI service error"
        ) if isinstance(header, dict) else "OpenAPI service error"
        raise PpsApiError(f"PPS API service error: {str(message)[:300]}")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise PpsApiError("PPS API 표준 response envelope가 없습니다.")
    header = response.get("header")
    body = response.get("body")
    if not isinstance(header, dict) or "resultCode" not in header:
        raise PpsApiError("PPS API response.header/resultCode가 없습니다.")
    if not isinstance(body, dict):
        raise PpsApiError("PPS API response.body가 없습니다.")
    result_code = str(header["resultCode"])
    if result_code not in {"0", "00"}:
        message = str(header.get("resultMsg", "unknown PPS error"))[:300]
        raise PpsApiError(f"PPS API resultCode={result_code}: {message}")
    if "totalCount" not in body:
        raise PpsApiError("PPS API response.body/totalCount가 없습니다.")
    item_container = body.get("items", {})
    if isinstance(item_container, list):
        raw_items = item_container
    elif isinstance(item_container, dict):
        raw_items = item_container.get("item", [])
    else:
        raw_items = []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raise PpsApiError("PPS API items.item 형식이 배열이 아닙니다.")
    items = [item for item in raw_items if isinstance(item, dict)]
    try:
        total = int(body["totalCount"] or 0)
    except (TypeError, ValueError) as exc:
        raise PpsApiError("PPS API totalCount가 숫자가 아닙니다.") from exc
    return items, total


class PpsClient:
    """Small, bounded PPS client for worker-side use.

    n8n is the production scheduler. This class keeps date splitting, paging,
    retry, normalisation, and credential redaction in a unit-testable boundary.
    The operation path is deliberately explicit because approved PPS services
    can expose different operation names and field sets.
    """

    def __init__(
        self,
        *,
        service_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 20,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not service_key.strip():
            raise ValueError("service_key is required")
        # data.go.kr exposes both decoded and percent-encoded key variants.
        # Normalise exactly once so httpx performs the only query encoding.
        self._service_key = unquote(service_key)
        self._max_retries = max_retries
        self._sleep = sleep
        self.request_count = 0
        self.hit_page_limit = False
        self.hit_time_limit = False
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PpsClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(self, operation_path: str, params: dict[str, Any]) -> dict[str, Any]:
        request_params = {**params, "serviceKey": self._service_key, "type": "json"}
        for attempt in range(self._max_retries + 1):
            try:
                self.request_count += 1
                response = self._client.get(operation_path.lstrip("/"), params=request_params)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise PpsApiError("PPS API 네트워크 요청이 실패했습니다.") from exc
                self._sleep(0.25 * (2**attempt))
                continue
            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self._sleep(0.25 * (2**attempt))
                continue
            if response.status_code >= 400:
                safe_url = redact_url(str(response.request.url))
                raise PpsApiError(f"PPS API HTTP {response.status_code}: {safe_url}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise PpsApiError("PPS API가 JSON이 아닌 응답을 반환했습니다.") from exc
            if not isinstance(payload, dict):
                raise PpsApiError("PPS API 응답 형식이 객체가 아닙니다.")
            return payload
        raise AssertionError("unreachable")

    def iter_notices(
        self,
        *,
        operation_path: str,
        start: date,
        end: date,
        rows: int = 100,
        max_window_days: int = 30,
        inquiry_division: str = "1",
        max_pages: int | None = None,
        extra_params: dict[str, Any] | None = None,
        deadline_monotonic: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not 1 <= rows <= 999:
            raise ValueError("rows must be between 1 and 999")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.hit_page_limit = False
        self.hit_time_limit = False
        for window in split_date_range(start, end, max_days=max_window_days):
            page = 1
            while True:
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    self.hit_time_limit = True
                    return
                payload = self._request(
                    operation_path,
                    {
                        **(extra_params or {}),
                        # getBidPblancListInfoServcPPSSrch requires inqryDiv=1
                        # for date-range searching; absence can yield a nonstandard
                        # error envelope that must never look like an empty result.
                        "inqryDiv": inquiry_division,
                        "inqryBgnDt": window.start.strftime("%Y%m%d0000"),
                        "inqryEndDt": window.end.strftime("%Y%m%d2359"),
                        "pageNo": page,
                        "numOfRows": rows,
                    },
                )
                raw_items, total = parse_paged_response(payload)
                for item in raw_items:
                    yield normalise_notice(item)
                if page * rows >= total or not raw_items:
                    break
                if max_pages is not None and page >= max_pages:
                    self.hit_page_limit = True
                    break
                page += 1
