from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from importlib import resources
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from .department_ranking import load_department_keyword_profiles
from .eligibility_policy import load_public_company_profile
from .models import CompanyFact, Evidence, ReferenceDataVersion
from .pricing_profiles import pricing_profile_for_document
from .public_performance import load_public_performance_seed
from .quantitative_scoring import load_quantitative_profile_catalog


@dataclass(frozen=True, slots=True)
class ReferenceSpec:
    dataset_key: str
    resource: str
    version_fields: tuple[str, ...]
    default_schema_version: str
    classification: str = "PUBLIC_REVIEWED"


REFERENCE_SPECS = (
    ReferenceSpec(
        "company_public_profile",
        "data/company_public_profile.json",
        ("profile_version",),
        "company-public-profile-1.0",
        "PUBLIC_SAFE_COMPANY_PROFILE",
    ),
    ReferenceSpec(
        "department_keyword_profiles",
        "data/department_keyword_profiles.json",
        ("version",),
        "department-keyword-profile-1.0",
        "PUBLIC_ORGANIZATION_PROFILE",
    ),
    ReferenceSpec(
        "public_performance",
        "data/public_performance_seed_v1.json",
        ("dataset_version",),
        "public-performance-seed-1.0.0",
        "PUBLIC_DERIVED",
    ),
    ReferenceSpec(
        "quantitative_notice_profiles",
        "data/quantitative_notice_profiles.json",
        ("profile_version",),
        "pai-loop-quantitative-notice-profiles-1.0.0",
        "PUBLIC_DOCUMENT_DERIVED",
    ),
    ReferenceSpec(
        "pricing_method_profiles",
        "data/pricing_method_profiles_v1.json",
        ("profile_version", "schema_version"),
        "grounded-pricing-method-1.0.0",
        "PUBLIC_DOCUMENT_DERIVED",
    ),
    ReferenceSpec(
        "eligibility_rules",
        "data/rules_v2_1.json",
        ("ruleset",),
        "eligibility-rules-registry-1.0",
        "PUBLIC_REVIEWED_RULES",
    ),
)

