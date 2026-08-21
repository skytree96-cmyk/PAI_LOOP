from __future__ import annotations

import hashlib
import io
import json
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import Any, Callable, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .department_ranking import (
    get_department_profile,
    load_department_keyword_profiles,
)
from .integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ExtractionOutcome,
    ExtractionPayload,
    OpenAIExtractionClient,
)
from .models import Notice, NoticeVersion


PPS_METADATA_KIND = "PPS_NOTICE_METADATA"
PPS_METADATA_SCHEMA = "pai-loop-pps-notice-metadata-0.1.0"
PPS_ATTACHMENT_SOURCE = "PPS_PUBLIC_ATTACHMENT"
G2B_ATTACHMENT_HOST = "www.g2b.go.kr"
G2B_ATTACHMENT_PATH = "/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
G2B_ATTACHMENT_QUERY_KEYS = {
    "bidPbancNo",
    "bidPbancOrd",
    "fileSeq",
    "fileType",
    "prcmBsneSeCd",
}
MAX_ATTACHMENTS_IN_MANIFEST = 10
DEFAULT_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_NOTICE_DOWNLOAD_BYTES = MAX_ATTACHMENTS_IN_MANIFEST * DEFAULT_MAX_DOWNLOAD_BYTES
MAX_EXTRACTED_DOCUMENT_CHARS = 2_000_000
MAX_NOTICE_EXTRACTED_CHARS = MAX_ATTACHMENTS_IN_MANIFEST * MAX_EXTRACTED_DOCUMENT_CHARS
MAX_ANALYSIS_INPUT_CHARS = 120_000
MAX_OPENAI_CALLS_PER_ATTACHMENT = 2
MAX_OPENAI_CALLS_PER_NOTICE = (
    MAX_ATTACHMENTS_IN_MANIFEST * MAX_OPENAI_CALLS_PER_ATTACHMENT
)
MAX_NEW_ATTACHMENTS_PER_REQUEST = 2
from .document_extraction import (
    DocumentExtractionError,
    DocumentExtractionResult,
    ExtractionLimits,
    extract_document_content,
)
PPS_PROCESSING_VERSION = "pps-document-processing-0.2.0"
MAX_PDF_PAGES = 120
MAX_HWPX_ENTRIES = 240
MAX_HWPX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
REVIEW_RETRY_COOLDOWN = timedelta(hours=24)
DETERMINISTIC_REVIEW_CODES = {
    "HWP_ONLY_UNSUPPORTED_R07",
    "HWP_BINARY_UNSUPPORTED",
    "UNSUPPORTED_ATTACHMENT_TYPE",
}

# These are high-recall provider discovery terms, separate from department
# ranking vocabulary.  ``연수`` and ``포럼`` cover relevant service notices
# whose titles do not contain the stronger ``교육``/``컨설팅`` labels, while
# ``위탁 운영`` covers the common procurement phrasing without the much
# noisier single token ``위탁``.  Department-specific strong terms still
# provide explainable coverage after these organization-wide queries.
PROFILE_DISCOVERY_KEYWORDS = ("교육", "컨설팅", "연수", "포럼", "위탁 운영")

_ATTACHMENT_ID_PATTERN = re.compile(r"^PPS-ATT-[a-f0-9]{24}$")
_SAFE_QUERY_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
_SUPPORTED_EXTENSION_MEDIA = {
    ".pdf": "application/pdf",
    ".hwpx": "application/hwp+zip",
    ".hwp": "application/x-hwp",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".html": "text/html",
    ".htm": "text/html",
    ".zip": "application/zip",
}
_EXTRACTABLE_EXTENSIONS = {
    ".pdf",
    ".hwpx",
    ".hwp",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".docx",
    ".pptx",
    ".html",
    ".htm",
    ".zip",
}
_PUBLIC_METADATA_EXTENSION = re.compile(r"^\.[a-z0-9]{1,12}$")
UNSUPPORTED_PUBLIC_MEDIA_TYPE = "application/octet-stream"
_PUBLIC_EMAIL_PATTERN = re.compile(
    r"(?i)\b[A-Z0-9._%+\-]+" + "@" + r"[A-Z0-9.\-]+\.[A-Z]{2,}\b"
)
_PUBLIC_PHONE_PATTERN = re.compile(r"(?<!\d)0\d{1,2}[ -]?\d{3,4}[ -]?\d{4}(?!\d)")
_PUBLIC_CONTACT_PATTERN = re.compile(
    r"(?i)(?:(?:문의|연락)\s*)?(?:담당자?|문의처?|연락처|contact)"
    r"\s*(?:[:：]\s*)?(?:[가-힣]{2,5}|[A-Z][A-Z .'-]{1,40})"
    r"(?:\s*(?:주무관|과장|팀장|담당자))?"
)
_METADATA_FIELDS = {
    "ntceKindNm": "notice_kind",
    "bidMethdNm": "bid_method",
    "cntrctCnclsMthdNm": "contract_method",
    "sucsfbidMthdNm": "award_method",
    "prearngPrceDcsnMthdNm": "estimated_price_method",
    "rsrvtnPrceReMkngMthdNm": "reserve_price_method",
    "srvceDivNm": "service_division",
    "pubPrcrmntClsfcNm": "procurement_classification",
    "bidPrceEvlRt": "price_evaluation_rate",
    "techAbltEvlRt": "technical_evaluation_rate",
    "sucsfbidLwltRate": "minimum_award_rate",
    "dcmtgOprtnDt": "briefing_at",
}


class PpsEnrichmentError(RuntimeError):
    """Public-safe failure code for bounded PPS attachment processing."""


@dataclass(frozen=True, slots=True)
class MetadataVersionResult:
    created: bool
    reused: bool
    attachment_count: int
    version_id: str | None


@dataclass(frozen=True, slots=True)
class PpsAttachmentEnrichmentResult:
    """Sanitised, manifest-bound audit for one public attachment.

    The audit deliberately excludes provider URLs, filenames, document text,
    and model response identifiers.  A row is emitted for every valid item in
    the current PPS manifest, including deterministic HWP reviews, so callers
    can prove that one failed attachment did not hide successful siblings.
    """

    attachment_id: str
    media_type: str
    status: Literal["COMPLETED", "REUSED", "REVIEW", "PLANNED"]
    reason_code: str
    attempted: bool = False
    content_extracted: bool = False
    source_read_complete: bool = False
    analysis_input_complete: bool = False
    source_characters: int = 0
    analysis_input_characters: int = 0
    members_discovered: int = 0
    members_processed: int = 0
    openai_calls: int = 0
    version_id: str | None = None


@dataclass(frozen=True, slots=True)
class PpsEnrichmentResult:
    status: Literal["COMPLETED", "REUSED", "REVIEW", "SKIPPED", "PLANNED"]
    attachments_discovered: int = 0
    attachments_attempted: int = 0
    attachments_processed: int = 0
    downloaded_bytes: int = 0
    source_characters: int = 0
    analysis_input_characters: int = 0
    source_read_complete: bool = False
    analysis_input_complete: bool = False
    members_discovered: int = 0
    members_processed: int = 0
    openai_calls: int = 0
    version_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    attachment_results: list[PpsAttachmentEnrichmentResult] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PpsAttachmentCoverage:
    discovered: int = 0
    valid: int = 0
    audited: int = 0
    supported: int = 0
    accepted: int = 0
    source_complete: int = 0
    complete: bool = False
    all_supported_accepted: bool = False


@dataclass(frozen=True, slots=True)
class DocumentAnalysisSelection:
    """Bounded LLM input plus an audit of how the full extracted text was scanned."""

    text: str
    complete: bool
    source_characters: int
    selected_characters: int
    total_lines: int
    selected_line_ranges: tuple[tuple[int, int], ...]
    signal_line_count: int


AnalysisState = Literal["ANALYZED", "REVIEW", "PENDING"]
AnalysisReasonCode = Literal[
    "ANALYZED",
    "ATTACHMENT_NONE",
    "ATTACHMENT_COVERAGE_INCOMPLETE",
    "HWP_ONLY_UNSUPPORTED",
    "UNSUPPORTED_ATTACHMENT",
    "HWPX_EXTRACT_FAILED",
    "PDF_EXTRACT_FAILED",
    "DOCUMENT_EXTRACT_FAILED",
    "OPENAI_REVIEW",
    "QUOTE_UNVERIFIED",
    "NOT_SELECTED",
]


@dataclass(frozen=True, slots=True)
class PublicAnalysisReason:
    """A deliberately small, public-safe explanation of analysis coverage.

    Raw provider errors, filenames, URLs, model response identifiers and
    internal exception names never cross this boundary. The reason is derived
    only from the current PPS attachment manifest and its latest bound
    extraction attempt, so a corrected notice cannot inherit stale history.
    """

    state: AnalysisState
    reason_code: AnalysisReasonCode
    reason: str
    attachment_count: int
    attempted: bool


_PUBLIC_ANALYSIS_MESSAGES: dict[AnalysisReasonCode, str] = {
    "ANALYZED": "현재 공고 첨부의 근거 추출과 검증이 완료되었습니다.",
    "ATTACHMENT_NONE": "조달청 공고에서 자동 분석 가능한 공개 첨부를 찾지 못했습니다.",
    "ATTACHMENT_COVERAGE_INCOMPLETE": "현재 공고의 공개 첨부 중 아직 분석 감사가 완료되지 않은 파일이 있습니다.",
    "HWP_ONLY_UNSUPPORTED": "구형 HWP 공개 첨부가 포함되어 해당 파일은 원문 검토가 필요합니다.",
    "UNSUPPORTED_ATTACHMENT": "현재 자동 텍스트 추출을 지원하지 않는 공개 첨부가 포함되어 원문 검토가 필요합니다.",
    "HWPX_EXTRACT_FAILED": "HWPX 첨부의 다운로드 또는 텍스트 추출을 완료하지 못했습니다.",
    "PDF_EXTRACT_FAILED": "PDF 첨부의 다운로드 또는 텍스트 추출을 완료하지 못했습니다.",
    "DOCUMENT_EXTRACT_FAILED": "공개 첨부의 다운로드 또는 안전한 텍스트 추출을 완료하지 못했습니다.",
    "OPENAI_REVIEW": "문서는 읽었지만 LLM 구조화 또는 검증 단계를 완료하지 못해 재검토가 필요합니다.",
    "QUOTE_UNVERIFIED": "LLM이 제시한 인용문을 첨부 원문에서 정확히 대조하지 못했습니다.",
    "NOT_SELECTED": "아직 일일 분석 대상으로 선택되지 않은 공고입니다.",
}


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_text(value: Any, *, maximum: int = 500) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or any(ord(character) < 32 for character in text):
        return None
    return text[:maximum]


def _safe_g2b_attachment_url(value: Any) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != G2B_ATTACHMENT_HOST
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.path != G2B_ATTACHMENT_PATH
        or parsed.fragment
    ):
        raise PpsEnrichmentError("UNSAFE_ATTACHMENT_URL")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    keys = [key for key, _value in pairs]
    if (
        not pairs
        or len(keys) != len(set(keys))
        or set(keys) - G2B_ATTACHMENT_QUERY_KEYS
        or not {"bidPbancNo", "fileSeq"}.issubset(keys)
        or any(
            not _SAFE_QUERY_VALUE.fullmatch(value)
            and not (key == "fileType" and value == "")
            for key, value in pairs
        )
    ):
        raise PpsEnrichmentError("UNSAFE_ATTACHMENT_URL")
    query = urlencode(sorted(pairs))
    return urlunsplit(("https", G2B_ATTACHMENT_HOST, parsed.path, query, ""))


