from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from fastapi import APIRouter, Depends, Query

from .auth import require_api_key

PUBLIC_PERFORMANCE_SCHEMA_VERSION = "public-performance-seed-1.0.0"
PUBLIC_PERFORMANCE_POLICY_VERSION = "public-allowlist-redaction-1.2.0"
PUBLIC_PERFORMANCE_RESOURCE = "data/public_performance_seed_v1.json"

PUBLIC_RECORD_FIELDS = (
    "record_key",
    "project_name",
    "overview",
    "agency",
    "contract_date",
    "contract_year",
    "contract_amount_krw",
    "keywords",
    "division",
)

_TEXT_LIMITS = {
    "project_name": 500,
    "overview": 1_200,
    "agency": 300,
    "division": 300,
    "keyword": 160,
}

_DIRECT_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email",
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    ),
    ("url", re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>]+")),
    (
        "domestic_phone",
        re.compile(r"(?<!\d)(?:0\d{1,2}|\(0\d{1,2}\))[\s.-]?\d{3,4}[\s.-]?\d{4}(?!\d)"),
    ),
    (
        "international_phone",
        re.compile(r"(?<!\d)\+?82[\s.-]?0?\d{1,2}[\s.-]?\d{3,4}[\s.-]?\d{4}(?!\d)"),
    ),
    ("compact_mobile", re.compile(r"(?<!\d)01[016789]\d{7,8}(?!\d)")),
    ("resident_or_corporate_id", re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)")),
    ("business_id", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)")),
    ("postal_code", re.compile(r"(?<!\d)\d{5}(?!\d)")),
)

_SENSITIVE_CONTEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "labelled_identifier",
        re.compile(
            r"(?i)(?:담당자|성명|대표자|연락처|전화번호|휴대전화|전자우편|이메일|주소|소재지|"
            r"생년월일|주민등록번호|사업자등록번호|법인등록번호|계약번호)\s*[:：]\s*"
            r"[^\s,;|/]{1,80}"
        ),
    ),
    (
        "role_name",
        re.compile(
            r"(?i)(?:"
            r"(?:사업\s*총괄\s*P\.?\s*M\.?)\s*(?:[:：=]|\s|\()\s*"
            r"[\(\[\{<「『]?\s*[가-힣]{2,5}\s*[\)\]\}>」』]?"
            r"|(?:P\.?\s*M\.?)\s*(?:[:：=]|\()\s*"
            r"[\(\[\{<「『]?\s*[가-힣]{2,5}\s*[\)\]\}>」』]?"
            r"|(?:명사\s*특강|총괄책임자|연구책임자|프로젝트책임자|강연자|"
            r"발표자|담당자|대표자|성명|책임자|강사명?|연사|교수|감독|선수)"
            r"\s*(?:[:：=]|\s|\()\s*[\(\[\{<「『]?\s*[가-힣]{2,5}\s*[\)\]\}>」』]?"
            r"(?:\s*[\(\[\{<「『][^\)\]\}>」』\r\n]{1,40}[\)\]\}>」』])?"
            r")"
        ),
    ),
    (
        "name_role",
        re.compile(
            r"[가-힣]{2,5}(?:\s+|[\(\[\{<「『])\s*"
            r"(?:작가|교수|박사|강사|연사|감독|선수)\s*[\)\]\}>」』]?"
        ),
    ),
    (
        "street_address",
        re.compile(
            r"(?:[가-힣]{1,12}(?:특별시|광역시|특별자치시|특별자치도|도)\s+)?"
            r"[가-힣]{1,12}(?:구|군|시)\s+[가-힣A-Za-z0-9·]{1,30}(?:로|길|동|읍|면)"
            r"\s*\d{1,4}(?:-\d{1,4})?"
        ),
    ),
    (
        "road_address",
        re.compile(r"[가-힣A-Za-z0-9·]{2,30}(?:로|길)\s+\d{1,4}(?:-\d{1,4})?"),
    ),
    (
        "long_number",
        re.compile(r"(?<!\d)\d(?:[\s.-]?\d){7,}(?!\d)"),
    ),
)

_SAFE_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_SAFE_RECORD_KEY_PATTERN = re.compile(r"^perf_[a-f0-9]{20}$")


class PublicPerformanceSeedError(RuntimeError):
    """Raised when the publication-safe snapshot fails closed validation."""


