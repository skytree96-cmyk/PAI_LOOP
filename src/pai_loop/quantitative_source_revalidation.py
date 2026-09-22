"""Revalidate one frozen supported extraction against its actual native bytes.

This local diagnostic does not create a new extraction, persist records, or
establish full-manifest coverage. Hash checks bind caller-supplied frozen inputs;
they do not independently authenticate a database or a remote source. Native to
canonical correspondence is reproduced here by the application's fixed parser,
never by a caller-provided callback or a claimed ``verified`` flag.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .extraction_contracts import (
    CURRENT_EXTRACTION_CONTRACT, PREVIOUS_EXTRACTION_CONTRACT,
    PREVIOUS_PROCESSING_CONTRACT, PREVIOUS_CASE_CONTRACT, classify_record_contract,
)
from .integrations.openai_extraction import ExtractionPayload
from .quantitative_rule_extraction import (
    QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION,
    QUANTITATIVE_CANDIDATE_PROFILE_VERSION,
    QuantitativeCandidateProfile,
    build_quantitative_candidate_profile,
)

SOURCE_REVALIDATION_VERSION = "quantitative-source-revalidation-2"
SOURCE_REVALIDATION_CONTRACTS = {
    "CURRENT": CURRENT_EXTRACTION_CONTRACT,
    "EXACT_PREVIOUS_EXTRACTION": PREVIOUS_EXTRACTION_CONTRACT,
    "EXACT_PREVIOUS_PROCESSING": PREVIOUS_PROCESSING_CONTRACT,
    "LEGACY_CASE_V2": PREVIOUS_CASE_CONTRACT,
}
_SHA = r"^[a-f0-9]{64}$"
_OLD_CASE_OPERATORS = frozenset({"GTE", "EQ", "IN", "LTE", "LT"})


def revalidation_json_sha256(value: object) -> str:
    """Canonical JSON digest for the operator's separately frozen input plan."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RevalidationOrigin(_Frozen):
    source_version_id: str = Field(min_length=1, max_length=160)
    extraction_contract: tuple[str, str, str, str]
    source_attempt_sha256: str = Field(pattern=_SHA)
    raw_result_sha256: str = Field(pattern=_SHA)
    original_record_sha256: str = Field(pattern=_SHA)
    original_validation_fingerprint_sha256: str = Field(pattern=_SHA)
    attachment_id: str
    native_sha256: str = Field(pattern=_SHA)
    manifest_sha256: str = Field(pattern=_SHA)
    manifest_attachment_ids: tuple[str, ...]
    source_authority: Literal["CALLER_SUPPLIED_FROZEN_INPUT"] = "CALLER_SUPPLIED_FROZEN_INPUT"

    @model_validator(mode="after")
    def exact_supported_contract(self) -> "RevalidationOrigin":
        if self.extraction_contract not in SOURCE_REVALIDATION_CONTRACTS.values():
            raise ValueError("SOURCE_REVALIDATION_CONTRACT_UNSUPPORTED")
        return self


class RevalidationProof(_Frozen):
    adapter_version: str = SOURCE_REVALIDATION_VERSION
    validator_version: str = QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION
    candidate_profile_version: str = QUANTITATIVE_CANDIDATE_PROFILE_VERSION
    processing_version: str
    canonical_sha256: str = Field(pattern=_SHA)
    parsed_text_sha256: str | None = Field(default=None, pattern=_SHA)
    parser_complete: bool | None = None
    parser_warning_codes: tuple[str, ...] = ()
    parser_members_discovered: int | None = None
    parser_members_processed: int | None = None
    previous_validation_reused: Literal[False] = False