def _safe_filename(value: Any) -> tuple[str, str]:
    filename = re.sub(r"\s+", " ", str(value or "")).strip()
    if not _SAFE_FILENAME.fullmatch(filename) or PurePath(filename).name != filename:
        raise PpsEnrichmentError("UNSAFE_ATTACHMENT_FILENAME")
    extension = PurePath(filename).suffix.casefold()
    if not _PUBLIC_METADATA_EXTENSION.fullmatch(extension):
        raise PpsEnrichmentError("UNSAFE_ATTACHMENT_FILENAME")
    # Unknown public formats remain in the immutable manifest and receive an
    # explicit per-attachment REVIEW.  They are never downloaded merely
    # because their metadata was preserved.
    media_type = _SUPPORTED_EXTENSION_MEDIA.get(extension, UNSUPPORTED_PUBLIC_MEDIA_TYPE)
    return filename, media_type


def build_attachment_manifest(raw_item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only public g2b attachment fields from a provider response.

    Provider payloads may contain officer names, email addresses, telephone
    numbers, and many unrelated fields. This function is the only bridge into a
    stored manifest and intentionally ignores everything outside the allowlist.
    """

    notice_no = _clean_text(raw_item.get("bidNtceNo"), maximum=80) or "unknown"
    revision = _clean_text(raw_item.get("bidNtceOrd"), maximum=20) or "00"
    manifest: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for slot in range(1, MAX_ATTACHMENTS_IN_MANIFEST + 1):
        raw_url = raw_item.get(f"ntceSpecDocUrl{slot}")
        raw_name = raw_item.get(f"ntceSpecFileNm{slot}")
        if not raw_url and not raw_name:
            continue
        try:
            if not raw_url or not raw_name:
                raise PpsEnrichmentError("ATTACHMENT_METADATA_INCOMPLETE")
            url = _safe_g2b_attachment_url(raw_url)
            filename, media_type = _safe_filename(raw_name)
        except PpsEnrichmentError as exc:
            # A provider-declared slot must never disappear from coverage just
            # because its metadata is unsafe. Persist only a stable digest and
            # public-safe code; raw filenames/URLs/contact data remain outside
            # the database boundary.
            manifest.append(
                {
                    "invalid_attachment_slot": slot,
                    "status": "INVALID",
                    "error_code": str(exc),
                    "metadata_sha256": _digest(
                        {"url": str(raw_url or ""), "name": str(raw_name or "")}
                    ),
                }
            )
            continue
        attachment_id = "PPS-ATT-" + _digest(
            {"notice_no": notice_no, "revision": revision, "slot": slot, "file_name": filename}
        )[:24]
        if attachment_id in seen_ids:
            continue
        seen_ids.add(attachment_id)
        manifest.append(
            {
                "attachment_id": attachment_id,
                "file_name": filename,
                "media_type": media_type,
                "url": url,
                "slot": slot,
            }
        )
    return manifest


def build_notice_metadata(raw_item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for provider_key, public_key in _METADATA_FIELDS.items():
        value = raw_item.get(provider_key)
        if provider_key in {"bidPrceEvlRt", "techAbltEvlRt", "sucsfbidLwltRate"}:
            try:
                metadata[public_key] = float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                continue
        else:
            cleaned = _clean_text(value)
            if cleaned:
                metadata[public_key] = cleaned
    bid_close = _parse_provider_datetime(raw_item.get("bidClseDt"))
    opening = _parse_provider_datetime(raw_item.get("opengDt") or raw_item.get("rbidOpengDt"))
    bid_method = _clean_text(raw_item.get("bidMethdNm")) or ""
    if bid_close is None and opening is not None and "직찰" in bid_method:
        # Additive only for PPS direct-bid rows that otherwise had no usable
        # deadline.  Contact fields and arbitrary provider data remain outside
        # the stored allowlist.
        metadata["deadline_basis"] = "OPENING_FALLBACK"
        metadata["opening_at"] = opening
    return metadata


def _parse_provider_datetime(value: Any) -> str | None:
    if value in (None, ""):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    if len(digits) == 14:
        try:
            parsed = datetime.strptime(digits, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone(timedelta(hours=9))).isoformat()
    if len(digits) == 12:
        try:
            parsed = datetime.strptime(digits, "%Y%m%d%H%M")
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone(timedelta(hours=9))).isoformat()
    return None


def resolve_ingestion_keywords(
    *,
    keyword: str | None,
    keywords: list[str],
    use_profile_keywords: bool,
    profile_department_ids: list[str],
    limit: int = 30,
) -> tuple[list[str], bool]:
    """Resolve profile queries with one representative for every selected department.

    Baseline supporting terms remain ranking vocabulary only. Query expansion
    uses five fixed discovery terms plus the first unique strong term from each
    department (24 for the full profile), leaving one slot for an explicit user
    term under the 30-query provider cap.
    """

    explicit: list[str] = []
    explicit_seen: set[str] = set()
    for raw_value in [item for item in [keyword, *keywords] if item]:
        for candidate in re.split(r"[,\n|]+", str(raw_value)):
            cleaned = re.sub(r"\s+", " ", candidate).strip()
            normalised = cleaned.casefold()
            if not cleaned or normalised in explicit_seen:
                continue
            if len(cleaned) > 60:
                raise ValueError("검색 키워드는 60자 이하여야 합니다.")
            explicit_seen.add(normalised)
            explicit.append(cleaned)
            if len(explicit) > limit:
                raise ValueError(f"조달청 검색 키워드는 최대 {limit}개까지 입력할 수 있습니다.")
    if not use_profile_keywords:
        return explicit[:limit], len(explicit) > limit

    catalog = load_department_keyword_profiles()
    if profile_department_ids:
        profiles = []
        for profile_id in profile_department_ids:
            selected = get_department_profile(profile_id)
            if selected is None:
                profiles = list(catalog["departments"])
                break
            profiles.append(selected)
    else:
        profiles = list(catalog["departments"])

    # Explicit operator terms must never be silently displaced by profile
    # expansion.  The fixed discovery set remains first for stable daily
    # contracts, followed by explicit terms and then per-department terms.
    candidates: list[str] = list(PROFILE_DISCOVERY_KEYWORDS)
    candidates.extend(explicit)
    seen = {re.sub(r"\s+", " ", item).strip().casefold() for item in candidates}
    for profile in profiles:
        strong = list(profile.get("strong_keywords", []))
        representative = next(
            (
                item
                for item in strong
                if re.sub(r"\s+", " ", str(item)).strip().casefold() not in seen
            ),
            strong[0] if strong else None,
        )
        if representative:
            cleaned = re.sub(r"\s+", " ", str(representative)).strip()
            if cleaned.casefold() not in seen:
                candidates.append(cleaned)
                seen.add(cleaned.casefold())
    unique: list[str] = []
    seen.clear()
    for candidate in candidates:
        cleaned = re.sub(r"\s+", " ", str(candidate)).strip()
        normalised = cleaned.casefold()
        if not cleaned or normalised in seen:
            continue
        if len(cleaned) > 60:
            raise ValueError("검색 키워드는 60자 이하여야 합니다.")
        seen.add(normalised)
        unique.append(cleaned)
    return unique[:limit], len(unique) > limit


def department_keyword_coverage_count(
    keywords: list[str],
    *,
    profile_department_ids: list[str],
) -> int:
    catalog = load_department_keyword_profiles()
    if profile_department_ids:
        profiles = [get_department_profile(item) for item in profile_department_ids]
        selected = [item for item in profiles if item is not None]
    else:
        selected = list(catalog["departments"])
    query_terms = {re.sub(r"\s+", " ", item).strip().casefold() for item in keywords}
    return sum(
        any(
            re.sub(r"\s+", " ", str(term)).strip().casefold() in query_terms
            for term in profile.get("strong_keywords", [])
        )
        for profile in selected
    )


def _current_manifest_attempts(
    versions: list[NoticeVersion],
) -> tuple[
    list[dict[str, Any]],
    int,
    dict[str, NoticeVersion],
]:
    """Return validated current attachments and their latest bound attempts."""

    metadata = next(
        (
            item
            for item in versions
            if isinstance(item.source_payload, dict)
            and item.source_payload.get("kind") == PPS_METADATA_KIND
            and isinstance(item.source_payload.get("attachment_manifest"), list)
        ),
        None,
    )
    if metadata is None:
        return [], 0, {}
    raw_manifest = [
        dict(item)
        for item in metadata.source_payload.get("attachment_manifest", [])
        if isinstance(item, dict)
    ]
    attachments, invalid_count = _validated_manifest_attachments(raw_manifest)
    current_digests = {
        attachment["attachment_id"]: _digest(attachment)
        for attachment in attachments
    }
    attempts: dict[str, NoticeVersion] = {}
    for version in sorted(versions, key=lambda item: item.version_no):
        payload = version.source_payload
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION"
            or payload.get("source_kind") != PPS_ATTACHMENT_SOURCE
            or payload.get("prompt_version") != PROMPT_VERSION
            or payload.get("processing_version") != PPS_PROCESSING_VERSION
        ):
            continue
        attachment_id = str(payload.get("attachment_id") or "")
        if payload.get("manifest_sha256") != current_digests.get(attachment_id):
            continue
        attempts[attachment_id] = version
    return attachments, invalid_count, attempts


def has_current_accepted_pps_extraction(session: Session, notice_id: str) -> bool:
    """Return true when every current manifest item has a terminal audit.

    Every supported item must be ACCEPTED with complete source/model coverage.
    A genuinely unsupported format counts as audited only after an explicit,
    manifest-bound deterministic REVIEW marker. Invalid or missing manifest
    entries always fail closed.
    """

    versions = list(
        session.scalars(
            select(NoticeVersion)
            .where(NoticeVersion.notice_id == notice_id)
            .order_by(NoticeVersion.version_no.desc())
        ).all()
    )
    attachments, invalid_count, attempts = _current_manifest_attempts(versions)
    if not attachments or invalid_count:
        return False
    for attachment in attachments:
        attempt = attempts.get(attachment["attachment_id"])
        if attempt is None or not isinstance(attempt.source_payload, dict):
            return False
        payload = attempt.source_payload
        extension = PurePath(attachment["file_name"]).suffix.casefold()
        if extension not in _EXTRACTABLE_EXTENSIONS:
            if (
                payload.get("status") != "REVIEW"
                or payload.get("error_code") not in DETERMINISTIC_REVIEW_CODES
            ):
                return False
        elif (
            payload.get("status") != "ACCEPTED"
            or attempt.extraction_status not in {"ACCEPTED", "COMPLETE"}
            or not attempt.document_complete
        ):
            return False
    return True


def current_pps_attachment_coverage(
    session: Session,
    notice_id: str,
) -> PpsAttachmentCoverage:
    versions = list(
        session.scalars(
            select(NoticeVersion)
            .where(NoticeVersion.notice_id == notice_id)
            .order_by(NoticeVersion.version_no.desc())
        ).all()
    )
    return pps_attachment_coverage(versions)


def pps_attachment_coverage(
    versions: list[NoticeVersion],
) -> PpsAttachmentCoverage:
    metadata = next(
        (
            item
            for item in versions
            if isinstance(item.source_payload, dict)
            and item.source_payload.get("kind") == PPS_METADATA_KIND
            and isinstance(item.source_payload.get("attachment_manifest"), list)
        ),
        None,
    )
    if metadata is None or not isinstance(metadata.source_payload, dict):
        return PpsAttachmentCoverage()
    raw_manifest = [
        dict(item)
        for item in metadata.source_payload.get("attachment_manifest", [])
        if isinstance(item, dict)
    ]
    attachments, invalid_count, attempts = _current_manifest_attempts(versions)
    supported = [
        item
        for item in attachments
        if PurePath(item["file_name"]).suffix.casefold() in _EXTRACTABLE_EXTENSIONS
    ]
    accepted = 0
    source_complete = 0
    for attachment in supported:
        attempt = attempts.get(attachment["attachment_id"])
        payload = attempt.source_payload if attempt and isinstance(attempt.source_payload, dict) else {}
        if (
            attempt is not None
            and payload.get("status") == "ACCEPTED"
            and attempt.extraction_status in {"ACCEPTED", "COMPLETE"}
            and attempt.document_complete
        ):
            accepted += 1
            source_complete += 1
    complete = (
        bool(attachments)
        and not invalid_count
        and len(raw_manifest) == len(attachments)
        and len(attempts) == len(attachments)
    )
    return PpsAttachmentCoverage(
        discovered=len(raw_manifest),
        valid=len(attachments),
        audited=len(attempts),
        supported=len(supported),
        accepted=accepted,
        source_complete=source_complete,
        complete=complete,
        all_supported_accepted=complete and accepted == len(supported),
    )


def _manifest_item(value: dict[str, Any]) -> dict[str, Any]:
    attachment_id = str(value.get("attachment_id") or "")
    if not _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id):
        raise PpsEnrichmentError("INVALID_ATTACHMENT_MANIFEST")
    filename, media_type = _safe_filename(value.get("file_name"))
    if value.get("media_type") != media_type:
        raise PpsEnrichmentError("INVALID_ATTACHMENT_MANIFEST")
    url = _safe_g2b_attachment_url(value.get("url"))
    slot = value.get("slot")
    if not isinstance(slot, int) or not 1 <= slot <= MAX_ATTACHMENTS_IN_MANIFEST:
        raise PpsEnrichmentError("INVALID_ATTACHMENT_MANIFEST")
    return {
        "attachment_id": attachment_id,
        "file_name": filename,
        "media_type": media_type,
        "url": url,
        "slot": slot,
    }


def _validated_manifest_attachments(
    manifest: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Validate every provider-bound manifest slot without silently truncating it."""

    validated: list[dict[str, Any]] = []
    invalid_count = max(0, len(manifest) - MAX_ATTACHMENTS_IN_MANIFEST)
    seen_ids: set[str] = set()
    # PPS exposes exactly ten numbered attachment slots.  The manifest builder
    # already enforces that provider boundary; slicing here protects legacy or
    # manually inserted rows without inventing a smaller business cap.
    for item in manifest[:MAX_ATTACHMENTS_IN_MANIFEST]:
        try:
            attachment = _manifest_item(item)
        except PpsEnrichmentError:
            invalid_count += 1
            continue
        if attachment["attachment_id"] in seen_ids:
            invalid_count += 1
            continue
        seen_ids.add(attachment["attachment_id"])
        validated.append(attachment)
    validated.sort(key=lambda item: (int(item["slot"]), item["attachment_id"]))
    return validated, invalid_count


def select_preferred_attachments(
    manifest: list[dict[str, Any]],
    *,
    limit: int = 1,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compatibility selector for ranked supported attachments.

    Enrichment no longer uses this function: it audits the entire validated
    manifest.  The selector remains for callers that explicitly need ranked
    supported files, and its upper bound is the PPS manifest boundary rather
    than the former one-file business cap.
    """

    if not 1 <= limit <= MAX_ATTACHMENTS_IN_MANIFEST:
        raise ValueError(
            f"max attachments per notice must be between 1 and {MAX_ATTACHMENTS_IN_MANIFEST}"
        )
    validated, invalid_count = _validated_manifest_attachments(manifest)
    supported = [
        item
        for item in validated
        if PurePath(item["file_name"]).suffix.casefold() in _EXTRACTABLE_EXTENSIONS
    ]
    if not supported:
        warnings = ["HWP_ONLY_UNSUPPORTED_R07"] if validated else ["ATTACHMENT_MANIFEST_EMPTY"]
        if invalid_count:
            warnings.append("INVALID_ATTACHMENT_MANIFEST")
        return [], warnings

    def priority(item: dict[str, Any]) -> tuple[int, int, int]:
        name = item["file_name"].casefold()
        semantic = max(
            (
                (50, "제안요청"),
                (45, "과업지시"),
                (40, "입찰공고"),
                (35, "공고문"),
                (20, "제안서"),
            ),
            key=lambda pair: pair[0] if pair[1] in name else -1,
        )[0]
        if not any(term in name for term in ("제안요청", "과업지시", "입찰공고", "공고문", "제안서")):
            semantic = 0
        extension = PurePath(name).suffix
        format_priority = 2 if extension == ".pdf" else 1
        return semantic, format_priority, -int(item["slot"])

    supported.sort(key=priority, reverse=True)
    warnings = ["INVALID_ATTACHMENT_MANIFEST"] if invalid_count else []
    return supported[:limit], warnings


def public_analysis_reason(
    versions: list[NoticeVersion],
    *,
    evaluated: bool = False,
    source_kind: str = "PPS",
) -> PublicAnalysisReason:
    """Classify why a notice is or is not analysed without exposing raw errors.

    ``NOT_SELECTED`` is intentionally distinct from a processing failure. It
    is the normal state for notices outside the bounded daily analysis slice.
    """

    def result(
        state: AnalysisState,
        code: AnalysisReasonCode,
        *,
        attachment_count: int = 0,
        attempted: bool = False,
    ) -> PublicAnalysisReason:
        return PublicAnalysisReason(
            state=state,
            reason_code=code,
            reason=_PUBLIC_ANALYSIS_MESSAGES[code],
            attachment_count=attachment_count,
            attempted=attempted,
        )

    # Curated/manual sources do not use a PPS manifest. Their evaluation is
    # still a legitimate completed analysis, while an untouched row remains a
    # pending selection rather than being mislabeled as a missing attachment.
    if source_kind != "PPS":
        return result("ANALYZED", "ANALYZED", attempted=True) if evaluated else result(
            "PENDING", "NOT_SELECTED"
        )

    ordered = sorted(versions, key=lambda item: item.version_no)
    metadata = next(
        (
            item
            for item in reversed(ordered)
            if isinstance(item.source_payload, dict)
            and item.source_payload.get("kind") == PPS_METADATA_KIND
            and isinstance(item.source_payload.get("attachment_manifest"), list)
        ),
        None,
    )
    if metadata is None:
        # A legacy evaluated PPS row may predate manifest persistence. Do not
        # regress a known completed result to a false attachment failure.
        if evaluated:
            return result("ANALYZED", "ANALYZED", attempted=True)
        return result("REVIEW", "ATTACHMENT_NONE")

    manifest = [
        dict(item)
        for item in metadata.source_payload.get("attachment_manifest", [])
        if isinstance(item, dict)
    ]
    attachment_count = len(manifest)
    current_versions = list(reversed(ordered))
    attachments, invalid_count, attempts = _current_manifest_attempts(current_versions)
    if not attachments:
        return result(
            "REVIEW",
            "ATTACHMENT_NONE",
            attachment_count=attachment_count,
        )
    if invalid_count:
        return result(
            "REVIEW",
            "ATTACHMENT_COVERAGE_INCOMPLETE",
            attachment_count=attachment_count,
            attempted=bool(attempts),
        )
    if any(attachment["attachment_id"] not in attempts for attachment in attachments):
        return result(
            "REVIEW" if attempts else "PENDING",
            "ATTACHMENT_COVERAGE_INCOMPLETE" if attempts else "NOT_SELECTED",
            attachment_count=attachment_count,
            attempted=bool(attempts),
        )

    failures: list[tuple[dict[str, Any], NoticeVersion, str]] = []
    for attachment in attachments:
        latest = attempts[attachment["attachment_id"]]
        payload = latest.source_payload if isinstance(latest.source_payload, dict) else {}
        if payload.get("status") == "ACCEPTED" and latest.extraction_status in {
            "ACCEPTED",
            "COMPLETE",
        }:
            if latest.document_complete:
                continue
            processing = (
                payload.get("document_processing")
                if isinstance(payload.get("document_processing"), dict)
                else {}
            )
            error_code = (
                "DOCUMENT_PROCESSING_INCOMPLETE"
                if processing.get("source_read_complete") is not True
                or processing.get("analysis_input_complete") is not True
                else "OPENAI_SOURCE_GAPS"
            )
            failures.append((attachment, latest, error_code))
            continue
        failures.append((attachment, latest, str(payload.get("error_code") or "")))
    if not failures:
        return result(
            "ANALYZED",
            "ANALYZED",
            attachment_count=attachment_count,
            attempted=True,
        )

    codes: set[AnalysisReasonCode] = set()
    for attachment, _latest, error_code in failures:
        extension = PurePath(str(attachment.get("file_name") or "")).suffix.casefold()
        if error_code == "UNVERIFIED_QUOTE":
            codes.add("QUOTE_UNVERIFIED")
        elif error_code in {"HWP_ONLY_UNSUPPORTED_R07", "HWP_BINARY_UNSUPPORTED"}:
            codes.add("HWP_ONLY_UNSUPPORTED")
        elif error_code == "UNSUPPORTED_ATTACHMENT_TYPE":
            codes.add("UNSUPPORTED_ATTACHMENT")
        elif extension == ".hwpx" and (
            error_code.startswith("HWPX_")
            or error_code.startswith("ATTACHMENT_")
            or error_code in {
                "DOCUMENT_TEXT_EMPTY_OR_SHORT",
                "DOCUMENT_TEXT_TOO_LARGE",
                "DOCUMENT_PROCESSING_INCOMPLETE",
                "INVALID_CONTENT_LENGTH",
                "UNEXPECTED_ATTACHMENT_CONTENT_TYPE",
            }
        ):
            codes.add("HWPX_EXTRACT_FAILED")
        elif extension == ".pdf" and (
            error_code.startswith("PDF_")
            or error_code.startswith("ATTACHMENT_")
            or error_code in {
                "DOCUMENT_TEXT_EMPTY_OR_SHORT",
                "DOCUMENT_TEXT_TOO_LARGE",
                "DOCUMENT_PROCESSING_INCOMPLETE",
                "INVALID_CONTENT_LENGTH",
                "UNEXPECTED_ATTACHMENT_CONTENT_TYPE",
            }
        ):
            codes.add("PDF_EXTRACT_FAILED")
        elif extension in {".hwp", ".xlsx", ".xlsm", ".xls", ".docx", ".pptx", ".html", ".htm", ".zip"} and (
            error_code.startswith(("HWP_", "XLSX_", "XLS_", "DOCX_", "PPTX_", "HTML_", "ZIP_", "ATTACHMENT_", "DOCUMENT_PROCESSING_"))
            or error_code in {
                "DOCUMENT_TEXT_EMPTY_OR_SHORT",
                "DOCUMENT_TEXT_TOO_LARGE",
                "INVALID_CONTENT_LENGTH",
                "UNEXPECTED_ATTACHMENT_CONTENT_TYPE",
            }
        ):
            codes.add("DOCUMENT_EXTRACT_FAILED")
        else:
            codes.add("OPENAI_REVIEW")
    # Stable worst-current ordering: incomplete binary/source extraction is
    # more fundamental than quote/schema review, but every sibling success is
    # still retained in the materialised source set.
    code = next(
        candidate
        for candidate in (
            "HWP_ONLY_UNSUPPORTED",
            "UNSUPPORTED_ATTACHMENT",
            "HWPX_EXTRACT_FAILED",
            "PDF_EXTRACT_FAILED",
            "DOCUMENT_EXTRACT_FAILED",
            "QUOTE_UNVERIFIED",
            "OPENAI_REVIEW",
        )
        if candidate in codes
    )
    return result(
        "REVIEW",
        code,  # type: ignore[arg-type]
        attachment_count=attachment_count,
        attempted=True,
    )


def download_public_attachment(
    attachment: dict[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
    max_redirects: int = 2,
    timeout_seconds: float = 20,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    current_url = _safe_g2b_attachment_url(attachment.get("url"))
    filename, _media_type = _safe_filename(attachment.get("file_name"))
    with httpx.Client(
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
        headers={"User-Agent": "PAI-LOOP-public-attachment/0.1"},
    ) as client:
        for redirect_count in range(max_redirects + 1):
            try:
                with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= max_redirects:
                            raise PpsEnrichmentError("ATTACHMENT_REDIRECT_LIMIT")
                        location = response.headers.get("Location")
                        current_url = _safe_g2b_attachment_url(urljoin(current_url, location or ""))
                        continue
                    if response.status_code != 200:
                        raise PpsEnrichmentError(f"ATTACHMENT_HTTP_{response.status_code}")
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise PpsEnrichmentError("ATTACHMENT_TOO_LARGE")
                        except ValueError as exc:
                            raise PpsEnrichmentError("INVALID_CONTENT_LENGTH") from exc
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
                    extension = PurePath(filename).suffix.casefold()
                    allowed_content_types = {
                        ".pdf": {"application/pdf", "application/octet-stream"},
                        ".hwpx": {
                            "application/hwp+zip",
                            "application/zip",
                            "application/octet-stream",
                        },
                        ".hwp": {
                            "application/x-hwp",
                            "application/haansofthwp",
                            "application/octet-stream",
                        },
                        ".xlsx": {
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            "application/zip",
                            "application/octet-stream",
                        },
                        ".xlsm": {
                            "application/vnd.ms-excel.sheet.macroenabled.12",
                            "application/zip",
                            "application/octet-stream",
                        },
                        ".xls": {
                            "application/vnd.ms-excel",
                            "application/msexcel",
                            "application/x-msexcel",
                            "application/octet-stream",
                        },
                        ".docx": {
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "application/zip",
                            "application/octet-stream",
                        },
                        ".pptx": {
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                            "application/zip",
                            "application/octet-stream",
                        },
                        ".zip": {"application/zip", "application/octet-stream"},
                        ".html": {"text/html", "application/octet-stream"},
                        ".htm": {"text/html", "application/octet-stream"},
                    }[extension]
                    if content_type and content_type not in allowed_content_types:
                        raise PpsEnrichmentError("UNEXPECTED_ATTACHMENT_CONTENT_TYPE")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise PpsEnrichmentError("ATTACHMENT_TOO_LARGE")
                        chunks.append(chunk)
                    if total == 0:
                        raise PpsEnrichmentError("ATTACHMENT_EMPTY")
                    return b"".join(chunks)
            except httpx.RequestError as exc:
                raise PpsEnrichmentError("ATTACHMENT_NETWORK_ERROR") from exc
    raise PpsEnrichmentError("ATTACHMENT_REDIRECT_LIMIT")


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - deployment dependency check
        raise PpsEnrichmentError("PDF_EXTRACTOR_UNAVAILABLE") from exc
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise PpsEnrichmentError("PDF_ENCRYPTED")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise PpsEnrichmentError("PDF_PAGE_LIMIT")
        parts: list[str] = []
        total = 0
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                part = f"\n[PAGE {index}]\n{text}"
                total += len(part)
                if total > MAX_EXTRACTED_DOCUMENT_CHARS:
                    raise PpsEnrichmentError("DOCUMENT_TEXT_TOO_LARGE")
                parts.append(part)
    except PpsEnrichmentError:
        raise
    except Exception as exc:
        raise PpsEnrichmentError("PDF_TEXT_EXTRACTION_FAILED") from exc
    return "".join(parts).strip()


def _extract_hwpx_text(content: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (ValueError, zipfile.BadZipFile) as exc:
        raise PpsEnrichmentError("HWPX_INVALID_ARCHIVE") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_HWPX_ENTRIES:
            raise PpsEnrichmentError("HWPX_ENTRY_LIMIT")
        total_uncompressed = sum(item.file_size for item in entries)
        if total_uncompressed > MAX_HWPX_UNCOMPRESSED_BYTES:
            raise PpsEnrichmentError("HWPX_UNCOMPRESSED_LIMIT")
        section_names = sorted(
            item.filename
            for item in entries
            if re.fullmatch(r"Contents/section\d+\.xml", item.filename)
            and not item.is_dir()
            and not (item.flag_bits & 0x1)
        )
        if not section_names:
            raise PpsEnrichmentError("HWPX_SECTION_MISSING")
        parts: list[str] = []
        total = 0
        for name in section_names:
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError as exc:
                raise PpsEnrichmentError("HWPX_XML_INVALID") from exc
            # HWPX commonly splits one word across multiple hp:run/hp:t nodes.
            # Joining every XML text node with a space turns e.g. ``과업지시서``
            # into ``과 업 지 시 서`` and also removes all paragraph boundaries.
            # Rebuild each hp:p from its hp:t descendants without inventing
            # characters, then keep paragraph boundaries for reliable quotes.
            for paragraph in root.iter():
                if str(paragraph.tag).rsplit("}", 1)[-1] != "p":
                    continue
                fragments: list[str] = []
                found_text_node = False
                for text_node in paragraph.iter():
                    if str(text_node.tag).rsplit("}", 1)[-1] != "t":
                        continue
                    found_text_node = True
                    fragments.extend(text_node.itertext())
                # Minimal/legacy HWPX producers (and our format fixtures) may
                # place character data directly below the paragraph element.
                if not found_text_node:
                    fragments.extend(paragraph.itertext())
                text = unicodedata.normalize("NFC", "".join(fragments))
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                total += len(text) + 1
                if total > MAX_EXTRACTED_DOCUMENT_CHARS:
                    raise PpsEnrichmentError("DOCUMENT_TEXT_TOO_LARGE")
                parts.append(text)
    return "\n".join(parts).strip()


def extract_pps_document_content(
    file_name: str,
    content: bytes,
) -> DocumentExtractionResult:
    """Extract one public document with structured, bounded coverage audit."""

    filename, _media_type = _safe_filename(file_name)
    try:
        result = extract_document_content(
            filename,
            content,
            leaf_extractors={
                ".pdf": lambda _name, value: _extract_pdf_text(value),
                ".hwpx": lambda _name, value: _extract_hwpx_text(value),
            },
            limits=ExtractionLimits(
                max_input_bytes=DEFAULT_MAX_DOWNLOAD_BYTES,
                max_document_chars=MAX_EXTRACTED_DOCUMENT_CHARS,
            ),
        )
    except DocumentExtractionError as exc:
        raise PpsEnrichmentError(str(exc)) from exc

    # Some malformed embedded PDF maps expose lone UTF-16 surrogate code
    # points. They are not Unicode scalar values and cannot be serialised as
    # UTF-8 by httpx. Replace only those invalid scalars while preserving every
    # valid source character and the exact-quote verification boundary.
    safe_text = "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in result.text
    )
    return DocumentExtractionResult(
        text=safe_text,
        warnings=result.warnings,
        members_discovered=result.members_discovered,
        members_processed=result.members_processed,
        complete=result.complete,
        member_issues=result.member_issues,
    )


def extract_document_text(file_name: str, content: bytes) -> str:
    """Compatibility text projection; enrichment consumes structured audit."""

    result = extract_pps_document_content(file_name, content)
    if len(result.text.strip()) < 20:
        error_code = result.warnings[0] if result.warnings else "DOCUMENT_TEXT_EMPTY_OR_SHORT"
        raise PpsEnrichmentError(error_code)
    return result.text


_ANALYSIS_SIGNAL_TERMS = (
    "참가자격",
    "입찰참가",
    "자격요건",
    "신청자격",
    "제안요청",
    "과업",
    "평가항목",
    "평가기준",
    "배점",
    "정량",
    "정성",
    "점수",
    "실적",
    "수행실적",
    "전문인력",
    "투입인력",
    "인력",
    "기술인력",
    "자격증",
    "인증",
    "직접생산",
    "업종",
    "지역제한",
    "공동수급",
    "컨소시엄",
    "제출서류",
    "제출기한",
    "마감",
    "가격평가",
    "기술평가",
    "evaluation",
    "qualification",
    "eligibility",
    "score",
)
_STRUCTURE_LINE = re.compile(
    r"^\s*\[(?:PAGE|SHEET|SLIDE|ARCHIVE MEMBER|DOCUMENT|SECTION)\b",
    re.IGNORECASE,
)


def select_document_analysis_input(
    text: str,
    *,
    maximum: int = MAX_ANALYSIS_INPUT_CHARS,
) -> DocumentAnalysisSelection:
    """Scan the full extracted text and select bounded, exact source spans.

    Every line is inspected for eligibility and quantitative-table signals.
    Inputs at or below the model boundary are passed intact. Larger sources
    are never silently truncated: deterministic signal/context, structural,
    head/tail, and evenly distributed sample lines are selected and the audit
    explicitly records that the model input was partial.
    """

    if maximum < 20:
        raise ValueError("maximum must be at least 20 characters")
    source = text.strip()
    if len(source) <= maximum:
        line_count = len(source.splitlines()) or int(bool(source))
        return DocumentAnalysisSelection(
            text=source,
            complete=True,
            source_characters=len(source),
            selected_characters=len(source),
            total_lines=line_count,
            selected_line_ranges=((1, line_count),) if line_count else (),
            signal_line_count=sum(
                1
                for line in source.splitlines()
                if any(term in line.casefold() for term in _ANALYSIS_SIGNAL_TERMS)
            ),
        )

    lines = source.splitlines()
    folded = [line.casefold() for line in lines]
    signal_indices = {
        index
        for index, line in enumerate(folded)
        if any(term in line for term in _ANALYSIS_SIGNAL_TERMS)
    }
    priorities: dict[int, int] = {}

    def offer(index: int, priority: int) -> None:
        if 0 <= index < len(lines) and lines[index].strip():
            priorities[index] = max(priority, priorities.get(index, 0))

    for index in signal_indices:
        for offset in range(-3, 4):
            offer(index + offset, 100 - abs(offset))
    for index, line in enumerate(lines):
        if _STRUCTURE_LINE.search(line):
            offer(index, 80)
            offer(index + 1, 79)
    for index in range(min(120, len(lines))):
        offer(index, 60)
    for index in range(max(0, len(lines) - 60), len(lines)):
        offer(index, 55)
    sample_count = min(96, len(lines))
    if sample_count:
        denominator = max(1, sample_count - 1)
        for offset in range(sample_count):
            offer(round(offset * (len(lines) - 1) / denominator), 20)

    selected: set[int] = set()
    consumed = 0
    # Select higher-value regions first, but emit them later in source order so
    # the model receives stable local context rather than a ranked collage.
    for index, _priority in sorted(
        priorities.items(),
        key=lambda pair: (-pair[1], pair[0]),
    ):
        line_cost = min(len(lines[index]), 8_000) + 1
        if consumed + line_cost > maximum:
            continue
        selected.add(index)
        consumed += line_cost
    if not selected:
        selected.add(0)

    ordered = sorted(selected)
    selected_lines = [lines[index][:8_000] for index in ordered]
    selected_text = "\n".join(selected_lines)
    if len(selected_text) > maximum:
        selected_text = selected_text[:maximum]

    ranges: list[tuple[int, int]] = []
    for zero_based in ordered:
        one_based = zero_based + 1
        if ranges and one_based == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], one_based)
        else:
            ranges.append((one_based, one_based))
    return DocumentAnalysisSelection(
        text=selected_text,
        complete=False,
        source_characters=len(source),
        selected_characters=len(selected_text),
        total_lines=len(lines),
        selected_line_ranges=tuple(ranges),
        signal_line_count=len(signal_indices),
    )


def _redact_public_text(value: str) -> str | None:
    """Redact direct contact identifiers and fail closed on residual matches."""

    text = _PUBLIC_EMAIL_PATTERN.sub("[비공개]", value)
    text = _PUBLIC_PHONE_PATTERN.sub("[비공개]", text)
    text = _PUBLIC_CONTACT_PATTERN.sub("담당자 [비공개]", text)
    if (
        _PUBLIC_EMAIL_PATTERN.search(text)
        or _PUBLIC_PHONE_PATTERN.search(text)
        or _PUBLIC_CONTACT_PATTERN.search(text)
    ):
        return None
    return text


def _redact_public_extraction(data: dict[str, Any]) -> dict[str, Any] | None:
    """Redact only schema-known prose; unexpected shapes fail closed upstream."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            redacted = _redact_public_text(value)
            if redacted is None:
                raise ValueError("residual public identifier")
            return redacted
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value

    try:
        return clean(data)
    except ValueError:
        return None


def safe_public_live_extraction(payload: Any) -> dict[str, Any] | None:
    """Strictly publish a validated PPS extraction without operational metadata."""

    if not isinstance(payload, dict):
        return None
    attachment_id = payload.get("attachment_id")
    source_label = payload.get("source_label")
    processing = (
        payload.get("document_processing")
        if isinstance(payload.get("document_processing"), dict)
        else {}
    )
    if (
        payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION"
        or payload.get("source_kind") != PPS_ATTACHMENT_SOURCE
        or payload.get("status") != "ACCEPTED"
        or payload.get("prompt_version") != PROMPT_VERSION
        or payload.get("processing_version") != PPS_PROCESSING_VERSION
        or payload.get("schema_version") != SCHEMA_VERSION
        or not isinstance(attachment_id, str)
        or not _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id)
        or not isinstance(source_label, str)
        or not re.fullmatch(
            r"[^/\\\x00-\x1f\x7f]{1,255}\.(?:pdf|hwpx|hwp|xlsx|xlsm|xls|docx|pptx|html|htm|zip)",
            source_label,
            re.IGNORECASE,
        )
        or not re.fullmatch(r"[a-f0-9]{64}", str(payload.get("document_sha256") or ""), re.IGNORECASE)
        or processing.get("source_read_complete") is not True
        or processing.get("analysis_input_complete") is not True
    ):
        return None
    try:
        result = ExtractionPayload.model_validate(payload.get("result"))
    except Exception:
        return None
    if any(
        anchor.attachment_id != attachment_id or not anchor.quote.strip()
        for requirement in result.requirements
        for anchor in requirement.evidence
    ):
        return None
    data = _redact_public_extraction(result.model_dump(mode="json"))
    safe_label = _redact_public_text(source_label)
    if data is None or safe_label is None:
        return None
    return {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "status": "ACCEPTED",
        "document_name": safe_label,
        "summary": data["summary"],
        "requirements": data["requirements"],
        "missing_or_unreadable": data["missing_or_unreadable"],
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _matching_extraction_version(
    versions: list[NoticeVersion],
    *,
    attachment_id: str,
    manifest_sha256: str,
    document_sha256: str,
) -> NoticeVersion | None:
    """Reuse deterministic output; REVIEW retries are capped to once per day."""

    now = datetime.now(timezone.utc)
    for item in versions:
        payload = item.source_payload
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION"
            or payload.get("source_kind") != PPS_ATTACHMENT_SOURCE
            or payload.get("attachment_id") != attachment_id
            or payload.get("manifest_sha256") != manifest_sha256
            or payload.get("document_sha256") != document_sha256
            or payload.get("prompt_version") != PROMPT_VERSION
            or payload.get("processing_version") != PPS_PROCESSING_VERSION
        ):
            continue
        if payload.get("status") == "ACCEPTED":
            return item
        error_code = str(payload.get("error_code") or "")
        if error_code in DETERMINISTIC_REVIEW_CODES:
            return item
        if item.created_at and now - _as_utc(item.created_at) < REVIEW_RETRY_COOLDOWN:
            return item
    return None


def _stored_outcome_is_idempotent(version: NoticeVersion, payload: dict[str, Any]) -> bool:
    if payload.get("status") == "ACCEPTED":
        return True
    if str(payload.get("error_code") or "") in DETERMINISTIC_REVIEW_CODES:
        return True
    return bool(
        version.created_at
        and datetime.now(timezone.utc) - _as_utc(version.created_at) < REVIEW_RETRY_COOLDOWN
    )


def _manifest_binding_is_current(
    session: Session,
    *,
    notice_id: str,
    attachment: dict[str, Any],
    manifest_sha256: str,
) -> bool:
    """Verify an exact attachment against the latest metadata inside a write tx."""

    metadata = session.scalar(
        select(NoticeVersion)
        .where(NoticeVersion.notice_id == notice_id)
        .order_by(NoticeVersion.version_no.desc())
    )
    # The first row may be an extraction/materialisation. Find the newest
    # metadata version explicitly rather than relying on row kind ordering.
    if metadata is None or not (
        isinstance(metadata.source_payload, dict)
        and metadata.source_payload.get("kind") == PPS_METADATA_KIND
    ):
        metadata = next(
            (
                item
                for item in session.scalars(
                    select(NoticeVersion)
                    .where(NoticeVersion.notice_id == notice_id)
                    .order_by(NoticeVersion.version_no.desc())
                ).all()
                if isinstance(item.source_payload, dict)
                and item.source_payload.get("kind") == PPS_METADATA_KIND
                and isinstance(item.source_payload.get("attachment_manifest"), list)
            ),
            None,
        )
    if metadata is None or not isinstance(metadata.source_payload, dict):
        return False
    current, invalid_count = _validated_manifest_attachments(
        [
            dict(item)
            for item in metadata.source_payload.get("attachment_manifest", [])
            if isinstance(item, dict)
        ]
    )
    if invalid_count:
        return False
    return any(
        item["attachment_id"] == attachment["attachment_id"]
        and _digest(item) == manifest_sha256
        for item in current
    )


def persist_pps_metadata_version(
    session: Session,
    notice: Notice,
    *,
    raw_item: dict[str, Any],
    search_keywords: list[str],
    dry_run: bool,
) -> MetadataVersionResult:
    manifest = build_attachment_manifest(raw_item)
    payload = {
        "kind": PPS_METADATA_KIND,
        "source_kind": "PPS",
        "schema_version": PPS_METADATA_SCHEMA,
        "notice_identity": {
            "bid_notice_no": notice.bid_notice_no,
            "revision_no": notice.revision_no,
        },
        "provenance": {
            "provider": "PPS_PUBLIC_API",
            "search_keywords": sorted(set(search_keywords), key=str.casefold),
        },
        "notice_metadata": build_notice_metadata(raw_item),
        "attachment_manifest": manifest,
    }
    payload_sha256 = _digest(payload)
    if dry_run:
        return MetadataVersionResult(False, False, len(manifest), None)
    session.flush()
    prior = session.scalar(
        select(NoticeVersion).where(
            NoticeVersion.notice_id == notice.id,
            NoticeVersion.file_sha256 == payload_sha256,
        )
    )
    if prior is not None and isinstance(prior.source_payload, dict) and prior.source_payload.get("kind") == PPS_METADATA_KIND:
        return MetadataVersionResult(False, True, len(manifest), prior.id)
    for _attempt in range(3):
        next_version = int(
            session.scalar(
                select(func.max(NoticeVersion.version_no)).where(NoticeVersion.notice_id == notice.id)
            )
            or 0
        ) + 1
        version = NoticeVersion(
            notice_id=notice.id,
            version_no=next_version,
            file_sha256=payload_sha256,
            document_complete=False,
            extraction_status="METADATA",
            extraction_confidence=1.0,
            source_payload=payload,
        )
        try:
            with session.begin_nested():
                session.add(version)
                session.flush()
            return MetadataVersionResult(True, False, len(manifest), version.id)
        except IntegrityError:
            session.expire_all()
            raced = session.scalar(
                select(NoticeVersion).where(
                    NoticeVersion.notice_id == notice.id,
                    NoticeVersion.file_sha256 == payload_sha256,
                )
            )
            if (
                raced is not None
                and isinstance(raced.source_payload, dict)
                and raced.source_payload.get("kind") == PPS_METADATA_KIND
            ):
                return MetadataVersionResult(False, True, len(manifest), raced.id)
    raise PpsEnrichmentError("NOTICE_VERSION_RACE")


def _persist_extraction_version(
    session: Session,
    *,
    notice_id: str,
    attachment: dict[str, Any],
    manifest_sha256: str,
    document_sha256: str,
    outcome: ExtractionOutcome | None,
    error_code: str | None,
    processing_audit: dict[str, Any] | None = None,
) -> NoticeVersion:
    if not _manifest_binding_is_current(
        session,
        notice_id=notice_id,
        attachment=attachment,
        manifest_sha256=manifest_sha256,
    ):
        raise PpsEnrichmentError("PPS_MANIFEST_CHANGED_DURING_ENRICHMENT")
    accepted = outcome is not None and outcome.status == "ACCEPTED" and outcome.data is not None
    data = outcome.data.model_dump(mode="json") if accepted and outcome and outcome.data else None
    confidence_values = [
        anchor.confidence
        for requirement in (outcome.data.requirements if accepted and outcome and outcome.data else [])
        for anchor in requirement.evidence
    ]
    confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
    payload = {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "source_kind": PPS_ATTACHMENT_SOURCE,
        "attachment_id": attachment["attachment_id"],
        "source_label": attachment["file_name"],
        "manifest_sha256": manifest_sha256,
        "document_sha256": document_sha256,
        "status": outcome.status if outcome else "REVIEW",
        "review_code": outcome.review_code if outcome else "R07",
        "error_code": outcome.error_code if outcome else error_code,
        "message": outcome.message if outcome else "공개 첨부를 자동 처리하지 못해 원문 검토가 필요합니다.",
        "response_id": outcome.response_id if outcome else None,
        "model": outcome.model if outcome else None,
        "prompt_version": outcome.prompt_version if outcome else PROMPT_VERSION,
        "processing_version": PPS_PROCESSING_VERSION,
        "schema_version": outcome.schema_version if outcome else SCHEMA_VERSION,
        "api_calls": outcome.api_calls if outcome else 0,
        "corrective_retry_used": outcome.corrective_retry_used if outcome else False,
        "correction_prompt_version": outcome.correction_prompt_version if outcome else None,
        "result": data,
        "document_processing": processing_audit,
    }
    existing_versions = list(
        session.scalars(
            select(NoticeVersion)
            .where(
                NoticeVersion.notice_id == notice_id,
                NoticeVersion.file_sha256 == document_sha256,
            )
            .order_by(NoticeVersion.version_no.desc())
        ).all()
    )
    for existing in existing_versions:
        prior = existing.source_payload
        if (
            isinstance(prior, dict)
            and prior.get("kind") == payload["kind"]
            and prior.get("source_kind") == payload["source_kind"]
            and prior.get("attachment_id") == payload["attachment_id"]
            and prior.get("manifest_sha256") == payload["manifest_sha256"]
            and prior.get("document_sha256") == payload["document_sha256"]
            and prior.get("prompt_version") == payload["prompt_version"]
            and prior.get("processing_version") == payload["processing_version"]
            and prior.get("status") == payload["status"]
            and prior.get("error_code") == payload["error_code"]
            and _stored_outcome_is_idempotent(existing, payload)
        ):
            return existing

    for _attempt in range(3):
        next_version = int(
            session.scalar(
                select(func.max(NoticeVersion.version_no)).where(NoticeVersion.notice_id == notice_id)
            )
            or 0
        ) + 1
        version = NoticeVersion(
            notice_id=notice_id,
            version_no=next_version,
            file_sha256=document_sha256,
            document_complete=bool(
                accepted
                and outcome
                and outcome.data
                and not outcome.data.missing_or_unreadable
                and isinstance(processing_audit, dict)
                and processing_audit.get("source_read_complete") is True
                and processing_audit.get("analysis_input_complete") is True
            ),
            extraction_status="ACCEPTED" if accepted else "REVIEW",
            extraction_confidence=confidence,
            source_payload=payload,
        )
        try:
            with session.begin_nested():
                session.add(version)
                session.flush()
            return version
        except IntegrityError:
            session.expire_all()
            for raced in session.scalars(
                select(NoticeVersion).where(
                    NoticeVersion.notice_id == notice_id,
                    NoticeVersion.file_sha256 == document_sha256,
                )
            ):
                prior = raced.source_payload
                if (
                    isinstance(prior, dict)
                    and prior.get("kind") == payload["kind"]
                    and prior.get("attachment_id") == payload["attachment_id"]
                    and prior.get("manifest_sha256") == payload["manifest_sha256"]
                    and prior.get("prompt_version") == payload["prompt_version"]
                    and prior.get("processing_version") == payload["processing_version"]
                    and prior.get("status") == payload["status"]
                    and _stored_outcome_is_idempotent(raced, payload)
                ):
                    return raced
    raise PpsEnrichmentError("NOTICE_VERSION_RACE")


def record_internal_pps_enrichment_failure(
    session: Session,
    *,
    notice_id: str,
    attachment: dict[str, Any],
    manifest_sha256: str,
    attachments_discovered: int,
) -> PpsEnrichmentResult:
    """Persist a public-safe attempt marker after an unexpected enrichment error.

    The caller supplies the exact validated attachment and manifest digest it
    actually attempted. Re-reading and binding whatever manifest happens to
    be latest here would let a concurrent PPS correction inherit a failure it
    never experienced. A changed current manifest therefore fails closed with
    no marker; its truthful state remains NOT_SELECTED until the refreshed
    work generation runs.
    """

    if session.in_transaction():
        raise RuntimeError("record_internal_pps_enrichment_failure requires a clean Session")
    warning = "INTERNAL_ENRICHMENT_ERROR"
    attempted_attachment = _manifest_item(attachment)
    if _digest(attempted_attachment) != manifest_sha256:
        raise PpsEnrichmentError("INVALID_ENRICHMENT_ATTEMPT_CONTEXT")
    with session.begin():
        notice = session.get(Notice, notice_id)
        if notice is None:
            return PpsEnrichmentResult(
                status="SKIPPED",
                warnings=["NOTICE_NOT_FOUND", warning],
            )
        versions = list(
            session.scalars(
                select(NoticeVersion)
                .where(NoticeVersion.notice_id == notice_id)
                .order_by(NoticeVersion.version_no.desc())
            ).all()
        )
        metadata = next(
            (
                item
                for item in versions
                if isinstance(item.source_payload, dict)
                and item.source_payload.get("kind") == PPS_METADATA_KIND
                and isinstance(item.source_payload.get("attachment_manifest"), list)
            ),
            None,
        )
        if metadata is None:
            return PpsEnrichmentResult(
                status="REVIEW",
                attachments_discovered=attachments_discovered,
                warnings=[warning, "PPS_MANIFEST_CHANGED_DURING_ENRICHMENT"],
            )
        current_manifest = [
            dict(item)
            for item in metadata.source_payload.get("attachment_manifest", [])
            if isinstance(item, dict)
        ]
        current_attachments, invalid_count = _validated_manifest_attachments(current_manifest)
        current_match = next(
            (
                item
                for item in current_attachments
                if item["attachment_id"] == attempted_attachment["attachment_id"]
            ),
            None,
        )
        if (
            invalid_count
            or current_match is None
            or _digest(current_match) != manifest_sha256
        ):
            return PpsEnrichmentResult(
                status="REVIEW",
                attachments_discovered=len(current_manifest),
                warnings=[warning, "PPS_MANIFEST_CHANGED_DURING_ENRICHMENT"],
            )

        accepted = next(
            (
                item
                for item in versions
                if isinstance(item.source_payload, dict)
                and item.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
                and item.source_payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
                and item.source_payload.get("status") == "ACCEPTED"
                and item.source_payload.get("prompt_version") == PROMPT_VERSION
                and item.source_payload.get("processing_version")
                == PPS_PROCESSING_VERSION
                and item.source_payload.get("attachment_id")
                == attempted_attachment["attachment_id"]
                and item.source_payload.get("manifest_sha256") == manifest_sha256
            ),
            None,
        )
        if accepted is not None:
            return PpsEnrichmentResult(
                status="REUSED",
                attachments_discovered=len(current_manifest),
                version_id=accepted.id,
                warnings=[warning, "CURRENT_EXTRACTION_ALREADY_ACCEPTED"],
            )

        document_sha256 = _digest(
            {"manifest": manifest_sha256, "error": warning}
        )
        version = _persist_extraction_version(
            session,
            notice_id=notice_id,
            attachment=attempted_attachment,
            manifest_sha256=manifest_sha256,
            document_sha256=document_sha256,
            outcome=None,
            error_code=warning,
            processing_audit={
                "processing_version": PPS_PROCESSING_VERSION,
                "source_read_complete": False,
                "analysis_input_complete": False,
                "source_characters": 0,
                "analysis_input_characters": 0,
                "members_discovered": 0,
                "members_processed": 0,
                "warnings": [warning],
                "member_issues": [],
            },
        )
    return PpsEnrichmentResult(
        status="REVIEW",
        attachments_discovered=attachments_discovered,
        version_id=version.id,
        warnings=[warning],
    )


def _accepted_outcome_for_duplicate_content(
    session: Session,
    *,
    notice_id: str,
    attachment_id: str,
    document_sha256: str,
) -> ExtractionOutcome | None:
    """Reuse an exact-byte accepted extraction while preserving attachment audit.

    Evidence IDs are rebound only because the downloaded bytes are identical.
    The cloned outcome is persisted under the sibling attachment's own current
    manifest digest, so every manifest item retains an independent audit row.
    """

    versions = list(
        session.scalars(
            select(NoticeVersion)
            .where(
                NoticeVersion.notice_id == notice_id,
                NoticeVersion.file_sha256 == document_sha256,
            )
            .order_by(NoticeVersion.version_no.desc())
        ).all()
    )
    for version in versions:
        payload = version.source_payload
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION"
            or payload.get("source_kind") != PPS_ATTACHMENT_SOURCE
            or payload.get("status") != "ACCEPTED"
            or payload.get("prompt_version") != PROMPT_VERSION
            or payload.get("processing_version") != PPS_PROCESSING_VERSION
            or not isinstance(payload.get("result"), dict)
        ):
            continue
        try:
            raw_result = json.loads(json.dumps(payload["result"], ensure_ascii=False))
            for requirement in raw_result.get("requirements", []):
                for anchor in requirement.get("evidence", []):
                    anchor["attachment_id"] = attachment_id
            data = ExtractionPayload.model_validate(raw_result)
        except Exception:
            continue
        return ExtractionOutcome(
            status="ACCEPTED",
            message="동일 바이트 공개 첨부의 검증된 추출 결과를 재사용했습니다.",
            model=(payload.get("model") if isinstance(payload.get("model"), str) else None),
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            api_calls=0,
            data=data,
        )
    return None


