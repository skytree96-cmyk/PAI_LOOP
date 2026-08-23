from __future__ import annotations

import re
import time
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any, Literal, TypeAlias

from .awards import normalise_award
from .pps import (
    DEFAULT_BASE_URL,
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
        max_window_days: int = 30,
        max_pages_per_window: int = 1,
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

        self.hit_page_limit = False
        self.hit_time_limit = False
        self.provider_mismatch_count = 0
        for scope in scopes:
            try:
                operation_path = AWARD_SCOPE_OPERATIONS[scope]
            except KeyError as exc:
                raise ValueError(f"unsupported award scope: {scope}") from exc
            # Newest windows first so the bounded UI response retains recent
            # awards if it reaches its hard record cap.
            for window in reversed(
                split_date_range(start, end, max_days=max_window_days)
            ):
                page = 1
                while True:
                    if (
                        deadline_monotonic is not None
                        and time.monotonic() >= deadline_monotonic
                    ):
                        self.hit_time_limit = True
                        return
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
                            self.provider_mismatch_count += 1
                            continue
                        yield normalise_company_award(raw, scope=scope)
                    if page * rows >= total or not raw_items:
                        break
                    if page >= max_pages_per_window:
                        self.hit_page_limit = True
                        break
                    page += 1
