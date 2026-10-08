"""Cross-check eligibility against PPS's own structured participation limits (2026-10-08).

PPS publishes, per notice, the industry-licence limit groups and the eligible
regions as structured rows. Measured on the 242 OPEN notices of 2026-10-08,
document analysis already caught every limit these rows express: no PASS
contradicted them and only two undecided notices would have been decided. So
this module never decides eligibility. It stores the rows and flags a notice
whose eligibility reads PASS while PPS's structure says the company cannot
take part, as a guard against a missed clause.

Licence groups are alternatives and every row inside one group is required
(confirmed on notices whose text reads "1517 또는 1518" and "1469+6527 또는
1469+6529 또는 9999"). A row is met by its own licence code or any code in its
permitted-industry list.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_api_key
from .integrations.pps import PpsApiError, PpsClient, parse_paged_response
from .models import Notice, PpsParticipationRestriction

logger = logging.getLogger(__name__)

LICENSE_OPERATION = "ad/BidPublicInfoService/getBidPblancListInfoLicenseLimit"
REGION_OPERATION = "ad/BidPublicInfoService/getBidPblancListInfoPrtcptPsblRgn"
_OWN_CODE_RE = re.compile(r"/\s*(\d{4})\s*$")
_PERMITTED_RE = re.compile(r"\[[^\]]*?/\s*(\d{4})\s*\]")
COMPANY_HEAD_OFFICE_REGION = "서울"


def _rows(client: PpsClient, operation: str, bid_notice_no: str, revision_no: str) -> list[dict[str, Any]]:
    payload = client._request(operation, {
        "inqryDiv": "2", "bidNtceNo": bid_notice_no, "bidNtceOrd": revision_no.zfill(3),
        "pageNo": 1, "numOfRows": 100,
    })
    items, _total = parse_paged_response(payload)
    return items


def fetch_restrictions(client: PpsClient, bid_notice_no: str, revision_no: str) -> tuple[list[list[list[str]]], list[str]]:
    """(licence groups → rows → allowed codes, eligible region names)."""

    groups: dict[str, list[list[str]]] = {}
    for item in _rows(client, LICENSE_OPERATION, bid_notice_no, revision_no):
        own = _OWN_CODE_RE.search(str(item.get("lcnsLmtNm") or ""))
        allowed = {own.group(1)} if own else set()
        allowed |= set(_PERMITTED_RE.findall(str(item.get("permsnIndstrytyList") or "")))
        if allowed:
            groups.setdefault(str(item.get("lmtGrpNo") or "1"), []).append(sorted(allowed))
    regions = sorted({str(item.get("prtcptPsblRgnNm") or "").strip()
                      for item in _rows(client, REGION_OPERATION, bid_notice_no, revision_no)} - {""})
    return [groups[key] for key in sorted(groups)], regions


def check_restrictions(license_groups: list[list[list[str]]], regions: list[str],
                       company_codes: set[str]) -> dict[str, Any]:
    reasons: list[str] = []
    if license_groups and not any(all(set(row) & company_codes for row in group) for group in license_groups):
        wanted = " 또는 ".join("+".join("/".join(row) for row in group) for group in license_groups)
        reasons.append(f"나라장터 업종 제한({wanted})을 회사 등록 업종이 충족하지 않습니다.")
    if regions and not any(COMPANY_HEAD_OFFICE_REGION in region for region in regions):
        shown = ", ".join(regions[:3]) + (f" 외 {len(regions) - 3}곳" if len(regions) > 3 else "")
        reasons.append(f"나라장터 참가가능지역({shown})에 회사 본점 소재지(서울)가 없습니다.")
    if reasons:
        return {"status": "CONFLICT", "reasons": reasons}
    return {"status": "CONSISTENT" if license_groups or regions else "NO_LIMIT", "reasons": []}


def _company_codes() -> set[str]:
    from .eligibility_policy import load_public_company_profile

    value = (load_public_company_profile().get("facts", {}).get("industry_code_inventory") or {}).get("value")
    return {str(code) for code in value} if isinstance(value, list) else set()


def restriction_check_for(session: Session, notice: Notice) -> dict[str, Any] | None:
    row = session.get(PpsParticipationRestriction, notice.id)
    if row is None or row.revision_no != notice.revision_no or row.error_code:
        return None
    result = check_restrictions(row.license_groups or [], row.regions or [], _company_codes())
    return {**result, "checked_at": row.fetched_at}


def refresh_open_notices(
    session: Session, *, service_key: str, now: datetime | None = None, max_notices: int = 150,
    wall_seconds: float = 120, client_factory: Callable[[str], PpsClient] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Fetch limits for OPEN notices without a row for their current revision. Bounded in count and time."""

    now = now or datetime.now(timezone.utc)
    existing = {row.notice_id: row for row in session.scalars(select(PpsParticipationRestriction))}
    targets = [notice for notice in session.scalars(
        select(Notice).where(Notice.status == "OPEN", Notice.deadline > now).order_by(Notice.deadline))
        if notice.notice_key.startswith("PPS-") and notice.bid_notice_no
        and (notice.id not in existing or existing[notice.id].revision_no != notice.revision_no
             or existing[notice.id].error_code)][:max_notices]
    deadline = monotonic() + wall_seconds
    fetched = failed = 0
    factory = client_factory or (lambda key: PpsClient(service_key=key))
    with factory(service_key) as client:
        for notice in targets:
            if monotonic() >= deadline:
                break
            row = existing.get(notice.id) or PpsParticipationRestriction(notice_id=notice.id)
            row.bid_notice_no, row.revision_no, row.fetched_at = notice.bid_notice_no, notice.revision_no, now
            try:
                row.license_groups, row.regions = fetch_restrictions(client, notice.bid_notice_no, notice.revision_no)
                row.error_code = None
                fetched += 1
            except PpsApiError as exc:
                row.error_code = str(getattr(exc, "error_type", "PPS_ERROR"))[:40]
                failed += 1
            session.add(row)
            session.commit()
    return {"targets": len(targets), "fetched": fetched, "failed": failed}


def refresh_in_background(session_factory: Callable[[], Session], service_key: str) -> None:
    """After the daily ingestion response; never raises into the request."""

    try:
        with session_factory() as session:
            result = refresh_open_notices(session, service_key=service_key)
        logger.info("pps restriction refresh %s", result)
    except Exception:
        logger.exception("pps restriction refresh failed")


router = APIRouter(prefix="/api/v1/operations", dependencies=[Depends(require_api_key)])


class RestrictionRefreshRequest(BaseModel):
    max_notices: int = Field(default=150, ge=1, le=300)


@router.post("/pps-restrictions/refresh")
def refresh_pps_restrictions(payload: RestrictionRefreshRequest, request: Request) -> dict[str, int]:
    settings = request.app.state.settings
    if not settings.pps_api_key:
        return {"targets": 0, "fetched": 0, "failed": 0}
    with request.app.state.session_factory() as session:
        return refresh_open_notices(session, service_key=settings.pps_api_key, max_notices=payload.max_notices,
                                    wall_seconds=240)