def _document_processing_audit(
    result: DocumentExtractionResult,
    selection: DocumentAnalysisSelection | None,
) -> dict[str, Any]:
    """Build an internal bounded audit without persisting paths or source text."""

    source_text = result.text.strip()
    safe_warnings = sorted(
        {
            warning
            for warning in result.warnings
            if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", warning)
        }
    )
    member_issues = [
        {
            # ZIP member names can contain names or contact details. A stable
            # digest proves the same member failed without exposing that data.
            "member_path_sha256": hashlib.sha256(
                issue.member_path.encode("utf-8", errors="replace")
            ).hexdigest(),
            "reason": issue.reason,
        }
        for issue in result.member_issues
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", issue.reason)
    ]
    return {
        "processing_version": PPS_PROCESSING_VERSION,
        "source_read_complete": bool(result.complete and source_text),
        "analysis_input_complete": bool(selection and selection.complete),
        "source_characters": len(source_text),
        "analysis_input_characters": selection.selected_characters if selection else 0,
        "source_text_sha256": (
            hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if source_text
            else None
        ),
        "analysis_input_sha256": (
            hashlib.sha256(selection.text.encode("utf-8")).hexdigest()
            if selection
            else None
        ),
        "analysis_selection": (
            {
                "strategy": (
                    "FULL_TEXT"
                    if selection.complete
                    else "FULL_SCAN_SIGNAL_AND_DISTRIBUTED_SPANS"
                ),
                "total_lines": selection.total_lines,
                "selected_line_ranges": [list(item) for item in selection.selected_line_ranges],
                "signal_line_count": selection.signal_line_count,
            }
            if selection
            else None
        ),
        "members_discovered": result.members_discovered,
        "members_processed": result.members_processed,
        "warnings": safe_warnings,
        "member_issues": member_issues,
    }


