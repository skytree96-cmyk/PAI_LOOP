from __future__ import annotations

import time
from datetime import date
from math import isfinite
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
DEFAULT_OPENING_RESULT_OPERATION = (
    "as/ScsbidInfoService/getOpengResultListInfoOpengCompt"
)

# Official operation schema: https://www.data.go.kr/data/15129397/openapi.do
# prcbdrNm, bidprcAmt and opengRank are documented. rmrk is a remark;
# sucsfbidAmt belongs to the separate final-award operation, never a bid.
# The score fields were observed in a service-procurement response but are
# absent from that Swagger schema. Only these exact fields are accepted;
# technical evaluation is not this product's quantitative component.
_OPENING_COMPANY_NAME_KEYS = ("prcbdrNm",)
_OPENING_BID_AMOUNT_KEYS = ("bidprcAmt",)
_OPENING_TECHNICAL_KEYS = ("techEvlVal",)
_OPENING_PRICE_KEYS = ("bidPrceEvlVal",)
_OPENING_TOTAL_KEYS = ("totalEvlAmtVal",)


class OpeningResultsIncomplete(PpsApiError):
    """An opening response cannot safely replace a stored company set."""


def _opening_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    number = _number(value)
    return number if number is not None and isfinite(number) and number >= 0 else None


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def normalise_opening_result(item: dict[str, Any]) -> dict[str, Any]:
    """Read one opening-result company row without inventing an absent score.

    Every numeric field is ``None`` when the provider omitted it or sent an
    empty string. ``is_winner`` is deliberately absent here: an opening rank
    is not a final award, and the caller resolves the winner from the separate
    award endpoint instead.
    """

    rank = _integer(item.get("opengRank"))
    return {
        "company_name": str(_first_present(item, _OPENING_COMPANY_NAME_KEYS) or "").strip(),
        "bid_amount": _opening_number(_first_present(item, _OPENING_BID_AMOUNT_KEYS)),
        "technical_evaluation": _opening_number(_first_present(item, _OPENING_TECHNICAL_KEYS)),
        "price_evaluation": _opening_number(_first_present(item, _OPENING_PRICE_KEYS)),
        "total_evaluation": _opening_number(_first_present(item, _OPENING_TOTAL_KEYS)),
        "opening_rank": rank if rank is not None and rank > 0 else None,
    }


