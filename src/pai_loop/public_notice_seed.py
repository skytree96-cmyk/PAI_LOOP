from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Notice, NoticeVersion


PUBLIC_NOTICE_SCHEMA_VERSION = "public-procurement-notice-seed-1.0.0"
PUBLIC_NOTICE_SEED_VERSION = "2026.08.17-v1"
PUBLIC_NOTICE_CLASSIFICATION = "PUBLIC_PROCUREMENT_DERIVED"
PUBLIC_NOTICE_RESOURCE = "data/public_notice_seed_v1.json"
PUBLIC_NOTICE_SOURCE_KEY = "MANUAL-INCHON-2025-17"
PUBLIC_NOTICE_SOURCE_URL = "https://www.g2b.go.kr/"

NOTICE_FIELDS = (
    "notice_key",
    "bid_notice_no",
    "revision_no",
    "title",
    "agency",
    "published_at",
    "deadline",
    "category",
    "source_url",
)
EXTRACTION_FIELDS = (
    "kind",
    "status",
    "prompt_version",
    "schema_version",
    "document_type",
    "summary",
    "requirements",
)
REQUIREMENT_FIELDS = (
    "requirement_id",
    "category",
    "logic",
    "normalized_condition",
    "mandatory",
    "deadline_basis",
    "evidence",
    "ambiguity_reason",
)
EVIDENCE_FIELDS = (
    "attachment_id",
    "page",
    "section",
    "quote",
    "confidence",
)
PROVENANCE_FIELDS = (
    "method",
    "source_label",
    "source_kind",
    "document_sha256",
    "source_version_no",
    "document_complete",
    "extraction_confidence",
    "requirement_count",
    "evidence_anchor_count",
    "payload_sha256",
)

_TOP_LEVEL_FIELDS = (
    "schema_version",
    "seed_version",
    "classification",
    "allowlist",
    "provenance",
    "notice",
    "extraction",
)
_ALLOWED_CATEGORIES = {
    "ENTITY",
    "INDUSTRY_CODE",
    "CERTIFICATION",
    "DIRECT_PRODUCTION",
    "REGION",
    "PERFORMANCE",
    "PERSONNEL",
    "FACILITY",
    "CONSORTIUM",
    "SANCTION",
    "SUBMISSION",
    "OTHER",
}
_ALLOWED_LOGIC = {"AND", "OR", "SINGLE"}
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(
    r"(?<!\d)(?:0\d{1,2}|\(0\d{1,2}\)|\+?82[ .-]?0?\d{1,2})"
    r"[ .-]?\d{3,4}[ .-]?\d{4}(?!\d)"
)
_BUSINESS_ID = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")
_RESIDENT_OR_CORPORATE_ID = re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)")
_LOCAL_PATH = re.compile(
    r"(?i)(?:(?<![A-Za-z])[A-Z]:[\\/]|[/\\]Users[/\\]|[/\\]home[/\\])"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})"
)
_LABELLED_PERSON = re.compile(
    r"(?:담당자|대표자|성명)\s*[:：]\s*[가-힣]{2,4}(?=\s|[,;/]|$)"
)
_SECRET_QUERY_KEYS = {"servicekey", "apikey", "api_key", "key", "token", "access_token"}
_FORBIDDEN_KEYS = {
    "actual_value",
    "actor_label",
    "address",
    "api_key",
    "birth_date",
    "business_registration_number",
    "company_fact",
    "company_facts",
    "contact",
    "credential",
    "decision",
    "decisions",
    "email",
    "phone",
    "person_name",
    "rationale",
    "representative",
    "resident_registration_number",
    "response_id",
    "service_key",
    "user_decision",
    "user_decisions",
}


class PublicNoticeSeedError(RuntimeError):
    """Raised when a procurement seed crosses the publication boundary."""


@dataclass(frozen=True, slots=True)
class PublicNoticeBuildResult:
    payload: dict[str, Any]
    requirements: int
    evidence_anchors: int
    privacy_findings: int

    def aggregate_only(self) -> dict[str, Any]:
        return {
            "schema_version": self.payload["schema_version"],
            "seed_version": self.payload["seed_version"],
            "classification": self.payload["classification"],
            "requirements": self.requirements,
            "evidence_anchors": self.evidence_anchors,
            "privacy_findings": self.privacy_findings,
            "payload_sha256": self.payload["provenance"]["payload_sha256"],
        }