def _stored_attachment_result(
    version: NoticeVersion,
    *,
    attachments_discovered: int,
) -> PpsEnrichmentResult | None:
    """Project a current terminal attempt so continuation never repeats work."""

    payload = version.source_payload
    if not isinstance(payload, dict) or not _stored_outcome_is_idempotent(version, payload):
        return None
    processing = (
        payload.get("document_processing")
        if isinstance(payload.get("document_processing"), dict)
        else {}
    )
    source_complete = processing.get("source_read_complete") is True
    input_complete = processing.get("analysis_input_complete") is True
    status = str(payload.get("status") or "REVIEW")
    if status == "ACCEPTED" and version.document_complete:
        result_status: Literal["REUSED", "REVIEW"] = "REUSED"
        warnings: list[str] = []
    elif status == "ACCEPTED":
        result_status = "REVIEW"
        warnings = [
            "OPENAI_SOURCE_GAPS"
            if source_complete and input_complete
            else "DOCUMENT_PROCESSING_INCOMPLETE"
        ]
    else:
        result_status = "REVIEW"
        warnings = [str(payload.get("error_code") or "OPENAI_REVIEW_R07")]
    return PpsEnrichmentResult(
        status=result_status,
        attachments_discovered=attachments_discovered,
        attachments_processed=int(bool(processing.get("source_characters"))),
        source_characters=int(processing.get("source_characters") or 0),
        analysis_input_characters=int(processing.get("analysis_input_characters") or 0),
        source_read_complete=source_complete,
        analysis_input_complete=input_complete,
        members_discovered=int(processing.get("members_discovered") or 0),
        members_processed=int(processing.get("members_processed") or 0),
        version_id=version.id,
        warnings=warnings,
    )


