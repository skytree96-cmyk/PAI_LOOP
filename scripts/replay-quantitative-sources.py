"""Local, non-persistent replay of actual source/quantitative domain stages.

No provider, socket, database connection, transaction, or subprocess is allowed.
The persistence-owning run_analysis_pipeline entry point is deliberately not
called. Its source selector is given only the exact frozen NoticeVersion SELECT.
Native revalidation remains a separate attachment diagnostic, never a new stored
record and never an input to a notice/company score.

Usage: python scripts/replay-quantitative-sources.py --snapshot snapshot.json
       --source-map source-map.json --output replay.json [--golden golden.json]

The snapshot contains notices/versions dictionaries of actual model columns.
The source map is keyed by version ID, or a sources list containing version_id,
native_path, canonical_path, native_sha256 and canonical_sha256. An empty object
means native bytes are unavailable. All paths are local and explicit. Reports
can contain company rationale and should be kept in a private location.

Golden v1 is documented with SYN data in test_quantitative_replay_cli.py.
It compares declared expectations only, never supplies facts or scoring rules.
Conditional matches are reported separately from observed frozen-input matches;
neither certifies live company evidence. A mismatch exits 1 after saving the report.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager, redirect_stderr
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from unittest.mock import patch
from typing import Literal
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import sqlalchemy
from sqlalchemy import Date, DateTime, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from pai_loop import analysis_pipeline as pipeline
from pai_loop import pps_enrichment as pps
from pai_loop.extraction_contracts import classify_attempt_header, classify_record_contract
from pai_loop.integrations.openai_extraction import (
    ExtractionPayload, OpenAIExtractionClient, PROMPT_VERSION,
)
from pai_loop.models import Notice, NoticeVersion, CompanyFact, Evidence, CompanyPerformanceRecord
from pai_loop.quantitative_rule_extraction import (
    build_quantitative_candidate_profile, validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    _current_dynamic_quantitative_profile, quantitative_request_from_candidate_profile,
    estimate_for_notice, QUANTITATIVE_ENGINE_VERSION,
)
from pai_loop.quantitative_source_revalidation import (
    revalidate_quantitative_source, revalidation_json_sha256,
)


def digest(value):
    return revalidation_json_sha256(value)


def local_path(value):
    text = str(value)
    if text.startswith(("\\\\", "//")) or "://" in text:
        raise ValueError("LOCAL_FILES_ONLY")
    return Path(value)


def load_json(path):
    return json.loads(local_path(path).read_text(encoding="utf-8-sig"))


class GoldenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class GoldenCriterion(GoldenModel):
    criterion_id: str = Field(min_length=1, max_length=300)
    status: Literal["CONFIRMED", "ESTIMATED", "REVIEW", "UNSCORABLE", "OUT_OF_SCOPE"]
    # This is the row's estimated_points, not its maximum or a missing-value zero.
    points: float | None = Field(ge=0)

    @model_validator(mode="after")
    def valid_points(self):
        if self.criterion_id != self.criterion_id.strip():
            raise ValueError("GOLDEN_ID_INVALID")
        if (self.status in {"REVIEW", "UNSCORABLE", "OUT_OF_SCOPE"} and self.points is not None
                or self.status == "CONFIRMED" and self.points is None):
            raise ValueError("GOLDEN_STATUS_POINTS_INVALID")
        return self


class GoldenSubtotal(GoldenModel):
    overall_status: Literal["CONFIRMED", "ESTIMATED", "REVIEW", "UNSCORABLE"]
    total_max_points: float | None = Field(ge=0)
    estimated_points: float | None = Field(ge=0)
    lower_points: float | None = Field(ge=0)
    upper_points: float | None = Field(ge=0)

    @model_validator(mode="after")
    def valid_range(self):
        if self.overall_status in {"REVIEW", "UNSCORABLE"} and self.estimated_points is not None:
            raise ValueError("GOLDEN_STATUS_POINTS_INVALID")
        if self.overall_status == "CONFIRMED" and self.estimated_points is None:
            raise ValueError("GOLDEN_STATUS_POINTS_INVALID")
        if self.lower_points is not None and self.upper_points is not None and self.lower_points > self.upper_points:
            raise ValueError("GOLDEN_RANGE_INVALID")
        if self.total_max_points is not None and any(
            value is not None and value > self.total_max_points
            for value in (self.estimated_points, self.lower_points, self.upper_points)
        ):
            raise ValueError("GOLDEN_RANGE_INVALID")
        return self


class GoldenCase(GoldenModel):
    notice_key: str = Field(min_length=1, max_length=300)
    expectation_basis: Literal["OBSERVED_FROZEN_INPUTS", "CONDITIONAL"]
    criteria: list[GoldenCriterion] = Field(max_length=1000)
    subtotal: GoldenSubtotal | None

    @model_validator(mode="after")
    def unique_expectations(self):
        if self.notice_key != self.notice_key.strip() or not self.criteria and self.subtotal is None:
            raise ValueError("GOLDEN_CASE_EMPTY_OR_INVALID")
        if len({row.criterion_id for row in self.criteria}) != len(self.criteria):
            raise ValueError("GOLDEN_DUPLICATE_CRITERION_ID")
        return self


class GoldenFile(GoldenModel):
    version: Literal["quantitative-replay-golden-1"]
    # Absolute points, with no relative tolerance; null is never numeric zero.
    absolute_tolerance: float = Field(ge=0, le=0.01)
    cases: list[GoldenCase] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_notices(self):
        if len({case.notice_key for case in self.cases}) != len(self.cases):
            raise ValueError("GOLDEN_DUPLICATE_NOTICE_KEY")
        return self


def load_golden(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("GOLDEN_DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def invalid_constant(_value):
        raise ValueError("GOLDEN_NONFINITE_NUMBER")
    content = local_path(path).read_bytes()
    if len(content) > 2_000_000:
        raise ValueError("GOLDEN_SIZE_LIMIT")
    try:
        raw = json.loads(content.decode("utf-8-sig"), object_pairs_hook=unique_object,
                         parse_constant=invalid_constant)
        return GoldenFile.model_validate(raw)
    except (ValidationError, json.JSONDecodeError, UnicodeError):
        # Do not echo arbitrary source/fact text accidentally placed in a golden.
        raise ValueError("GOLDEN_SCHEMA_INVALID") from None


def compare_golden(golden, results):
    by_notice = {row["notice_key"]: row for row in results}
    if len(by_notice) != len(results):
        raise ValueError("DUPLICATE_REPLAY_NOTICE_KEY")
    if any(case.notice_key not in by_notice for case in golden.cases):
        raise ValueError("GOLDEN_NOTICE_NOT_IN_SNAPSHOT")
    def number_matches(actual, expected):
        if actual is None or expected is None:
            return actual is expected
        return (type(actual) in {int, float} and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=0.0, abs_tol=golden.absolute_tolerance))
    cases = []
    for expected in golden.cases:
        actual = by_notice[expected.notice_key]["estimate"]
        rows = actual["criteria"]
        row_counts = Counter(row["criterion_id"] for row in rows)
        expected_ids = {row.criterion_id for row in expected.criteria}
        compared = []
        for criterion in expected.criteria:
            count = row_counts[criterion.criterion_id]
            if count != 1:
                compared.append({"criterion_id": criterion.criterion_id, "matched": False,
                    "result": "UNMATCHED" if not count else "AMBIGUOUS", "mismatched_fields": []})
                continue
            row = next(row for row in rows if row["criterion_id"] == criterion.criterion_id)
            mismatches = [name for name, matches in (
                ("status", row["status"] == criterion.status),
                ("points", number_matches(row["estimated_points"], criterion.points)),
            ) if not matches]
            compared.append({"criterion_id": criterion.criterion_id, "matched": not mismatches,
                "result": "MATCH" if not mismatches else "MISMATCH", "mismatched_fields": mismatches})
        subtotal = None
        if expected.subtotal is not None:
            mismatches = [key for key, value in expected.subtotal.model_dump().items()
                if not (actual[key] == value if key == "overall_status" else number_matches(actual[key], value))]
            subtotal = {"matched": not mismatches, "mismatched_fields": mismatches}
        matched = all(row["matched"] for row in compared) and (subtotal is None or subtotal["matched"])
        cases.append({"notice_key": expected.notice_key, "expectation_basis": expected.expectation_basis,
            "declared_expectations_match": matched, "criteria": compared, "subtotal": subtotal,
            "expected_criterion_count": len(compared), "actual_criterion_count": len(rows),
            "unchecked_actual_criterion_count": sum(row["criterion_id"] not in expected_ids for row in rows),
            "criterion_coverage_complete": set(row_counts) == expected_ids and all(v == 1 for v in row_counts.values())})
    rates = {}
    for basis in ("OBSERVED_FROZEN_INPUTS", "CONDITIONAL"):
        selected = [case for case in cases if case["expectation_basis"] == basis]
        matched = sum(case["declared_expectations_match"] for case in selected)
        rates[basis] = {"expected_cases": len(selected), "matched_cases": matched,
            "failed_cases": len(selected) - matched, "declared_case_match_rate": matched / len(selected) if selected else None}
    return {"version": golden.version, "golden_sha256": digest(golden.model_dump(mode="json")),
        "absolute_tolerance": golden.absolute_tolerance, "relative_tolerance": 0,
        "points_field": "criterion.estimated_points", "cohort_notices": len(results),
        "golden_notices": len(cases), "unchecked_cohort_notices": len(results) - len(cases),
        "cohort_coverage_complete": len(results) == len(cases), "rates_by_expectation_basis": rates,
        "all_declared_expectations_match": all(case["declared_expectations_match"] for case in cases),
        "live_company_evidence_verified": False,
        "limitation": "Only declared expectations are compared; conditional matches never count as observed matches or certify company evidence.",
        "cases": cases}


class ForbiddenEffect(BaseException):
    """Cannot be swallowed by ordinary application exception handlers."""


@contextmanager
def no_external_effects():
    calls = Counter()
    def deny(label):
        def blocked(*args, **kwargs):
            calls[label] += 1
            raise ForbiddenEffect(label)
        return blocked
    targets = [
        (socket.socket, "connect", "network"), (socket.socket, "connect_ex", "network"),
        (socket.socket, "sendto", "network"), (socket, "create_connection", "network"),
        (socket, "getaddrinfo", "network"), (urllib.request, "urlopen", "network"),
        (httpx.Client, "send", "network"), (httpx.AsyncClient, "send", "network"),
        (OpenAIExtractionClient, "extract", "provider"),
        (OpenAIExtractionClient, "extract_quantitative_probe", "provider"),
        (OpenAIExtractionClient, "_post", "provider"),
        (pps, "download_public_attachment", "network"),
        (pps, "enrich_notice_from_pps", "enrichment"),
        (pps, "_persist_extraction_version", "database"),
        (pps, "persist_pps_metadata_version", "database"),
        (Engine, "connect", "database"), (Engine, "raw_connection", "database"),
        (sqlalchemy, "create_engine", "database"), (sqlite3, "connect", "database"),
        (subprocess, "Popen", "subprocess"),
    ]
    targets.extend((Session, name, "database") for name in
                   ("add", "add_all", "flush", "commit", "execute", "scalars", "begin"))
    with ExitStack() as stack:
        for obj, attr, label in targets:
            stack.enter_context(patch.object(obj, attr, deny(label)))
        # Block a direct driver connection as well as SQLAlchemy. No credentials
        # or environment values are read by this runner.
        try:
            import psycopg
        except ImportError:
            pass
        else:
            stack.enter_context(patch.object(psycopg, "connect", deny("database")))
        yield calls


class FrozenVersionReader:
    """One query's frozen rows; not a Session or a general SQL emulator."""
    def __init__(self, notice):
        self.notice = notice
        self.queries = 0

    def scalars(self, statement):
        expected = select(NoticeVersion).where(
            NoticeVersion.notice_id == self.notice.id).order_by(NoticeVersion.version_no)
        assert statement.compare(expected), "UNSUPPORTED_FROZEN_QUERY"
        assert statement.compile().params == expected.compile().params, "WRONG_NOTICE_BINDING"
        self.queries += 1
        rows = sorted(self.notice.versions, key=lambda row: row.version_no)
        assert all(row.notice_id == self.notice.id for row in rows), "CROSS_NOTICE_ROW"
        class Rows:
            def all(self):
                return list(rows)
        return Rows()