@dataclass(frozen=True, slots=True)
class PublicNoticeImportResult:
    created_notices: int
    updated_notices: int
    created_versions: int
    updated_versions: int
    unchanged: bool
    requirement_count: int
    payload_sha256: str

    def aggregate_only(self) -> dict[str, Any]:
        return {
            "created_notices": self.created_notices,
            "updated_notices": self.updated_notices,
            "created_versions": self.created_versions,
            "updated_versions": self.updated_versions,
            "unchanged": self.unchanged,
            "requirement_count": self.requirement_count,
            "payload_sha256": self.payload_sha256,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def calculate_public_notice_payload_digest(payload: Mapping[str, Any]) -> str:
    """Digest only the publishable business payload, not mutable provenance fields."""

    selected = {"notice": payload.get("notice"), "extraction": payload.get("extraction")}
    return hashlib.sha256(_canonical_json(selected).encode("utf-8")).hexdigest()


def _normalise_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PublicNoticeSeedError(f"{field} must be text")
    text = " ".join(value.replace("\ufeff", " ").replace("\u200b", " ").split())
    if not text or len(text) > maximum:
        raise PublicNoticeSeedError(f"{field} length is invalid")
    return text


def _iso_datetime(value: Any, *, field: str) -> str:
    text = _normalise_text(value, field=field, maximum=64)
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise PublicNoticeSeedError(f"{field} is not an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise PublicNoticeSeedError(f"{field} must include a timezone")
    return parsed.isoformat()


def _parse_source_datetime(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicNoticeSeedError("source notice datetime is invalid") from exc
    if parsed.tzinfo is None:
        # The source notice was manually recorded from a Korean public notice.
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9), name="Asia/Seoul"))
    return parsed.isoformat()


def _safe_public_url(value: Any) -> str:
    raw = str(value or "").strip() or PUBLIC_NOTICE_SOURCE_URL
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        raise PublicNoticeSeedError("source URL must be HTTPS")
    host = parts.hostname.casefold()
    if host != "g2b.go.kr" and not host.endswith(".g2b.go.kr"):
        raise PublicNoticeSeedError("source URL must use the official g2b.go.kr host")
    if parts.username or parts.password:
        raise PublicNoticeSeedError("source URL must not contain user information")
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        if key.casefold() in _SECRET_QUERY_KEYS:
            continue
        query.append((key, item))
    return urlunsplit(("https", parts.netloc, parts.path or "/", urlencode(query), ""))


def _privacy_findings(value: Any, *, path: str = "seed") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                findings.append(f"{path}.{key}:forbidden_key")
            findings.extend(_privacy_findings(item, path=f"{path}.{key}"))
        return findings
    if isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_privacy_findings(item, path=f"{path}[{index}]"))
        return findings
    if not isinstance(value, str):
        return findings
    if _DIGEST.fullmatch(value):
        return findings
    patterns = (
        ("email", _EMAIL),
        ("phone", _PHONE),
        ("business_id", _BUSINESS_ID),
        ("resident_or_corporate_id", _RESIDENT_OR_CORPORATE_ID),
        ("local_path", _LOCAL_PATH),
        ("secret", _SECRET_VALUE),
        ("labelled_person", _LABELLED_PERSON),
    )
    findings.extend(f"{path}:{label}" for label, pattern in patterns if pattern.search(value))
    return findings


def _validate_allowlist(payload: Mapping[str, Any]) -> None:
    expected = {
        "notice": list(NOTICE_FIELDS),
        "extraction": list(EXTRACTION_FIELDS),
        "requirement": list(REQUIREMENT_FIELDS),
        "evidence": list(EVIDENCE_FIELDS),
    }
    if payload.get("allowlist") != expected:
        raise PublicNoticeSeedError("public notice seed allowlist is invalid")


