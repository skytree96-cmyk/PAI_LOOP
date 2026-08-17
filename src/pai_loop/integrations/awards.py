from __future__ import annotations

import time
from datetime import date
from typing import Any, Iterator

from .pps import (
    DEFAULT_BASE_URL,
    DateWindow,
    PpsApiError,
    PpsClient,
    _number,
    _parse_datetime,
    parse_paged_response,
    split_date_range,
)

DEFAULT_AWARD_OPERATION = "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch"


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None


def normalise_award(item: dict[str, Any]) -> dict[str, Any]:
    """Retain public business facts while discarding people/contact/identifier fields."""

    notice_no = str(item.get("bidNtceNo") or "").strip()
    revision = str(item.get("bidNtceOrd") or "000").zfill(3)
    classification = str(item.get("bidClsfcNo") or "0")
    rebid = str(item.get("rbidNo") or "000").zfill(3)
    return {
        "identity": f"{notice_no}|{revision}|{classification}|{rebid}",
        "bid_notice_no": notice_no,
        "revision_no": revision,
        "classification_no": classification,
        "rebid_no": rebid,
        "title": str(item.get("bidNtceNm") or "").strip(),
        "participant_count": _integer(item.get("prtcptCnum")),
        "winner_name": str(item.get("bidwinnrNm") or "").strip(),
        "award_amount": _number(item.get("sucsfbidAmt")),
        "award_rate": _number(item.get("sucsfbidRate")),
        "opened_at": _parse_datetime(item.get("rlOpengDt")),
        "agency": str(item.get("dminsttNm") or "").strip(),
        "registered_at": _parse_datetime(item.get("rgstDt")),
        "awarded_at": _parse_datetime(item.get("fnlSucsfDate") or item.get("FnlSucsfDate")),
    }


class PpsAwardClient(PpsClient):
    """Bounded client for service-award history using the common PPS envelope."""

    def __init__(self, *, service_key: str, base_url: str = DEFAULT_BASE_URL, **kwargs: Any) -> None:
        super().__init__(service_key=service_key, base_url=base_url, **kwargs)
        self.fallback_window_count = 0
        self.window_errors: list[str] = []

    def _fetch_window(
        self,
        *,
        window: DateWindow,
        keyword: str,
        operation_path: str,
        rows: int,
        max_pages: int,
        deadline_monotonic: float | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        folded_keyword = keyword.casefold()
        page = 1
        while True:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.hit_time_limit = True
                return results
            payload = self._request(
                operation_path,
                {
                    "inqryDiv": "1",
                    "inqryBgnDt": window.start.strftime("%Y%m%d0000"),
                    "inqryEndDt": window.end.strftime("%Y%m%d2359"),
                    "bidNtceNm": keyword,
                    "pageNo": page,
                    "numOfRows": rows,
                },
            )
            raw_items, total = parse_paged_response(payload)
            for raw in raw_items:
                award = normalise_award(raw)
                if folded_keyword in award["title"].casefold():
                    results.append(award)
            if page * rows >= total or not raw_items:
                break
            if page >= max_pages:
                self.hit_page_limit = True
                break
            page += 1
        return results

    def iter_awards(
        self,
        *,
        start: date,
        end: date,
        keyword: str,
        operation_path: str = DEFAULT_AWARD_OPERATION,
        rows: int = 100,
        max_window_days: int = 30,
        max_pages_per_window: int = 1,
        fallback_window_days: int = 7,
        continue_on_window_error: bool = False,
        deadline_monotonic: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not keyword.strip():
            raise ValueError("keyword is required")
        if not 1 <= rows <= 999:
            raise ValueError("rows must be between 1 and 999")
        if max_pages_per_window < 1:
            raise ValueError("max_pages_per_window must be positive")
        if not 1 <= fallback_window_days < max_window_days:
            raise ValueError("fallback_window_days must be shorter than max_window_days")
        self.hit_page_limit = False
        self.hit_time_limit = False
        self.fallback_window_count = 0
        self.window_errors = []
        for window in split_date_range(start, end, max_days=max_window_days):
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.hit_time_limit = True
                return
            try:
                results = self._fetch_window(
                    window=window,
                    keyword=keyword,
                    operation_path=operation_path,
                    rows=rows,
                    max_pages=max_pages_per_window,
                    deadline_monotonic=deadline_monotonic,
                )
            except PpsApiError:
                # A small number of otherwise-valid 30-day PPS queries return a
                # nonstandard envelope. Retry only that window in bounded 7-day
                # slices; never silently reinterpret the error as zero results.
                self.fallback_window_count += 1
                results = []
                for fallback in split_date_range(
                    window.start,
                    window.end,
                    max_days=fallback_window_days,
                ):
                    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                        self.hit_time_limit = True
                        return
                    try:
                        results.extend(
                            self._fetch_window(
                                window=fallback,
                                keyword=keyword,
                                operation_path=operation_path,
                                rows=rows,
                                max_pages=max_pages_per_window,
                                deadline_monotonic=deadline_monotonic,
                            )
                        )
                    except PpsApiError:
                        safe_window = f"{fallback.start.isoformat()}..{fallback.end.isoformat()}"
                        self.window_errors.append(safe_window)
                        if not continue_on_window_error:
                            raise
            yield from results