@dataclass(frozen=True, slots=True)
class PublicSeedBuildResult:
    payload: dict[str, Any]
    source_rows_seen: int
    output_records: int
    direct_identifier_findings: int

    def aggregate_only(self) -> dict[str, Any]:
        provenance = self.payload["provenance"]
        return {
            "schema_version": self.payload["schema_version"],
            "dataset_version": self.payload["dataset_version"],
            "input_records": self.source_rows_seen,
            "output_records": self.output_records,
            "direct_identifier_findings": self.direct_identifier_findings,
            "records_sha256": provenance["records_sha256"],
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalise_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = " ".join(text.replace("\u200b", " ").replace("\ufeff", " ").split())
    return text or None


def _sanitise_text(
    value: Any,
    *,
    field: str,
    redactions: Counter[str],
) -> str | None:
    text = _normalise_text(value)
    if text is None:
        return None
    for label, pattern in (*_DIRECT_IDENTIFIER_PATTERNS, *_SENSITIVE_CONTEXT_PATTERNS):
        text, count = pattern.subn("[비식별]", text)
        redactions[label] += count
    text = re.sub(r"(?:\[비식별\]\s*){2,}", "[비식별] ", text).strip()
    limit = _TEXT_LIMITS[field]
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
        redactions[f"{field}_truncated"] += 1
    return text or None


def _normalise_date(value: Any) -> str | None:
    text = _normalise_text(value)
    if not text:
        return None
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _normalise_amount(value: Any) -> str | None:
    text = _normalise_text(value)
    if text is None:
        return None
    cleaned = re.sub(r"[^0-9+.-]", "", text.replace(",", ""))
    if not cleaned or cleaned in {"+", "-", ".", "+.", "-."}:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if amount < 0 or not amount.is_finite():
        return None
    return format(amount.normalize(), "f")


def _normalise_keywords(value: Any, redactions: Counter[str]) -> list[str]:
    text = _normalise_text(value)
    if not text:
        return []
    parts = re.split(r"[,;/|·\n]+", text)
    keywords: list[str] = []
    seen: set[str] = set()
    for part in parts:
        keyword = _sanitise_text(part, field="keyword", redactions=redactions)
        if not keyword:
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)
        if len(keywords) == 30:
            redactions["keyword_count_truncated"] += max(0, len(parts) - len(keywords))
            break
    return keywords


def _safe_record(
    source: Mapping[str, Any],
    *,
    redactions: Counter[str],
) -> dict[str, Any]:
    contract_date = _normalise_date(source.get("contract_date_iso"))
    return {
        "project_name": _sanitise_text(
            source.get("project_name"), field="project_name", redactions=redactions
        ),
        "overview": _sanitise_text(
            source.get("project_overview_redacted"),
            field="overview",
            redactions=redactions,
        ),
        "agency": _sanitise_text(
            source.get("agency"), field="agency", redactions=redactions
        ),
        "contract_date": contract_date,
        "contract_year": int(contract_date[:4]) if contract_date else None,
        "contract_amount_krw": _normalise_amount(source.get("contract_amount_source")),
        "keywords": _normalise_keywords(source.get("keywords_source"), redactions),
        "division": _sanitise_text(
            source.get("division_source"), field="division", redactions=redactions
        ),
    }