def orm_row(model, row):
    """Only real supplied column values; absent facts/identity are never invented."""
    values = {}
    for column in inspect(model).columns:
        if column.key not in row:
            continue
        value = row[column.key]
        if isinstance(value, str):
            if isinstance(column.type, DateTime):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            elif isinstance(column.type, Date):
                value = date.fromisoformat(value)
            elif column.key in {"effective_from", "effective_to"}:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        values[column.key] = value
    return model(**values)


def load_notices(snapshot):
    for field in ("id", "notice_key"):
        values = [row[field] for row in snapshot["notices"]]
        if len(values) != len(set(values)):
            raise ValueError("DUPLICATE_SNAPSHOT_NOTICE_IDENTITY")
    notices = {row["id"]: orm_row(Notice, row) for row in snapshot["notices"]}
    versions = snapshot.get("versions")
    if versions is None:
        versions = snapshot.get("metadata", []) + snapshot.get("extractions", [])
    seen = set()
    for row in versions:
        assert row["id"] not in seen, "DUPLICATE_VERSION_ID"
        seen.add(row["id"])
        assert row["notice_id"] in notices, "VERSION_NOTICE_ABSENT"
        version = orm_row(NoticeVersion, row)
        notices[row["notice_id"]].versions.append(version)
    return list(notices.values())