MAX_REFERENCE_PAYLOAD_BYTES = 5 * 1024 * 1024
_FORBIDDEN_VALUE_KEYS = {
    "api_key",
    "authorization",
    "contact",
    "credential",
    "document_text",
    "email",
    "local_path",
    "password",
    "person_name",
    "phone",
    "raw_payload",
    "secret",
    "service_key",
    "token",
}
_OPAQUE_IDENTIFIER_KEYS = {
    "content_sha256",
    "document_sha256",
    "evidence_key",
    "payload_sha256",
    "record_key",
    "sha256",
}
_SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|bearer\s+[A-Za-z0-9._~+/-]{16,})"
)
_LOCAL_PATH = re.compile(r"(?i)(?:(?<![A-Za-z])[A-Z]:[\\/]|[/\\]Users[/\\]|[/\\]home[/\\])")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:0\d{1,2}|\+?82[ .-]?0?\d{1,2})[ .-]?\d{3,4}[ .-]?\d{4}(?!\d)")
_BUSINESS_ID = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")
_SECRET_QUERY_KEYS = {"apikey", "api_key", "key", "servicekey", "token", "access_token"}


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _assert_reference_safe(value: Any, *, path: str = "reference") -> None:
    """Reject secrets, local paths, and direct contact identifiers before DB sync."""

    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).casefold()
            # A zero redaction counter such as ``redactions.email: 0`` is safe;
            # a populated sensitive field is not.
            if key_text in _FORBIDDEN_VALUE_KEYS and item not in (None, "", 0, False, [], {}):
                raise ValueError(f"reference dataset contains forbidden field: {path}.{key}")
            if key_text in _OPAQUE_IDENTIFIER_KEYS and isinstance(item, str) and re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,200}", item
            ):
                continue
            _assert_reference_safe(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_reference_safe(item, path=f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    if _SECRET.search(value) or _LOCAL_PATH.search(value) or _EMAIL.search(value) or _PHONE.search(value) or _BUSINESS_ID.search(value):
        raise ValueError(f"reference dataset contains a sensitive value: {path}")
    parts = urlsplit(value)
    if parts.scheme in {"http", "https"} and any(
        key.casefold() in _SECRET_QUERY_KEYS for key, _item in parse_qsl(parts.query, keep_blank_values=True)
    ):
        raise ValueError(f"reference dataset URL contains a credential query: {path}")


def _validate_reference_payload(spec: ReferenceSpec, payload: dict[str, Any]) -> None:
    encoded = _canonical_bytes(payload)
    if len(encoded) > MAX_REFERENCE_PAYLOAD_BYTES:
        raise ValueError(
            f"reference dataset exceeds the {MAX_REFERENCE_PAYLOAD_BYTES}-byte online JSON limit: "
            f"{spec.dataset_key}"
        )
    _assert_reference_safe(payload, path=spec.dataset_key)

    # Reuse the established publication/shape validators rather than treating
    # a file as trusted merely because it was packaged into the image.
    if spec.dataset_key == "company_public_profile":
        validated = load_public_company_profile()
        if _canonical_bytes(validated) != encoded:
            raise ValueError("company profile validator returned different content")
    elif spec.dataset_key == "department_keyword_profiles":
        validated = load_department_keyword_profiles()
        if _canonical_bytes(validated) != encoded:
            raise ValueError("department profile validator returned different content")
    elif spec.dataset_key == "public_performance":
        validated = load_public_performance_seed()
        if _canonical_bytes(validated) != encoded:
            raise ValueError("public performance validator returned different content")
    elif spec.dataset_key == "quantitative_notice_profiles":
        validated = load_quantitative_profile_catalog()
        if _canonical_bytes(validated) != encoded:
            raise ValueError("quantitative profile validator returned different content")
    elif spec.dataset_key == "pricing_method_profiles":
        if payload.get("classification") != "PUBLIC_DOCUMENT_DERIVED":
            raise ValueError("pricing reference classification is invalid")
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            raise ValueError("pricing reference profiles must be a list")
        for profile in profiles:
            digest = str(profile.get("document_sha256") or "") if isinstance(profile, dict) else ""
            if pricing_profile_for_document(digest) is None:
                raise ValueError("pricing reference profile failed its document-digest validator")
    elif spec.dataset_key == "eligibility_rules":
        required = {"ruleset", "evaluation_order", "pass_rules", "review_rules", "aggregation_rules", "default_fail"}
        if required - payload.keys():
            raise ValueError("eligibility rule registry is incomplete")
        pass_ids = [str(item.get("id") or "") for item in payload.get("pass_rules", []) if isinstance(item, dict)]
        review_ids = [str(item.get("id") or "") for item in payload.get("review_rules", []) if isinstance(item, dict)]
        if not pass_ids or not review_ids or len(pass_ids) != len(set(pass_ids)) or len(review_ids) != len(set(review_ids)):
            raise ValueError("eligibility rule identifiers are empty or duplicated")


def _load_spec(spec: ReferenceSpec) -> tuple[dict[str, Any], str, str, str]:
    raw = resources.files("pai_loop").joinpath(spec.resource).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"reference dataset must be an object: {spec.dataset_key}")
    _validate_reference_payload(spec, payload)
    digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    version = next(
        (str(payload[field]).strip() for field in spec.version_fields if payload.get(field)),
        f"sha256-{digest[:16]}",
    )
    schema_version = str(payload.get("schema_version") or spec.default_schema_version)
    return payload, digest, version, schema_version


def packaged_reference_manifest() -> list[dict[str, Any]]:
    """Return publication-safe metadata for every packaged decision basis."""

    result: list[dict[str, Any]] = []
    for spec in REFERENCE_SPECS:
        _payload, digest, version, schema_version = _load_spec(spec)
        result.append(
            {
                "dataset_key": spec.dataset_key,
                "version": version,
                "schema_version": schema_version,
                "content_sha256": digest,
                "classification": spec.classification,
                "source": "GIT_PACKAGE",
            }
        )
    return result


def sync_packaged_reference_data(
    session: Session,
    *,
    effective_at: datetime | None = None,
) -> dict[str, Any]:
    """Idempotently mirror reviewed Git registries into the online database.

    A version label is immutable: reusing it with different content fails
    closed.  Activating a new version retires the previous ACTIVE row while
    preserving its payload and effective period for historical replay.
    """

    now = effective_at or datetime.now(timezone.utc)
    created = activated = unchanged = retired = 0
    datasets: list[dict[str, Any]] = []

    for spec in REFERENCE_SPECS:
        payload, digest, version, schema_version = _load_spec(spec)
        rows = list(
            session.scalars(
                select(ReferenceDataVersion).where(
                    ReferenceDataVersion.dataset_key == spec.dataset_key
                ).options(
                    load_only(
                        ReferenceDataVersion.id,
                        ReferenceDataVersion.dataset_key,
                        ReferenceDataVersion.version,
                        ReferenceDataVersion.schema_version,
                        ReferenceDataVersion.content_sha256,
                        ReferenceDataVersion.classification,
                        ReferenceDataVersion.source,
                        ReferenceDataVersion.status,
                        ReferenceDataVersion.effective_from,
                        ReferenceDataVersion.effective_to,
                    )
                )
            ).all()
        )
        existing = next((row for row in rows if row.version == version), None)
        if existing is not None and (
            existing.content_sha256 != digest
            or existing.schema_version != schema_version
            or existing.classification != spec.classification
            or existing.source != "GIT_PACKAGE"
        ):
            raise ValueError(
                f"reference version is immutable: {spec.dataset_key}/{version}"
            )

        for row in rows:
            if row.status == "ACTIVE" and row is not existing:
                row.status = "RETIRED"
                row.effective_to = now
                retired += 1

        if existing is None:
            existing = ReferenceDataVersion(
                dataset_key=spec.dataset_key,
                version=version,
                schema_version=schema_version,
                content_sha256=digest,
                classification=spec.classification,
                source="GIT_PACKAGE",
                status="ACTIVE",
                payload_json=copy.deepcopy(payload),
                effective_from=now,
            )
            session.add(existing)
            created += 1
        elif existing.status != "ACTIVE":
            existing.status = "ACTIVE"
            existing.effective_from = now
            existing.effective_to = None
            activated += 1
        else:
            unchanged += 1

        datasets.append(
            {
                "dataset_key": spec.dataset_key,
                "version": version,
                "content_sha256": digest,
                "status": "ACTIVE",
            }
        )
    session.flush()
    return {
        "created": created,
        "activated": activated,
        "unchanged": unchanged,
        "retired": retired,
        "datasets": datasets,
    }


def _profile_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = date.fromisoformat(str(value)[:10])
    return datetime.combine(parsed, time.min, tzinfo=timezone.utc)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _utc_naive(left) == _utc_naive(right)
    return left == right


def _set_changed(target: Any, field: str, value: Any) -> bool:
    if _same_value(getattr(target, field), value):
        return False
    setattr(target, field, value)
    return True


def sync_public_company_profile(
    session: Session,
    *,
    effective_at: datetime | None = None,
) -> dict[str, int]:
    """Materialise the reviewed public company profile for the evaluator.

    The package profile remains the publication boundary. Only its safe fact
    values, evidence hashes, and validity dates are copied; certificate bodies,
    identifiers, contacts, and local paths never enter this database path.
    """

    spec = next(item for item in REFERENCE_SPECS if item.dataset_key == "company_public_profile")
    profile, _digest, version, _schema = _load_spec(spec)
    evidence_by_key: dict[str, Evidence] = {}
    now = effective_at or datetime.now(timezone.utc)
    evidence_created = evidence_updated = evidence_retired = 0
    fact_created = fact_updated = fact_retired = 0
    managed_evidence = list(
        session.scalars(
            select(Evidence).where(Evidence.evidence_type == "PUBLIC_PROFILE_REFERENCE")
        ).all()
    )
    evidence_index = {item.evidence_key: item for item in managed_evidence}
    seen_evidence_keys: set[str] = set()

    for item in profile.get("evidence", []):
        if not isinstance(item, dict) or not item.get("evidence_key"):
            continue
        key = str(item["evidence_key"])
        seen_evidence_keys.add(key)
        evidence = evidence_index.get(key)
        if evidence is None:
            collision = session.scalar(select(Evidence).where(Evidence.evidence_key == key))
            if collision is not None:
                raise ValueError(f"public profile evidence key collides with another source: {key}")
        values = {
            "name": str(item.get("display_name") or key),
            "evidence_type": "PUBLIC_PROFILE_REFERENCE",
            "status": "VERIFIED",
            "valid_from": _profile_datetime(item.get("valid_from")),
            "valid_until": _profile_datetime(item.get("valid_until")),
            "source_location": f"package://pai_loop/{spec.resource}#{key}",
            "sha256": item.get("sha256"),
            "metadata_json": {
                "profile_version": version,
                "last_observed_at": item.get("last_observed_at"),
                "validity_policy": item.get("validity_policy"),
            },
        }
        if evidence is None:
            evidence = Evidence(evidence_key=key, **values)
            session.add(evidence)
            evidence_created += 1
        else:
            changed = False
            for field, field_value in values.items():
                changed = _set_changed(evidence, field, field_value) or changed
            evidence_updated += int(changed)
        evidence_by_key[key] = evidence
    session.flush()

    for evidence in managed_evidence:
        if evidence.evidence_key in seen_evidence_keys:
            continue
        changed = _set_changed(evidence, "status", "RETIRED")
        if evidence.valid_until is None or _utc_naive(evidence.valid_until) > _utc_naive(now):
            changed = _set_changed(evidence, "valid_until", now) or changed
        evidence_retired += int(changed)

    managed_facts = list(
        session.scalars(
            select(CompanyFact).where(CompanyFact.source == "PUBLIC_PROFILE")
        ).all()
    )
    facts_by_key: dict[str, list[CompanyFact]] = {}
    for fact in managed_facts:
        facts_by_key.setdefault(fact.fact_key, []).append(fact)
    seen_fact_keys: set[str] = set()

    for fact_key, item in profile.get("facts", {}).items():
        if not isinstance(item, dict):
            continue
        effective_from = _profile_datetime(item.get("effective_from"))
        if effective_from is None:
            raise ValueError(f"public company fact has no effective_from: {fact_key}")
        key = str(fact_key)
        seen_fact_keys.add(key)
        candidates = facts_by_key.get(key, [])
        fact = next(
            (
                candidate
                for candidate in candidates
                if _same_value(candidate.effective_from, effective_from)
            ),
            None,
        )
        evidence = evidence_by_key.get(str(item.get("evidence_key") or ""))
        values = {
            "value": item.get("value"),
            "value_label": str(item.get("evidence_state") or "PUBLIC_PROFILE"),
            "effective_to": _profile_datetime(item.get("effective_to")),
            "evidence_id": evidence.id if evidence else None,
            "verified": str(item.get("evidence_state") or "").startswith("VERIFIED"),
            "source": "PUBLIC_PROFILE",
        }
        if fact is None:
            fact = CompanyFact(
                fact_key=key,
                effective_from=effective_from,
                **values,
            )
            session.add(fact)
            fact_created += 1
        else:
            changed = False
            for field, field_value in values.items():
                changed = _set_changed(fact, field, field_value) or changed
            fact_updated += int(changed)

        # A later profile period supersedes any other open period for this fact.
        for candidate in candidates:
            if candidate is fact or candidate.effective_to is not None:
                continue
            _set_changed(candidate, "effective_to", now)
            fact_retired += 1

    for fact in managed_facts:
        if fact.fact_key in seen_fact_keys or fact.effective_to is not None:
            continue
        _set_changed(fact, "effective_to", now)
        fact_retired += 1

    session.flush()
    return {
        "evidence_created": evidence_created,
        "evidence_updated": evidence_updated,
        "evidence_retired": evidence_retired,
        "facts_created": fact_created,
        "facts_updated": fact_updated,
        "facts_retired": fact_retired,
    }


def active_reference_metadata(session: Session) -> list[dict[str, Any]]:
    rows = list(
        session.scalars(
            select(ReferenceDataVersion)
            .where(ReferenceDataVersion.status == "ACTIVE")
            .options(
                load_only(
                    ReferenceDataVersion.dataset_key,
                    ReferenceDataVersion.version,
                    ReferenceDataVersion.schema_version,
                    ReferenceDataVersion.content_sha256,
                    ReferenceDataVersion.classification,
                    ReferenceDataVersion.source,
                    ReferenceDataVersion.status,
                    ReferenceDataVersion.effective_from,
                    ReferenceDataVersion.effective_to,
                )
            )
            .order_by(ReferenceDataVersion.dataset_key)
        ).all()
    )
    return [
        {
            "dataset_key": row.dataset_key,
            "version": row.version,
            "schema_version": row.schema_version,
            "content_sha256": row.content_sha256,
            "classification": row.classification,
            "source": row.source,
            "status": row.status,
            "effective_from": row.effective_from,
            "effective_to": row.effective_to,
        }
        for row in rows
    ]