def _enrich_selected_pps_attachment(
    session: Session,
    *,
    notice_id: str,
    versions: list[NoticeVersion],
    attachment: dict[str, Any],
    manifest_sha256: str,
    attachments_discovered: int,
    openai_api_key: str | None,
    openai_model: str,
    transport: httpx.BaseTransport | None,
    openai_client_factory: Callable[..., OpenAIExtractionClient],
    download_timeout_seconds: float,
    openai_timeout_seconds: float,
    openai_max_retries: int,
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
) -> PpsEnrichmentResult:
    """Run one exact selected attachment; expected document failures are persisted."""

    try:
        content = download_public_attachment(
            attachment,
            transport=transport,
            timeout_seconds=download_timeout_seconds,
            max_bytes=max_download_bytes,
        )
        document_sha256 = hashlib.sha256(content).hexdigest()
        extraction = extract_pps_document_content(attachment["file_name"], content)
    except PpsEnrichmentError as exc:
        error_code = str(exc)
        document_sha256 = _digest(
            {"manifest": manifest_sha256, "error": error_code}
        )
        prior = _matching_extraction_version(
            versions,
            attachment_id=attachment["attachment_id"],
            manifest_sha256=manifest_sha256,
            document_sha256=document_sha256,
        )
        if prior is not None:
            return PpsEnrichmentResult(
                status="REUSED",
                attachments_discovered=attachments_discovered,
                version_id=prior.id,
                warnings=[error_code],
            )
        processing_audit = {
            "processing_version": PPS_PROCESSING_VERSION,
            "source_read_complete": False,
            "analysis_input_complete": False,
            "source_characters": 0,
            "analysis_input_characters": 0,
            "members_discovered": 0,
            "members_processed": 0,
            "warnings": [error_code],
            "member_issues": [],
        }
        with session.begin():
            version = _persist_extraction_version(
                session,
                notice_id=notice_id,
                attachment=attachment,
                manifest_sha256=manifest_sha256,
                document_sha256=document_sha256,
                outcome=None,
                error_code=error_code,
                processing_audit=processing_audit,
            )
        return PpsEnrichmentResult(
            status="REVIEW",
            attachments_discovered=attachments_discovered,
            version_id=version.id,
            warnings=[error_code],
        )

    source_text = extraction.text.strip()
    selection = (
        select_document_analysis_input(source_text)
        if len(source_text) >= 20
        else None
    )
    processing_audit = _document_processing_audit(extraction, selection)
    if not source_text or selection is None:
        error_code = (
            extraction.warnings[0]
            if extraction.warnings
            else "DOCUMENT_TEXT_EMPTY_OR_SHORT"
        )
        with session.begin():
            version = _persist_extraction_version(
                session,
                notice_id=notice_id,
                attachment=attachment,
                manifest_sha256=manifest_sha256,
                document_sha256=document_sha256,
                outcome=None,
                error_code=error_code,
                processing_audit=processing_audit,
            )
        return PpsEnrichmentResult(
            status="REVIEW",
            attachments_discovered=attachments_discovered,
            downloaded_bytes=len(content),
            source_characters=len(source_text),
            source_read_complete=False,
            members_discovered=extraction.members_discovered,
            members_processed=extraction.members_processed,
            version_id=version.id,
            warnings=sorted(set([error_code, *extraction.warnings])),
        )

    prior = _matching_extraction_version(
        versions,
        attachment_id=attachment["attachment_id"],
        manifest_sha256=manifest_sha256,
        document_sha256=document_sha256,
    )
    if prior is not None:
        stored = _stored_attachment_result(
            prior,
            attachments_discovered=attachments_discovered,
        )
        if stored is not None:
            return stored

    with session.begin():
        duplicate_outcome = _accepted_outcome_for_duplicate_content(
            session,
            notice_id=notice_id,
            attachment_id=attachment["attachment_id"],
            document_sha256=document_sha256,
        )
        if duplicate_outcome is not None:
            version = _persist_extraction_version(
                session,
                notice_id=notice_id,
                attachment=attachment,
                manifest_sha256=manifest_sha256,
                document_sha256=document_sha256,
                outcome=duplicate_outcome,
                error_code=None,
                processing_audit=processing_audit,
            )
            duplicate_complete = bool(version.document_complete)
            return PpsEnrichmentResult(
                status="REUSED" if duplicate_complete else "REVIEW",
                attachments_discovered=attachments_discovered,
                attachments_processed=1,
                downloaded_bytes=len(content),
                source_characters=len(source_text),
                analysis_input_characters=selection.selected_characters,
                source_read_complete=extraction.complete,
                analysis_input_complete=selection.complete,
                members_discovered=extraction.members_discovered,
                members_processed=extraction.members_processed,
                version_id=version.id,
                warnings=[
                    "DUPLICATE_CONTENT_REUSED",
                    *([] if duplicate_complete else ["DOCUMENT_PROCESSING_INCOMPLETE"]),
                ],
            )

    if not openai_api_key:
        with session.begin():
            version = _persist_extraction_version(
                session,
                notice_id=notice_id,
                attachment=attachment,
                manifest_sha256=manifest_sha256,
                document_sha256=document_sha256,
                outcome=None,
                error_code="OPENAI_KEY_MISSING",
                processing_audit=processing_audit,
            )
        return PpsEnrichmentResult(
            status="REVIEW",
            attachments_discovered=attachments_discovered,
            attachments_processed=1,
            downloaded_bytes=len(content),
            source_characters=len(source_text),
            analysis_input_characters=selection.selected_characters,
            source_read_complete=extraction.complete,
            analysis_input_complete=selection.complete,
            members_discovered=extraction.members_discovered,
            members_processed=extraction.members_processed,
            version_id=version.id,
            warnings=["OPENAI_KEY_MISSING"],
        )

    with openai_client_factory(
        api_key=openai_api_key,
        model=openai_model,
        timeout_seconds=openai_timeout_seconds,
        max_retries=openai_max_retries,
    ) as client:
        outcome = client.extract(
            document_text=selection.text,
            allowed_attachment_ids={attachment["attachment_id"]},
        )
    if outcome.api_calls > MAX_OPENAI_CALLS_PER_ATTACHMENT:
        raise PpsEnrichmentError("OPENAI_ATTACHMENT_CALL_LIMIT")
    with session.begin():
        version = _persist_extraction_version(
            session,
            notice_id=notice_id,
            attachment=attachment,
            manifest_sha256=manifest_sha256,
            document_sha256=document_sha256,
            outcome=outcome,
            error_code=outcome.error_code,
            processing_audit=processing_audit,
        )
    retry_warnings = (
        ["CORRECTIVE_EXTRACTION_RETRY_USED"]
        if outcome.corrective_retry_used
        else []
    )
    document_complete = bool(version.document_complete)
    processing_warnings = [
        *extraction.warnings,
        *([] if selection.complete else ["ANALYSIS_INPUT_PARTIAL"]),
    ]
    terminal_warning = None
    if outcome.status != "ACCEPTED":
        terminal_warning = outcome.error_code or "OPENAI_REVIEW_R07"
    elif not document_complete:
        terminal_warning = (
            "OPENAI_SOURCE_GAPS"
            if extraction.complete and selection.complete
            else "DOCUMENT_PROCESSING_INCOMPLETE"
        )
    return PpsEnrichmentResult(
        status=(
            "COMPLETED"
            if outcome.status == "ACCEPTED" and document_complete
            else "REVIEW"
        ),
        attachments_discovered=attachments_discovered,
        attachments_processed=1,
        downloaded_bytes=len(content),
        source_characters=len(source_text),
        analysis_input_characters=selection.selected_characters,
        source_read_complete=extraction.complete,
        analysis_input_complete=selection.complete,
        members_discovered=extraction.members_discovered,
        members_processed=extraction.members_processed,
        openai_calls=outcome.api_calls,
        version_id=version.id,
        warnings=[
            *retry_warnings,
            *processing_warnings,
            *([terminal_warning] if terminal_warning else []),
        ],
    )