def load_company(snapshot):
    if not snapshot:
        return [], [], {"status": "NOT_SUPPLIED", "complete": False}
    facts, records = [], []
    evidence_by_id = {row["id"]: orm_row(Evidence, row)
                      for row in snapshot.get("evidence", []) if row.get("id")}
    for row in snapshot.get("facts", snapshot.get("company_facts", [])):
        fact = orm_row(CompanyFact, row)
        fact.evidence = evidence_by_id.get(row.get("evidence_id"))
        if fact.evidence is None and row.get("evidence_present"):
            # Older private snapshot used a SQL join but omitted relationship IDs,
            # key and reference. Preserve that omission; no synthetic linkage.
            ev = {k: row[k] for k in ("issued_at", "valid_from", "valid_until", "sha256", "metadata_json") if k in row}
            ev.update(status=row.get("evidence_status"), evidence_type=row.get("evidence_type"))
            fact.evidence = orm_row(Evidence, ev)
        facts.append(fact)
    for row in snapshot.get("records", snapshot.get("performance_records", [])):
        if row.get("record_status") != "ARCHIVED":
            records.append(orm_row(CompanyPerformanceRecord, row))
    return facts, records, {
        "status": "SUPPLIED_FROZEN_SUBSET", "complete": False,
        "measured_at": snapshot.get("measured_at"), "snapshot_sha256": digest(snapshot),
        "facts": len(facts), "performance_records": len(records),
        "facts_missing_identity_or_evidence_link": sum(
            not f.id or not f.evidence_id or not f.evidence or not f.evidence.id for f in facts),
        "limitation": "Snapshot subset/omitted IDs remain unavailable; no current company score attestation.",
    }


