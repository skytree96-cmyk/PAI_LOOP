from __future__ import annotations

import hashlib
import io
import json
import re
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
MAX_DOCUMENT_CHARS = 120_000
MAX_PDF_PAGES = 120
MAX_HWPX_ENTRIES = 240
MAX_HWPX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
REVIEW_RETRY_COOLDOWN = timedelta(hours=24)
DETERMINISTIC_REVIEW_CODES = {
    "HWP_ONLY_UNSUPPORTED_R07",
    "HWP_BINARY_UNSUPPORTED",
    "UNSUPPORTED_ATTACHMENT_TYPE",
}

_ATTACHMENT_ID_PATTERN = re.compile(r"^PPS-ATT-[a-f0-9]{24}$")
_SAFE_QUERY_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
_SUPPORTED_EXTENSION_MEDIA = {
    ".pdf": "application/pdf",
    ".hwpx": "application/hwp+zip",
    ".hwp": "application/x-hwp",
}
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
class PpsEnrichmentResult:
    status: Literal["COMPLETED", "REUSED", "REVIEW", "SKIPPED", "PLANNED"]
    attachments_discovered: int = 0
    attachments_processed: int = 0
    openai_calls: int = 0
    version_id: str | None = None
    warnings: list[str] = field(default_factory=list)


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
    media_type = _SUPPORTED_EXTENSION_MEDIA.get(extension)
    if media_type is None:
        raise PpsEnrichmentError("UNSUPPORTED_ATTACHMENT_TYPE")
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
        if not raw_url or not raw_name:
            continue
        try:
            url = _safe_g2b_attachment_url(raw_url)
            filename, media_type = _safe_filename(raw_name)
        except PpsEnrichmentError:
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
    return metadata


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
    uses the two organization baselines plus the first unique strong term from
    each department (24 for the full profile), leaving four slots for explicit
    user terms under the 30-query provider cap.
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

    candidates: list[str] = list(catalog["baseline"].get("strong_keywords", []))
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
    candidates.extend(explicit)
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


def has_current_accepted_pps_extraction(session: Session, notice_id: str) -> bool:
    """Return true only when ACCEPTED evidence matches the latest manifest."""

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
        return False
    manifest = [
        dict(item)
        for item in metadata.source_payload.get("attachment_manifest", [])
        if isinstance(item, dict)
    ]
    selected, _warnings = select_preferred_attachments(manifest, limit=1)
    if not selected:
        return False
    attachment = selected[0]
    manifest_sha256 = _digest(attachment)
    return any(
        isinstance(item.source_payload, dict)
        and item.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
        and item.source_payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
        and item.source_payload.get("status") == "ACCEPTED"
        and item.source_payload.get("prompt_version") == PROMPT_VERSION
        and item.source_payload.get("attachment_id") == attachment["attachment_id"]
        and item.source_payload.get("manifest_sha256") == manifest_sha256
        for item in versions
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


def select_preferred_attachments(
    manifest: list[dict[str, Any]],
    *,
    limit: int = 1,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not 1 <= limit <= 1:
        raise ValueError("max attachments per notice must be exactly 1")
    validated: list[dict[str, Any]] = []
    for item in manifest[:MAX_ATTACHMENTS_IN_MANIFEST]:
        try:
            validated.append(_manifest_item(item))
        except PpsEnrichmentError:
            continue
    supported = [item for item in validated if PurePath(item["file_name"]).suffix.casefold() in {".pdf", ".hwpx"}]
    if not supported:
        return [], ["HWP_ONLY_UNSUPPORTED_R07"] if validated else ["ATTACHMENT_MANIFEST_EMPTY"]

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
    return supported[:limit], []


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
                if total > MAX_DOCUMENT_CHARS:
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
            text = " ".join(value.strip() for value in root.itertext() if value.strip())
            total += len(text)
            if total > MAX_DOCUMENT_CHARS:
                raise PpsEnrichmentError("DOCUMENT_TEXT_TOO_LARGE")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def extract_document_text(file_name: str, content: bytes) -> str:
    filename, _media_type = _safe_filename(file_name)
    extension = PurePath(filename).suffix.casefold()
    if extension == ".pdf":
        text = _extract_pdf_text(content)
    elif extension == ".hwpx":
        text = _extract_hwpx_text(content)
    else:
        raise PpsEnrichmentError("HWP_BINARY_UNSUPPORTED")
    if len(text.strip()) < 20:
        raise PpsEnrichmentError("DOCUMENT_TEXT_EMPTY_OR_SHORT")
    return text[:MAX_DOCUMENT_CHARS]


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
    if (
        payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION"
        or payload.get("source_kind") != PPS_ATTACHMENT_SOURCE
        or payload.get("status") != "ACCEPTED"
        or payload.get("prompt_version") != PROMPT_VERSION
        or payload.get("schema_version") != SCHEMA_VERSION
        or not isinstance(attachment_id, str)
        or not _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id)
        or not isinstance(source_label, str)
        or not re.fullmatch(r"[^/\\\x00-\x1f\x7f]{1,255}\.(?:pdf|hwpx)", source_label, re.IGNORECASE)
        or not re.fullmatch(r"[a-f0-9]{64}", str(payload.get("document_sha256") or ""), re.IGNORECASE)
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
) -> NoticeVersion:
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
        "schema_version": outcome.schema_version if outcome else SCHEMA_VERSION,
        "result": data,
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
                accepted and outcome and outcome.data and not outcome.data.missing_or_unreadable
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
                    and prior.get("status") == payload["status"]
                    and _stored_outcome_is_idempotent(raced, payload)
                ):
                    return raced
    raise PpsEnrichmentError("NOTICE_VERSION_RACE")


