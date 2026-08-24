from __future__ import annotations

import math
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from enum import Enum

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from .integrations.company_awards import (
    DEFAULT_COMPANY_BUSINESS_NUMBER,
    DEFAULT_COMPANY_NAME,
    AwardScope,
    PpsCompanyAwardClient,
    normalise_business_number,
)
from .integrations.pps import KST, PpsApiError
from .manual_analysis import (
    _manual_feature_enabled,
    _require_manual_operator,
    _same_origin_request,
)

router = APIRouter(prefix="/api/v1", tags=["company awards"])

_MAX_SEARCH_API_CALLS = 60
# Three calendar years can contain a leap day; inclusive date bounds therefore
# need room for 1,097 days.
_MAX_SEARCH_DAYS = 1097
# The PPS PPSSrch contract says "one month", not "30 days".  In particular,
# a 30-day inclusive window beginning in February is rejected with provider
# resultCode=07 (input range exceeded).  Twenty-eight inclusive days are valid
# in every calendar month and keep the operator search deterministic.
_WINDOW_DAYS = 28
_ROWS_PER_PAGE = 100
# The current Render deployment runs one application worker, so a process lock
# prevents overlapping operator searches without blocking the separate manual
# OpenAI execution slot.  Replace this with a distributed/advisory lock before
# scaling the web service to multiple workers or instances.
_SEARCH_LOCK = threading.Lock()
_SEARCH_WALL_SECONDS = 70.0
_SEARCH_HTTP_TIMEOUT_SECONDS = 12.0
_SEARCH_MAX_RETRIES = 1
_MAX_RESPONSE_RECORDS = 500


class CompanyAwardScope(str, Enum):
    goods = "goods"
    construction = "construction"
    service = "service"
    foreign = "foreign"


def _default_start_date() -> date:
    today = datetime.now(KST).date()
    try:
        return today.replace(year=today.year - 3)
    except ValueError:
        return today - timedelta(days=365 * 3)


def _today_kst() -> date:
    return datetime.now(KST).date()


class CompanyAwardSearchRequest(BaseModel):
    """Operator-approved PPS lookup; the identifier is never persisted."""

    model_config = ConfigDict(extra="forbid")

    business_number: str = DEFAULT_COMPANY_BUSINESS_NUMBER
    start_date: date = Field(default_factory=_default_start_date)
    end_date: date = Field(default_factory=_today_kst)
    scopes: list[CompanyAwardScope] = Field(
        default_factory=lambda: [CompanyAwardScope.service],
        min_length=1,
        max_length=4,
    )
    max_pages_per_window: int = Field(default=1, ge=1, le=2)


class CompanyAwardCompany(BaseModel):
    name: str | None
    is_default: bool


class CompanyAwardQuery(BaseModel):
    start_date: date
    end_date: date
    scopes: list[CompanyAwardScope]


class CompanyAwardRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scope: CompanyAwardScope
    bid_notice_no: str
    revision_no: str
    classification_no: str
    rebid_no: str
    title: str
    participant_count: int | None = None
    winner_name: str
    award_amount: float | None = None
    award_rate: float | None = None
    opened_at: datetime | None = None
    agency: str
    registered_at: datetime | None = None
    awarded_at: datetime | None = None


class CompanyAwardSearchResponse(BaseModel):
    company: CompanyAwardCompany
    query: CompanyAwardQuery
    records: list[CompanyAwardRecord]
    count: int
    api_calls: int
    truncated: bool
    partial: bool
    warnings: list[str]


def _require_company_award_operator(request: Request) -> None:
    if not _manual_feature_enabled(request):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="회사별 낙찰 결과 조회 기능이 비활성화되어 있습니다.",
        )
    if not _same_origin_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="홈페이지와 동일한 출처에서만 낙찰 결과를 조회할 수 있습니다.",
        )
    _require_manual_operator(request)


def _validated_search_bounds(
    payload: CompanyAwardSearchRequest,
) -> tuple[str, list[AwardScope], int]:
    try:
        business_number = normalise_business_number(payload.business_number)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="사업자등록번호는 숫자 10자리로 입력해 주세요.",
        ) from exc
    if payload.end_date < payload.start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="종료일은 시작일보다 빠를 수 없습니다.",
        )
    day_count = (payload.end_date - payload.start_date).days + 1
    if day_count > _MAX_SEARCH_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="한 번에 조회할 수 있는 기간은 최대 3년입니다.",
        )
    scopes = [scope.value for scope in payload.scopes]
    if len(set(scopes)) != len(scopes):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="조회 업무 범위는 중복될 수 없습니다.",
        )
    window_count = math.ceil(day_count / _WINDOW_DAYS)
    maximum_calls = window_count * len(scopes) * payload.max_pages_per_window
    if maximum_calls > _MAX_SEARCH_API_CALLS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "조회 범위가 너무 큽니다. 기간, 업무 범위 또는 페이지 수를 줄여 주세요."
            ),
        )
    return business_number, scopes, maximum_calls


