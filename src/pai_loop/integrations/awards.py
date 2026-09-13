from __future__ import annotations

import time
from collections import Counter
from datetime import date
from math import isfinite
from typing import Any, Iterator

from ..award_scope import award_agency_is_verifiable, matches_award_agency, normalize_award_agency
from ..outcome_identity import normalise_opening_identity
from .pps import (
    DEFAULT_BASE_URL,
    DateWindow,
    PpsApiError,
    PpsApiCallBudgetExceeded,
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
        # Internal matching fact, not part of the public award-row schema.
        "demand_agency_code": str(item.get("dminsttCd") or "").strip() or None,
        "registered_at": _parse_datetime(item.get("rgstDt")),
        "awarded_at": _parse_datetime(item.get("fnlSucsfDate") or item.get("FnlSucsfDate")),
    }


def is_pps_rate_limit_error(error: PpsApiError) -> bool:
    """Official portal code 22 is daily quota, 23 is per-second quota."""
    metadata = error.safe_metadata()
    return (
        metadata["error_type"] == "HTTP_ERROR" and metadata["http_status"] == 429
    ) or (
        metadata["error_type"] in {"SERVICE_ERROR", "PROVIDER_RESULT_ERROR"}
        and metadata["provider_code"] in {"22", "23"}
    )


class _AwardRateLimitReached(PpsApiError):
    def __init__(self, error: PpsApiError, completed_rows: list[dict[str, Any]]) -> None:
        super().__init__(*error.args, **error.safe_metadata())
        self.completed_rows = completed_rows


_SHAPE_LIMIT = 32
_SHAPE_ROW_LIMIT = 1000
_MISSING = object()


def _json_shape(value: object) -> str:
    if value is _MISSING:
        return "MISSING"
    return {
        type(None): "NULL", bool: "BOOLEAN", int: "INTEGER", float: "NUMBER",
        str: "STRING", list: "ARRAY", dict: "OBJECT",
    }.get(type(value), "OTHER")