def profile_summary(profile):
    if profile is None:
        return None
    data = profile.model_dump(mode="json")
    return {"status": data.get("status"),
            "expected_attachments": len(profile.expected_attachment_ids),
            "document_bindings": len(profile.document_bindings),
            "available_candidates": len(profile.available_candidates),
            "review_candidates": len(profile.review_candidates),
            "tables": len(profile.tables),
            "issue_counts": dict(Counter(item.code for item in profile.issues)),
            "table_diagnostics": [{k: getattr(row, k) for k in
                ("table_id", "status", "criterion_ids", "available_criterion_ids", "review_criterion_ids")}
                for row in profile.tables],
            "candidate_diagnostics": [{"table_id": row.table_id, "criterion_id": row.criterion_id,
                "status": row.status, "metric": row.metric,
                "issue_codes": getattr(row, "issue_codes", ())}
                for row in (*profile.available_candidates, *profile.review_candidates)]}


def diagnostic_raw_candidate(version, entry):
    """Inspect unchanged raw against current native text without contract promotion.

This deliberately does not call a record writer, compatibility classifier or
notice scorer. Unsupported historical contracts can be diagnosed, but every
profile remains unbound and ineligible for persistence. Supplied canonical text
is a separately labeled frozen comparison, not current-parser provenance.
"""
    boundary = {"purpose": "UNBOUND_RAW_SOURCE_DIAGNOSTIC_ONLY", "persistence_eligible": False,
                "attachment_coverage_complete": False, "production_score_input": False}
    if entry is None:
        return {**boundary, "status": "LOCAL_EXACT_SOURCE_UNAVAILABLE"}
    attempt = version.source_payload or {}
    native_path = local_path(entry["native_path"])
    if not native_path.is_file():
        return {**boundary, "status": "LOCAL_EXACT_SOURCE_UNAVAILABLE"}
    native = native_path.read_bytes()
    native_sha = hashlib.sha256(native).hexdigest()
    if native_sha != version.file_sha256 or native_sha != attempt.get("document_sha256") or native_sha != entry["native_sha256"]:
        return {**boundary, "status": "NATIVE_SHA256_MISMATCH"}
    origin = {"prompt_version": attempt.get("prompt_version"), "schema_version": attempt.get("schema_version"),
              "processing_version": attempt.get("processing_version"), "raw_result_sha256": digest(attempt.get("result")),
              "native_sha256": native_sha}
    if attempt.get("result") is None:
        return {**boundary, "status": "RAW_PAYLOAD_UNAVAILABLE", "origin": origin}
    try:
        raw = ExtractionPayload.model_validate(attempt.get("result"))
    except ValidationError as exc:
        return {**boundary, "status": "RAW_SCHEMA_INVALID", "origin": origin,
                "schema_error_types": dict(Counter(error["type"] for error in exc.errors(include_input=False)))}
    attachment_id = attempt.get("attachment_id")
    # The production parser remains fixed. No caller-provided parser is trusted.
    parsed = pps.extract_pps_document_content(attempt["source_label"], native)
    profile = build_quantitative_candidate_profile(
        {attachment_id: raw}, {attachment_id: parsed.text}, expected_attachment_ids={attachment_id})
    assert not profile.document_bindings and profile.manifest_sha256 is None
    parsed_sha = hashlib.sha256(parsed.text.encode()).hexdigest()
    result = {**boundary, "status": "CURRENT_NATIVE_RAW_DIAGNOSED", "origin": origin,
              "parser_complete": parsed.complete, "parser_warnings": list(parsed.warnings),
              "parsed_text_sha256": parsed_sha,
              "current_canonical_matches_stored": parsed_sha == (attempt.get("document_processing") or {}).get("source_text_sha256"),
              "current_source_candidate": profile_summary(profile)}
    canonical_path = local_path(entry["canonical_path"])
    if canonical_path.is_file():
        frozen = canonical_path.read_text(encoding="utf-8-sig")
        frozen_sha = hashlib.sha256(frozen.encode()).hexdigest()
        if frozen_sha != entry["canonical_sha256"]:
            result["frozen_canonical_status"] = "SUPPLIED_CANONICAL_SHA256_MISMATCH"
        else:
            frozen_profile = build_quantitative_candidate_profile(
                {attachment_id: raw}, {attachment_id: frozen}, expected_attachment_ids={attachment_id})
            result.update(frozen_canonical_status="CALLER_FROZEN_COMPARISON_ONLY",
                frozen_canonical_matches_current_parser=frozen_sha == parsed_sha,
                frozen_canonical_matches_stored=frozen_sha == (attempt.get("document_processing") or {}).get("source_text_sha256"),
                frozen_source_candidate=profile_summary(frozen_profile))
    return result