def enrich_notice_from_pps(
    session: Session,
    *,
    notice_id: str,
    openai_api_key: str | None,
    openai_model: str,
    max_attachments: int = 1,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    openai_client_factory: Callable[..., OpenAIExtractionClient] = OpenAIExtractionClient,
    download_timeout_seconds: float = 12,
    openai_timeout_seconds: float = 45,
    openai_max_retries: int = 0,
) -> PpsEnrichmentResult:
    """Download one preferred public attachment in memory and persist only evidence output."""

    if session.in_transaction():
        raise RuntimeError("enrich_notice_from_pps requires a clean Session")
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
        selected, selection_warnings = select_preferred_attachments(manifest, limit=max_attachments)
        discovered = len(manifest)
        if not selected:
            if dry_run:
                return PpsEnrichmentResult(
                    status="PLANNED",
                    attachments_discovered=discovered,
                    warnings=["DRY_RUN_NO_EXTERNAL_CALLS", *selection_warnings],
                )
            fallback = next((item for item in manifest if isinstance(item, dict)), None)
            if fallback is None:
                return PpsEnrichmentResult(
                    status="SKIPPED",
                    attachments_discovered=discovered,
                    warnings=selection_warnings,
                )
            try:
                attachment = _manifest_item(fallback)
            except PpsEnrichmentError:
                return PpsEnrichmentResult(
                    status="SKIPPED",
                    attachments_discovered=discovered,
                    warnings=["INVALID_ATTACHMENT_MANIFEST"],
                )
            manifest_sha256 = _digest(attachment)
            document_sha256 = _digest({"manifest": manifest_sha256, "error": selection_warnings})
        else:
            attachment = selected[0]
            manifest_sha256 = _digest(attachment)
            if dry_run:
                return PpsEnrichmentResult(
                    status="PLANNED",
                    attachments_discovered=discovered,
                    warnings=["DRY_RUN_NO_EXTERNAL_CALLS"],
                )
            document_sha256 = ""

    if not selected:
        prior = _matching_extraction_version(
            versions,
            attachment_id=attachment["attachment_id"],
            manifest_sha256=manifest_sha256,
            document_sha256=document_sha256,
        )
        if prior is not None:
            return PpsEnrichmentResult(
                status="REUSED",
                attachments_discovered=discovered,
                version_id=prior.id,
                warnings=selection_warnings,
            )
        with session.begin():
            version = _persist_extraction_version(
                session,
                notice_id=notice_id,
                attachment=attachment,
                manifest_sha256=manifest_sha256,
                document_sha256=document_sha256,
                outcome=None,
                error_code=selection_warnings[0] if selection_warnings else "ATTACHMENT_UNSUPPORTED",
            )
        return PpsEnrichmentResult(
            status="REVIEW",
            attachments_discovered=discovered,
            version_id=version.id,
            warnings=selection_warnings,
        )

    try:
        content = download_public_attachment(
            attachment,
            transport=transport,
            timeout_seconds=download_timeout_seconds,
        )
        document_sha256 = hashlib.sha256(content).hexdigest()
        document_text = extract_document_text(attachment["file_name"], content)
    except PpsEnrichmentError as exc:
        error_code = str(exc)
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
                attachments_discovered=discovered,
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
            attachments_discovered=discovered,
            version_id=version.id,
            warnings=[error_code],
        )

    prior = _matching_extraction_version(
        versions,
        attachment_id=attachment["attachment_id"],
        manifest_sha256=manifest_sha256,
        document_sha256=document_sha256,
    )
    if prior is not None:
        prior_status = (
            prior.source_payload.get("status")
            if isinstance(prior.source_payload, dict)
            else None
        )
        return PpsEnrichmentResult(
            status="REUSED" if prior_status == "ACCEPTED" else "REVIEW",
            attachments_discovered=discovered,
            attachments_processed=1,
            version_id=prior.id,
            warnings=(
                []
                if prior_status == "ACCEPTED"
                else ["REVIEW_COOLDOWN_REUSED"]
            ),
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
            )
        return PpsEnrichmentResult(
            status="REVIEW",
            attachments_discovered=discovered,
            attachments_processed=1,
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
            document_text=document_text,
            allowed_attachment_ids={attachment["attachment_id"]},
        )
    with session.begin():
        version = _persist_extraction_version(
            session,
            notice_id=notice_id,
            attachment=attachment,
            manifest_sha256=manifest_sha256,
            document_sha256=document_sha256,
            outcome=outcome,
            error_code=outcome.error_code,
        )
    return PpsEnrichmentResult(
        status="COMPLETED" if outcome.status == "ACCEPTED" else "REVIEW",
        attachments_discovered=discovered,
        attachments_processed=1,
        openai_calls=1,
        version_id=version.id,
        warnings=[] if outcome.status == "ACCEPTED" else [outcome.error_code or "OPENAI_REVIEW_R07"],
    )