def _award_page_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Describe only fixed paths/types/counts; never retain provider values."""
    response = payload.get("response", _MISSING)
    header = response.get("header", _MISSING) if isinstance(response, dict) else _MISSING
    body = response.get("body", _MISSING) if isinstance(response, dict) else _MISSING
    total = body.get("totalCount", _MISSING) if isinstance(body, dict) else _MISSING
    items = body.get("items", _MISSING) if isinstance(body, dict) else _MISSING
    item = items.get("item", _MISSING) if isinstance(items, dict) else _MISSING
    rows = item if isinstance(items, dict) else items
    return {
        "response_type": _json_shape(response),
        "header_type": _json_shape(header),
        "result_code_type": _json_shape(header.get("resultCode", _MISSING) if isinstance(header, dict) else _MISSING),
        "body_type": _json_shape(body),
        "total_count_type": _json_shape(total),
        "total_count_explicit_zero": (type(total) in (int, float) and total == 0) or (type(total) is str and total == "0"),
        "items_type": _json_shape(items),
        "item_type": _json_shape(item),
        "array_length": min(len(rows), _SHAPE_ROW_LIMIT) if isinstance(rows, list) else None,
        "object_rows": sum(isinstance(row, dict) for row in rows[:_SHAPE_ROW_LIMIT]) if isinstance(rows, list) else (1 if isinstance(rows, dict) else None),
        "array_length_capped": isinstance(rows, list) and len(rows) > _SHAPE_ROW_LIMIT,
    }


class _AwardPageParseError(PpsApiError):
    def __init__(self, error: PpsApiError, payload: dict[str, Any]) -> None:
        super().__init__(*error.args, **error.safe_metadata())
        self.page_shape = _award_page_shape(payload)


class _AwardProbeLimitReached(RuntimeError):
    """Local request boundary, never classified as a provider failure."""


class PpsAwardClient(PpsClient):
    """Bounded client for service-award history using the common PPS envelope."""

    def __init__(
        self, *, service_key: str, base_url: str = DEFAULT_BASE_URL,
        diagnostic_probe: bool = False, **kwargs: Any,
    ) -> None:
        if diagnostic_probe:
            kwargs["max_retries"] = 0
        super().__init__(service_key=service_key, base_url=base_url, **kwargs)
        self._diagnostic_probe = diagnostic_probe
        self._diagnostic_request_reserved = False
        self.fallback_window_count = 0
        self.window_errors: list[str] = []
        self.hit_incomplete_response = False
        self.hit_api_call_limit = False
        self.hit_rate_limit = False
        self._window_error_counts: Counter = Counter()
        self._page_shape_counts: Counter = Counter()
        self._suppressed_page_shapes = 0

    def _request(
        self, operation_path: str, params: dict[str, Any], *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if self._diagnostic_probe:
            # Reserve before entering the shared HTTP implementation, including
            # errors/timeouts. The reservation lasts for this client lifetime.
            with self._request_count_lock:
                if self._diagnostic_request_reserved:
                    raise _AwardProbeLimitReached("diagnostic probe request already consumed")
                self._diagnostic_request_reserved = True
        return super()._request(operation_path, params, timeout_seconds=timeout_seconds)

    @property
    def page_shape_diagnostics(self) -> dict[str, Any]:
        return {
            "counts": [
                {"phase": phase, "error_type": kind, "shape": dict(shape), "count": count}
                for (phase, kind, shape), count in self._page_shape_counts.items()
            ],
            "suppressed_count": self._suppressed_page_shapes,
        }

    @property
    def window_error_counts(self) -> list[dict[str, Any]]:
        """Counts of failed window attempts, never exception prose or URLs."""
        return [
            {"phase": phase, "error_type": kind, "http_status": http_status,
             "provider_code": provider_code, "count": count}
            for (phase, kind, http_status, provider_code), count in sorted(
                self._window_error_counts.items(), key=lambda item: tuple(str(value or "") for value in item[0])
            )
        ]

    def _record_window_error(self, phase: str, error: PpsApiError) -> None:
        metadata = error.safe_metadata()
        self._window_error_counts[(phase, metadata["error_type"], metadata["http_status"], metadata["provider_code"])] += 1
        if isinstance(error, _AwardPageParseError):
            key = (phase, metadata["error_type"], tuple(error.page_shape.items()))
            if key in self._page_shape_counts or len(self._page_shape_counts) < _SHAPE_LIMIT:
                self._page_shape_counts[key] += 1
            else:
                self._suppressed_page_shapes += 1

    def _fetch_window(
        self,
        *,
        window: DateWindow,
        keyword: str,
        operation_path: str,
        rows: int,
        max_pages: int,
        deadline_monotonic: float | None = None,
        demand_agency_name: str | None = None,
        demand_agency_code: str | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        folded_keyword = keyword.casefold()
        has_agency_filter = bool(normalize_award_agency(demand_agency_name)
                                 or normalize_award_agency(demand_agency_code))
        page = 1
        expected_total: int | None = None
        while True:
            if self._diagnostic_probe and self._diagnostic_request_reserved:
                return results
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.hit_time_limit = True
                return results
            try:
                payload = self._request(
                    operation_path,
                    {
                        "inqryDiv": "1",
                        "inqryBgnDt": window.start.strftime("%Y%m%d0000"),
                        "inqryEndDt": window.end.strftime("%Y%m%d2359"),
                        "bidNtceNm": keyword,
                        "pageNo": page,
                        "numOfRows": rows,
                        **({"dminsttCd": demand_agency_code.strip()}
                           if normalize_award_agency(demand_agency_code) else
                           {"dminsttNm": demand_agency_name.strip()}
                           if normalize_award_agency(demand_agency_name) else {}),
                    },
                    timeout_seconds=(
                        max(0.1, deadline_monotonic - time.monotonic())
                        if deadline_monotonic is not None else None
                    ),
                )
            except PpsApiCallBudgetExceeded:
                self.hit_api_call_limit = True
                return results
            except PpsApiError as exc:
                if is_pps_rate_limit_error(exc):
                    raise _AwardRateLimitReached(exc, results) from exc
                raise
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.hit_time_limit = True
                if not self._diagnostic_probe:
                    return results
            try:
                raw_items, total = parse_paged_response(payload)
                body = payload["response"]["body"]
                container = body.get("items")
                raw = container.get("item", []) if isinstance(container, dict) else container
                raw = [raw] if isinstance(raw, dict) else raw
                if raw in (None, "") and total == 0 and "items" in body:
                    raw = []
                # The common parser has already validated the success envelope.
                # An omitted collection is empty only with an exact explicit zero;
                # do not infer zero from bool/float/null/blank or a missing count.
                count = body["totalCount"]
                if "items" not in body and (
                    (type(count) is int and count == 0)
                    or (type(count) is str and count == "0")
                ):
                    raw = []
                if not isinstance(raw, list) or len(raw) != len(raw_items) or not str(body["totalCount"]).isdigit():
                    raise PpsApiError("낙찰 결과 업체 행 또는 전체 건수가 불완전합니다.", error_type="AWARD_PAGE_INVALID")
            except PpsApiError as exc:
                if is_pps_rate_limit_error(exc):
                    raise _AwardRateLimitReached(exc, results) from exc
                raise _AwardPageParseError(exc, payload) from exc
            incomplete = (
                (expected_total is not None and total != expected_total)
                or len(raw_items) != min(rows, max(0, total - (page - 1) * rows))
            )
            expected_total = total
            for raw in raw_items:
                award = normalise_award(raw)
                if has_agency_filter and not award_agency_is_verifiable(award,
                        demand_agency_name=demand_agency_name, demand_agency_code=demand_agency_code):
                    self.hit_incomplete_response = True
                    continue
                if (folded_keyword in award["title"].casefold()
                        and (not has_agency_filter or matches_award_agency(award,
                            demand_agency_name=demand_agency_name, demand_agency_code=demand_agency_code))):
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
        company_business_number: str | None = None,
        winner_business_number: str | None = None,
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

        # The public history projection stays unchanged. The outcome adapter
        # can request a private exact-match boolean before identifiers vanish.
        company_number = None
        winner_number = None
        opening_identity = None
        if company_business_number is not None:
            from .company_awards import normalise_business_number
            company_number = normalise_business_number(company_business_number)
            if winner_business_number is not None:
                winner_number = normalise_business_number(winner_business_number)
            opening_identity = normalise_opening_identity({
                "bid_notice_no": bid_notice_no, "revision_no": revision_no,
                "classification_no": classification_no, "rebid_no": rebid_no,
            })
            if opening_identity is None or max_pages > 2:
                raise ValueError("complete opening identity and at most two pages are required")

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
                returned_number = None
                if company_number is not None:
                    actual_identity = normalise_opening_identity({
                        target: raw.get(source) for target, source in (
                            ("bid_notice_no", "bidNtceNo"), ("revision_no", "bidNtceOrd"),
                            ("classification_no", "bidClsfcNo"), ("rebid_no", "rbidNo"),
                        )
                    })
                    if actual_identity is None or actual_identity != opening_identity:
                        raise OpeningResultsIncomplete("개찰 결과의 전체 회차를 확인할 수 없습니다.")
                    try:
                        returned_number = normalise_business_number(str(raw.get("prcbdrBizno") or ""))
                    except ValueError:
                        raise OpeningResultsIncomplete("개찰 참여 업체 식별자를 확인할 수 없습니다.") from None
                if any(
                    (str(raw.get(key) or "").strip() if key == "bidNtceNo" else str(raw.get(key) or "").strip().zfill(3)) != value
                    or key not in raw
                    for key, value in identity.items()
                ):
                    raise OpeningResultsIncomplete("개찰 결과 공고 식별자가 일치하지 않습니다.")
                company = normalise_opening_result(raw)
                if company_number is not None:
                    company["company_business_number_match"] = returned_number == company_number
                    if winner_number is not None:
                        company["final_winner_match"] = returned_number == winner_number
                    company["opening_identity"] = opening_identity
                # Only use the provider identifier to detect duplicate pages;
                # it is never retained in the public company projection.
                company_key = returned_number or str(raw.get("prcbdrBizno") or company["company_name"]).strip()
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
        # The same PPSSrch operation used by company-award search enforces a
        # calendar-month range. A 30-day February interval can return code 07;
        # 28 inclusive days cover every month safely without needless fallback.
        max_window_days: int = 28,
        max_pages_per_window: int = 1,
        fallback_window_days: int = 7,
        continue_on_window_error: bool = False,
        deadline_monotonic: float | None = None,
        demand_agency_name: str | None = None,
        demand_agency_code: str | None = None,
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
        if self._diagnostic_probe and self._diagnostic_request_reserved:
            return
        self.hit_page_limit = False
        self.hit_time_limit = False
        self.fallback_window_count = 0
        self.window_errors = []
        self.hit_incomplete_response = False
        self.hit_api_call_limit = False
        self.hit_rate_limit = False
        self._window_error_counts.clear()
        self._page_shape_counts.clear()
        self._suppressed_page_shapes = 0
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
                    demand_agency_name=demand_agency_name,
                    demand_agency_code=demand_agency_code,
                )
            except PpsApiError as exc:
                self._record_window_error("PRIMARY", exc)
                if is_pps_rate_limit_error(exc):
                    self.hit_rate_limit = True
                    self.window_errors.append(f"{window.start.isoformat()}..{window.end.isoformat()}")
                    yield from getattr(exc, "completed_rows", [])
                    return
                if self._diagnostic_probe:
                    self.window_errors.append(f"{window.start.isoformat()}..{window.end.isoformat()}")
                    return
                # Do not repeat an identical short interval. A failed query is
                # still missing coverage, even when no rows have been returned.
                if (window.end - window.start).days + 1 <= fallback_window_days:
                    self.window_errors.append(f"{window.start.isoformat()}..{window.end.isoformat()}")
                    if not continue_on_window_error:
                        raise
                    continue
                # Preserve the existing bounded smaller-window recovery policy;
                # fixed metadata distinguishes transport/provider/parser failures.
                self.fallback_window_count += 1
                results = []
                for fallback in split_date_range(
                    window.start,
                    window.end,
                    max_days=fallback_window_days,
                ):
                    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                        self.hit_time_limit = True
                        yield from results
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
                                demand_agency_name=demand_agency_name,
                                demand_agency_code=demand_agency_code,
                            )
                        )
                    except PpsApiError as exc:
                        self._record_window_error("FALLBACK", exc)
                        safe_window = f"{fallback.start.isoformat()}..{fallback.end.isoformat()}"
                        self.window_errors.append(safe_window)
                        if is_pps_rate_limit_error(exc):
                            self.hit_rate_limit = True
                            yield from results
                            yield from getattr(exc, "completed_rows", [])
                            return
                        if not continue_on_window_error:
                            raise
                    if self.hit_api_call_limit:
                        break
            yield from results
            if self._diagnostic_probe or self.hit_api_call_limit:
                return
