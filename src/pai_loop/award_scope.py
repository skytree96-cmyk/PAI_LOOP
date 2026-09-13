from __future__ import annotations

import unicodedata
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .models import Notice


AWARD_SCOPE_VERSION = "demand-agency-keyword-v1"
AWARD_AGENCY_UNAVAILABLE = "AWARD_AGENCY_UNAVAILABLE"
AWARD_AGENCY_METADATA_SOURCE = "PPS_NOTICE_LOOKUP"
_PPS_METADATA_KIND = "PPS_NOTICE_METADATA"
_AWARD_TITLE_STOPWORDS = {
    "공고", "긴급", "변경", "재공고", "입찰", "사업", "용역", "위탁",
    "위탁운영", "운영", "시행", "계약",
}
_AwardRow = TypeVar("_AwardRow")


def award_title_tokens(title: str) -> list[str]:
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", title.casefold())
    return [token for token in tokens if token not in _AWARD_TITLE_STOPWORDS
            and not re.fullmatch(r"(?:19|20)\d{2}(?:학년도|년도|년)?", token)
            and len(token) > 1]


def derive_award_keyword(title: str) -> str:
    tokens = award_title_tokens(title)
    if not tokens:
        raise ValueError("낙찰 이력 검색 키워드를 직접 입력해 주세요.")
    return " ".join(tokens[:3])[:100]


def filter_notice_awards(notice: Notice, rows: Iterable[_AwardRow]) -> list[_AwardRow]:
    """Select explicit demand agency AND every current project keyword term.

    This projection preserves the retained rows. Each consumer keeps its own
    existing time-window/as-of policy after applying the common search scope.
    """
    scope = resolve_notice_award_scope(notice)
    if not scope.available:
        return []
    try:
        terms = derive_award_keyword(notice.title).casefold().split()
    except ValueError:
        return []
    return [row for row in rows if scope.matches_award(row)
            and isinstance(title := _value(row, "title"), str)
            and all(term in title.casefold() for term in terms)]


def _agency_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    return value or None


def normalize_award_agency(value: Any) -> str:
    """Compare explicit agency identities without dropping punctuation or units."""
    return "".join((_agency_text(value) or "").casefold().split())


def _value(row: Any, field: str) -> Any:
    return row.get(field) if isinstance(row, Mapping) else getattr(row, field, None)


def award_agency_is_verifiable(
    row: Any, *, demand_agency_name: str | None = None,
    demand_agency_code: str | None = None,
) -> bool:
    """Distinguish a known agency mismatch from missing comparison evidence."""
    if (normalize_award_agency(demand_agency_code)
            and normalize_award_agency(_value(row, "demand_agency_code"))):
        return True
    return bool(normalize_award_agency(demand_agency_name) and normalize_award_agency(
        _value(row, "demand_agency_name") or _value(row, "agency")
    ))


def matches_award_agency(
    row: Any, *, demand_agency_name: str | None = None,
    demand_agency_code: str | None = None,
) -> bool:
    """Use authoritative codes when both are present; otherwise exact names.

    ``agency`` is the legacy award record's demand-agency projection, never
    the target Notice's ambiguous announcing-agency display field.
    """
    expected_code = normalize_award_agency(demand_agency_code)
    actual_code = normalize_award_agency(_value(row, "demand_agency_code"))
    if expected_code and actual_code:
        return expected_code == actual_code
    expected_name = normalize_award_agency(demand_agency_name)
    actual_name = normalize_award_agency(
        _value(row, "demand_agency_name") or _value(row, "agency")
    )
    return bool(expected_name and actual_name and expected_name == actual_name)


@dataclass(frozen=True, slots=True)
class AwardScope:
    demand_agency_name: str | None = None
    demand_agency_code: str | None = None
    announcing_agency_name: str | None = None
    announcing_agency_code: str | None = None
    metadata_version_no: int | None = None
    reason_code: str | None = AWARD_AGENCY_UNAVAILABLE

    @property
    def available(self) -> bool:
        return bool(self.demand_agency_name or self.demand_agency_code)

    def matches_award(self, row: Any) -> bool:
        return matches_award_agency(
            row, demand_agency_name=self.demand_agency_name,
            demand_agency_code=self.demand_agency_code,
        )


def _scope(metadata: Any, version_no: int) -> AwardScope:
    values = {
        field: _agency_text(_value(metadata, field))
        for field in (
            "demand_agency_name", "demand_agency_code",
            "announcing_agency_name", "announcing_agency_code",
        )
    }
    return AwardScope(
        **values, metadata_version_no=version_no,
        reason_code=None if values["demand_agency_name"] or values["demand_agency_code"]
        else AWARD_AGENCY_UNAVAILABLE,
    )


def _observed_key(row: Any) -> tuple[datetime, str]:
    value = _value(row, "observed_at")
    if not isinstance(value, datetime):
        value = datetime.min.replace(tzinfo=timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value, str(_value(row, "id") or "")


def resolve_notice_award_scope(notice: Notice) -> AwardScope:
    """Resolve demand agency from the current PPS basis or its exact sidecar.

    No older metadata, Notice.agency, announcing agency, or title is a substitute
    for missing demand agency. Callers should preload both relationships when
    resolving a batch; this resolver performs no requests or persistence.
    """
    versions = [
        version for version in notice.versions
        if isinstance(version.source_payload, dict)
        and version.source_payload.get("kind") == _PPS_METADATA_KIND
    ]
    if not versions:
        return AwardScope()
    current = max(versions, key=lambda version: version.version_no)
    payload = current.source_payload
    identity = payload.get("notice_identity")
    if isinstance(identity, dict) and any(
        identity.get(field) != getattr(notice, field)
        for field in ("bid_notice_no", "revision_no")
    ):
        return AwardScope(metadata_version_no=current.version_no)
    direct = _scope(payload.get("notice_metadata"), current.version_no)
    if direct.available:
        return direct
    current_id = current.id
    if not current_id:
        return direct
    candidates = [
        record for record in getattr(notice, "award_agency_metadata", ())
        if record.notice_id == notice.id
        and record.notice_version_id == current_id
        and record.bid_notice_no == notice.bid_notice_no
        and record.revision_no == notice.revision_no
        and record.source == AWARD_AGENCY_METADATA_SOURCE
    ]
    if not candidates:
        return direct
    # A newest empty observation is authoritative; never revive an old success.
    return _scope(max(candidates, key=_observed_key), current.version_no)