def _audit_result_for_attachment(
    attachment: dict[str, Any],
    result: PpsEnrichmentResult,
    *,
    attempted: bool,
    reason_code: str | None = None,
) -> PpsAttachmentEnrichmentResult:
    warning_code = next(
        (
            item
            for item in result.warnings
            if item
            not in {
                "CORRECTIVE_EXTRACTION_RETRY_USED",
                "REVIEW_COOLDOWN_REUSED",
                "DUPLICATE_CONTENT_REUSED",
            }
        ),
        None,
    )
    if reason_code is None:
        if result.status in {"COMPLETED", "REUSED"} and not warning_code:
            reason_code = "ANALYZED"
        elif result.status == "PLANNED":
            reason_code = "DRY_RUN_PLANNED"
        else:
            reason_code = warning_code or "OPENAI_REVIEW"
    audit_status: Literal["COMPLETED", "REUSED", "REVIEW", "PLANNED"]
    if result.status == "PLANNED":
        audit_status = "PLANNED"
    elif reason_code == "ANALYZED" and result.status in {"COMPLETED", "REUSED"}:
        audit_status = result.status
    else:
        audit_status = "REVIEW"
    return PpsAttachmentEnrichmentResult(
        attachment_id=attachment["attachment_id"],
        media_type=attachment["media_type"],
        status=audit_status,
        reason_code=reason_code,
        attempted=attempted,
        content_extracted=result.attachments_processed > 0,
        source_read_complete=result.source_read_complete,
        analysis_input_complete=result.analysis_input_complete,
        source_characters=result.source_characters,
        analysis_input_characters=result.analysis_input_characters,
        members_discovered=result.members_discovered,
        members_processed=result.members_processed,
        openai_calls=result.openai_calls,
        version_id=result.version_id,
    )