def score_summary(score):
    names = ("engine_version", "rule_source_status", "source_validation_status",
             "activation_status", "activation_reasons", "overall_status", "total_max_points",
             "confirmed_points", "estimated_points", "lower_points", "upper_points",
             "unscorable_points", "out_of_scope_points", "evidence_coverage_pct")
    result = {name: getattr(score, name) for name in names}
    result["criteria"] = [{k: getattr(row, k) for k in
        ("criterion_id", "status", "estimated_points", "lower_points", "upper_points", "rationale")}
        for row in score.criteria]
    return result


def stage_funnel(profile, request, estimate):
    """Dynamic compilation and actual scorer output have distinct authority.

    The scorer can select a curated profile, so its activation/rows must not be
    attributed to the dynamic request or to an attachment-only raw diagnostic.
    """
    return {"profile_status": profile.status if profile is not None else "NONE",
        "activation_status": estimate.activation_status,
        "compiled_activation_status": request.activation_status if request is not None else "NONE",
        "criteria": len(request.criteria) if request is not None else 0,
        "review_criteria": len(request.review_criteria) if request is not None else 0,
        "criterion_status_counts": dict(Counter(row.status for row in estimate.criteria)),
        "numeric_criterion_count": sum(row.estimated_points is not None for row in estimate.criteria),
        "numeric_total": estimate.estimated_points is not None,
        "overall_status": estimate.overall_status}


def aggregate_stage_funnel(results):
    rows = [result["stage_funnel"] for result in results]
    predicates = (
        ("snapshot_notices", lambda r: True),
        ("dynamic_profile_present", lambda r: r["profile_status"] != "NONE"),
        ("dynamic_profile_available", lambda r: r["profile_status"] == "AVAILABLE"),
        ("dynamic_profile_review", lambda r: r["profile_status"] == "REVIEW"),
        ("dynamic_request_has_criteria", lambda r: r["criteria"] > 0),
        ("dynamic_request_has_review_criteria", lambda r: r["review_criteria"] > 0),
        ("runtime_auto_active", lambda r: r["activation_status"] == "AUTO_ACTIVE"),
        ("runtime_partial_active", lambda r: r["activation_status"] == "PARTIAL_ACTIVE"),
        ("runtime_has_numeric_criterion", lambda r: r["numeric_criterion_count"] > 0),
        ("runtime_has_numeric_total", lambda r: r["numeric_total"]),
        ("runtime_confirmed_numeric_total", lambda r: r["numeric_total"] and r["overall_status"] == "CONFIRMED"),
    )
    return {"notice_denominator": len(rows),
        "interpretation": "Stages and branches, not nested counts: profile/compiled fields use the dynamic profile; runtime fields use the actual scorer, including curated routing.",
        "attachment_diagnostics_included": False,
        "table": [{"stage": name, "notices": sum(predicate(row) for row in rows),
                   "denominator": len(rows)} for name, predicate in predicates],
        "profile_status_counts": dict(Counter(row["profile_status"] for row in rows)),
        "activation_status_counts": dict(Counter(row["activation_status"] for row in rows)),
        "compiled_activation_status_counts": dict(Counter(row["compiled_activation_status"] for row in rows)),
        "compiled_criteria": sum(row["criteria"] for row in rows),
        "compiled_review_criteria": sum(row["review_criteria"] for row in rows),
        "criterion_status_counts": dict(sum((Counter(row["criterion_status_counts"]) for row in rows), Counter()))}


def raw_candidate_comparison(results):
    rows = [source["raw_source_diagnostic"] for result in results for source in result["source_attempts"]
            if source["raw_source_diagnostic"].get("current_source_candidate")]
    compared = [row for row in rows if row.get("frozen_source_candidate")]
    def totals(key):
        profiles = [row[key] for row in compared]
        return {"available_candidates": sum(p["available_candidates"] for p in profiles),
                "review_candidates": sum(p["review_candidates"] for p in profiles),
                "profile_status_counts": dict(Counter(p["status"] for p in profiles)),
                "issue_counts": dict(sum((Counter(p["issue_counts"]) for p in profiles), Counter())),
                "issue_attachment_counts": dict(sum((Counter(p["issue_counts"].keys()) for p in profiles), Counter()))}
    return {"diagnosed_attachments": len(rows), "compared_attachments": len(compared),
            "native_parser_complete": sum(row["parser_complete"] for row in rows),
            "current_canonical_matches_stored": sum(row["current_canonical_matches_stored"] for row in rows),
            "changed_canonical_attachments": sum(not row["frozen_canonical_matches_current_parser"] for row in compared),
            "changed_candidate_count_attachments": sum(row["frozen_source_candidate"]["available_candidates"]
                != row["current_source_candidate"]["available_candidates"] for row in compared),
            "changed_issue_attachments": sum(row["frozen_source_candidate"]["issue_counts"]
                != row["current_source_candidate"]["issue_counts"] for row in compared),
            "frozen_canonical_comparison_only": totals("frozen_source_candidate"),
            "current_native_parser": totals("current_source_candidate")}