def opening_result_loses_recorded_numbers(
    previous: list[dict[str, Any]], incoming: list[dict[str, Any]],
) -> bool:
    """Detect missing known numbers; never merge fields between company rows.

    The caller must first bind both snapshots to the same complete award
    identity (notice, revision, classification and rebid). Names are used only
    to detect potential loss. Ambiguous same-name rows fail conservatively;
    they are never paired to transfer values or identify a winner.
    """
    prior_by_name: dict[str, list[dict[str, Any]]] = {}
    for company in previous:
        name = str(company.get("company_name") or "").strip()
        if name:
            prior_by_name.setdefault(name, []).append(company)
    for company in incoming:
        previous_companies = prior_by_name.get(str(company.get("company_name") or "").strip(), [])
        for field in ("bid_amount", "technical_evaluation", "price_evaluation", "total_evaluation", "opening_rank"):
            if _opening_number(company.get(field)) is None and any(
                _opening_number(prior.get(field)) is not None for prior in previous_companies
            ):
                return True
    return False


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
        self.hit_incomplete_response = False

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
        expected_total: int | None = None
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
                timeout_seconds=(
                    max(0.1, deadline_monotonic - time.monotonic())
                    if deadline_monotonic is not None else None
                ),
            )
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.hit_time_limit = True
                return results
            raw_items, total = parse_paged_response(payload)
            body = payload["response"]["body"]
            container = body.get("items")
            raw = container.get("item", []) if isinstance(container, dict) else container
            raw = [raw] if isinstance(raw, dict) else raw
            if raw in (None, "") and total == 0 and "items" in body:
                raw = []
            if not isinstance(raw, list) or len(raw) != len(raw_items) or not str(body["totalCount"]).isdigit():
                raise PpsApiError("낙찰 결과 업체 행 또는 전체 건수가 불완전합니다.")
            incomplete = (
                (expected_total is not None and total != expected_total)
                or len(raw_items) != min(rows, max(0, total - (page - 1) * rows))
            )
            expected_total = total
            for raw in raw_items:
                award = normalise_award(raw)
                if folded_keyword in award["title"].casefold():
                    results.append(award)
            if incomplete:
                self.hit_incomplete_response = True
                return results
            if page * rows >= total or not raw_items:
                break
            if page >= max_pages:
                self.hit_page_limit = True
                break
            page += 1
        return results

    def fetch_opening_results(
        self,
        *,
        bid_notice_no: str,
        revision_no: str = "000",
        classification_no: str = "0",
        rebid_no: str = "000",
        operation_path: str = DEFAULT_OPENING_RESULT_OPERATION,
        rows: int = 100,
        max_pages: int = 2,
        deadline_monotonic: float | None = None,
    ) -> list[dict[str, Any]]:
        """Read the opening-result companies for exactly one notice.

        The bound is deliberately tight: one notice per call, a caller-set page
        cap and the same wall deadline the award sweep already honours.
        Incomplete or ambiguous sets raise instead of masquerading as a full
        company list or an empty successful response.
        """

        if not str(bid_notice_no).strip():
            raise ValueError("bid_notice_no is required")
        if not 1 <= rows <= 999:
            raise ValueError("rows must be between 1 and 999")
        if not 1 <= max_pages <= 3:
            raise ValueError("max_pages must be between 1 and 3")

        self.hit_page_limit = False
        self.hit_time_limit = False
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        expected_total: int | None = None
        identity = {
            "bidNtceNo": str(bid_notice_no).strip(),
            "bidNtceOrd": str(revision_no or "000").zfill(3),
            "bidClsfcNo": str(classification_no or "0").zfill(3),
            "rbidNo": str(rebid_no or "000").zfill(3),
        }
        page = 1
        while page <= max_pages:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.hit_time_limit = True
                raise OpeningResultsIncomplete("개찰 결과 수집 제한 시간에 도달했습니다.")
            payload = self._request(
                operation_path,
                {
                    "bidNtceNo": str(bid_notice_no).strip(),
                    "bidNtceOrd": str(revision_no or "000"),
                    "bidClsfcNo": str(classification_no or "0"),
                    "rbidNo": str(rebid_no or "000"),
                    "pageNo": page,
                    "numOfRows": rows,
                },
                timeout_seconds=(
                    max(0.1, deadline_monotonic - time.monotonic())
                    if deadline_monotonic is not None else None
                ),
            )
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.hit_time_limit = True
                raise OpeningResultsIncomplete("개찰 결과 수집 제한 시간에 도달했습니다.")
            raw_items, total = parse_paged_response(payload)
            body = payload["response"]["body"]
            container = body.get("items")
            raw = container.get("item", []) if isinstance(container, dict) else container
            raw = [raw] if isinstance(raw, dict) else raw
            # PPS may encode a genuinely empty collection as an empty string.
            if raw in (None, "") and total == 0 and "items" in body:
                raw = []
            if (
                not isinstance(raw, list)
                or len(raw) != len(raw_items)
                or not str(body.get("totalCount", "")).isdigit()
                or total < 0
                or (expected_total is not None and total != expected_total)
                or len(raw_items) != min(rows, max(0, total - len(results)))
                or ("pageNo" in body and str(body["pageNo"]) != str(page))
                or ("numOfRows" in body and str(body["numOfRows"]) != str(rows))
            ):
                raise OpeningResultsIncomplete("개찰 결과 페이지 또는 전체 건수가 불완전합니다.")
            expected_total = total
            for raw in raw_items:
                if any(
                    (str(raw.get(key) or "").strip() if key == "bidNtceNo" else str(raw.get(key) or "").strip().zfill(3)) != value
                    or key not in raw
                    for key, value in identity.items()
                ):
                    raise OpeningResultsIncomplete("개찰 결과 공고 식별자가 일치하지 않습니다.")
                company = normalise_opening_result(raw)
                # Only use the provider identifier to detect duplicate pages;
                # it is never retained in the public company projection.
                company_key = str(raw.get("prcbdrBizno") or company["company_name"]).strip()
                if not company["company_name"] or company_key in seen:
                    raise OpeningResultsIncomplete("개찰 결과 업체 행이 누락되거나 중복되었습니다.")
                seen.add(company_key)
                results.append(company)
            if len(results) == total:
                return results
            if page >= max_pages:
                self.hit_page_limit = True
                raise OpeningResultsIncomplete("개찰 결과 페이지 제한에 도달했습니다.")
            page += 1
        raise AssertionError("unreachable")

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
        if not 1 <= max_window_days <= 30:
            raise ValueError("max_window_days must be between 1 and 30")
        if not 1 <= fallback_window_days < max_window_days:
            raise ValueError("fallback_window_days must be shorter than max_window_days")
        self.hit_page_limit = False
        self.hit_time_limit = False
        self.fallback_window_count = 0
        self.window_errors = []
        self.hit_incomplete_response = False
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