class QuantitativeSourceRevalidation(_Frozen):
    purpose: Literal["QUANTITATIVE_SOURCE_REVALIDATION_ONLY"] = "QUANTITATIVE_SOURCE_REVALIDATION_ONLY"
    persistence_eligible: Literal[False] = False
    attachment_coverage_complete: Literal[False] = False
    native_canonical_status: Literal["VERIFIED", "DIAGNOSTIC_ONLY"]
    diagnostic_codes: tuple[str, ...] = ()
    origin: RevalidationOrigin
    proof: RevalidationProof
    # No manifest/document bindings are attached: this is one attachment's
    # current validation, not a complete notice or an authorized score request.
    profile: QuantitativeCandidateProfile | None
    result_fingerprint_sha256: str = Field(pattern=_SHA)

    @model_validator(mode="after")
    def preserve_diagnostic_boundary(self) -> "QuantitativeSourceRevalidation":
        verified = self.native_canonical_status == "VERIFIED"
        if verified != (self.profile is not None):
            raise ValueError("SOURCE_REVALIDATION_PROFILE_PROOF_MISMATCH")
        if verified and (self.proof.parser_complete is not True
                or self.proof.parsed_text_sha256 != self.proof.canonical_sha256
                or self.diagnostic_codes):
            raise ValueError("SOURCE_REVALIDATION_NATIVE_PROOF_INCOMPLETE")
        if self.profile is not None and (self.profile.manifest_sha256 is not None
                or self.profile.document_bindings
                or self.profile.expected_attachment_ids != (self.origin.attachment_id,)):
            raise ValueError("SOURCE_REVALIDATION_ATTACHMENT_SCOPE_REQUIRED")
        if self.result_fingerprint_sha256 != revalidation_json_sha256(
                self.model_dump(mode="json", exclude={"result_fingerprint_sha256"})):
            raise ValueError("SOURCE_REVALIDATION_RESULT_FINGERPRINT_MISMATCH")
        return self


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _old_case_vocabulary(rows: object) -> bool:
    return isinstance(rows, list) and all(isinstance(row, dict)
        and row.get("operator") in _OLD_CASE_OPERATORS
        and row.get("comparison_upper_value") is None for row in rows)


