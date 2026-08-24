from __future__ import annotations

import re
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Literal, TypeAlias

from .awards import normalise_award
from .pps import (
    DEFAULT_BASE_URL,
    PpsApiError,
    PpsClient,
    parse_paged_response,
    split_date_range,
)

AwardScope: TypeAlias = Literal["goods", "construction", "service", "foreign"]

AWARD_SCOPE_OPERATIONS: dict[AwardScope, str] = {
    "goods": "as/ScsbidInfoService/getScsbidListSttusThngPPSSrch",
    "construction": "as/ScsbidInfoService/getScsbidListSttusCnstwkPPSSrch",
    "service": "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch",
    "foreign": "as/ScsbidInfoService/getScsbidListSttusFrgcptPPSSrch",
}

DEFAULT_COMPANY_BUSINESS_NUMBER = "1058201810"
DEFAULT_COMPANY_NAME = "사단법인 한국능률협회"

_BUSINESS_NUMBER_INPUT = re.compile(r"[0-9\s-]+")
_PPS_RESULT_CODE = re.compile(r"resultCode=([A-Za-z0-9_-]{1,20})")


def _safe_pps_error_code(exc: PpsApiError) -> str:
    """Classify a provider failure without retaining URLs or identifiers."""

    message = str(exc)
    result = _PPS_RESULT_CODE.search(message)
    if result:
        return f"RESULT_{result.group(1)}"
    folded = message.casefold()
    if "네트워크" in message or "network" in folded:
        return "NETWORK"
    if "http " in folded:
        return "HTTP"
    if "json" in folded:
        return "INVALID_JSON"
    return "INVALID_RESPONSE"


def normalise_business_number(value: str) -> str:
    """Return the ten PPS digits while accepting the familiar hyphen form."""

    text = str(value or "").strip()
    if not text or _BUSINESS_NUMBER_INPUT.fullmatch(text) is None:
        raise ValueError("business number must contain only digits and hyphens")
    digits = re.sub(r"\D", "", text)
    if len(digits) != 10:
        raise ValueError("business number must contain exactly 10 digits")
    return digits


def normalise_company_award(
    item: dict[str, Any],
    *,
    scope: AwardScope,
) -> dict[str, Any]:
    """Project one PPS row onto public, non-contact award facts."""

    award = normalise_award(item)
    return {"scope": scope, **award}