def validate_public_notice_seed(payload: Mapping[str, Any]) -> None:
    if tuple(payload) != _TOP_LEVEL_FIELDS:
        raise PublicNoticeSeedError("public notice seed has unexpected top-level fields")
    if payload.get("schema_version") != PUBLIC_NOTICE_SCHEMA_VERSION:
        raise PublicNoticeSeedError("unsupported public notice seed schema")
    if payload.get("seed_version") != PUBLIC_NOTICE_SEED_VERSION:
        raise PublicNoticeSeedError("unsupported public notice seed version")
    if payload.get("classification") != PUBLIC_NOTICE_CLASSIFICATION:
        raise PublicNoticeSeedError("public notice seed classification is invalid")
    _validate_allowlist(payload)

    provenance = payload.get("provenance")
    notice = payload.get("notice")
    extraction = payload.get("extraction")
    if not isinstance(provenance, dict) or tuple(provenance) != PROVENANCE_FIELDS:
        raise PublicNoticeSeedError("public notice provenance allowlist is invalid")
    if not isinstance(notice, dict) or tuple(notice) != NOTICE_FIELDS:
        raise PublicNoticeSeedError("public notice identity allowlist is invalid")
    if not isinstance(extraction, dict) or tuple(extraction) != EXTRACTION_FIELDS:
        raise PublicNoticeSeedError("public notice extraction allowlist is invalid")
    if provenance.get("method") != "read-only-allowlist-public-procurement-extraction":
        raise PublicNoticeSeedError("public notice provenance method is invalid")
    for key in ("document_sha256", "payload_sha256"):
        if not isinstance(provenance.get(key), str) or not _DIGEST.fullmatch(provenance[key]):
            raise PublicNoticeSeedError(f"public notice {key} is invalid")
    if calculate_public_notice_payload_digest(payload) != provenance["payload_sha256"]:
        raise PublicNoticeSeedError("public notice payload digest does not match")

    for field, maximum in (
        ("notice_key", 160),
        ("bid_notice_no", 80),
        ("revision_no", 20),
        ("title", 500),
        ("agency", 255),
        ("category", 120),
    ):
        _normalise_text(notice.get(field), field=f"notice.{field}", maximum=maximum)
    _iso_datetime(notice.get("published_at"), field="notice.published_at")
    _iso_datetime(notice.get("deadline"), field="notice.deadline")
    if _safe_public_url(notice.get("source_url")) != notice.get("source_url"):
        raise PublicNoticeSeedError("source URL is not canonical or contains a credential query")

    if extraction.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION":
        raise PublicNoticeSeedError("public notice extraction kind is invalid")
    if extraction.get("status") != "ACCEPTED":
        raise PublicNoticeSeedError("only ACCEPTED extraction can be published")
    _normalise_text(extraction.get("summary"), field="extraction.summary", maximum=1_000)
    requirements = extraction.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise PublicNoticeSeedError("public notice requirements must be a non-empty list")
    if provenance.get("requirement_count") != len(requirements):
        raise PublicNoticeSeedError("public notice requirement count does not match")

    requirement_ids: set[str] = set()
    anchor_count = 0
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict) or tuple(requirement) != REQUIREMENT_FIELDS:
            raise PublicNoticeSeedError(f"requirement {index} allowlist is invalid")
        requirement_id = _normalise_text(
            requirement.get("requirement_id"),
            field=f"requirement[{index}].requirement_id",
            maximum=120,
        )
        if requirement_id in requirement_ids:
            raise PublicNoticeSeedError("public notice requirement IDs are not unique")
        requirement_ids.add(requirement_id)
        if requirement.get("category") not in _ALLOWED_CATEGORIES:
            raise PublicNoticeSeedError(f"requirement {index} category is invalid")
        if requirement.get("logic") not in _ALLOWED_LOGIC:
            raise PublicNoticeSeedError(f"requirement {index} logic is invalid")
        if not isinstance(requirement.get("mandatory"), bool):
            raise PublicNoticeSeedError(f"requirement {index} mandatory flag is invalid")
        _normalise_text(
            requirement.get("normalized_condition"),
            field=f"requirement[{index}].normalized_condition",
            maximum=2_000,
        )
        for optional in ("deadline_basis", "ambiguity_reason"):
            value = requirement.get(optional)
            if value is not None:
                _normalise_text(value, field=f"requirement[{index}].{optional}", maximum=1_000)
        evidence = requirement.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise PublicNoticeSeedError(f"requirement {index} has no public evidence anchor")
        for evidence_index, anchor in enumerate(evidence):
            if not isinstance(anchor, dict) or tuple(anchor) != EVIDENCE_FIELDS:
                raise PublicNoticeSeedError(
                    f"requirement {index} evidence {evidence_index} allowlist is invalid"
                )
            _normalise_text(
                anchor.get("attachment_id"),
                field=f"requirement[{index}].evidence[{evidence_index}].attachment_id",
                maximum=160,
            )
            page = anchor.get("page")
            if page is not None and (not isinstance(page, int) or page < 1):
                raise PublicNoticeSeedError("public evidence page is invalid")
            section = anchor.get("section")
            if section is not None:
                _normalise_text(section, field="public evidence section", maximum=500)
            _normalise_text(anchor.get("quote"), field="public evidence quote", maximum=500)
            confidence = anchor.get("confidence")
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise PublicNoticeSeedError("public evidence confidence is invalid")
            anchor_count += 1
    if provenance.get("evidence_anchor_count") != anchor_count:
        raise PublicNoticeSeedError("public notice evidence anchor count does not match")

    findings = _privacy_findings(payload)
    if findings:
        raise PublicNoticeSeedError(
            f"publication privacy validation failed with {len(findings)} finding(s)"
        )
    if "synthetic" in _canonical_json(payload).casefold():
        raise PublicNoticeSeedError("actual public procurement seed cannot be marked synthetic")