def revalidate_quantitative_source(
    *, source_version_id: str, source_attempt: Mapping[str, object],
    expected_attempt_sha256: str, native_bytes: bytes, expected_native_sha256: str,
    canonical_text: str, expected_canonical_sha256: str,
    full_manifest: list[dict[str, object]], expected_manifest_sha256: str,
) -> QuantitativeSourceRevalidation:
    """Reproduce native parsing, then validate unchanged, contract-bound raw.

    Integrity/contract errors raise stable ValueError codes. Unsupported or
    partial native parsing, or an unreproduced canonical, returns diagnostics
    with no source-verified profile. Prior validation fingerprints are preserved
    as history, not trusted or recomputed using a different validator revision.
    This function performs no network/provider/DB operations.
    """
    # Import locally: PPS enrichment itself imports the extraction/validator
    # modules. It supplies fixed, bounded readers; no parser is accepted in input.
    from .pps_enrichment import (
        PPS_PROCESSING_VERSION, PpsEnrichmentError,
        _validated_manifest_attachments, extract_pps_document_content,
    )

    _require(isinstance(native_bytes, bytes) and bool(native_bytes), "NATIVE_BYTES_REQUIRED")
    _require(isinstance(canonical_text, str) and 0 < len(canonical_text) <= 2_000_000,
             "CANONICAL_TEXT_REQUIRED_OR_TOO_LARGE")
    for expected in (expected_attempt_sha256, expected_native_sha256,
                     expected_canonical_sha256, expected_manifest_sha256):
        _require(isinstance(expected, str) and bool(re.fullmatch(_SHA, expected)), "EXPECTED_SHA256_INVALID")
    _require(revalidation_json_sha256(source_attempt) == expected_attempt_sha256, "SOURCE_ATTEMPT_SHA256_MISMATCH")
    _require(hashlib.sha256(native_bytes).hexdigest() == expected_native_sha256, "NATIVE_SHA256_MISMATCH")
    _require(hashlib.sha256(canonical_text.encode("utf-8")).hexdigest() == expected_canonical_sha256,
             "CANONICAL_SHA256_MISMATCH")
    _require(revalidation_json_sha256(full_manifest) == expected_manifest_sha256, "MANIFEST_SHA256_MISMATCH")
    # Detach input containers before inspecting or parsing them. Raw JSON is
    # retained unchanged; current-model defaults exist only in the local copy.
    attempt = json.loads(json.dumps(source_attempt, ensure_ascii=False, allow_nan=False))
    manifest = json.loads(json.dumps(full_manifest, ensure_ascii=False, allow_nan=False))
    _require(isinstance(manifest, list) and bool(manifest)
             and all(isinstance(item, dict) for item in manifest), "FULL_MANIFEST_INVALID")
    validated, invalid = _validated_manifest_attachments(manifest)
    _require(not invalid and validated == manifest
             and len({item["slot"] for item in validated}) == len(validated), "FULL_MANIFEST_INVALID")
    original_record = attempt.get("quantitative_validation_record")
    contract_kind = (classify_record_contract(attempt, original_record)
                     if isinstance(original_record, dict) else "UNSUPPORTED")
    _require(contract_kind in SOURCE_REVALIDATION_CONTRACTS,
             "SOURCE_REVALIDATION_CONTRACT_UNSUPPORTED")
    _require(attempt.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
             and attempt.get("source_kind") == "PPS_PUBLIC_ATTACHMENT"
             and attempt.get("status") in {"ACCEPTED", "REVIEW"}
             and isinstance(attempt.get("result"), dict), "PREVIOUS_RAW_EXTRACTION_REQUIRED")
    processing = attempt.get("document_processing")
    _require(isinstance(processing, dict)
             and processing.get("source_text_sha256") == expected_canonical_sha256,
             "STORED_CANONICAL_SHA256_MISMATCH")
    aid = attempt.get("attachment_id")
    attachment = next((item for item in manifest if item["attachment_id"] == aid), None)
    _require(attachment is not None, "ATTACHMENT_OUTSIDE_FULL_MANIFEST")
    _require(attempt.get("document_sha256") == expected_native_sha256
             and attempt.get("current_manifest_sha256") == expected_manifest_sha256
             and attempt.get("manifest_sha256") == revalidation_json_sha256(attachment)
             and attempt.get("source_label") == attachment["file_name"]
             and original_record.get("attachment_id") == aid
             and original_record.get("document_sha256") == expected_native_sha256
             and original_record.get("manifest_sha256") == expected_manifest_sha256,
             "SOURCE_ATTACHMENT_BINDING_MISMATCH")
    raw = attempt["result"]
    payload = ExtractionPayload.model_validate(raw)
    if contract_kind == "LEGACY_CASE_V2":
        _require(all(_old_case_vocabulary(candidate.get("cases", []))
                     for table in raw.get("quantitative_tables", []) for candidate in table["criteria"])
                 and all(_old_case_vocabulary(candidate.get("cases", []))
                         for candidate in original_record.get("available_candidates", [])),
                 "PREVIOUS_CASE_VOCABULARY_VIOLATION")
    # Check every source anchor, including raw REVIEW rows omitted by the old
    # derived record. Required-field and numeric checks remain in Pydantic.
    def anchors_belong(value: object) -> bool:
        if isinstance(value, dict):
            return ("attachment_id" not in value or value["attachment_id"] == aid) and all(
                anchors_belong(item) for item in value.values())
        return not isinstance(value, list) or all(anchors_belong(item) for item in value)
    _require(anchors_belong(raw), "RAW_ATTACHMENT_ANCHOR_MISMATCH")
    origin = RevalidationOrigin(source_version_id=source_version_id,
        extraction_contract=tuple(SOURCE_REVALIDATION_CONTRACTS[contract_kind]),
        source_attempt_sha256=expected_attempt_sha256,
        raw_result_sha256=revalidation_json_sha256(raw), original_record_sha256=revalidation_json_sha256(original_record),
        original_validation_fingerprint_sha256=original_record.get("validation_fingerprint_sha256"),
        attachment_id=aid, native_sha256=expected_native_sha256, manifest_sha256=expected_manifest_sha256,
        manifest_attachment_ids=tuple(item["attachment_id"] for item in manifest))
    proof = RevalidationProof(processing_version=PPS_PROCESSING_VERSION,
                              canonical_sha256=expected_canonical_sha256)
    diagnostics: tuple[str, ...] = ()
    profile = None
    try:
        parsed = extract_pps_document_content(attachment["file_name"], native_bytes)
    except PpsEnrichmentError:
        # Parser messages/member paths may contain private source data.
        diagnostics = ("NATIVE_PARSER_REJECTED",)
    else:
        proof = proof.model_copy(update=dict(
            parsed_text_sha256=hashlib.sha256(parsed.text.encode("utf-8")).hexdigest(),
            parser_complete=parsed.complete,
            parser_warning_codes=tuple(sorted({code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", code)
                else "PARSER_WARNING_UNCLASSIFIED" for code in parsed.warnings})),
            parser_members_discovered=parsed.members_discovered, parser_members_processed=parsed.members_processed))
        if not parsed.complete or parsed.member_issues:
            diagnostics += ("NATIVE_PARSER_INCOMPLETE",)
        if parsed.text != canonical_text:
            diagnostics += ("NATIVE_CANONICAL_MISMATCH",)
        if not parsed.analysis_content_characters:
            diagnostics += ("NATIVE_CONTENT_EMPTY",)
        if not diagnostics:
            profile = build_quantitative_candidate_profile({aid: payload}, {aid: canonical_text},
                                                           expected_attachment_ids={aid})
    data = dict(native_canonical_status="DIAGNOSTIC_ONLY" if diagnostics else "VERIFIED",
                diagnostic_codes=diagnostics, origin=origin, proof=proof, profile=profile)
    # Include fixed boundary fields in the proof without bypassing validation
    # of the result returned to a caller.
    provisional = QuantitativeSourceRevalidation.model_construct(**data)
    fingerprint = revalidation_json_sha256(provisional.model_dump(mode="json"))
    return QuantitativeSourceRevalidation(**data, result_fingerprint_sha256=fingerprint)