class PpsCompanyAwardClient(PpsClient):
    """Stateless, bounded PPS award lookup by an exact business number.

    PPS added the optional ``bizno`` filter to all four PPSSrch award
    operations.  The provider identifier is used only to verify each returned
    row and is discarded before yielding the display-safe projection.
    """

    def __init__(
        self,
        *,
        service_key: str,
        base_url: str = DEFAULT_BASE_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(service_key=service_key, base_url=base_url, **kwargs)
        self.provider_mismatch_count = 0

    def iter_company_awards(
        self,
        *,
        start: date,
        end: date,
        business_number: str,
        scopes: Sequence[AwardScope] = ("service",),
        rows: int = 100,
        # PPS enforces a calendar-month limit.  A fixed 30-day inclusive
        # window can cross from February 20 to March 21 and is rejected as an
        # input-range error, so the standalone client also defaults to the
        # universally safe 28-day boundary used by the API route.
        max_window_days: int = 28,
        max_pages_per_window: int = 1,
        max_workers: int = 1,
        deadline_monotonic: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        company_number = normalise_business_number(business_number)
        if not scopes:
            raise ValueError("at least one award scope is required")
        if len(set(scopes)) != len(scopes):
            raise ValueError("award scopes must be unique")
        if not 1 <= rows <= 999:
            raise ValueError("rows must be between 1 and 999")
        if max_pages_per_window < 1:
            raise ValueError("max_pages_per_window must be positive")
        if not 1 <= max_workers <= 8:
            raise ValueError("max_workers must be between 1 and 8")

        self.hit_page_limit = False
        self.hit_time_limit = False
        self.provider_mismatch_count = 0
        self.planned_window_count = 0
        self.attempted_window_count = 0
        self.successful_window_count = 0
        self.failed_window_count = 0
        self.window_errors: list[str] = []

        def fetch_window(
            scope: AwardScope,
            operation_path: str,
            window: Any,
        ) -> tuple[list[dict[str, Any]], bool, bool, int, str | None, bool]:
            records: list[dict[str, Any]] = []
            page_limited = False
            time_limited = False
            mismatch_count = 0
            attempted = False
            page = 1
            try:
                while True:
                    remaining_seconds: float | None = None
                    if deadline_monotonic is not None:
                        remaining_seconds = deadline_monotonic - time.monotonic()
                        if remaining_seconds <= 0:
                            time_limited = True
                            break
                    attempted = True
                    payload = self._request(
                        operation_path,
                        {
                            # PPSSrch defines 1 as notice-posted time and 2 as
                            # opening time.  A company award history must use
                            # the opening basis so notices posted in an earlier
                            # period are not omitted from the selected result
                            # period.
                            "inqryDiv": "2",
                            "inqryBgnDt": window.start.strftime("%Y%m%d0000"),
                            "inqryEndDt": window.end.strftime("%Y%m%d2359"),
                            "bizno": company_number,
                            "pageNo": page,
                            "numOfRows": rows,
                        },
                        timeout_seconds=remaining_seconds,
                    )
                    raw_items, total = parse_paged_response(payload)
                    for raw in raw_items:
                        try:
                            returned_number = normalise_business_number(
                                str(raw.get("bidwinnrBizno") or "")
                            )
                        except ValueError:
                            returned_number = ""
                        if returned_number != company_number:
                            mismatch_count += 1
                            continue
                        records.append(normalise_company_award(raw, scope=scope))
                    if page * rows >= total or not raw_items:
                        break
                    if page >= max_pages_per_window:
                        page_limited = True
                        break
                    page += 1
            except PpsApiError as exc:
                # A single slow 28-day window must not erase successful
                # neighbouring windows.  The route returns a safe partial
                # warning, while this already-redacted reason is retained for
                # server diagnostics only.
                return (
                    records,
                    page_limited,
                    time_limited,
                    mismatch_count,
                    _safe_pps_error_code(exc),
                    attempted,
                )
            return (
                records,
                page_limited,
                time_limited,
                mismatch_count,
                None,
                attempted,
            )

        for scope in scopes:
            try:
                operation_path = AWARD_SCOPE_OPERATIONS[scope]
            except KeyError as exc:
                raise ValueError(f"unsupported award scope: {scope}") from exc
            # Newest windows first so the bounded UI response retains recent
            # awards if it reaches its hard record cap.
            windows = list(
                reversed(split_date_range(start, end, max_days=max_window_days))
            )
            self.planned_window_count += len(windows)
            # Submit only one bounded batch at a time.  Besides avoiding a
            # slow 40-request serial path for the default three-year search,
            # this preserves newest-first output and lets a caller close the
            # generator without having already submitted every older window.
            with ThreadPoolExecutor(max_workers=min(max_workers, len(windows))) as executor:
                for offset in range(0, len(windows), max_workers):
                    if (
                        deadline_monotonic is not None
                        and time.monotonic() >= deadline_monotonic
                    ):
                        self.hit_time_limit = True
                        return
                    batch = windows[offset : offset + max_workers]
                    futures = [
                        executor.submit(fetch_window, scope, operation_path, window)
                        for window in batch
                    ]
                    for future in futures:
                        (
                            records,
                            page_limited,
                            time_limited,
                            mismatch_count,
                            window_error,
                            attempted,
                        ) = future.result()
                        self.hit_page_limit = self.hit_page_limit or page_limited
                        self.hit_time_limit = self.hit_time_limit or time_limited
                        self.provider_mismatch_count += mismatch_count
                        if attempted:
                            self.attempted_window_count += 1
                        if window_error:
                            self.failed_window_count += 1
                            self.window_errors.append(window_error)
                        elif not time_limited:
                            self.successful_window_count += 1
                        yield from records
                    if self.hit_time_limit:
                        return
