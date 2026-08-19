from __future__ import annotations

import re
import time
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .pps import KST, PpsClient, parse_paged_response, split_date_range


DEFAULT_PRESPEC_OPERATION = (
    "ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc"
)
PRESPEC_SOURCE_KIND = "PPS_PRESPEC"
_PRESPEC_DOCUMENT_HOST = "www.g2b.go.kr"
_PRESPEC_DOCUMENT_PATH = "/pn/pnz/pnza/UntyAtchFile/downloadFile.do"
_PRESPEC_DOCUMENT_QUERY_KEYS = {"bfSpecRegNo", "fileType", "fileSeq"}
_SAFE_QUERY_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _text(value: Any, *, maximum: int = 500) -> str | None:
    if value in (None, ""):
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    if not cleaned or any(ord(character) < 32 for character in cleaned):
        return None
    return cleaned[:maximum]


def _datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    for pattern, size in (("%Y%m%d%H%M%S", 14), ("%Y%m%d%H%M", 12)):
        if len(digits) != size:
            continue
        try:
            return datetime.strptime(digits, pattern).replace(tzinfo=KST)
        except ValueError:
            return None
    return None


def _amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        amount = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return amount if amount >= 0 else None


def _safe_document_url(value: Any, *, registry_no: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _PRESPEC_DOCUMENT_HOST
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.path != _PRESPEC_DOCUMENT_PATH
        or parsed.fragment
    ):
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    keys = [key for key, _value in pairs]
    values = dict(pairs)
    if (
        len(pairs) != 3
        or len(keys) != len(set(keys))
        or set(keys) != _PRESPEC_DOCUMENT_QUERY_KEYS
        or values.get("bfSpecRegNo") != registry_no
        or values.get("fileType") != "BFDTL"
        or not str(values.get("fileSeq") or "").isdigit()
        or any(not _SAFE_QUERY_VALUE.fullmatch(item) for item in values.values())
    ):
        return None
    return urlunsplit(
        (
            "https",
            _PRESPEC_DOCUMENT_HOST,
            _PRESPEC_DOCUMENT_PATH,
            urlencode(sorted(pairs)),
            "",
        )
    )


def normalise_pre_specification(item: dict[str, Any]) -> dict[str, Any]:
    """Return the public-business allowlist for one PPS service pre-specification.

    Officer name/telephone and every unknown provider field are deliberately
    discarded.  Pre-specifications are not bid notices and therefore receive a
    separate source kind and identity; callers must not put them into the GO
    decision queue until a linked ``bidNtceNoList`` notice exists.
    """

    registry_no = _text(item.get("bfSpecRgstNo"), maximum=40) or ""
    title = _text(item.get("prdctClsfcNoNm"), maximum=500) or ""
    linked_notice_nos: list[str] = []
    seen_notices: set[str] = set()
    for raw_notice_no in re.split(r"[,|\s]+", str(item.get("bidNtceNoList") or "")):
        notice_no = _text(raw_notice_no, maximum=80)
        if not notice_no or notice_no.casefold() in seen_notices:
            continue
        seen_notices.add(notice_no.casefold())
        linked_notice_nos.append(notice_no)

    document_urls: list[str] = []
    if registry_no:
        for slot in range(1, 6):
            safe_url = _safe_document_url(
                item.get(f"specDocFileUrl{slot}"),
                registry_no=registry_no,
            )
            if safe_url and safe_url not in document_urls:
                document_urls.append(safe_url)

    return {
        "source_kind": PRESPEC_SOURCE_KIND,
        "pre_specification_key": f"PRESPEC-{registry_no}" if registry_no else "",
        "registry_no": registry_no,
        "title": title,
        "business_division": _text(item.get("bsnsDivNm"), maximum=80),
        "reference_no": _text(item.get("refNo"), maximum=120),
        "ordering_agency": _text(item.get("orderInsttNm"), maximum=255),
        "demand_agency": _text(item.get("rlDminsttNm"), maximum=255),
        "budget_amount": _amount(item.get("asignBdgtAmt")),
        "received_at": _datetime(item.get("rcptDt")),
        "opinion_deadline": _datetime(item.get("opninRgstClseDt")),
        "delivery_due": _datetime(item.get("dlvrTmlmtDt")),
        "registered_at": _datetime(item.get("rgstDt")),
        "changed_at": _datetime(item.get("chgDt")),
        "software_business": str(item.get("swBizObjYn") or "").strip().upper() == "Y",
        "document_urls": document_urls,
        "linked_bid_notice_nos": linked_notice_nos,
    }


def matched_pre_specification_keywords(
    record: dict[str, Any],
    keywords: Iterable[str],
) -> list[str]:
    """Return normalized exact-substring matches for transparent local search."""

    haystack = " ".join(
        str(record.get(field) or "")
        for field in ("title", "ordering_agency", "demand_agency")
    ).casefold()
    matches: list[str] = []
    seen: set[str] = set()
    for raw_keyword in keywords:
        keyword = re.sub(r"\s+", " ", str(raw_keyword)).strip()
        folded = keyword.casefold()
        if not keyword or folded in seen:
            continue
        seen.add(folded)
        if folded in haystack:
            matches.append(keyword)
    return matches


class PpsPreSpecificationClient(PpsClient):
    """Bounded reader for the official PPS service pre-specification list."""

    def iter_pre_specifications(
        self,
        *,
        start: date,
        end: date,
        operation_path: str = DEFAULT_PRESPEC_OPERATION,
        rows: int = 100,
        max_window_days: int = 30,
        max_pages: int | None = 20,
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
                        "inqryDiv": "1",
                        "inqryBgnDt": window.start.strftime("%Y%m%d0000"),
                        "inqryEndDt": window.end.strftime("%Y%m%d2359"),
                        "pageNo": page,
                        "numOfRows": rows,
                    },
                )
                raw_items, total = parse_paged_response(payload)
                for raw_item in raw_items:
                    record = normalise_pre_specification(raw_item)
                    if record["registry_no"] and record["title"]:
                        yield record
                if page * rows >= total or not raw_items:
                    break
                if max_pages is not None and page >= max_pages:
                    self.hit_page_limit = True
                    break
                page += 1
