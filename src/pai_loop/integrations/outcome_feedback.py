from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from .awards import normalise_award
from .company_awards import normalise_business_number
from .pps import (
    DEFAULT_BASE_URL,
    PpsClient,
    parse_paged_response,
    split_date_range,
)


DEFAULT_OUTCOME_OPERATION = (
    "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch"
)


def canonical_pps_revision(value: object) -> str:
    """Return the PPS ordinal used for exact, zero-padding-agnostic matching.

    Notice APIs commonly expose ``00`` while award APIs expose ``000`` for the
    same ordinal.  Numeric ordinals are canonicalised without leading zeros;
    non-numeric provider values remain exact after whitespace normalisation.
    """

    text = str(value or "0").strip()
    if text.isdigit():
        return str(int(text))
    return " ".join(text.split()).casefold()


def _safe_digest(value: dict[str, Any]) -> str:
    serialised = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.isoformat(),
    )
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def _normalise_outcome_row(
    raw: dict[str, Any],
    *,
    company_business_number: str,
) -> dict[str, Any]:
    """Project one provider row without retaining a business identifier or PII."""

    award = normalise_award(raw)
    returned_number = str(raw.get("bidwinnrBizno") or "").strip()
    if returned_number:
        try:
            business_number_match: bool | None = (
                normalise_business_number(returned_number)
                == company_business_number
            )
            business_number_status = "PRESENT_VALID"
        except ValueError:
            business_number_match = None
            business_number_status = "PRESENT_INVALID"
    else:
        business_number_match = None
        business_number_status = "ABSENT"

    safe = {
        **award,
        "company_business_number_match": business_number_match,
        "company_business_number_status": business_number_status,
    }
    # The digest covers only the retained public projection and the match
    # result. Raw provider payloads and identifiers are never returned.
    safe["provider_result_sha256"] = _safe_digest(safe)
    return safe


@dataclass(frozen=True, slots=True)
class ExactNoticeAwardFetch:
    rows: list[dict[str, Any]]
    fetched_count: int
    mismatched_count: int
    quarantined_count: int
    api_calls: int
    hit_page_limit: bool
    hit_time_limit: bool


class PpsOutcomeFeedbackClient(PpsClient):
    """Bounded PPS final-award lookup with mandatory exact identity filtering."""

    def __init__(
        self,
        *,
        service_key: str,
        base_url: str = DEFAULT_BASE_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(service_key=service_key, base_url=base_url, **kwargs)

    def fetch_exact_notice_awards(
        self,
        *,
        bid_notice_no: str,
        revision_no: str,
        start: date,
        end: date,
        company_business_number: str,
        operation_path: str = DEFAULT_OUTCOME_OPERATION,
        rows: int = 100,
        max_window_days: int = 30,
        max_pages_per_window: int = 1,
        deadline_monotonic: float | None = None,
    ) -> ExactNoticeAwardFetch:
        notice_number = str(bid_notice_no or "").strip()
        if not notice_number:
            raise ValueError("bid_notice_no is required")
        expected_revision = canonical_pps_revision(revision_no)
        company_number = normalise_business_number(company_business_number)
        if not 1 <= rows <= 999:
            raise ValueError("rows must be between 1 and 999")
        if max_pages_per_window < 1:
            raise ValueError("max_pages_per_window must be positive")

        call_start = self.request_count
        exact: dict[str, dict[str, Any]] = {}
        fetched_count = 0
        mismatched_count = 0
        quarantined_count = 0
        hit_page_limit = False
        hit_time_limit = False

        for window in split_date_range(start, end, max_days=max_window_days):
            page = 1
            while True:
                if (
                    deadline_monotonic is not None
                    and time.monotonic() >= deadline_monotonic
                ):
                    hit_time_limit = True
                    break
                payload = self._request(
                    operation_path,
                    {
                        # PPSSrch inqryDiv=1 uses the notice-posted clock. This
                        # keeps the query window narrow and stable even when the
                        # final award is registered much later.
                        "inqryDiv": "1",
                        "inqryBgnDt": window.start.strftime("%Y%m%d0000"),
                        "inqryEndDt": window.end.strftime("%Y%m%d2359"),
                        "bidNtceNo": notice_number,
                        "pageNo": page,
                        "numOfRows": rows,
                    },
                )
                raw_items, total = parse_paged_response(payload)
                fetched_count += len(raw_items)
                for raw in raw_items:
                    projected = _normalise_outcome_row(
                        raw,
                        company_business_number=company_number,
                    )
                    if (
                        projected["bid_notice_no"] != notice_number
                        or canonical_pps_revision(projected["revision_no"])
                        != expected_revision
                    ):
                        mismatched_count += 1
                        continue
                    if not projected["identity"] or not projected["winner_name"]:
                        quarantined_count += 1
                        continue
                    exact[str(projected["identity"])] = projected
                if page * rows >= total or not raw_items:
                    break
                if page >= max_pages_per_window:
                    hit_page_limit = True
                    break
                page += 1
            if hit_time_limit:
                break

        return ExactNoticeAwardFetch(
            rows=list(exact.values()),
            fetched_count=fetched_count,
            mismatched_count=mismatched_count,
            quarantined_count=quarantined_count,
            api_calls=self.request_count - call_start,
            hit_page_limit=hit_page_limit,
            hit_time_limit=hit_time_limit,
        )