def diagnostic_native(version, full_manifest, entry):
    if entry is None:
        return {"status": "LOCAL_EXACT_SOURCE_UNAVAILABLE", "persistence_eligible": False}
    attempt = version.source_payload
    native_path, text_path = local_path(entry["native_path"]), local_path(entry["canonical_path"])
    if not native_path.is_file() or not text_path.is_file():
        return {"status": "LOCAL_EXACT_SOURCE_UNAVAILABLE", "persistence_eligible": False}
    native = native_path.read_bytes()
    canonical = text_path.read_text(encoding="utf-8-sig")
    native_sha = hashlib.sha256(native).hexdigest()
    canonical_sha = hashlib.sha256(canonical.encode()).hexdigest()
    expected_native = entry.get("native_sha256", version.file_sha256)
    expected_text = entry.get("canonical_sha256", (attempt.get("document_processing") or {}).get("source_text_sha256"))
    checks = {
        "native_digest": native_sha == expected_native == version.file_sha256 == attempt.get("document_sha256"),
        "canonical_digest": canonical_sha == expected_text == (attempt.get("document_processing") or {}).get("source_text_sha256"),
        "whole_manifest": digest(full_manifest) == attempt.get("current_manifest_sha256"),
    }
    descriptors = [item for item in full_manifest if item.get("attachment_id") == attempt.get("attachment_id")]
    checks["descriptor"] = len(descriptors) == 1 and digest(descriptors[0]) == attempt.get("manifest_sha256")
    if not all(checks.values()):
        return {"status": "FROZEN_SOURCE_BINDING_MISMATCH", "checks": checks, "persistence_eligible": False}
    kind = classify_record_contract(attempt, attempt.get("quantitative_validation_record") or {})
    if kind == "LEGACY_CASE_V2":
        result = revalidate_quantitative_source(source_version_id=version.id,
            source_attempt=attempt, expected_attempt_sha256=digest(attempt),
            native_bytes=native, expected_native_sha256=expected_native,
            canonical_text=canonical, expected_canonical_sha256=expected_text,
            full_manifest=full_manifest, expected_manifest_sha256=digest(full_manifest))
        return {"status": result.native_canonical_status, "contract": kind,
                "purpose": result.purpose, "persistence_eligible": result.persistence_eligible,
                "attachment_coverage_complete": result.attachment_coverage_complete,
                "diagnostic_codes": result.diagnostic_codes, "profile": profile_summary(result.profile),
                "proof": result.proof.model_dump(mode="json")}
    parsed = pps.extract_pps_document_content(descriptors[0]["file_name"], native)
    if not parsed.complete or parsed.text != canonical:
        return {"status": "NATIVE_CANONICAL_NOT_REPRODUCED", "persistence_eligible": False}
    if kind != "CURRENT":
        return {"status": "NATIVE_REPRODUCED_REVALIDATION_CONTRACT_UNSUPPORTED",
                "contract": kind, "persistence_eligible": False}
    record = validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(attempt["result"]), source_text=canonical,
        attachment_id=attempt["attachment_id"], document_sha256=expected_native,
        manifest_sha256=digest(full_manifest), prompt_version=attempt["prompt_version"],
        extraction_schema_version=attempt["schema_version"])
    return {"status": "CURRENT_RAW_REVALIDATED_DIAGNOSTIC_ONLY", "contract": kind,
            "persistence_eligible": False, "record_status": record.status,
            "available_candidates": len(record.available_candidates),
            "issue_counts": dict(Counter(issue.code for issue in record.issues))}