def _record_unsupported_pps_attachment(
    session: Session,
    *,
    notice_id: str,
    versions: list[NoticeVersion],
    attachment: dict[str, Any],
    attachments_discovered: int,
) -> PpsEnrichmentResult:
    extension = PurePath(attachment["file_name"]).suffix.casefold()
    error_code = (
        "HWP_BINARY_UNSUPPORTED"
        if extension == ".hwp"
        else "UNSUPPORTED_ATTACHMENT_TYPE"
    )
    manifest_sha256 = _digest(attachment)
    document_sha256 = _digest({"manifest": manifest_sha256, "error": error_code})
    prior = _matching_extraction_version(
        versions,
        attachment_id=attachment["attachment_id"],
        manifest_sha256=manifest_sha256,
        document_sha256=document_sha256,
    )
    if prior is not None:
        return PpsEnrichmentResult(
            status="REUSED",
            attachments_discovered=attachments_discovered,
            version_id=prior.id,
            warnings=[error_code],
        )
    with session.begin():
        version = _persist_extraction_version(
            session,
            notice_id=notice_id,
            attachment=attachment,
            manifest_sha256=manifest_sha256,
            document_sha256=document_sha256,
            outcome=None,
            error_code=error_code,
        )
    return PpsEnrichmentResult(
        status="REVIEW",
        attachments_discovered=attachments_discovered,
        version_id=version.id,
        warnings=[error_code],
    )


