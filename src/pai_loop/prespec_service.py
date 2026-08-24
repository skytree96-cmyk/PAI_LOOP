from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import PurePath
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import unquote, urljoin

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    OpenAIExtractionClient,
    OpenAITelemetry,
    merge_openai_telemetry,
)
from .integrations.pps import PpsApiError
from .integrations.prespec import (
    DEFAULT_PRESPEC_OPERATION,
    PpsPreSpecificationClient,
    canonical_pre_specification_document_url,
    matched_pre_specification_keywords,
)
from .models import Notice
from .pps_enrichment import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    PpsEnrichmentError,
    extract_pps_document_content,
    select_document_analysis_input,
)
from .prespec_models import (
    PreSpecification,
    PreSpecificationAnalysisRun,
    PreSpecificationDocument,
    PreSpecificationVersion,
)


PRESPEC_SEARCH_ROWS = 999
PRESPEC_SEARCH_MAX_PAGES = 3
PRESPEC_SEARCH_DEADLINE_SECONDS = 25
PRESPEC_MAX_DOCUMENTS = 5
PRESPEC_MAX_OPENAI_CALLS = PRESPEC_MAX_DOCUMENTS * 2

_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
_SAFE_EXTENSIONS = {
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
_CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/hwp+zip": ".hwpx",
    "application/x-hwp": ".hwp",
    "application/haansofthwp": ".hwp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
    "application/vnd.ms-excel": ".xls",
    "text/html": ".html",
    "application/zip": ".zip",
}
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)0\d{1,2}[ -]?\d{3,4}[ -]?\d{4}(?!\d)")
_CONTACT = re.compile(
    r"(?i)(?:(?:문의|연락)\s*)?(?:담당자?|문의처?|연락처|contact)"
    r"\s*(?:[:：]\s*)?(?:[가-힣]{2,5}|[A-Z][A-Z .'-]{1,40})"
    r"(?:\s*(?:주무관|과장|팀장|담당자))?"
)


class PreSpecificationServiceError(RuntimeError):
    """A public-safe, deterministic pre-specification failure code."""


@dataclass(frozen=True, slots=True)
class PreSpecificationFetchResult:
    records: list[dict[str, Any]]
    api_calls: int
    fetched: int
    quarantined: int
    status: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PreSpecificationUpsertResult:
    pre_specification: PreSpecification
    outcome: str
    version_created: bool
    linked_notice_count: int


@dataclass(frozen=True, slots=True)
class FetchedPreSpecificationDocument:
    file_name: str
    content: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class PreSpecificationAnalysisResult:
    status: str
    result_json: dict[str, Any] | None
    document_results: list[dict[str, Any]]
    openai_calls: int
    openai_telemetry: OpenAITelemetry
    warnings: list[str]


class PreSpecificationDocumentFetcher(Protocol):
    def fetch(
        self,
        *,
        safe_url: str,
        registry_no: str,
        slot: int,
    ) -> FetchedPreSpecificationDocument: ...


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _sha256(value: Any) -> str:
    encoded = value if isinstance(value, bytes) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def pre_specification_source_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    """Return the complete persisted provider allowlist for digesting/versioning."""

    registry_no = str(record.get("registry_no") or "")
    urls: list[str] = []
    for raw_url in list(record.get("document_urls") or [])[:PRESPEC_MAX_DOCUMENTS]:
        safe_url = canonical_pre_specification_document_url(
            raw_url,
            registry_no=registry_no,
        )
        if safe_url and safe_url not in urls:
            urls.append(safe_url)
    return {
        "source_kind": "PPS_PRESPEC",
        "pre_specification_key": str(record.get("pre_specification_key") or ""),
        "registry_no": registry_no,
        "title": str(record.get("title") or ""),
        "business_division": record.get("business_division"),
        "reference_no": record.get("reference_no"),
        "ordering_agency": record.get("ordering_agency"),
        "demand_agency": record.get("demand_agency"),
        "budget_amount": record.get("budget_amount"),
        "received_at": _iso(record.get("received_at")),
        "opinion_deadline": _iso(record.get("opinion_deadline")),
        "delivery_due": _iso(record.get("delivery_due")),
        "registered_at": _iso(record.get("registered_at")),
        "changed_at": _iso(record.get("changed_at")),
        "software_business": bool(record.get("software_business")),
        "document_urls": urls,
        "linked_bid_notice_nos": list(record.get("linked_bid_notice_nos") or []),
    }


