"""Connect locally reviewed native-source rules to the existing company scorer.

This is an explicit, non-persistent preview producer. Frozen hashes establish
input consistency, not remote authenticity or a complete human/source audit.
Neither its result nor a successful literal check authorizes runtime activation.
It never accepts a precompiled request, caller-supplied company binding, or old
validation record as source proof.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
from pathlib import PurePath
import re

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.models import CompanyFact, CompanyPerformanceRecord
from pai_loop.quantitative_rule_extraction import (
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QuantitativeEstimateResult,
    bind_quantitative_company_inputs,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
)
from pai_loop.quantitative_source_revalidation import revalidation_json_sha256


@dataclass(frozen=True)
class ReviewedNativeAttachment:
    attachment_id: str
    native_bytes: bytes
    # A separate frozen digest prevents accidental edits after local review.
    # It is not a signature, reviewer authentication, or an approval event.
    reviewed_payload: Mapping[str, object]
    reviewed_payload_sha256: str


@dataclass(frozen=True)
class ReviewedAttachmentCheck:
    attachment_id: str
    code: str
    native_sha256: str | None = None
    canonical_sha256: str | None = None
    reviewed_payload_sha256: str | None = None


@dataclass(frozen=True)
class ReviewedQuantitativePreview:
    manifest_sha256: str
    attachment_checks: tuple[ReviewedAttachmentCheck, ...]
    profile_status: str
    profile_issue_codes: tuple[str, ...]
    estimate: QuantitativeEstimateResult

    def private_report(self) -> dict[str, object]:
        """Private output; no complete source documents, records or engine request.

        The nested estimate can contain source anchors and company references.
        It must not be used as a public API response or a persisted notice score.
        """
        from dataclasses import asdict

        return {
            "purpose": "LOCAL_REVIEWED_QUANTITATIVE_PREVIEW",
            "source_authority": "CALLER_SUPPLIED_FROZEN_INPUT",
            "persistence_eligible": False,
            "production_eligible": False,
            "source_coverage_verified": False,
            "manifest_sha256": self.manifest_sha256,
            "attachment_checks": [asdict(check) for check in self.attachment_checks],
            "profile_status": self.profile_status,
            "profile_issue_codes": list(self.profile_issue_codes),
            "estimate": self.estimate.model_dump(mode="json"),
            "limitations": [
                "Native parsing and supplied-rule literal checks do not prove omitted rules, ownership or review completeness.",
                "The nested activation/status describes this local preview, never a production notice approval.",
                "Missing company evidence remains unavailable; no values, identities or bindings are invented.",
            ],
        }


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _pdf_pages_have_text(file_name: str, native_bytes: bytes) -> bool:
    # The shared PDF parser currently skips textless pages while reporting
    # complete=True. A mixed scanned/text PDF is not a complete local review.
    # Even a blank page remains unproven here; OCR/provenance is a separate task.
    if PurePath(file_name).suffix.casefold() != ".pdf":
        return True
    from pypdf import PdfReader

    try:
        pages = PdfReader(io.BytesIO(native_bytes)).pages
        return bool(pages) and all((page.extract_text() or "").strip() for page in pages)
    except Exception:
        return False


def preview_reviewed_quantitative_inputs(
    *,
    full_manifest: list[dict[str, object]],
    expected_manifest_sha256: str,
    expected_documents: Mapping[str, str],
    reviewed_attachments: Sequence[ReviewedNativeAttachment],
    as_of: datetime,
    bid_notice_at: datetime | None = None,
    company_facts: Iterable[CompanyFact] = (),
    performance_records: Iterable[CompanyPerformanceRecord] = (),
) -> ReviewedQuantitativePreview:
    """Reparse native bytes, validate submitted rules, then use runtime resolvers.

    All expected descriptors and native hashes must come from the frozen input
    plan. A missing/unsupported native or raw rule stays visibly incomplete.
    Descriptor filenames select the fixed parser: substituting a converted PDF
    for an HWP source is not a supported provenance path. No callback, network,
    provider, database, stored extraction or rule-draft approval is consumed.
    """
    from pai_loop.pps_enrichment import (
        PpsEnrichmentError, _validated_manifest_attachments,
        extract_pps_document_content,
    )

    _require(isinstance(as_of, datetime) and as_of.utcoffset() is not None,
             "EXPLICIT_DEADLINE_REQUIRED")
    _require(bid_notice_at is None or (
        isinstance(bid_notice_at, datetime) and bid_notice_at.utcoffset() is not None
    ), "EXPLICIT_NOTICE_TIME_REQUIRED")
    _require(_sha(expected_manifest_sha256), "EXPECTED_MANIFEST_SHA256_INVALID")
    _require(isinstance(full_manifest, list) and bool(full_manifest)
             and all(isinstance(item, dict) for item in full_manifest), "FULL_MANIFEST_INVALID")
    manifest = json.loads(json.dumps(full_manifest, ensure_ascii=False, allow_nan=False))
    _require(revalidation_json_sha256(manifest) == expected_manifest_sha256,
             "MANIFEST_SHA256_MISMATCH")
    validated, invalid = _validated_manifest_attachments(manifest)
    _require(not invalid and validated == manifest
             and len({item["slot"] for item in manifest}) == len(manifest), "FULL_MANIFEST_INVALID")
    descriptors = {item["attachment_id"]: item for item in manifest}
    documents = dict(expected_documents)
    _require(set(documents) == set(descriptors) and all(_sha(v) for v in documents.values()),
             "EXPECTED_DOCUMENT_BINDINGS_INCOMPLETE")
    supplied = {}
    for item in reviewed_attachments:
        _require(isinstance(item, ReviewedNativeAttachment), "REVIEWED_NATIVE_INPUT_REQUIRED")
        _require(item.attachment_id in descriptors, "REVIEWED_ATTACHMENT_OUTSIDE_MANIFEST")
        _require(item.attachment_id not in supplied, "DUPLICATE_REVIEWED_ATTACHMENT")
        supplied[item.attachment_id] = item

    records, checks, incomplete = [], [], []
    attachment_profiles = {}
    for aid, descriptor in descriptors.items():
        item = supplied.get(aid)
        if item is None:
            incomplete.append(aid)
            checks.append(ReviewedAttachmentCheck(aid, "REVIEWED_NATIVE_INPUT_MISSING"))
            continue
        _require(isinstance(item.native_bytes, bytes) and bool(item.native_bytes), "NATIVE_BYTES_REQUIRED")
        native_sha = hashlib.sha256(item.native_bytes).hexdigest()
        _require(native_sha == documents[aid], "NATIVE_SHA256_MISMATCH")
        _require(_sha(item.reviewed_payload_sha256), "REVIEWED_PAYLOAD_SHA256_INVALID")
        raw = json.loads(json.dumps(dict(item.reviewed_payload), ensure_ascii=False, allow_nan=False))
        raw_sha = revalidation_json_sha256(raw)
        _require(raw_sha == item.reviewed_payload_sha256, "REVIEWED_PAYLOAD_SHA256_MISMATCH")
        # Reparse a detached raw mapping; do not trust a constructed Pydantic
        # instance or accept the draft module's limited SOURCE_VERIFIED label.
        payload = ExtractionPayload.model_validate(raw)
        try:
            parsed = extract_pps_document_content(descriptor["file_name"], item.native_bytes)
        except PpsEnrichmentError:
            incomplete.append(aid)
            checks.append(ReviewedAttachmentCheck(aid, "NATIVE_PARSER_REJECTED", native_sha,
                                                   reviewed_payload_sha256=raw_sha))
            continue
        canonical_sha = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()
        if not parsed.complete or parsed.member_issues or not parsed.analysis_content_characters:
            incomplete.append(aid)
            checks.append(ReviewedAttachmentCheck(aid, "NATIVE_PARSER_INCOMPLETE", native_sha,
                                                   canonical_sha, raw_sha))
            continue
        if PurePath(descriptor["file_name"]).suffix.casefold() == ".zip":
            # Per-PDF page coverage cannot be inferred from an archive-level
            # complete flag. Keep ZIP previews closed until every member has
            # its own equivalent page audit; do not silently skip nested PDFs.
            incomplete.append(aid)
            checks.append(ReviewedAttachmentCheck(aid, "NATIVE_MEMBER_TEXT_COVERAGE_UNVERIFIED",
                                                   native_sha, canonical_sha, raw_sha))
            continue
        if not _pdf_pages_have_text(descriptor["file_name"], item.native_bytes):
            incomplete.append(aid)
            checks.append(ReviewedAttachmentCheck(aid, "PDF_PAGE_TEXT_UNVERIFIED", native_sha,
                                                   canonical_sha, raw_sha))
            continue
        # Fresh transient validation of an explicit reviewed payload, not a
        # stored model record with its contract/fingerprint rewritten. Records
        # are used only in memory for the existing merge invariants, never
        # exposed by this preview producer or written into NoticeVersion.
        record = validate_quantitative_attachment_extraction(
            payload, source_text=parsed.text, attachment_id=aid,
            document_sha256=native_sha, manifest_sha256=expected_manifest_sha256,
        )
        records.append(record)
        attachment_profiles[aid] = {
            "document_type": payload.document_type, "source_label": descriptor["file_name"],
            "missing_or_unreadable": list(payload.missing_or_unreadable),
        }
        checks.append(ReviewedAttachmentCheck(aid, "NATIVE_AND_RULES_CHECKED", native_sha,
                                               canonical_sha, raw_sha))
    profile = merge_validated_quantitative_records(
        records, expected_documents=documents, manifest_sha256=expected_manifest_sha256,
        incomplete_attachment_ids=incomplete, attachment_profiles=attachment_profiles,
    )
    request = quantitative_request_from_candidate_profile(profile)
    request = bind_quantitative_company_inputs(
        request, company_facts, performance_records, as_of=as_of, bid_notice_at=bid_notice_at,
    )
    return ReviewedQuantitativePreview(
        manifest_sha256=expected_manifest_sha256, attachment_checks=tuple(checks),
        profile_status=profile.status, profile_issue_codes=tuple(sorted({i.code for i in profile.issues})),
        estimate=estimate_quantitative_score(request),
    )
