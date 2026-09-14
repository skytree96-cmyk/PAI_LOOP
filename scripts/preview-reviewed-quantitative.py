"""Explicit local native/rule preview; private new output only, never persistence.

Plan v1 identifies frozen notices, full manifest/native hashes, and reviewed raw
JSON files. It accepts neither compiled rules nor company values in the plan.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("review_preview_replay_helpers", ROOT / "scripts/replay-quantitative-sources.py")
replay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = replay
_spec.loader.exec_module(replay)
from pai_loop.quantitative_review_preview import ReviewedNativeAttachment, preview_reviewed_quantitative_inputs

_SAFE_CODES = frozenset("""LOCAL_FILES_ONLY NEW_PRIVATE_OUTPUT_REQUIRED OUTPUT_INPUT_COLLISION
INPUT_CHANGED DUPLICATE_JSON_KEY NONFINITE_JSON DUPLICATE_PLAN_NOTICE_KEY PLAN_NOTICE_NOT_IN_SNAPSHOT
CURRENT_FROZEN_METADATA_REQUIRED MANIFEST_SHA256_MISMATCH EXTERNAL_EFFECT_ATTEMPTED
DUPLICATE_NOTICE_VERSION_NUMBER
EXPLICIT_DEADLINE_REQUIRED EXPLICIT_NOTICE_TIME_REQUIRED EXPECTED_MANIFEST_SHA256_INVALID
FULL_MANIFEST_INVALID EXPECTED_DOCUMENT_BINDINGS_INCOMPLETE REVIEWED_NATIVE_INPUT_REQUIRED
REVIEWED_ATTACHMENT_OUTSIDE_MANIFEST DUPLICATE_REVIEWED_ATTACHMENT NATIVE_BYTES_REQUIRED
NATIVE_SHA256_MISMATCH REVIEWED_PAYLOAD_SHA256_INVALID REVIEWED_PAYLOAD_SHA256_MISMATCH""".split())


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class AttachmentPlan(PlanModel):
    attachment_id: str = Field(min_length=1)
    native_path: str = Field(min_length=1)
    payload_path: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CasePlan(PlanModel):
    notice_key: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_documents: dict[str, str]
    attachments: list[AttachmentPlan] = Field(max_length=10)


class ReviewPlan(PlanModel):
    version: Literal["quantitative-review-preview-1"]
    cases: list[CasePlan] = Field(min_length=1, max_length=1000)


def _path(value):
    return replay.local_path(replay.local_path(value).resolve())


def _json(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def nonfinite(_value):
        raise ValueError("NONFINITE_JSON")
    return json.loads(content.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=nonfinite)


def run(args):
    output = _path(args.output)
    if not output.is_relative_to(ROOT.resolve() / ".local") or output.exists():
        raise ValueError("NEW_PRIVATE_OUTPUT_REQUIRED")
    inputs = {}
    def read(value):
        path = _path(value)
        if path == output:
            raise ValueError("OUTPUT_INPUT_COLLISION")
        content = path.read_bytes()
        if path in inputs and inputs[path] != content:
            raise ValueError("INPUT_CHANGED")
        inputs[path] = content
        return content
    snapshot = _json(read(args.snapshot))
    company = _json(read(args.company_snapshot)) if args.company_snapshot else None
    plan = ReviewPlan.model_validate(_json(read(args.review_plan)))
    if len({case.notice_key for case in plan.cases}) != len(plan.cases):
        raise ValueError("DUPLICATE_PLAN_NOTICE_KEY")
    results = []
    with replay.no_external_effects() as effects, redirect_stderr(io.StringIO()):
        notices = {notice.notice_key: notice for notice in replay.load_notices(snapshot)}
        facts, records, company_audit = replay.load_company(company)
        for case in plan.cases:
            if case.notice_key not in notices:
                raise ValueError("PLAN_NOTICE_NOT_IN_SNAPSHOT")
            notice = notices[case.notice_key]
            if len({v.version_no for v in notice.versions}) != len(notice.versions):
                raise ValueError("DUPLICATE_NOTICE_VERSION_NUMBER")
            metadata = next((v.source_payload for v in sorted(notice.versions, key=lambda v: v.version_no, reverse=True)
                if isinstance(v.source_payload, dict) and v.source_payload.get("kind") == replay.pps.PPS_METADATA_KIND), None)
            if metadata is None or metadata.get("schema_version") != replay.pps.PPS_METADATA_SCHEMA:
                raise ValueError("CURRENT_FROZEN_METADATA_REQUIRED")
            manifest = metadata.get("attachment_manifest")
            if replay.digest(manifest) != case.manifest_sha256:
                raise ValueError("MANIFEST_SHA256_MISMATCH")
            attachments = [ReviewedNativeAttachment(
                item.attachment_id, read(item.native_path), _json(read(item.payload_path)), item.payload_sha256,
            ) for item in case.attachments]
            preview = preview_reviewed_quantitative_inputs(
                full_manifest=manifest, expected_manifest_sha256=case.manifest_sha256,
                expected_documents=case.expected_documents, reviewed_attachments=attachments,
                as_of=notice.deadline, bid_notice_at=notice.published_at,
                company_facts=facts, performance_records=records,
            )
            results.append({"notice_key": case.notice_key, "local_review_preview": preview.private_report(),
                "actual_stored_estimate": replay.estimate_for_notice(notice, facts, records).model_dump(mode="json")})
    if effects:
        raise ValueError("EXTERNAL_EFFECT_ATTEMPTED")
    if any(path.read_bytes() != content for path, content in inputs.items()):
        raise ValueError("INPUT_CHANGED")
    report = {"purpose": "LOCAL_REVIEWED_QUANTITATIVE_PREVIEW", "production_eligible": False,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": replay.QUANTITATIVE_ENGINE_VERSION,
        "implementation_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("src/pai_loop/quantitative_review_preview.py", "src/pai_loop/quantitative_scoring.py",
                         "scripts/preview-reviewed-quantitative.py") if (ROOT / name).is_file()},
        "persistence_eligible": False, "source_coverage_verified": False, "company_snapshot": company_audit,
        "input_bytes_sha256": {str(path): hashlib.sha256(content).hexdigest() for path, content in inputs.items()},
        "inputs_unchanged": True, "external_effect_attempts": dict(effects), "results": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    if not _path(output).is_relative_to(ROOT.resolve() / ".local"):
        raise ValueError("NEW_PRIVATE_OUTPUT_REQUIRED")
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"cases": len(results), "preview_numeric_totals": sum(
        row["local_review_preview"]["estimate"]["estimated_points"] is not None for row in results),
        "stored_numeric_totals": sum(row["actual_stored_estimate"]["estimated_points"] is not None for row in results),
        "external_effect_attempts": 0}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "review-plan", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--company-snapshot")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (Exception, replay.ForbiddenEffect) as error:
        # Never include validation inputs, source text, parser diagnostics or paths.
        code = str(error) if type(error) is ValueError and str(error) in _SAFE_CODES else type(error).__name__
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