def pre_specification_selection_token(record: dict[str, Any]) -> str:
    snapshot = pre_specification_source_snapshot(record)
    return "PRESPECSEL-" + _sha256(snapshot)[:32]


def pre_specification_status(
    *,
    opinion_deadline: datetime | None,
    linked_bid_notice_nos: Iterable[str],
    now: datetime | None = None,
) -> str:
    if any(str(value).strip() for value in linked_bid_notice_nos):
        return "LINKED_TO_BID"
    if opinion_deadline is None:
        return "OPINION_CLOSED"
    current = now or datetime.now(timezone.utc)
    deadline = (
        opinion_deadline.replace(tzinfo=timezone.utc)
        if opinion_deadline.tzinfo is None
        else opinion_deadline.astimezone(timezone.utc)
    )
    return "OPEN_FOR_OPINION" if deadline > current.astimezone(timezone.utc) else "OPINION_CLOSED"


def fetch_pre_specifications(
    settings: Settings,
    *,
    query: str,
    from_date: date,
    to_date: date,
    client_factory: Callable[..., PpsPreSpecificationClient] | None = None,
) -> PreSpecificationFetchResult:
    """Fetch a bounded registration window and filter locally with zero writes/model calls."""

    if not settings.pps_api_key:
        raise PreSpecificationServiceError("PPS_NOT_CONFIGURED")
    try:
        deadline = time.monotonic() + PRESPEC_SEARCH_DEADLINE_SECONDS
        selected_client_factory = client_factory or PpsPreSpecificationClient
        with selected_client_factory(
            service_key=settings.pps_api_key,
            base_url=settings.pps_base_url,
            timeout_seconds=12,
            max_retries=1,
        ) as client:
            fetched_records = list(
                client.iter_pre_specifications(
                    start=from_date,
                    end=to_date,
                    operation_path=DEFAULT_PRESPEC_OPERATION,
                    rows=PRESPEC_SEARCH_ROWS,
                    max_pages=PRESPEC_SEARCH_MAX_PAGES,
                    deadline_monotonic=deadline,
                )
            )
            api_calls = client.request_count
            hit_page_limit = bool(client.hit_page_limit)
            hit_time_limit = bool(client.hit_time_limit)
            fetched = int(getattr(client, "rows_fetched", len(fetched_records)))
            quarantined = int(getattr(client, "rows_quarantined", 0))
    except PpsApiError as exc:
        raise PreSpecificationServiceError("PPS_PROVIDER_ERROR") from exc

    records: list[dict[str, Any]] = []
    for record in fetched_records:
        matches = matched_pre_specification_keywords(record, [query])
        if matches:
            records.append({**record, "matched_keywords": matches})
    records.sort(
        key=lambda item: (
            item.get("changed_at")
            or item.get("registered_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            str(item.get("registry_no") or ""),
        ),
        reverse=True,
    )
    warnings: list[str] = []
    if hit_page_limit:
        warnings.append("PPS_PAGE_LIMIT_REACHED")
    if hit_time_limit:
        warnings.append("PPS_TIME_LIMIT_REACHED")
    if quarantined:
        warnings.append("PPS_ROWS_QUARANTINED")
    return PreSpecificationFetchResult(
        records=records,
        api_calls=api_calls,
        fetched=fetched,
        quarantined=quarantined,
        status=(
            "PARTIAL"
            if hit_page_limit or hit_time_limit or quarantined
            else "COMPLETED"
        ),
        warnings=warnings,
    )


def _merge_keywords(existing: Iterable[str], incoming: Iterable[str]) -> list[str]:
    values: dict[str, str] = {}
    for raw_value in [*existing, *incoming]:
        value = " ".join(str(raw_value).split())[:60]
        if value:
            values.setdefault(value.casefold(), value)
    return sorted(values.values(), key=str.casefold)[:50]


def persist_pre_specification(
    session: Session,
    *,
    record: dict[str, Any],
    matched_keywords: Iterable[str],
    now: datetime | None = None,
) -> PreSpecificationUpsertResult:
    """Idempotently persist one selected allowlisted record and immutable version."""

    current = now or datetime.now(timezone.utc)
    snapshot = pre_specification_source_snapshot(record)
    registry_no = snapshot["registry_no"]
    title = snapshot["title"]
    if not registry_no or not title:
        raise PreSpecificationServiceError("PRESPEC_IDENTITY_INCOMPLETE")
    if len(list(record.get("document_urls") or [])) > PRESPEC_MAX_DOCUMENTS:
        raise PreSpecificationServiceError("PRESPEC_DOCUMENT_LIMIT")
    source_digest = _sha256(snapshot)
    linked_notice_nos = list(snapshot["linked_bid_notice_nos"])
    status_value = pre_specification_status(
        opinion_deadline=record.get("opinion_deadline"),
        linked_bid_notice_nos=linked_notice_nos,
        now=current,
    )
    stored = session.scalar(
        select(PreSpecification).where(PreSpecification.registry_no == registry_no)
    )
    created = stored is None
    version_created = False
    if stored is None:
        stored = PreSpecification(
            registry_no=registry_no,
            pre_specification_key=snapshot["pre_specification_key"],
            title=title,
            source_digest=source_digest,
            first_seen_at=current,
            last_seen_at=current,
        )
        session.add(stored)
        session.flush()
    stored.last_seen_at = current
    stored.matched_keywords = _merge_keywords(
        list(stored.matched_keywords or []),
        matched_keywords,
    )

    if created or stored.source_digest != source_digest:
        stored.pre_specification_key = snapshot["pre_specification_key"]
        stored.title = title
        stored.business_division = snapshot["business_division"]
        stored.reference_no = snapshot["reference_no"]
        stored.ordering_agency = snapshot["ordering_agency"]
        stored.demand_agency = snapshot["demand_agency"]
        stored.budget_amount = snapshot["budget_amount"]
        stored.received_at = record.get("received_at")
        stored.opinion_deadline = record.get("opinion_deadline")
        stored.delivery_due = record.get("delivery_due")
        stored.registered_at = record.get("registered_at")
        stored.changed_at = record.get("changed_at")
        stored.software_business = bool(snapshot["software_business"])
        stored.status = status_value
        stored.linked_bid_notice_nos = linked_notice_nos
        stored.source_digest = source_digest
        next_version = int(
            session.scalar(
                select(func.max(PreSpecificationVersion.version_no)).where(
                    PreSpecificationVersion.pre_specification_id == stored.id
                )
            )
            or 0
        ) + 1
        version = PreSpecificationVersion(
            pre_specification_id=stored.id,
            version_no=next_version,
            source_digest=source_digest,
            provider_changed_at=record.get("changed_at"),
            source_snapshot=snapshot,
        )
        session.add(version)
        session.flush()
        for slot, safe_url in enumerate(snapshot["document_urls"], start=1):
            session.add(
                PreSpecificationDocument(
                    pre_specification_id=stored.id,
                    pre_specification_version_id=version.id,
                    slot=slot,
                    safe_url=safe_url,
                    source_digest=_sha256({"slot": slot, "safe_url": safe_url}),
                )
            )
        version_created = True
    else:
        # Time can close an unchanged record; status is derived without making
        # a fake provider version.
        stored.status = status_value

    linked_notice_count = 0
    if linked_notice_nos:
        linked_notice_count = int(
            session.scalar(
                select(func.count(Notice.id)).where(
                    Notice.bid_notice_no.in_(linked_notice_nos)
                )
            )
            or 0
        )
    session.flush()
    return PreSpecificationUpsertResult(
        pre_specification=stored,
        outcome="CREATED" if created else ("UPDATED" if version_created else "ALREADY_STORED"),
        version_created=version_created,
        linked_notice_count=linked_notice_count,
    )


def current_pre_specification_documents(
    session: Session,
    pre_specification: PreSpecification,
) -> list[PreSpecificationDocument]:
    version_id = session.scalar(
        select(PreSpecificationVersion.id).where(
            PreSpecificationVersion.pre_specification_id == pre_specification.id,
            PreSpecificationVersion.source_digest == pre_specification.source_digest,
        )
    )
    if version_id is None:
        return []
    return list(
        session.scalars(
            select(PreSpecificationDocument)
            .where(PreSpecificationDocument.pre_specification_version_id == version_id)
            .order_by(PreSpecificationDocument.slot)
        ).all()
    )


def latest_pre_specification_analysis(
    session: Session,
    pre_specification: PreSpecification,
) -> PreSpecificationAnalysisRun | None:
    return session.scalar(
        select(PreSpecificationAnalysisRun)
        .where(
            PreSpecificationAnalysisRun.pre_specification_id == pre_specification.id,
            PreSpecificationAnalysisRun.source_digest == pre_specification.source_digest,
        )
        .order_by(PreSpecificationAnalysisRun.created_at.desc())
    )


def _filename_from_response(
    response: httpx.Response,
    *,
    registry_no: str,
    slot: int,
) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    candidate = ""
    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if match:
        candidate = unquote(match.group(1).strip().strip('"'))
    if not candidate:
        match = re.search(r"filename\s*=\s*(?:\"([^\"]+)\"|([^;]+))", disposition, re.IGNORECASE)
        if match:
            candidate = unquote((match.group(1) or match.group(2) or "").strip())
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
    if not candidate:
        extension = _CONTENT_TYPE_EXTENSIONS.get(content_type)
        if extension:
            candidate = f"prespec-{registry_no}-{slot}{extension}"
    if (
        not candidate
        or not _SAFE_FILENAME.fullmatch(candidate)
        or PurePath(candidate).name != candidate
        or PurePath(candidate).suffix.casefold() not in _SAFE_EXTENSIONS
    ):
        raise PreSpecificationServiceError("PRESPEC_DOCUMENT_FILENAME_UNSAFE")
    return candidate


class HttpPreSpecificationDocumentFetcher:
    """Strict fetcher for the one documented PPS pre-spec document endpoint."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 12,
        max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        max_redirects: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.transport = transport

    def fetch(
        self,
        *,
        safe_url: str,
        registry_no: str,
        slot: int,
    ) -> FetchedPreSpecificationDocument:
        current_url = canonical_pre_specification_document_url(
            safe_url,
            registry_no=registry_no,
        )
        if current_url is None:
            raise PreSpecificationServiceError("PRESPEC_DOCUMENT_URL_UNSAFE")
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            transport=self.transport,
            headers={"User-Agent": "PAI-LOOP-prespec-document/0.1"},
        ) as client:
            for redirect_count in range(self.max_redirects + 1):
                try:
                    with client.stream("GET", current_url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect_count >= self.max_redirects:
                                raise PreSpecificationServiceError(
                                    "PRESPEC_DOCUMENT_REDIRECT_LIMIT"
                                )
                            redirected = canonical_pre_specification_document_url(
                                urljoin(current_url, response.headers.get("Location", "")),
                                registry_no=registry_no,
                            )
                            if redirected is None:
                                raise PreSpecificationServiceError(
                                    "PRESPEC_DOCUMENT_REDIRECT_UNSAFE"
                                )
                            current_url = redirected
                            continue
                        if response.status_code != 200:
                            raise PreSpecificationServiceError(
                                f"PRESPEC_DOCUMENT_HTTP_{response.status_code}"
                            )
                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            try:
                                if int(content_length) > self.max_bytes:
                                    raise PreSpecificationServiceError(
                                        "PRESPEC_DOCUMENT_TOO_LARGE"
                                    )
                            except ValueError as exc:
                                raise PreSpecificationServiceError(
                                    "PRESPEC_DOCUMENT_LENGTH_INVALID"
                                ) from exc
                        file_name = _filename_from_response(
                            response,
                            registry_no=registry_no,
                            slot=slot,
                        )
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise PreSpecificationServiceError(
                                    "PRESPEC_DOCUMENT_TOO_LARGE"
                                )
                            chunks.append(chunk)
                        if not chunks:
                            raise PreSpecificationServiceError(
                                "PRESPEC_DOCUMENT_EMPTY"
                            )
                        return FetchedPreSpecificationDocument(
                            file_name=file_name,
                            content=b"".join(chunks),
                            content_type=response.headers.get("Content-Type", "")[:200],
                        )
                except httpx.RequestError as exc:
                    raise PreSpecificationServiceError(
                        "PRESPEC_DOCUMENT_NETWORK_ERROR"
                    ) from exc
        raise PreSpecificationServiceError("PRESPEC_DOCUMENT_REDIRECT_LIMIT")


def _redact_public_value(value: Any) -> Any:
    if isinstance(value, str):
        text = _EMAIL.sub("[비공개]", value)
        text = _PHONE.sub("[비공개]", text)
        return _CONTACT.sub("담당자 [비공개]", text)
    if isinstance(value, list):
        return [_redact_public_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_public_value(item) for key, item in value.items()}
    return value


def analyse_pre_specification_documents(
    *,
    registry_no: str,
    source_digest: str,
    documents: list[PreSpecificationDocument],
    openai_api_key: str,
    openai_model: str,
    llm_provider: str = "openai",
    llm_gateway_base_url: str | None = None,
    document_fetcher: PreSpecificationDocumentFetcher | None = None,
    openai_client_factory: Callable[..., OpenAIExtractionClient] = OpenAIExtractionClient,
) -> PreSpecificationAnalysisResult:
    """Download and analyse every current safe document under explicit hard caps."""

    if len(documents) > PRESPEC_MAX_DOCUMENTS:
        raise PreSpecificationServiceError("PRESPEC_DOCUMENT_LIMIT")
    if not documents:
        return PreSpecificationAnalysisResult(
            status="REVIEW",
            result_json=None,
            document_results=[],
            openai_calls=0,
            openai_telemetry=OpenAITelemetry(),
            warnings=["PRESPEC_DOCUMENT_NONE"],
        )
    if not openai_api_key.strip():
        raise PreSpecificationServiceError("OPENAI_NOT_CONFIGURED")

    fetcher = document_fetcher or HttpPreSpecificationDocumentFetcher()
    document_results: list[dict[str, Any]] = []
    accepted_payloads: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_calls = 0
    total_telemetry = OpenAITelemetry()
    with openai_client_factory(
        api_key=openai_api_key,
        model=openai_model,
        provider=llm_provider,
        base_url=llm_gateway_base_url,
        timeout_seconds=180,
        max_retries=0,
        max_input_chars=120_000,
        max_output_tokens=20_000,
        max_total_api_calls=2,
    ) as openai_client:
        for document in sorted(documents, key=lambda item: item.slot):
            attachment_id = "PRESPEC-ATT-" + _sha256(
                {
                    "registry_no": registry_no,
                    "source_digest": source_digest,
                    "slot": document.slot,
                    "url_digest": document.source_digest,
                }
            )[:24]
            audit: dict[str, Any] = {
                "attachment_id": attachment_id,
                "slot": document.slot,
                "url_sha256": document.source_digest,
                "status": "REVIEW",
                "reason_code": "PRESPEC_DOCUMENT_PROCESSING_FAILED",
                "openai_calls": 0,
            }
            try:
                fetched = fetcher.fetch(
                    safe_url=document.safe_url,
                    registry_no=registry_no,
                    slot=document.slot,
                )
                audit["document_sha256"] = _sha256(fetched.content)
                extraction = extract_pps_document_content(
                    fetched.file_name,
                    fetched.content,
                )
                source_text = extraction.text.strip()
                audit.update(
                    {
                        "source_characters": len(source_text),
                        "source_read_complete": extraction.complete,
                        "members_discovered": extraction.members_discovered,
                        "members_processed": extraction.members_processed,
                    }
                )
                if len(source_text) < 20:
                    raise PreSpecificationServiceError(
                        "PRESPEC_DOCUMENT_TEXT_EMPTY_OR_SHORT"
                    )
                selection = select_document_analysis_input(source_text)
                audit.update(
                    {
                        "analysis_input_characters": selection.selected_characters,
                        "analysis_input_complete": selection.complete,
                    }
                )
                if not extraction.complete:
                    warnings.append("PRESPEC_SOURCE_READ_PARTIAL")
                if not selection.complete:
                    warnings.append("PRESPEC_ANALYSIS_INPUT_PARTIAL")
                outcome = openai_client.extract(
                    document_text=selection.text,
                    allowed_attachment_ids={attachment_id},
                )
                total_calls += outcome.api_calls
                total_telemetry = merge_openai_telemetry(
                    total_telemetry,
                    outcome.openai_telemetry,
                )
                audit["openai_calls"] = outcome.api_calls
                if outcome.status == "ACCEPTED" and outcome.data is not None:
                    public_payload = _redact_public_value(
                        outcome.data.model_dump(mode="json")
                    )
                    audit.update(
                        {
                            "status": "ACCEPTED",
                            "reason_code": "ACCEPTED",
                            "result": public_payload,
                        }
                    )
                    accepted_payloads.append(public_payload)
                else:
                    code = str(outcome.error_code or "OPENAI_REVIEW")[:80]
                    audit["reason_code"] = code
                    warnings.append(code)
            except (PreSpecificationServiceError, PpsEnrichmentError) as exc:
                code = str(exc)
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", code):
                    code = "PRESPEC_DOCUMENT_PROCESSING_FAILED"
                audit["reason_code"] = code
                warnings.append(code)
            except Exception:
                # An unexpected failure after a provider boundary makes exact
                # accounting unknowable. Never guess zero-cost completeness.
                total_telemetry = total_telemetry.model_copy(
                    update={"accounting_complete": False}
                )
                audit["reason_code"] = "PRESPEC_ANALYSIS_INTERNAL_ERROR"
                warnings.append("PRESPEC_ANALYSIS_INTERNAL_ERROR")
            document_results.append(audit)

    if total_calls > PRESPEC_MAX_OPENAI_CALLS:
        raise PreSpecificationServiceError("PRESPEC_OPENAI_CALL_LIMIT")
    accepted_count = sum(item["status"] == "ACCEPTED" for item in document_results)
    coverage_partial = any(
        warning in {"PRESPEC_SOURCE_READ_PARTIAL", "PRESPEC_ANALYSIS_INPUT_PARTIAL"}
        for warning in warnings
    )
    if accepted_count == len(document_results) and not coverage_partial:
        status_value = "COMPLETED"
    elif accepted_count:
        status_value = "PARTIAL"
    else:
        status_value = "REVIEW"
    public_result = None
    if accepted_payloads:
        public_result = {
            "source_kind": "PPS_PRESPEC",
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "documents_accepted": accepted_count,
            "documents_total": len(document_results),
            "extractions": accepted_payloads,
        }
    return PreSpecificationAnalysisResult(
        status=status_value,
        result_json=public_result,
        document_results=document_results,
        openai_calls=total_calls,
        openai_telemetry=total_telemetry,
        warnings=sorted(set(warnings)),
    )