def _company_name(records: list[dict[str, object]], *, is_default: bool) -> str | None:
    if is_default:
        return DEFAULT_COMPANY_NAME
    names = Counter(
        str(record.get("winner_name") or "").strip()
        for record in records
        if str(record.get("winner_name") or "").strip()
    )
    return names.most_common(1)[0][0] if names else None


def _award_sort_key(item: dict[str, object]) -> tuple[float, str]:
    value = item.get("awarded_at") or item.get("opened_at")
    if not isinstance(value, datetime):
        timestamp = float("-inf")
    else:
        if value.tzinfo is None:
            value = value.replace(tzinfo=KST)
        timestamp = value.timestamp()
    return timestamp, str(item.get("bid_notice_no") or "")


@router.post("/company-awards/search", response_model=CompanyAwardSearchResponse)
def search_company_awards(
    payload: CompanyAwardSearchRequest,
    request: Request,
) -> CompanyAwardSearchResponse:
    """Search PPS awards without OpenAI calls or database persistence."""

    _require_company_award_operator(request)
    business_number, scopes, _maximum_calls = _validated_search_bounds(payload)
    settings = request.app.state.settings
    if not settings.pps_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="조달청 API 키가 설정되지 않았습니다.",
        )
    if not _SEARCH_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="다른 낙찰 결과 조회를 처리 중입니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": "15"},
        )

    records_by_key: dict[tuple[str, str], dict[str, object]] = {}
    warnings: list[str] = []
    successful_scopes = 0
    api_calls = 0
    truncated = False
    stopped_early = False
    result_capped = False
    deadline_monotonic = time.monotonic() + _SEARCH_WALL_SECONDS
    try:
        with PpsCompanyAwardClient(
            service_key=settings.pps_api_key,
            base_url=settings.pps_base_url,
            timeout_seconds=_SEARCH_HTTP_TIMEOUT_SECONDS,
            max_retries=_SEARCH_MAX_RETRIES,
        ) as client:
            for scope in scopes:
                before = client.request_count
                try:
                    iterator = client.iter_company_awards(
                        start=payload.start_date,
                        end=payload.end_date,
                        business_number=business_number,
                        scopes=[scope],
                        rows=_ROWS_PER_PAGE,
                        max_window_days=_WINDOW_DAYS,
                        max_pages_per_window=payload.max_pages_per_window,
                        deadline_monotonic=deadline_monotonic,
                    )
                    for record in iterator:
                        identity = str(record.get("identity") or "")
                        key = (str(record.get("scope") or ""), identity)
                        if (
                            key not in records_by_key
                            and len(records_by_key) >= _MAX_RESPONSE_RECORDS
                        ):
                            result_capped = True
                            stopped_early = True
                            truncated = True
                            iterator.close()
                            break
                        records_by_key[key] = record
                    successful_scopes += 1
                    truncated = truncated or client.hit_page_limit
                    if client.provider_mismatch_count:
                        warnings.append(
                            f"{scope}: 조회 대상과 일치하지 않는 제공기관 행을 제외했습니다."
                        )
                    if (
                        client.hit_time_limit
                        or time.monotonic() >= deadline_monotonic
                    ):
                        stopped_early = True
                        truncated = True
                        warnings.append(
                            "전체 조회시간 제한에 도달하여 일부 기간 또는 업무 범위를 조회하지 못했습니다."
                        )
                except PpsApiError:
                    warnings.append(f"{scope}: 조달청 조회에 실패했습니다.")
                finally:
                    api_calls += client.request_count - before
                if (
                    not stopped_early
                    and time.monotonic() >= deadline_monotonic
                ):
                    stopped_early = True
                    truncated = True
                    warnings.append(
                        "전체 조회시간 제한에 도달하여 일부 기간 또는 업무 범위를 조회하지 못했습니다."
                    )
                if result_capped or stopped_early:
                    break
    finally:
        _SEARCH_LOCK.release()

    if not successful_scopes:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="조달청 낙찰 결과를 조회하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )

    if result_capped:
        warnings.append(
            f"화면 응답은 최대 {_MAX_RESPONSE_RECORDS}건으로 제한되어 나머지 결과를 생략했습니다."
        )
    ordered = sorted(
        records_by_key.values(),
        key=_award_sort_key,
        reverse=True,
    )
    is_default = business_number == DEFAULT_COMPANY_BUSINESS_NUMBER
    response_records = [CompanyAwardRecord.model_validate(item) for item in ordered]
    return CompanyAwardSearchResponse(
        company=CompanyAwardCompany(
            name=_company_name(ordered, is_default=is_default),
            is_default=is_default,
        ),
        query=CompanyAwardQuery(
            start_date=payload.start_date,
            end_date=payload.end_date,
            scopes=[CompanyAwardScope(scope) for scope in scopes],
        ),
        records=response_records,
        count=len(response_records),
        api_calls=api_calls,
        truncated=truncated,
        partial=stopped_early or successful_scopes != len(scopes),
        warnings=warnings,
    )