def replay_notice(notice, facts=(), records=(), sources=None):
    before = {row.id: digest(row.source_payload) for row in notice.versions}
    reader = FrozenVersionReader(notice)
    selected = pipeline._select_source_versions(reader, notice_id=notice.id,
        prompt_version=PROMPT_VERSION, source_version_ids=None)
    parsed = [pipeline._parse_source(row, prompt_version=PROMPT_VERSION, allow_compatible_pps=True)
              for row in selected]
    merged_requirements = pipeline._merge_requirements(parsed)
    basis = pipeline._current_pps_manifest_basis(notice.versions, prompt_version=PROMPT_VERSION)
    attachments, invalid, attempts = pps._current_manifest_attempts(notice.versions, validate_accepted=False)
    profile = _current_dynamic_quantitative_profile(notice)
    request = quantitative_request_from_candidate_profile(profile) if profile is not None else None
    # This is the same function called inside run_analysis_pipeline before its
    # persistence block, including real company/register bridges and scorer.
    estimate = estimate_for_notice(notice, company_facts=facts, performance_records=records)
    source_rows = []
    metadata = max((v for v in notice.versions if (v.source_payload or {}).get("kind") == "PPS_NOTICE_METADATA"),
                   key=lambda v: v.version_no, default=None)
    manifest = (metadata.source_payload.get("attachment_manifest") or []) if metadata else []
    # Audit every latest manifest-bound generation, including unsupported headers
    # that the actual runtime selector correctly excludes. Never resurrect older
    # compatible successes behind a newer failure/unsupported attempt.
    latest_bound = {}
    descriptor_sha = {item.get("attachment_id"): digest(item) for item in manifest if isinstance(item, dict)}
    whole_sha = digest(manifest)
    for version in sorted(notice.versions, key=lambda row: row.version_no):
        payload = version.source_payload or {}
        aid = payload.get("attachment_id")
        if (payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
                and payload.get("source_kind") == "PPS_PUBLIC_ATTACHMENT"
                and aid in descriptor_sha and payload.get("manifest_sha256") == descriptor_sha[aid]
                and payload.get("current_manifest_sha256") == whole_sha):
            latest_bound[aid] = version
    for aid, version in sorted(latest_bound.items()):
        payload = version.source_payload
        raw = payload.get("result") or {}
        stored_record = payload.get("quantitative_validation_record") or {}
        item = {"version_id": version.id, "attachment_id": aid,
                "header_contract": classify_attempt_header(payload),
                "record_contract": classify_record_contract(payload, stored_record),
                "source_status": payload.get("status"), "selected_by_pipeline": version in selected,
                "selected_by_current_manifest_reader": attempts.get(aid) is version,
                "raw_tables": len(raw.get("quantitative_tables") or []),
                "raw_criteria": sum(len(t.get("criteria") or []) for t in raw.get("quantitative_tables") or []),
                "stored_record_status": stored_record.get("status"),
                "stored_available_candidates": len(stored_record.get("available_candidates") or []),
                "stored_record_issues": dict(Counter(x.get("code") for x in stored_record.get("issues") or []))}
        try:
            with redirect_stderr(io.StringIO()):
                item["native_diagnostic"] = diagnostic_native(version, manifest, (sources or {}).get(version.id))
        except Exception as exc:
            # No arbitrary exception text: source/URL/body values stay private.
            item["native_diagnostic"] = {"status": "DIAGNOSTIC_REJECTED", "error_type": type(exc).__name__, "persistence_eligible": False}
        try:
            with redirect_stderr(io.StringIO()):
                item["raw_source_diagnostic"] = diagnostic_raw_candidate(version, (sources or {}).get(version.id))
        except Exception as exc:
            item["raw_source_diagnostic"] = {"status": "DIAGNOSTIC_REJECTED", "error_type": type(exc).__name__, "persistence_eligible": False}
        source_rows.append(item)
    assert before == {row.id: digest(row.source_payload) for row in notice.versions}, "SOURCE_INPUT_MUTATED"
    return {"notice_id": notice.id, "notice_key": notice.notice_key,
            "snapshot_versions": len(notice.versions), "frozen_select_calls": reader.queries,
            "selected_sources": len(selected), "merged_requirements": len(merged_requirements),
            "parsed_sources": [{"version_id": row.version.id, "materializable": row.materializable,
                "complete": row.complete, "warnings": row.warnings} for row in parsed],
            "manifest_basis": basis, "invalid_manifest_slots": invalid,
            "latest_manifest_bound_attempts": len(latest_bound),
            "profile": profile_summary(profile),
            "compiled_request": None if request is None else {
                "activation_status": request.activation_status, "activation_reasons": request.activation_reasons,
                "criteria": len(request.criteria), "review_criteria": len(request.review_criteria)},
            "stage_funnel": stage_funnel(profile, request, estimate),
            "estimate": score_summary(estimate), "source_attempts": source_rows,
            "original_source_payloads_unchanged": True}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--company-snapshot", type=Path)
    parser.add_argument("--source-map", type=Path, required=True)
    parser.add_argument("--golden", type=Path,
        help="Strict SYN-documented v1 expectations; comparison only, never company inputs")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output = local_path(args.output)
    if args.output.exists():
        parser.error("output already exists; choose a new local artifact path")
    implementation_sha256 = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "scripts/replay-quantitative-sources.py", "src/pai_loop/analysis_pipeline.py",
        "src/pai_loop/pps_enrichment.py", "src/pai_loop/extraction_contracts.py",
        "src/pai_loop/quantitative_rule_extraction.py", "src/pai_loop/quantitative_scoring.py",
        "src/pai_loop/quantitative_source_revalidation.py")}
    snapshot = load_json(args.snapshot)
    golden = load_golden(args.golden) if args.golden else None
    company = load_json(args.company_snapshot) if args.company_snapshot else None
    source_map = load_json(args.source_map) if args.source_map else {}
    if isinstance(source_map, dict) and "sources" in source_map:
        source_map = source_map["sources"]
    if isinstance(source_map, list):
        if len({row["version_id"] for row in source_map}) != len(source_map):
            raise ValueError("DUPLICATE_SOURCE_VERSION_MAPPING")
        source_map = {row["version_id"]: row for row in source_map}
    with no_external_effects() as calls:
        notices = load_notices(snapshot)
        if golden is not None and any(case.notice_key not in {notice.notice_key for notice in notices}
                                      for case in golden.cases):
            raise ValueError("GOLDEN_NOTICE_NOT_IN_SNAPSHOT")
        facts, records, company_audit = load_company(company)
        results = [replay_notice(notice, facts, records, source_map) for notice in notices]
        assert not calls, "EXTERNAL_EFFECT_ATTEMPTED"
    aggregate = {
        "notices": len(results), "selected_sources": sum(r["selected_sources"] for r in results),
        "expected_attachments": sum(len((r["manifest_basis"] or {}).get("expected_attachment_ids", [])) for r in results),
        "latest_manifest_bound_attempts": sum(r["latest_manifest_bound_attempts"] for r in results),
        "activation_counts": dict(Counter(r["estimate"]["activation_status"] for r in results)),
        "estimate_status_counts": dict(Counter(r["estimate"]["overall_status"] for r in results)),
        "profile_issue_counts": dict(sum((Counter((r["profile"] or {}).get("issue_counts", {})) for r in results), Counter())),
        "native_diagnostic_counts": dict(Counter(a["native_diagnostic"]["status"] for r in results for a in r["source_attempts"])),
        "source_contract_counts": dict(Counter(a["header_contract"] for r in results for a in r["source_attempts"])),
        "raw_source_diagnostic_counts": dict(Counter(a["raw_source_diagnostic"]["status"] for r in results for a in r["source_attempts"])),
        "raw_declared_table_attachments_diagnosed": sum(a["raw_tables"] > 0 for r in results for a in r["source_attempts"]
            if a["raw_source_diagnostic"].get("current_source_candidate")),
        "raw_declared_criteria_diagnosed": sum(a["raw_criteria"] for r in results for a in r["source_attempts"]
            if a["raw_source_diagnostic"].get("current_source_candidate")),
        "actual_source_table_presence_census_complete": False,
        "current_raw_candidate_issue_counts": dict(sum((Counter(a["raw_source_diagnostic"].get("current_source_candidate", {}).get("issue_counts", {}))
            for r in results for a in r["source_attempts"]), Counter())),
        "confirmed_total_count": sum(r["estimate"]["overall_status"] == "CONFIRMED" for r in results),
        "company_score_basis": "SUPPLIED_FROZEN_SUBSET_ONLY_NOT_LIVE_DB_EVIDENCE_AUDIT",
        "raw_candidate_comparison": raw_candidate_comparison(results),
        "stage_funnel": aggregate_stage_funnel(results),
    }
    golden_comparison = compare_golden(golden, results) if golden is not None else None
    output = {"purpose": "OFFLINE_DIAGNOSTIC_REPLAY_ONLY", "persistence_eligible": False,
        "measured_at": datetime.now(timezone.utc).isoformat(), "snapshot_sha256": digest(snapshot),
        "source_map_sha256": digest(source_map), "company_snapshot": company_audit,
        "engine_version": QUANTITATIVE_ENGINE_VERSION,
        "implementation_sha256": implementation_sha256,
        "guard_counts": dict(calls), "network_calls": 0, "provider_calls": 0, "database_connections": 0,
        "persistence_entry_called": False, "previous_contract_restamped": False,
        "limitations": ["Pure actual domain stages plus one exact frozen SELECT; execution lease, idempotency transaction and persisted API snapshot are not exercised.",
                       "Native revalidation is attachment-only diagnostic; its output is never merged into the stored production path.",
                       "No missing company evidence or contract IDs are synthesized; missing fields in this frozen subset do not establish absence from the live database."],
        "aggregate": aggregate, "golden_comparison": golden_comparison, "cases": results}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    comparison = aggregate["raw_candidate_comparison"]
    print(json.dumps({"output": str(args.output), "notices": len(results),
        "activation_counts": aggregate["activation_counts"],
        "raw_diagnostic_counts": aggregate["raw_source_diagnostic_counts"],
        "frozen_candidate_count": comparison["frozen_canonical_comparison_only"]["available_candidates"],
        "current_candidate_count": comparison["current_native_parser"]["available_candidates"],
        "golden_rates_by_expectation_basis": golden_comparison["rates_by_expectation_basis"] if golden_comparison else None,
        "external_calls": dict(calls)}, ensure_ascii=False))
    return 1 if golden_comparison is not None and not golden_comparison["all_declared_expectations_match"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