def enrich_notice_from_pps(
    session: Session,
    *,
    notice_id: str,
    openai_api_key: str | None,
    openai_model: str,
    max_attachments: int = MAX_ATTACHMENTS_IN_MANIFEST,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    openai_client_factory: Callable[..., OpenAIExtractionClient] = OpenAIExtractionClient,
    download_timeout_seconds: float = 12,
    openai_timeout_seconds: float = 45,
    openai_max_retries: int = 0,
    deadline_monotonic: float | None = None,
) -> PpsEnrichmentResult:
    """Audit and analyse every valid attachment in the current PPS manifest."""

    if session.in_transaction():
        raise RuntimeError("enrich_notice_from_pps requires a clean Session")
    if max_attachments != MAX_ATTACHMENTS_IN_MANIFEST:
        raise ValueError(
            f"max_attachments must equal the PPS manifest bound ({MAX_ATTACHMENTS_IN_MANIFEST})"
        )
    with session.begin():
        notice = session.get(Notice, notice_id)
        if notice is None:
            return PpsEnrichmentResult(status="SKIPPED", warnings=["NOTICE_NOT_FOUND"])
        versions = list(
            session.scalars(
                select(NoticeVersion)
                .where(NoticeVersion.notice_id == notice_id)
                .order_by(NoticeVersion.version_no.desc())
            ).all()
        )
        metadata = next(
            (
                item
                for item in versions
                if isinstance(item.source_payload, dict)
                and item.source_payload.get("kind") == PPS_METADATA_KIND
                and isinstance(item.source_payload.get("attachment_manifest"), list)
            ),
            None,
        )
        if metadata is None:
            return PpsEnrichmentResult(status="SKIPPED", warnings=["PPS_ATTACHMENT_MANIFEST_MISSING"])
        raw_manifest = metadata.source_payload.get("attachment_manifest", [])
        manifest = [dict(item) for item in raw_manifest if isinstance(item, dict)]
        attachments, invalid_count = _validated_manifest_attachments(manifest)
        _current, _current_invalid, current_attempts = _current_manifest_attempts(versions)
        discovered = len(manifest)
    base_warnings = ["INVALID_ATTACHMENT_MANIFEST"] if invalid_count else []
    if not attachments:
        return PpsEnrichmentResult(
            status="PLANNED" if dry_run else ("REVIEW" if invalid_count else "SKIPPED"),
            attachments_discovered=discovered,
            warnings=[
                *( ["DRY_RUN_NO_EXTERNAL_CALLS"] if dry_run else [] ),
                *(base_warnings or ["ATTACHMENT_MANIFEST_EMPTY"]),
            ],
        )

    if dry_run:
        planned = PpsEnrichmentResult(status="PLANNED")
        audits = [
            _audit_result_for_attachment(
                attachment,
                planned,
                attempted=False,
            )
            for attachment in attachments
        ]
        return PpsEnrichmentResult(
            status="PLANNED",
            attachments_discovered=discovered,
            warnings=["DRY_RUN_NO_EXTERNAL_CALLS", *base_warnings],
            attachment_results=audits,
        )

    audits: list[PpsAttachmentEnrichmentResult] = []
    warnings = list(base_warnings)
    processed = openai_calls = 0
    downloaded_bytes = source_characters = analysis_input_characters = 0
    members_discovered = members_processed = 0
    new_attempts = 0
    last_version_id: str | None = None
    for attachment in attachments:
        stored_version = current_attempts.get(attachment["attachment_id"])
        stored_result = (
            _stored_attachment_result(
                stored_version,
                attachments_discovered=discovered,
            )
            if stored_version is not None
            else None
        )
        if stored_result is not None:
            audit = _audit_result_for_attachment(
                attachment,
                stored_result,
                attempted=True,
            )
            audits.append(audit)
            warnings.extend(stored_result.warnings)
            processed += stored_result.attachments_processed
            source_characters += stored_result.source_characters
            analysis_input_characters += stored_result.analysis_input_characters
            members_discovered += stored_result.members_discovered
            members_processed += stored_result.members_processed
            if stored_result.version_id:
                last_version_id = stored_result.version_id
            continue

        # One missing attachment can require one download plus an initial and
        # corrective Responses call. Start it only when the enclosing request
        # still has enough time for that complete bounded unit. Successfully
        # persisted siblings remain durable and the notice is re-leased.
        worst_case_seconds = (
            (download_timeout_seconds * 3)
            + (openai_timeout_seconds * MAX_OPENAI_CALLS_PER_ATTACHMENT)
            + 5
        )
        if new_attempts >= MAX_NEW_ATTACHMENTS_PER_REQUEST or (
            deadline_monotonic is not None
            and deadline_monotonic - time.monotonic() < worst_case_seconds
        ):
            warnings.extend(
                [
                    "ATTACHMENT_CONTINUATION_REQUIRED",
                    "ATTACHMENT_COVERAGE_INCOMPLETE",
                ]
            )
            break
        new_attempts += 1
        extension = PurePath(attachment["file_name"]).suffix.casefold()
        if extension not in _EXTRACTABLE_EXTENSIONS:
            try:
                item_result = _record_unsupported_pps_attachment(
                    session,
                    notice_id=notice_id,
                    versions=versions,
                    attachment=attachment,
                    attachments_discovered=discovered,
                )
            except Exception:
                session.rollback()
                item_result = record_internal_pps_enrichment_failure(
                    session,
                    notice_id=notice_id,
                    attachment=attachment,
                    manifest_sha256=_digest(attachment),
                    attachments_discovered=discovered,
                )
            reason_code = (
                "HWP_BINARY_UNSUPPORTED"
                if extension == ".hwp"
                else "UNSUPPORTED_ATTACHMENT_TYPE"
            )
        else:
            try:
                item_result = _enrich_selected_pps_attachment(
                    session,
                    notice_id=notice_id,
                    versions=versions,
                    attachment=attachment,
                    manifest_sha256=_digest(attachment),
                    attachments_discovered=discovered,
                    openai_api_key=openai_api_key,
                    openai_model=openai_model,
                    transport=transport,
                    openai_client_factory=openai_client_factory,
                    download_timeout_seconds=download_timeout_seconds,
                    openai_timeout_seconds=openai_timeout_seconds,
                    openai_max_retries=openai_max_retries,
                )
            except Exception:
                # Each exact attachment owns its failure marker. Continue with
                # siblings so one corrupt document never discards successes.
                session.rollback()
                item_result = record_internal_pps_enrichment_failure(
                    session,
                    notice_id=notice_id,
                    attachment=attachment,
                    manifest_sha256=_digest(attachment),
                    attachments_discovered=discovered,
                )
            reason_code = None
        audit = _audit_result_for_attachment(
            attachment,
            item_result,
            attempted=True,
            reason_code=reason_code,
        )
        audits.append(audit)
        warnings.extend(item_result.warnings)
        processed += item_result.attachments_processed
        downloaded_bytes += item_result.downloaded_bytes
        source_characters += item_result.source_characters
        analysis_input_characters += item_result.analysis_input_characters
        members_discovered += item_result.members_discovered
        members_processed += item_result.members_processed
        openai_calls += item_result.openai_calls
        if openai_calls > MAX_OPENAI_CALLS_PER_NOTICE:
            raise PpsEnrichmentError("OPENAI_NOTICE_CALL_LIMIT")
        if item_result.version_id:
            last_version_id = item_result.version_id

    continuation_required = "ATTACHMENT_CONTINUATION_REQUIRED" in warnings
    status: Literal["COMPLETED", "REUSED", "REVIEW", "SKIPPED", "PLANNED"]
    if continuation_required:
        status = "SKIPPED"
    elif invalid_count or any(item.status == "REVIEW" for item in audits):
        status = "REVIEW"
    elif any(item.status == "COMPLETED" for item in audits):
        status = "COMPLETED"
    else:
        status = "REUSED"
    return PpsEnrichmentResult(
        status=status,
        attachments_discovered=discovered,
        attachments_attempted=len(audits),
        attachments_processed=processed,
        downloaded_bytes=downloaded_bytes,
        source_characters=source_characters,
        analysis_input_characters=analysis_input_characters,
        source_read_complete=(
            len(audits) == len(attachments)
            and all(
                item.source_read_complete
                for item in audits
                if PurePath(
                    next(
                        attachment["file_name"]
                        for attachment in attachments
                        if attachment["attachment_id"] == item.attachment_id
                    )
                ).suffix.casefold()
                in _EXTRACTABLE_EXTENSIONS
            )
        ),
        analysis_input_complete=(
            len(audits) == len(attachments)
            and all(
                item.analysis_input_complete
                for item in audits
                if PurePath(
                    next(
                        attachment["file_name"]
                        for attachment in attachments
                        if attachment["attachment_id"] == item.attachment_id
                    )
                ).suffix.casefold()
                in _EXTRACTABLE_EXTENSIONS
            )
        ),
        members_discovered=members_discovered,
        members_processed=members_processed,
        openai_calls=openai_calls,
        version_id=last_version_id,
        warnings=sorted(set(warnings)),
        attachment_results=audits,
    )