def _privacy_findings(payload: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    records = payload.get("records")
    if not isinstance(records, list):
        return ["records_not_list"]
    for record_index, record in enumerate(records):
        if not isinstance(record, dict):
            findings.append(f"record_{record_index}_not_object")
            continue
        for field in ("project_name", "overview", "agency", "division"):
            value = record.get(field)
            if isinstance(value, str):
                labels = [
                    label
                    for label, pattern in (
                        *_DIRECT_IDENTIFIER_PATTERNS,
                        *_SENSITIVE_CONTEXT_PATTERNS,
                    )
                    if pattern.search(value)
                ]
                findings.extend(
                    f"record_{record_index}_{field}_{label}" for label in labels
                )
        keywords = record.get("keywords")
        if isinstance(keywords, list):
            for keyword_index, keyword in enumerate(keywords):
                if isinstance(keyword, str):
                    labels = [
                        label
                        for label, pattern in (
                            *_DIRECT_IDENTIFIER_PATTERNS,
                            *_SENSITIVE_CONTEXT_PATTERNS,
                        )
                        if pattern.search(keyword)
                    ]
                    findings.extend(
                        f"record_{record_index}_keyword_{keyword_index}_{label}"
                        for label in labels
                    )
    return findings


def _build_aggregate(records: Sequence[Mapping[str, Any]], redactions: Counter[str]) -> dict[str, Any]:
    field_coverage = {
        field: sum(
            record.get(field) not in (None, "", [])
            for record in records
        )
        for field in PUBLIC_RECORD_FIELDS
        if field != "record_key"
    }
    years = Counter(
        str(record["contract_year"])
        for record in records
        if isinstance(record.get("contract_year"), int)
    )
    divisions = Counter(
        str(record["division"])
        for record in records
        if isinstance(record.get("division"), str) and str(record["division"]).strip()
    )
    dates = sorted(
        record["contract_date"]
        for record in records
        if isinstance(record.get("contract_date"), str)
    )
    canonical_without_keys = [
        {field: record.get(field) for field in PUBLIC_RECORD_FIELDS if field != "record_key"}
        for record in records
    ]
    duplicate_count = len(canonical_without_keys) - len(
        {_canonical_json(record) for record in canonical_without_keys}
    )
    return {
        "record_count": len(records),
        "field_coverage": field_coverage,
        "year_counts": dict(sorted(years.items())),
        "division_counts": dict(sorted(divisions.items())),
        "contract_date_min": dates[0] if dates else None,
        "contract_date_max": dates[-1] if dates else None,
        "exact_public_duplicate_count": duplicate_count,
        "redactions": dict(sorted(redactions.items())),
        "direct_identifier_findings": 0,
    }


def build_public_seed(
    source_rows: Iterable[Mapping[str, Any]],
    *,
    dataset_version: str,
) -> PublicSeedBuildResult:
    if not re.fullmatch(r"[0-9A-Za-z._-]{3,80}", dataset_version):
        raise PublicPerformanceSeedError("dataset_version has an unsupported format")

    redactions: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    canonical_occurrences: Counter[str] = Counter()
    source_rows_seen = 0
    for source in source_rows:
        source_rows_seen += 1
        safe = _safe_record(source, redactions=redactions)
        canonical = _canonical_json(safe)
        occurrence = canonical_occurrences[canonical]
        canonical_occurrences[canonical] += 1
        key_material = _canonical_json({"record": safe, "occurrence": occurrence})
        records.append(
            {
                "record_key": "perf_" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:20],
                **safe,
            }
        )

    records.sort(key=lambda item: item["record_key"])
    records_sha256 = hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()
    payload: dict[str, Any] = {
        "schema_version": PUBLIC_PERFORMANCE_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "classification": "PUBLIC_DERIVED",
        "policy_version": PUBLIC_PERFORMANCE_POLICY_VERSION,
        "provenance": {
            "method": "allowlist-redacted-snapshot",
            "records_sha256": records_sha256,
            "input_record_count": source_rows_seen,
        },
        "aggregate": {},
        "records": records,
    }
    findings = _privacy_findings(payload)
    if findings:
        raise PublicPerformanceSeedError(
            f"publication privacy validation failed with {len(findings)} finding(s)"
        )
    payload["aggregate"] = _build_aggregate(records, redactions)
    validate_public_performance_seed(payload)
    return PublicSeedBuildResult(
        payload=payload,
        source_rows_seen=source_rows_seen,
        output_records=len(records),
        direct_identifier_findings=0,
    )