def _read_public_source(source_db: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = source_db.expanduser().resolve()
    if not resolved.is_file():
        raise PublicNoticeSeedError("source database does not exist")
    try:
        connection = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise PublicNoticeSeedError("source database could not be opened read-only") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        notice = connection.execute(
            """
            SELECT id, notice_key, bid_notice_no, revision_no, title, agency,
                   published_at, deadline, category, source_url
              FROM notices
             WHERE notice_key = ?
            """,
            (PUBLIC_NOTICE_SOURCE_KEY,),
        ).fetchone()
        if notice is None:
            raise PublicNoticeSeedError("bounded source notice was not found")
        versions = connection.execute(
            """
            SELECT version_no, file_sha256, document_complete, extraction_status,
                   extraction_confidence, source_payload
              FROM notice_versions
             WHERE notice_id = ? AND extraction_status = 'ACCEPTED'
             ORDER BY version_no DESC
            """,
            (notice["id"],),
        ).fetchall()
        for version in versions:
            try:
                source_payload = json.loads(version["source_payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(source_payload, dict)
                and source_payload.get("status") == "ACCEPTED"
                and isinstance(source_payload.get("result"), dict)
                and isinstance(source_payload["result"].get("requirements"), list)
            ):
                return dict(notice), {**dict(version), "source_payload": source_payload}
        raise PublicNoticeSeedError("latest ACCEPTED OpenAI extraction was not found")
    except sqlite3.Error as exc:
        raise PublicNoticeSeedError("bounded source snapshot could not be read") from exc
    finally:
        connection.close()


def build_public_notice_seed_from_db(source_db: str | Path) -> PublicNoticeBuildResult:
    """Build one real, publication-safe notice snapshot from the ignored source DB.

    The source connection is SQLite ``mode=ro`` plus ``query_only``. The query
    selects no company facts, user decisions, contact records, API credentials,
    local source paths, or private-import tables. Only the latest ACCEPTED
    public-procurement extraction is projected through fixed field allowlists.
    """

    source_notice, source_version = _read_public_source(Path(source_db))
    source_payload = source_version["source_payload"]
    result = source_payload["result"]
    requirements: list[dict[str, Any]] = []
    for item in result["requirements"]:
        if not isinstance(item, dict):
            raise PublicNoticeSeedError("source extraction requirement is not an object")
        evidence = [
            {field: anchor.get(field) for field in EVIDENCE_FIELDS}
            for anchor in item.get("evidence", [])
            if isinstance(anchor, dict)
        ]
        requirements.append(
            {
                "requirement_id": item.get("requirement_id"),
                "category": item.get("category"),
                "logic": item.get("logic"),
                "normalized_condition": item.get("normalized_condition"),
                "mandatory": item.get("mandatory"),
                "deadline_basis": item.get("deadline_basis"),
                "evidence": evidence,
                "ambiguity_reason": item.get("ambiguity_reason"),
            }
        )

    category = str(source_notice.get("category") or "").strip()
    if not category or "\ufffd" in category:
        category = "용역"
    notice = {
        "notice_key": source_notice["notice_key"],
        "bid_notice_no": source_notice["bid_notice_no"],
        "revision_no": source_notice["revision_no"],
        "title": source_notice["title"],
        "agency": source_notice["agency"],
        "published_at": _parse_source_datetime(source_notice["published_at"]),
        "deadline": _parse_source_datetime(source_notice["deadline"]),
        "category": category,
        "source_url": _safe_public_url(source_notice.get("source_url")),
    }
    extraction = {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "status": "ACCEPTED",
        "prompt_version": source_payload.get("prompt_version"),
        "schema_version": source_payload.get("schema_version"),
        "document_type": result.get("document_type"),
        "summary": result.get("summary"),
        "requirements": requirements,
    }
    source_label = _normalise_text(
        source_payload.get("source_label"), field="source label", maximum=255
    )
    source_kind = _normalise_text(
        source_payload.get("source_kind"), field="source kind", maximum=80
    )
    evidence_anchor_count = sum(len(item["evidence"]) for item in requirements)
    payload: dict[str, Any] = {
        "schema_version": PUBLIC_NOTICE_SCHEMA_VERSION,
        "seed_version": PUBLIC_NOTICE_SEED_VERSION,
        "classification": PUBLIC_NOTICE_CLASSIFICATION,
        "allowlist": {
            "notice": list(NOTICE_FIELDS),
            "extraction": list(EXTRACTION_FIELDS),
            "requirement": list(REQUIREMENT_FIELDS),
            "evidence": list(EVIDENCE_FIELDS),
        },
        "provenance": {
            "method": "read-only-allowlist-public-procurement-extraction",
            "source_label": source_label,
            "source_kind": source_kind,
            "document_sha256": source_payload.get("document_sha256")
            or source_version["file_sha256"],
            "source_version_no": source_version["version_no"],
            "document_complete": bool(source_version["document_complete"]),
            "extraction_confidence": float(source_version["extraction_confidence"]),
            "requirement_count": len(requirements),
            "evidence_anchor_count": evidence_anchor_count,
            "payload_sha256": "",
        },
        "notice": notice,
        "extraction": extraction,
    }
    payload["provenance"]["payload_sha256"] = calculate_public_notice_payload_digest(payload)
    validate_public_notice_seed(payload)
    return PublicNoticeBuildResult(
        payload=payload,
        requirements=len(requirements),
        evidence_anchors=evidence_anchor_count,
        privacy_findings=0,
    )


def write_public_notice_seed(result: PublicNoticeBuildResult, output: str | Path) -> Path:
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(result.payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


@lru_cache(maxsize=1)
def _load_public_notice_seed_template() -> dict[str, Any]:
    resource = resources.files("pai_loop").joinpath(PUBLIC_NOTICE_RESOURCE)
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicNoticeSeedError("packaged public notice seed could not be loaded") from exc
    if not isinstance(payload, dict):
        raise PublicNoticeSeedError("packaged public notice seed is not an object")
    validate_public_notice_seed(payload)
    return payload


def load_public_notice_seed() -> dict[str, Any]:
    """Return an isolated copy so callers cannot mutate the validated cache."""

    return copy.deepcopy(_load_public_notice_seed_template())


def _to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _set_changed(target: Any, field: str, value: Any) -> bool:
    current = getattr(target, field)
    if isinstance(current, datetime) and isinstance(value, datetime):
        if current.tzinfo is None and value.tzinfo is not None:
            current = current.replace(tzinfo=value.tzinfo)
        elif current.tzinfo is not None and value.tzinfo is None:
            value = value.replace(tzinfo=current.tzinfo)
    if current == value:
        return False
    setattr(target, field, value)
    return True


def import_public_notice_seed(
    session: Session,
    payload: Mapping[str, Any] | None = None,
) -> PublicNoticeImportResult:
    """Explicit, idempotent upsert into an already-initialised application DB.

    This function is intentionally not called from application startup. It
    writes only the bounded Notice/NoticeVersion fields represented in the
    publication seed and never creates company facts, decisions, or evaluations.
    """

    seed = copy.deepcopy(dict(payload)) if payload is not None else load_public_notice_seed()
    validate_public_notice_seed(seed)
    notice_payload = seed["notice"]
    extraction = seed["extraction"]
    provenance = seed["provenance"]
    notice = session.scalar(
        select(Notice).where(Notice.notice_key == notice_payload["notice_key"])
    )
    created_notices = 0
    updated_notices = 0
    if notice is None:
        notice = Notice(
            notice_key=notice_payload["notice_key"],
            bid_notice_no=notice_payload["bid_notice_no"],
            revision_no=notice_payload["revision_no"],
            title=notice_payload["title"],
            agency=notice_payload["agency"],
            published_at=_to_datetime(notice_payload["published_at"]),
            deadline=_to_datetime(notice_payload["deadline"]),
            status="CLOSED"
            if _to_datetime(notice_payload["deadline"]) <= datetime.now(timezone.utc)
            else "OPEN",
            category=notice_payload["category"],
            source_url=notice_payload["source_url"],
            estimated_amount=None,
            risk_dimensions=None,
        )
        session.add(notice)
        session.flush()
        created_notices = 1
    else:
        changed = False
        values = {
            "bid_notice_no": notice_payload["bid_notice_no"],
            "revision_no": notice_payload["revision_no"],
            "title": notice_payload["title"],
            "agency": notice_payload["agency"],
            "published_at": _to_datetime(notice_payload["published_at"]),
            "deadline": _to_datetime(notice_payload["deadline"]),
            "category": notice_payload["category"],
            "source_url": notice_payload["source_url"],
        }
        for field, value in values.items():
            changed = _set_changed(notice, field, value) or changed
        expected_status = (
            "CLOSED"
            if _to_datetime(notice_payload["deadline"]) <= datetime.now(timezone.utc)
            else "OPEN"
        )
        changed = _set_changed(notice, "status", expected_status) or changed
        updated_notices = int(changed)

    source_payload = {
        "kind": extraction["kind"],
        "status": extraction["status"],
        "classification": seed["classification"],
        "seed_version": seed["seed_version"],
        "seed_digest": provenance["payload_sha256"],
        "prompt_version": extraction["prompt_version"],
        "schema_version": extraction["schema_version"],
        "source_kind": provenance["source_kind"],
        "source_label": provenance["source_label"],
        "document_sha256": provenance["document_sha256"],
        "result": {
            "document_type": extraction["document_type"],
            "summary": extraction["summary"],
            "requirements": extraction["requirements"],
        },
    }
    source_version_no = int(provenance["source_version_no"])
    version = session.scalar(
        select(NoticeVersion).where(
            NoticeVersion.notice_id == notice.id,
            NoticeVersion.version_no == source_version_no,
        )
    )
    created_versions = 0
    updated_versions = 0
    if version is None:
        version = NoticeVersion(
            notice_id=notice.id,
            version_no=source_version_no,
            file_sha256=provenance["document_sha256"],
            document_complete=bool(provenance["document_complete"]),
            extraction_status="ACCEPTED",
            extraction_confidence=float(provenance["extraction_confidence"]),
            source_payload=source_payload,
        )
        session.add(version)
        created_versions = 1
    else:
        changed = False
        values = {
            "file_sha256": provenance["document_sha256"],
            "document_complete": bool(provenance["document_complete"]),
            "extraction_status": "ACCEPTED",
            "extraction_confidence": float(provenance["extraction_confidence"]),
            "source_payload": source_payload,
        }
        for field, value in values.items():
            changed = _set_changed(version, field, value) or changed
        updated_versions = int(changed)
    session.commit()
    return PublicNoticeImportResult(
        created_notices=created_notices,
        updated_notices=updated_notices,
        created_versions=created_versions,
        updated_versions=updated_versions,
        unchanged=not any(
            (created_notices, updated_notices, created_versions, updated_versions)
        ),
        requirement_count=len(extraction["requirements"]),
        payload_sha256=provenance["payload_sha256"],
    )