def write_public_seed(result: PublicSeedBuildResult, output: str | Path) -> Path:
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(result.payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def validate_public_performance_seed(payload: Mapping[str, Any]) -> None:
    top_level = {
        "schema_version",
        "dataset_version",
        "classification",
        "policy_version",
        "provenance",
        "aggregate",
        "records",
    }
    if set(payload) != top_level:
        raise PublicPerformanceSeedError("public performance seed has unexpected top-level fields")
    if payload.get("schema_version") != PUBLIC_PERFORMANCE_SCHEMA_VERSION:
        raise PublicPerformanceSeedError("unsupported public performance seed schema")
    if payload.get("classification") != "PUBLIC_DERIVED":
        raise PublicPerformanceSeedError("public performance classification is invalid")
    if payload.get("policy_version") != PUBLIC_PERFORMANCE_POLICY_VERSION:
        raise PublicPerformanceSeedError("public performance redaction policy is invalid")
    dataset_version = payload.get("dataset_version")
    if not isinstance(dataset_version, str) or not re.fullmatch(
        r"[0-9A-Za-z._-]{3,80}", dataset_version
    ):
        raise PublicPerformanceSeedError("public performance dataset version is invalid")

    records = payload.get("records")
    provenance = payload.get("provenance")
    aggregate = payload.get("aggregate")
    if not isinstance(records, list) or not isinstance(provenance, dict) or not isinstance(aggregate, dict):
        raise PublicPerformanceSeedError("public performance seed sections are invalid")
    if set(provenance) != {"method", "records_sha256", "input_record_count"}:
        raise PublicPerformanceSeedError("public performance provenance allowlist is invalid")
    if provenance.get("method") != "allowlist-redacted-snapshot":
        raise PublicPerformanceSeedError("public performance provenance method is invalid")
    expected_digest = provenance.get("records_sha256")
    if not isinstance(expected_digest, str) or not _SAFE_DIGEST_PATTERN.fullmatch(expected_digest):
        raise PublicPerformanceSeedError("public performance provenance digest is invalid")
    actual_digest = hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()
    if actual_digest != expected_digest:
        raise PublicPerformanceSeedError("public performance records digest does not match")
    if provenance.get("input_record_count") != len(records):
        raise PublicPerformanceSeedError("public performance source/output count does not match")
    if aggregate.get("record_count") != len(records):
        raise PublicPerformanceSeedError("public performance aggregate count does not match")
    if aggregate.get("direct_identifier_findings") != 0:
        raise PublicPerformanceSeedError("public performance seed is not publication safe")

    keys: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or tuple(record) != PUBLIC_RECORD_FIELDS:
            raise PublicPerformanceSeedError("public performance record allowlist is invalid")
        record_key = record.get("record_key")
        if not isinstance(record_key, str) or not _SAFE_RECORD_KEY_PATTERN.fullmatch(record_key):
            raise PublicPerformanceSeedError("public performance record key is invalid")
        if record_key in keys:
            raise PublicPerformanceSeedError("public performance record keys are not unique")
        keys.add(record_key)
        if not isinstance(record.get("keywords"), list) or not all(
            isinstance(keyword, str) for keyword in record["keywords"]
        ):
            raise PublicPerformanceSeedError("public performance keywords are invalid")
        contract_year = record.get("contract_year")
        if contract_year is not None and (
            not isinstance(contract_year, int) or contract_year < 1900 or contract_year > 2200
        ):
            raise PublicPerformanceSeedError("public performance contract year is invalid")

    findings = _privacy_findings(payload)
    if findings:
        raise PublicPerformanceSeedError(
            f"public performance seed contains {len(findings)} direct identifier finding(s)"
        )

    redactions = aggregate.get("redactions")
    if not isinstance(redactions, dict) or not all(
        isinstance(key, str)
        and re.fullmatch(r"[a-z0-9_]{1,80}", key)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        for key, value in redactions.items()
    ):
        raise PublicPerformanceSeedError("public performance redaction aggregate is invalid")
    expected_aggregate = _build_aggregate(records, Counter(redactions))
    if aggregate != expected_aggregate:
        raise PublicPerformanceSeedError("public performance aggregate does not match records")


@lru_cache(maxsize=1)
def load_public_performance_seed() -> dict[str, Any]:
    resource = resources.files("pai_loop").joinpath(PUBLIC_PERFORMANCE_RESOURCE)
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicPerformanceSeedError("packaged public performance seed could not be loaded") from exc
    if not isinstance(payload, dict):
        raise PublicPerformanceSeedError("packaged public performance seed is not an object")
    validate_public_performance_seed(payload)
    return payload


def public_performance_summary() -> dict[str, Any]:
    seed = load_public_performance_seed()
    return {
        "schema_version": seed["schema_version"],
        "dataset_version": seed["dataset_version"],
        "classification": seed["classification"],
        "policy_version": seed["policy_version"],
        "provenance": dict(seed["provenance"]),
        "aggregate": dict(seed["aggregate"]),
    }


def query_public_performance(
    *,
    q: str | None = None,
    year: int | None = None,
    division: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    seed = load_public_performance_seed()
    query = _normalise_text(q)
    division_query = _normalise_text(division)
    query_folded = query.casefold() if query else None
    division_folded = division_query.casefold() if division_query else None

    def matches(record: Mapping[str, Any]) -> bool:
        if year is not None and record.get("contract_year") != year:
            return False
        if division_folded and division_folded not in str(record.get("division") or "").casefold():
            return False
        if query_folded:
            haystack = " ".join(
                [
                    str(record.get("project_name") or ""),
                    str(record.get("overview") or ""),
                    str(record.get("agency") or ""),
                    str(record.get("division") or ""),
                    " ".join(record.get("keywords") or []),
                ]
            ).casefold()
            if query_folded not in haystack:
                return False
        return True

    selected = [record for record in seed["records"] if matches(record)]
    return {
        "dataset_version": seed["dataset_version"],
        "records_sha256": seed["provenance"]["records_sha256"],
        "total": len(selected),
        "offset": offset,
        "limit": limit,
        "records": selected[offset : offset + limit],
    }


public_performance_router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_api_key)],
    tags=["public performance"],
)


@public_performance_router.get("/performance/summary")
def get_public_performance_summary() -> dict[str, Any]:
    return public_performance_summary()


@public_performance_router.get("/performance")
def get_public_performance(
    q: str | None = None,
    year: int | None = Query(default=None, ge=1900, le=2200),
    division: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return query_public_performance(
        q=q,
        year=year,
        division=division,
        limit=limit,
        offset=offset,
    )
