from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .award_intelligence import (
    ANALYTICS_VERSION,
    COMPETITION_RISK_VERSION,
    build_award_intelligence,
)
from .department_ranking import (
    load_department_keyword_profiles,
    rank_notice_across_departments,
)
from .eligibility_policy import (
    POLICY_VERSION,
    classify_requirements,
    expand_statutory_qualification_requirements,
    load_public_company_profile,
)
from .evaluator import (
    MIN_EXTRACTION_CONFIDENCE,
    MIN_REVIEWABLE_EXTRACTION_CONFIDENCE,
    RISK_METHOD_VERSION,
    RISK_WEIGHTS,
    RULESET_VERSION,
    EvaluationResult,
    evaluate_notice,
    fact_is_effective,
)
from .integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    EvidenceAnchor,
    ExtractedRequirement,
    ExtractionPayload,
)
from .models import (
    AnalysisRun,
    AtomicRequirement,
    AwardHistoryItem,
    CompanyFact,
    CompanyPerformanceRecord,
    Evaluation,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    ReferenceDataVersion,
    RequirementResultSnapshot,
    ScoreSnapshot,
)
from .notice_freshness import authoritative_pps_notice_is_cancelled
from .extraction_contracts import classify_attempt_header, EXTRACTION_READ_POLICY_VERSION, BOUND_PREDECESSOR_KINDS, LEGACY_CASE_KINDS
from .pricing_profiles import pricing_profile_for_document
from .quantitative_scoring import (
    QUANTITATIVE_ENGINE_VERSION,
    QUANTITATIVE_CANONICAL_FACT_KEYS,
    build_public_quantitative_criteria_snapshot,
    estimate_for_notice,
    load_quantitative_profile_catalog,
)
from .quantitative_rule_extraction import (
    QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION,
    ValidatedQuantitativeAttachmentRecord,
    validated_quantitative_record_fingerprint,
    quantitative_record_contract_is_usable,
)
from .source_gap_policy import (
    has_compound_source_absence_claim,
    is_explicit_qualitative_only_exclusion,
    is_explicit_non_quantitative_notice_schedule_gap,
    is_explicit_qualitative_table_local_absence,
    is_quantitative_irrelevant_gap,
    normalise_source_gap,
    quantitative_table_local_absence_targets,
    source_label_document_types,
)
from .pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_METADATA_SCHEMA,
    PPS_PROCESSING_VERSION,
    _validated_manifest_attachments,
    _current_manifest_attempts,
    _has_valid_quantitative_record,
)


PIPELINE_VERSION = "analysis-pipeline-0.6.6"
MATERIALIZATION_VERSION = "atomic-materializer-0.3.1"
SNAPSHOT_VERSION = "analysis-snapshot-0.3.0"
SOURCE_KIND = "OPENAI_REQUIREMENT_EXTRACTION"
MATERIALIZED_KIND = "ANALYSIS_PIPELINE_MATERIALIZATION"
RUN_KIND = "FULL_REVIEW"
_LEGACY_PUBLIC_PROMPT_VERSION = "pai-loop-extraction-0.2.1"
_LEGACY_PUBLIC_SCHEMA_VERSION = "pai-loop-requirements-0.1.0"


def _is_legacy_curated_public_source(
    payload: Mapping[str, Any],
    *,
    prompt_version: str,
) -> bool:
    """Allow only the exact reviewed demo seed after the live schema bump."""

    return (
        payload.get("source_kind") == "PUBLIC_NOTICE"
        and payload.get("classification") == "PUBLIC_PROCUREMENT_DERIVED"
        and payload.get("prompt_version") == _LEGACY_PUBLIC_PROMPT_VERSION
        and payload.get("schema_version") == _LEGACY_PUBLIC_SCHEMA_VERSION
        and prompt_version == PROMPT_VERSION
    )

_PASS_RULE_BY_CATEGORY = {
    "ENTITY": "P-ENTITY",
    "INDUSTRY_CODE": "P-QUAL-CODE-ANY",
    "CERTIFICATION": "P-CERT-DIRECT",
    "DIRECT_PRODUCTION": "P-CERT-DIRECT",
    "REGION": "P-REGION",
    "PERFORMANCE": "P-PERFORMANCE",
    "PERSONNEL": "P-RESOURCE",
    "FACILITY": "P-EDU-FACILITY",
    "CONSORTIUM": "P-CONSORTIUM",
    "SANCTION": "P-SANCTION-CLEAR",
    "SUBMISSION": "P-DOCUMENT",
    "OTHER": "P-DOCUMENT",
}

_ELIGIBILITY_GAP_TERMS = (
    "참가자격",
    "입찰참가",
    "자격요건",
    "면허",
    "사업자등록",
    "제조물품 등록",
    "공급물품 등록",
    "세부품명 등록",
    "직접생산",
    "지역제한",
    "주된 영업소",
    "본점 소재지",
    "공동수급",
    "컨소시엄",
    "부정당",
    "제재",
    "업종",
    "중소",
    "소기업",
    "결격",
)
_KNOWN_NON_ELIGIBILITY_GAP_TERMS = (
    "가격",
    "예산",
    "원가",
    "산출내역",
    "평가배점",
    "평점산식",
    "과업",
    "사업내용",
    "성과물",
    "계약",
    "일정",
    "서식",
    "도면",
    "이미지",
    "목차",
    "정량",
    "평가표",
    "배점표",
    "평가항목",
    "평가 항목",
    "공고번호",
    "품명",
    "수량",
    "납품",
    "설치",
    "규격",
    "사양",
    "시방",
    "페이지",
    "해상도",
    "운영체제",
)
_BLOCKING_ACTION_GAP_TERMS = (
    "제안설명회",
    "현장설명",
    "설명회",
    "참여",
    "참석",
    "발표",
    "제출",
    "방문",
    "접수",
    "마감",
    "서명",
    "날인",
    "서약",
)


class AnalysisPipelineError(RuntimeError):
    """Base error for the deterministic materialisation boundary."""


class AnalysisPipelineTransactionError(AnalysisPipelineError):
    """Raised when a caller attempts to share an already-open transaction."""


class AnalysisPipelineSourceError(AnalysisPipelineError):
    """Raised when an explicit source selection is invalid."""


@dataclass(slots=True, frozen=True)
class AnalysisPipelineResult:
    analysis_run_id: str
    notice_id: str
    notice_version_id: str
    evaluation_id: str
    idempotency_key: str
    input_sha256: str
    status: str
    reused: bool
    source_count: int
    accepted_source_count: int
    materialized_requirement_count: int
    requirement_snapshot_count: int
    score_snapshot_count: int
    recommendation_snapshot_count: int
    eligibility: str
    reason_code: str
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class _SourceDocument:
    version: NoticeVersion
    attachment_id: str
    document_sha256: str
    prompt_version: str
    schema_version: str
    status: str
    model_name: str | None
    error_code: str | None
    data: ExtractionPayload | None
    result_sha256: str
    materializable: bool
    complete: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _MergedRequirement:
    requirement_key: str
    requirement: ExtractedRequirement
    source_document_sha256s: set[str] = field(default_factory=set)
    attachment_ids: set[str] = field(default_factory=set)
    anchors: list[EvidenceAnchor] = field(default_factory=list)
    source_confidences: list[float] = field(default_factory=list)


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalise_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _attachment_identity(payload: dict[str, Any], version: NoticeVersion) -> str:
    explicit = payload.get("attachment_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    anchor_ids: set[str] = set()
    result = payload.get("result")
    if isinstance(result, dict):
        for requirement in result.get("requirements", []):
            if not isinstance(requirement, dict):
                continue
            for anchor in requirement.get("evidence", []):
                if not isinstance(anchor, dict):
                    continue
                attachment_id = anchor.get("attachment_id")
                if isinstance(attachment_id, str) and attachment_id.strip():
                    anchor_ids.add(attachment_id.strip())
    if len(anchor_ids) == 1:
        return next(iter(anchor_ids))
    if anchor_ids:
        return f"multi:{_digest(sorted(anchor_ids))[:32]}"
    return f"document:{version.file_sha256}"


def _select_source_versions(
    session: Session,
    *,
    notice_id: str,
    prompt_version: str,
    source_version_ids: Sequence[str] | None,
) -> list[NoticeVersion]:
    statement = (
        select(NoticeVersion)
        .where(NoticeVersion.notice_id == notice_id)
        .order_by(NoticeVersion.version_no)
    )
    versions = list(session.scalars(statement).all())
    requested = set(source_version_ids or ())
    if source_version_ids is not None:
        if len(requested) != len(source_version_ids):
            raise AnalysisPipelineSourceError("source_version_ids must be unique")
        found = {version.id for version in versions}
        missing = requested - found
        if missing:
            raise AnalysisPipelineSourceError(
                "one or more source versions do not belong to the notice"
            )
        versions = [version for version in versions if version.id in requested]
    else:
        # Automatic PPS analysis is bound to the latest attachment manifest.
        # Historical attachment IDs remain in the audit table but must not be
        # merged after a correction replaces/removes a document. Explicit
        # source_version_ids intentionally retain the audit override.
        latest_metadata = next(
            (
                version
                for version in reversed(versions)
                if isinstance(version.source_payload, dict)
                and version.source_payload.get("kind") == "PPS_NOTICE_METADATA"
            ),
            None,
        )
        if latest_metadata is not None:
            metadata_schema_current = (
                latest_metadata.source_payload.get("schema_version")
                == PPS_METADATA_SCHEMA
            )
            stored_manifest = latest_metadata.source_payload.get("attachment_manifest")
            raw_manifest_values = stored_manifest if isinstance(stored_manifest, list) else []
            raw_manifest = [
                dict(item)
                for item in raw_manifest_values
                if isinstance(item, dict)
            ]
            current_manifest_sha256 = _digest(raw_manifest_values)
            allowed_pps_sources = {
                str(item.get("attachment_id")): _digest(item)
                for item in raw_manifest
                if metadata_schema_current
                and isinstance(item, dict)
                and isinstance(item.get("attachment_id"), str)
            }
            versions = [
                version
                for version in versions
                if not (
                    isinstance(version.source_payload, dict)
                    and version.source_payload.get("kind") == SOURCE_KIND
                    and version.source_payload.get("source_kind") == "PPS_PUBLIC_ATTACHMENT"
                )
                or (
                    version.source_payload.get("attachment_id") in allowed_pps_sources
                    and version.source_payload.get("manifest_sha256")
                    == allowed_pps_sources.get(version.source_payload.get("attachment_id"))
                    and version.source_payload.get("current_manifest_sha256")
                    == current_manifest_sha256
                )
            ]

    latest_pps_numbers: dict[str, int] = {}
    latest_new_processing_numbers: dict[str, int] = {}
    for version in versions:
        payload = version.source_payload
        if isinstance(payload, dict) and payload.get("kind") == SOURCE_KIND and payload.get("source_kind") == PPS_ATTACHMENT_SOURCE:
            aid = _attachment_identity(payload, version)
            latest_pps_numbers[aid] = max(latest_pps_numbers.get(aid, -1), version.version_no)
            if classify_attempt_header(payload) in {"CURRENT", "UNSUPPORTED"}:
                latest_new_processing_numbers[aid] = max(
                    latest_new_processing_numbers.get(aid, -1), version.version_no,
                )
    latest_by_attachment: dict[str, NoticeVersion] = {}
    for version in versions:
        payload = version.source_payload
        if not isinstance(payload, dict) or payload.get("kind") != SOURCE_KIND:
            if source_version_ids is not None and version.id in requested:
                raise AnalysisPipelineSourceError(
                    "an explicitly selected version is not an OpenAI requirement extraction"
                )
            continue
        legacy_curated_public = _is_legacy_curated_public_source(
            payload,
            prompt_version=prompt_version,
        )
        if (
            payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
            and classify_attempt_header(payload) in LEGACY_CASE_KINDS
            and version.version_no < latest_pps_numbers.get(_attachment_identity(payload, version), -1)
        ):
            # Even an unsupported newer header prevents legacy-success fallback.
            continue
        if (
            payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
            and classify_attempt_header(payload) == "EXACT_PREVIOUS_PROCESSING"
            and version.version_no < latest_new_processing_numbers.get(_attachment_identity(payload, version), -1)
        ):
            # Preserve the old modern generation only until a new parser
            # generation supersedes it, including an invalid new attempt.
            continue
        compatible_pps = (
            prompt_version == PROMPT_VERSION
            and payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
            and classify_attempt_header(payload) != "UNSUPPORTED"
        )
        if payload.get("prompt_version") != prompt_version and not (legacy_curated_public or compatible_pps):
            if source_version_ids is not None and version.id in requested:
                raise AnalysisPipelineSourceError(
                    "an explicitly selected source has a different prompt version"
                )
            continue
        if (
            payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
            and payload.get("processing_version") != PPS_PROCESSING_VERSION
            and not compatible_pps
        ):
            if source_version_ids is not None and version.id in requested:
                raise AnalysisPipelineSourceError(
                    "an explicitly selected PPS source has a different processing version"
                )
            continue
        attachment_id = _attachment_identity(payload, version)
        if payload.get("source_kind") == PPS_ATTACHMENT_SOURCE:
            contract_kind = classify_attempt_header(payload)
            if contract_kind == "UNSUPPORTED" or (
                contract_kind in BOUND_PREDECESSOR_KINDS
                and payload.get("status") == "ACCEPTED"
                and not _has_valid_quantitative_record(
                    version, attachment_id=attachment_id,
                    current_manifest_sha256=str(payload.get("current_manifest_sha256") or ""),
                )
            ):
                if source_version_ids is not None and version.id in requested:
                    raise AnalysisPipelineSourceError(
                        "an explicitly selected PPS source lacks a supported extraction proof"
                    )
                continue
        previous = latest_by_attachment.get(attachment_id)
        if previous is None or previous.version_no < version.version_no:
            latest_by_attachment[attachment_id] = version
    return sorted(
        latest_by_attachment.values(),
        key=lambda item: (_attachment_identity(item.source_payload or {}, item), item.version_no),
    )


def _parse_source(
    version: NoticeVersion, *, prompt_version: str,
    allow_compatible_pps: bool = False,
) -> _SourceDocument:
    payload = version.source_payload if isinstance(version.source_payload, dict) else {}
    attachment_id = _attachment_identity(payload, version)
    document_sha256 = str(payload.get("document_sha256") or version.file_sha256).casefold()
    stored_prompt = str(payload.get("prompt_version") or "")
    schema_version = str(payload.get("schema_version") or "")
    status = str(payload.get("status") or version.extraction_status or "REVIEW").upper()
    model_name = payload.get("model") if isinstance(payload.get("model"), str) else None
    error_code = payload.get("error_code") if isinstance(payload.get("error_code"), str) else None
    warnings: list[str] = []
    legacy_curated_public = _is_legacy_curated_public_source(
        payload,
        prompt_version=prompt_version,
    )

    compatible_pps = bool(
        allow_compatible_pps and prompt_version == PROMPT_VERSION
        and payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
        and classify_attempt_header(payload) in BOUND_PREDECESSOR_KINDS
        and _has_valid_quantitative_record(
            version, attachment_id=attachment_id,
            current_manifest_sha256=str(payload.get("current_manifest_sha256") or ""),
        )
    )
    if document_sha256 != version.file_sha256.casefold():
        warnings.append("DOCUMENT_SHA_MISMATCH")
    if stored_prompt != prompt_version and not (legacy_curated_public or compatible_pps):
        warnings.append("PROMPT_VERSION_MISMATCH")
    if schema_version != SCHEMA_VERSION and not (legacy_curated_public or compatible_pps):
        warnings.append("UNSUPPORTED_SCHEMA_VERSION")
    if (
        payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
        and payload.get("processing_version") != PPS_PROCESSING_VERSION
        and not compatible_pps
    ):
        warnings.append("PROCESSING_VERSION_MISMATCH")
    if status != "ACCEPTED" or version.extraction_status not in {"ACCEPTED", "COMPLETE"}:
        warnings.append("SOURCE_STATUS_NOT_ACCEPTED")

    raw_result = payload.get("result")
    data: ExtractionPayload | None = None
    result_sha256 = _digest(raw_result) if isinstance(raw_result, dict) else _digest(None)
    if isinstance(raw_result, dict):
        # The curated public fixture predates this optional publication field.
        # Its NoticeVersion.document_complete flag is the reviewed source of
        # truth, so an omitted list is normalised to empty without inventing
        # any extracted requirement or evidence.
        normalized_result = copy.deepcopy(raw_result)
        normalized_result.setdefault("missing_or_unreadable", [])
        if legacy_curated_public:
            # This historical public fixture predates quantitative-table
            # extraction. Empty fields mean "not captured", not verified N/A.
            normalized_result.setdefault("quantitative_tables", [])
            normalized_result.setdefault("quantitative_table_not_applicable", None)
        try:
            data = ExtractionPayload.model_validate(normalized_result)
        except ValidationError:
            warnings.append("INVALID_EXTRACTION_PAYLOAD")
    else:
        warnings.append("MISSING_EXTRACTION_PAYLOAD")

    anchors_complete = True
    anchor_identity_ok = True
    if data is not None:
        for requirement in data.requirements:
            if not requirement.evidence:
                anchors_complete = False
            explicit_attachment = payload.get("attachment_id")
            if isinstance(explicit_attachment, str) and explicit_attachment.strip():
                if any(
                    anchor.attachment_id != explicit_attachment.strip()
                    for anchor in requirement.evidence
                ):
                    anchor_identity_ok = False
        if data.missing_or_unreadable:
            warnings.append("SOURCE_MISSING_OR_UNREADABLE")
    if not anchors_complete:
        warnings.append("MISSING_EVIDENCE_ANCHOR")
    if not anchor_identity_ok:
        warnings.append("EVIDENCE_ATTACHMENT_MISMATCH")
    if not version.document_complete:
        warnings.append("DOCUMENT_INCOMPLETE")
    if version.extraction_confidence < 0.90:
        warnings.append("LOW_EXTRACTION_CONFIDENCE")

    materializable = (
        data is not None
        and status == "ACCEPTED"
        and version.extraction_status in {"ACCEPTED", "COMPLETE"}
        and document_sha256 == version.file_sha256.casefold()
        and (stored_prompt == prompt_version or legacy_curated_public or compatible_pps)
        and (schema_version == SCHEMA_VERSION or legacy_curated_public or compatible_pps)
        and anchor_identity_ok
    )
    complete = (
        materializable
        and version.document_complete
        and version.extraction_confidence >= 0.90
        and anchors_complete
        and not data.missing_or_unreadable
    )
    return _SourceDocument(
        version=version,
        attachment_id=attachment_id,
        document_sha256=document_sha256,
        prompt_version=stored_prompt,
        schema_version=schema_version,
        status=status,
        model_name=model_name,
        error_code=error_code,
        data=data,
        result_sha256=result_sha256,
        materializable=materializable,
        complete=complete,
        warnings=sorted(set(warnings)),
    )


def _requirement_fingerprint(requirement: ExtractedRequirement) -> str:
    return _digest(
        {
            "category": requirement.category,
            "logic": requirement.logic,
            "condition": _normalise_text(requirement.normalized_condition),
            "mandatory": requirement.mandatory,
            "deadline_basis": _normalise_text(requirement.deadline_basis),
        }
    )


def _merge_requirements(sources: Sequence[_SourceDocument]) -> list[_MergedRequirement]:
    merged: dict[str, _MergedRequirement] = {}
    for source in sorted(sources, key=lambda item: (item.attachment_id, item.document_sha256)):
        if not source.materializable or source.data is None:
            continue
        for requirement in sorted(
            source.data.requirements,
            key=lambda item: (_requirement_fingerprint(item), item.requirement_id),
        ):
            fingerprint = _requirement_fingerprint(requirement)
            requirement_key = f"ai-{fingerprint[:32]}"
            item = merged.get(fingerprint)
            if item is None:
                item = _MergedRequirement(
                    requirement_key=requirement_key,
                    requirement=requirement,
                )
                merged[fingerprint] = item
            item.source_document_sha256s.add(source.document_sha256)
            item.attachment_ids.add(source.attachment_id)
            item.anchors.extend(requirement.evidence)
            item.source_confidences.append(source.version.extraction_confidence)
    return [merged[key] for key in sorted(merged)]


def _policy_items(
    merged: Sequence[_MergedRequirement],
    *,
    notice: Notice,
    profile: dict[str, Any],
) -> list[tuple[_MergedRequirement, dict[str, Any]]]:
    expanded: list[_MergedRequirement] = []
    for item in merged:
        original = {**item.requirement.model_dump(mode="json"), "requirement_id": item.requirement_key}
        parts = expand_statutory_qualification_requirements([original])
        for part in parts:
            if part is original:
                expanded.append(item)
            else:
                expanded.append(replace(
                    item,
                    requirement_key=part["requirement_id"],
                    requirement=ExtractedRequirement.model_validate(part),
                ))
    merged = expanded
    requirements = [
        {
            **item.requirement.model_dump(mode="json"),
            "requirement_id": item.requirement_key,
        }
        for item in merged
    ]
    classified = classify_requirements(
        requirements,
        profile=profile,
        deadline=notice.deadline,
    )
    classified_items = classified["items"]
    expected_keys = [item.requirement_key for item in merged]
    actual_keys = [str(item.get("requirement_id")) for item in classified_items]
    if actual_keys != expected_keys or len(set(actual_keys)) != len(actual_keys):
        raise AnalysisPipelineSourceError("Requirement policy expansion changed source pairing")
    return list(zip(merged, classified_items, strict=True))


def _source_location(item: _MergedRequirement) -> str | None:
    locations = []
    for anchor in sorted(
        item.anchors,
        key=lambda value: (
            value.attachment_id,
            value.page or 0,
            value.section or "",
            value.quote,
        ),
    ):
        location = anchor.attachment_id
        if anchor.page is not None:
            location += f"#page={anchor.page}"
        if anchor.section:
            location += f":{anchor.section}"
        if location not in locations:
            locations.append(location)
    joined = " | ".join(locations)
    return joined[:255] or None


def _source_excerpt(item: _MergedRequirement) -> str | None:
    quotes = sorted({anchor.quote.strip() for anchor in item.anchors if anchor.quote.strip()})
    return quotes[0][:500] if quotes else None


def _parse_confidence(item: _MergedRequirement) -> float:
    # A document-level mean describes the attachment, not every extracted
    # condition.  Using it in the per-requirement minimum allowed one unrelated
    # low-confidence clause to turn otherwise exact anchors into R07.  Prefer
    # the requirement's own anchors and retain the source value only as a
    # legacy fallback for an unanchored curated record.
    anchor_values = [anchor.confidence for anchor in item.anchors]
    values = anchor_values or item.source_confidences
    return min(values) if values else 0.0


def _has_anchor_at_confidence(
    item: _MergedRequirement,
    *,
    minimum_confidence: float,
) -> bool:
    """Trust only exact anchors already accepted by the extraction boundary."""

    if (
        not item.anchors
        or not item.attachment_ids
        or not item.source_document_sha256s
        or _parse_confidence(item) < minimum_confidence
    ):
        return False
    return all(
        bool(anchor.quote.strip())
        and anchor.attachment_id in item.attachment_ids
        and anchor.confidence >= minimum_confidence
        for anchor in item.anchors
    )


def _has_verified_anchor(item: _MergedRequirement) -> bool:
    return _has_anchor_at_confidence(
        item,
        minimum_confidence=MIN_EXTRACTION_CONFIDENCE,
    )


def _has_reviewable_anchor(item: _MergedRequirement) -> bool:
    return _has_anchor_at_confidence(
        item,
        minimum_confidence=MIN_REVIEWABLE_EXTRACTION_CONFIDENCE,
    )


def _known_non_eligibility_gaps_only(sources: Sequence[_SourceDocument]) -> bool:
    """Allow a partial gate only for explicitly described non-eligibility gaps.

    Unknown, empty, or eligibility-adjacent descriptions fail closed.  A
    rejected/unmaterializable attachment also fails closed because its omitted
    content cannot be classified safely.
    """

    if not sources or any(not source.materializable or source.data is None for source in sources):
        return False
    gaps, _resolved = _aggregate_source_gaps(sources)
    if not gaps or any(not gap for gap in gaps):
        return False
    return all(
        not any(term in gap for term in _ELIGIBILITY_GAP_TERMS)
        and not any(term in gap for term in _BLOCKING_ACTION_GAP_TERMS)
        and any(term in gap for term in _KNOWN_NON_ELIGIBILITY_GAP_TERMS)
        for gap in gaps
    )


def _current_complete_pps_evidence(
    sources: Sequence[_SourceDocument],
    manifest_basis: dict[str, Any] | None,
) -> bool:
    """Prove full source coverage without treating its mean as every clause's confidence.

    This capability is limited to the exact current PPS attempts whose complete
    extraction records already passed manifest, document and validator checks.
    An unrelated weak anchor remains weak; it cannot revoke an independently
    verified mandatory clause, nor can this path fill any missing source.
    """

    if not sources or manifest_basis is None or not manifest_basis["coverage_complete"]:
        return False
    expected = manifest_basis["expected_attachment_ids"]
    if (
        not expected
        or manifest_basis["accepted_attachment_ids"] != expected
        or {source.version.id for source in sources}
        != set(manifest_basis["selected_attempt_ids"])
    ):
        return False
    complete = all(
        source.materializable
        and source.version.document_complete
        and source.data is not None
        and not source.data.missing_or_unreadable
        and not (set(source.warnings) - {"LOW_EXTRACTION_CONFIDENCE"})
        for source in sources
    )
    return complete and any(
        source.version.extraction_confidence < MIN_EXTRACTION_CONFIDENCE
        or any(
            anchor.confidence < MIN_EXTRACTION_CONFIDENCE
            for requirement in source.data.requirements
            for anchor in requirement.evidence
        )
        for source in sources
    )


def _partial_gate_candidate_keys(
    *,
    run_status: str,
    sources: Sequence[_SourceDocument],
    policy_items: Sequence[tuple[_MergedRequirement, dict[str, Any]]],
    pps_manifest_basis: dict[str, Any] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    complete_pps_evidence = (
        run_status in {"COMPLETED", "PARTIAL"}
        and _current_complete_pps_evidence(sources, pps_manifest_basis)
    )
    if not complete_pps_evidence and (
        run_status != "PARTIAL" or not _known_non_eligibility_gaps_only(sources)
    ):
        return frozenset(), frozenset()

    eligibility_items = [
        item
        for item, policy in policy_items
        if item.requirement.mandatory and policy.get("policy_class") == "ELIGIBILITY"
    ]
    anchor_check = _has_verified_anchor if complete_pps_evidence else _has_reviewable_anchor
    if not eligibility_items or not all(anchor_check(item) for item in eligibility_items):
        return frozenset(), frozenset()

    verified_materialized_keys = frozenset(
        item.requirement_key
        for item, policy in policy_items
        if _is_materialized_policy_item(item, policy) and anchor_check(item)
    )
    return verified_materialized_keys, frozenset(
        item.requirement_key for item in eligibility_items
    )


def _bounded_axis(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number > 100:
        return None
    return round(number, 2)


def _derive_risk_dimensions(
    *,
    notice: Notice,
    evaluation: EvaluationResult,
    policy_items: Sequence[tuple[_MergedRequirement, dict[str, Any]]],
    sources: Sequence[_SourceDocument],
    run_status: str,
    eligibility_gate_applied: bool,
    award_intelligence: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Build auditable risk axes without model-generated direct scores."""

    dimensions: dict[str, float] = {}
    basis: dict[str, Any] = {}
    eligibility_items = [
        item
        for item, policy in policy_items
        if item.requirement.mandatory and policy.get("policy_class") == "ELIGIBILITY"
    ]
    no_blocking_requirements_verified = (
        evaluation.reason_code == "NO_BLOCKING_REQUIREMENTS"
    )
    if eligibility_items or no_blocking_requirements_verified:
        qualification = {"PASS": 10.0, "REVIEW": 60.0, "FAIL": 100.0}[
            evaluation.eligibility.value
        ]
        dimensions["qualification"] = qualification
        basis["qualification"] = {
            "source": "DETERMINISTIC_ELIGIBILITY_EVALUATION",
            "method": "PASS=10; REVIEW=60; FAIL=100",
            "requirement_count": len(eligibility_items),
            "outcome": evaluation.eligibility.value,
            "no_blocking_requirements_verified": no_blocking_requirements_verified,
        }

        execution = round(max(0.0, min(100.0, 100.0 - evaluation.readiness_score)), 2)
        dimensions["execution"] = execution
        basis["execution"] = {
            "source": "DETERMINISTIC_READINESS_EVALUATION",
            "method": "max(0, 100 - readiness_score)",
            "readiness_score": evaluation.readiness_score,
        }

    if sources:
        minimum_confidence = min(source.version.extraction_confidence for source in sources)
        document = (
            round(max(0.0, 100.0 - minimum_confidence * 100.0), 2)
            if run_status == "COMPLETED"
            else 100.0
        )
        dimensions["document"] = document
        basis["document"] = {
            "source": "VALIDATED_EXTRACTION_STATUS",
            "method": (
                "max(0, 100 - minimum_extraction_confidence*100) when COMPLETED; "
                "otherwise 100"
            ),
            "run_status": run_status,
            "source_count": len(sources),
            "minimum_extraction_confidence": round(minimum_confidence, 4),
        }

    atomic_by_key = {
        str(item.get("requirement_key")): item for item in evaluation.atomic_results
    }
    action_items = [
        item
        for item, policy in policy_items
        if item.requirement.mandatory and policy.get("policy_class") == "ACTION_REQUIRED"
    ]
    checklist_count = sum(
        item.requirement.mandatory and policy.get("policy_class") == "CHECKLIST"
        for item, policy in policy_items
    )
    incomplete_action_count = sum(
        atomic_by_key.get(item.requirement_key, {}).get("result") != "PASS"
        for item in action_items
    )
    if policy_items and (run_status == "COMPLETED" or eligibility_gate_applied):
        operation = float(min(100, incomplete_action_count * 20 + checklist_count * 5))
        dimensions["operation"] = operation
        basis["operation"] = {
            "source": "MATERIALIZED_POLICY_REQUIREMENTS",
            "method": "min(100, incomplete_ACTION_REQUIRED*20 + mandatory_CHECKLIST*5)",
            "action_required_count": len(action_items),
            "incomplete_action_count": incomplete_action_count,
            "mandatory_checklist_count": checklist_count,
        }

    competition = award_intelligence.get("competition_risk")
    if isinstance(competition, dict):
        competition_score = _bounded_axis(competition.get("score"))
        if competition_score is not None and competition.get("status") == "MODEL_ESTIMATE":
            dimensions["competition"] = competition_score
            coverage = competition.get("coverage")
            basis["competition"] = {
                "source": "STORED_3Y_AWARD_HISTORY",
                "method": str(competition.get("method") or ""),
                "method_version": str(
                    competition.get("method_version") or COMPETITION_RISK_VERSION
                ),
                "sample_count": int(competition.get("sample_count") or 0),
                "coverage_sufficient": bool(
                    isinstance(coverage, dict) and coverage.get("sufficient")
                ),
            }

    prediction = award_intelligence.get("prediction")
    award_rate = prediction.get("award_rate") if isinstance(prediction, dict) else None
    if isinstance(award_rate, dict) and award_rate.get("status") == "MODEL_ESTIMATE":
        center = award_rate.get("center")
        if isinstance(center, (int, float)) and not isinstance(center, bool) and math.isfinite(float(center)):
            profitability = round(max(0.0, min(100.0, 100.0 - float(center))), 2)
            dimensions["profitability"] = profitability
            basis["profitability"] = {
                "source": "STORED_3Y_AWARD_RATE_PREDICTION",
                "method": "max(0, min(100, 100 - predicted_award_rate_center))",
                "prediction_method": str(award_rate.get("method") or ""),
                "sample_count": int(award_rate.get("sample_count") or 0),
                "award_rate_center": round(float(center), 4),
            }

    manual = notice.risk_dimensions if isinstance(notice.risk_dimensions, dict) else {}
    for key in RISK_WEIGHTS:
        value = _bounded_axis(manual.get(key))
        if value is None:
            continue
        dimensions[key] = value
        basis[key] = {
            "source": "NOTICE_RISK_DIMENSIONS_AUTHORITATIVE_OVERRIDE",
            "method": "validated explicit 0..100 axis override",
        }

    return dimensions, basis


def _is_materialized_policy_item(item: _MergedRequirement, policy: dict[str, Any]) -> bool:
    return bool(item.requirement.mandatory) and policy.get("policy_class") in {
        "ELIGIBILITY",
        "ACTION_REQUIRED",
    }


def _atomic_requirement(
    item: _MergedRequirement,
    policy: dict[str, Any],
    *,
    sequence: int,
) -> AtomicRequirement:
    policy_class = str(policy["policy_class"])
    category = item.requirement.category
    if policy_class == "ACTION_REQUIRED":
        fact_key = f"action.{item.requirement_key[3:27]}.confirmed"
    else:
        mapped_fact = policy.get("evaluation_fact_key") or policy.get("company_fact_key")
        fact_key = (
            str(mapped_fact)
            if isinstance(mapped_fact, str) and mapped_fact.strip()
            else f"eligibility.{category.casefold()}.{item.requirement_key[3:19]}"
        )
    ambiguous = bool(item.requirement.ambiguity_reason)
    linked_review_code = "R05" if ambiguous else "R04"
    return AtomicRequirement(
        requirement_key=item.requirement_key,
        group_key=f"G-{item.requirement_key[3:27]}",
        path_key="PATH-PRIMARY",
        sequence=sequence,
        label=item.requirement.normalized_condition[:500],
        fact_key=fact_key[:120],
        operator=str(policy.get("operator") or "eq"),
        required_value=policy.get("required_value", True),
        # The reviewed public policy explicitly distinguishes certificate-backed
        # facts from a current company declaration. Requiring an Evidence row for
        # the latter would incorrectly turn conviction_clear into R04 even though
        # its policy is PASS-current plus a pre-submission reconfirmation.
        evidence_required=(
            policy_class == "ELIGIBILITY" and bool(policy.get("evidence"))
        ),
        mandatory=True,
        pass_rule_id=_PASS_RULE_BY_CATEGORY.get(category, "P-DOCUMENT"),
        linked_review_code=linked_review_code,
        review_trigger_value=policy.get("review_trigger_value", "__MISSING__"),
        parse_confidence=_parse_confidence(item),
        source_excerpt=_source_excerpt(item),
        source_location=_source_location(item),
        active=True,
    )


def _selected_fact_manifest(
    company_facts: Sequence[CompanyFact],
    *,
    fact_keys: set[str],
    deadline: datetime,
) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    for fact in company_facts:
        if fact.fact_key not in fact_keys or not fact_is_effective(fact, deadline):
            continue
        evidence = fact.evidence
        basis_sha256 = _digest(
            {
                "fact_key": fact.fact_key,
                "value": fact.value,
                "effective_from": fact.effective_from,
                "effective_to": fact.effective_to,
                "verified": fact.verified,
                "source": fact.source,
                "evidence_id": fact.evidence_id,
                "evidence_status": evidence.status if evidence else None,
                "evidence_sha256": evidence.sha256 if evidence else None,
                "evidence_issued_at": evidence.issued_at if evidence else None,
                "evidence_valid_from": evidence.valid_from if evidence else None,
                "evidence_valid_until": evidence.valid_until if evidence else None,
            }
        )
        manifest.append({"company_fact_id": fact.id, "basis_sha256": basis_sha256})
    return sorted(manifest, key=lambda item: (item["company_fact_id"], item["basis_sha256"]))


def _safe_actual_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    return "[REDACTED_NON_BOOLEAN_FACT]"


def _sanitized_evaluation_payload(
    atomic_results: Sequence[dict[str, Any]],
    explanation: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    safe_atomics = []
    for result in atomic_results:
        safe = copy.deepcopy(result)
        safe["actual_value"] = _safe_actual_value(safe.get("actual_value"))
        safe_atomics.append(safe)
    safe_explanation = copy.deepcopy(explanation)
    for item in safe_explanation.get("default_fail_details", []):
        if isinstance(item, dict):
            item["current_value"] = _safe_actual_value(item.get("current_value"))
    return safe_atomics, safe_explanation


def _confidence_value(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(max(0.0, min(1.0, float(value))), 4)
    return {
        "HIGH": 0.9,
        "MEDIUM": 0.7,
        "LOW": 0.4,
        "INSUFFICIENT": 0.0,
    }.get(str(value or "").upper(), 0.0)


def _active_reference_manifest(session: Session) -> tuple[list[dict[str, str]], dict[str, str]]:
    rows = list(
        session.scalars(
            select(ReferenceDataVersion)
            .where(ReferenceDataVersion.status == "ACTIVE")
            .order_by(ReferenceDataVersion.dataset_key, ReferenceDataVersion.version)
        ).all()
    )
    manifest = [
        {
            "reference_id": row.id,
            "dataset_key": row.dataset_key,
            "version": row.version,
            "content_sha256": row.content_sha256,
        }
        for row in rows
    ]
    versions = {row.dataset_key: row.version for row in rows}
    return manifest, versions


def _award_history_manifest(rows: Sequence[AwardHistoryItem]) -> list[dict[str, str]]:
    result = []
    for row in rows:
        result.append(
            {
                "award_history_id": row.id,
                "basis_sha256": _digest(
                    {
                        "external_identity": row.external_identity,
                        "bid_notice_no": row.bid_notice_no,
                        "winner_name": row.winner_name,
                        "participant_count": row.participant_count,
                        "award_amount": row.award_amount,
                        "award_rate": row.award_rate,
                        "opened_at": row.opened_at,
                        "awarded_at": row.awarded_at,
                        "similarity_score": row.similarity_score,
                        "source": row.source,
                    }
                ),
            }
        )
    return sorted(result, key=lambda item: item["award_history_id"])


def _current_pps_manifest_basis(
    versions: Sequence[NoticeVersion],
    *,
    prompt_version: str,
) -> dict[str, Any] | None:
    metadata = next(
        (
            version
            for version in sorted(versions, key=lambda item: item.version_no, reverse=True)
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == "PPS_NOTICE_METADATA"
        ),
        None,
    )
    if metadata is None or not isinstance(metadata.source_payload, dict):
        return None
    stored_manifest = metadata.source_payload.get("attachment_manifest")
    manifest_shape_valid = isinstance(stored_manifest, list)
    raw_manifest_values = stored_manifest if manifest_shape_valid else []
    raw_manifest = [
        dict(item)
        for item in raw_manifest_values
        if isinstance(item, dict)
    ]
    current_manifest_sha256 = _digest(raw_manifest_values)
    validated_manifest, invalid_count = _validated_manifest_attachments(raw_manifest)
    invalid_count += len(raw_manifest_values) - len(raw_manifest)
    if not manifest_shape_valid or metadata.source_payload.get("schema_version") != PPS_METADATA_SCHEMA:
        invalid_count = max(1, invalid_count)
    expected = [
        {
            "attachment_id": str(item.get("attachment_id") or ""),
            "manifest_sha256": _digest(item),
        }
        for item in validated_manifest
    ]
    expected_by_id = {item["attachment_id"]: item["manifest_sha256"] for item in expected}
    if prompt_version == PROMPT_VERSION and manifest_shape_valid:
        _attachments, _invalid, attempts = _current_manifest_attempts(
            list(versions), validate_accepted=False,
        )
    else:
        attempts = {}
    accepted_ids = sorted(
        attachment_id
        for attachment_id, version in attempts.items()
        if isinstance(version.source_payload, dict)
        and version.source_payload.get("status") == "ACCEPTED"
        and version.extraction_status in {"ACCEPTED", "COMPLETE"}
        and version.document_complete
        and _has_valid_quantitative_record(
            version, attachment_id=attachment_id,
            current_manifest_sha256=current_manifest_sha256,
        )
    )
    audited_ids = sorted(attempts)
    expected_ids = sorted(expected_by_id)
    coverage_complete = (
        bool(expected)
        and invalid_count == 0
        and len(expected) == len(raw_manifest_values)
        and len(expected_by_id) == len(expected)
        and audited_ids == expected_ids
    )
    return {
        "metadata_version_id": metadata.id,
        "manifest_sha256": current_manifest_sha256,
        "expected_attachments": sorted(
            expected,
            key=lambda item: (item["attachment_id"], item["manifest_sha256"]),
        ),
        "expected_attachment_ids": expected_ids,
        "audited_attachment_ids": audited_ids,
        "accepted_attachment_ids": accepted_ids,
        "selected_attempt_ids": sorted(version.id for version in attempts.values()),
        "processing_version": PPS_PROCESSING_VERSION,
        "coverage_complete": coverage_complete,
    }


_TYPED_SIBLING_GAP_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^(?:별도\s*)?제안\s*요청서(?:가|는|은|를|을)?\s*"
            r"(?:없음|누락|미포함|포함되지\s*않음|별도\s*제공)$"
        ),
        "RFP",
    ),
    (
        re.compile(
            r"^(?:별도\s*)?과업\s*(?:지시서|내용서)(?:가|는|은|를|을)?\s*"
            r"(?:없음|누락|미포함|포함되지\s*않음|별도\s*제공)$"
        ),
        "SCOPE",
    ),
)

_SIBLING_DOCUMENT_GAP_MARKERS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("제안요청서", "제안 요청서"), frozenset({"RFP"})),
    (("과업지시서", "과업 지시서", "과업내용서", "과업 내용서"), frozenset({"SCOPE"})),
    (("입찰공고", "공고문", "입찰 제출서류"), frozenset({"NOTICE"})),
    (
        ("규격서", "사양서", "세부사양", "시방서", "내역서"),
        frozenset({"RFP", "SCOPE"}),
    ),
)

_ATTACHMENT_LOCAL_ABSENCE_TERMS = (
    "본문에 포함되지",
    "포함되지",
    "포함되어 있지 않",
    "본문에 없음",
    "정보가 없음",
    "내용이 없음",
    "별도 첨부",
    "별도 문서",
    "별도 제공",
    "첨부되지",
    "제공되지",
    "제시되지",
    "기재되지",
    "미포함",
    "누락",
)
_UNREADABLE_GAP_TERMS = ("판독", "식별 불가", "불명확", "훼손", "흐림")
_QUANTITATIVE_TABLE_GAP_TERMS = (
    "정량",
    "평가표",
    "배점표",
    "평가배점",
    "평점산식",
)


def _gap_is_covered_by_aggregate_sources(
    gap: str,
    *,
    current_document_type: str,
    available_types: set[str],
    sibling_document_labels: set[str],
    validated_quantitative_supplier_roles: set[tuple[str, str]],
) -> bool:
    """Resolve an attachment-local absence only when a sibling supplies it."""

    if is_explicit_qualitative_only_exclusion(gap):
        return True
    if is_explicit_non_quantitative_notice_schedule_gap(gap):
        return current_document_type != "NOTICE" and "NOTICE" in available_types

    if not _contains_any(gap, _ATTACHMENT_LOCAL_ABSENCE_TERMS) or _contains_any(
        gap,
        _UNREADABLE_GAP_TERMS,
    ):
        return False
    if has_compound_source_absence_claim(gap):
        return False

    referenced_document_groups = [
        (markers, allowed_types)
        for markers, allowed_types in _SIBLING_DOCUMENT_GAP_MARKERS
        if any(marker in gap for marker in markers)
        and current_document_type not in allowed_types
    ]

    # A table omission is a capability claim, not proof that a named document
    # merely exists. Close it only with an independently validated AVAILABLE
    # record and only for the exact table-local absence shape. Compound gaps
    # (for example, eligibility requirements plus a table) remain open. When
    # the gap names a source role, the validated supplier must also satisfy
    # that role instead of lending an unrelated FORM/SCOPE table.
    if (
        _contains_any(gap, _QUANTITATIVE_TABLE_GAP_TERMS)
        and not is_explicit_qualitative_table_local_absence(gap)
    ):
        local_targets = quantitative_table_local_absence_targets(gap)
        if not local_targets:
            return False
        supplier_roles = validated_quantitative_supplier_roles
        if not supplier_roles:
            return False
        required_targets = [
            (frozenset(required_types) - {current_document_type}, label_markers)
            for required_types, label_markers in local_targets
            if frozenset(required_types) - {current_document_type}
        ]
        if not required_targets:
            return False
        return bool(
            all(
                any(
                    _validated_supplier_matches_target(
                        supplier_type,
                        supplier_label,
                        required_types=required_types,
                        label_markers=label_markers,
                    )
                    for supplier_type, supplier_label in supplier_roles
                )
                for required_types, label_markers in required_targets
            )
        )

    if referenced_document_groups and all(
        bool(available_types & allowed_types)
        or any(
            any(marker in label for marker in markers)
            for label in sibling_document_labels
        )
        for markers, allowed_types in referenced_document_groups
    ):
        return True
    return False


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def _validated_supplier_matches_target(
    supplier_type: str,
    supplier_label: str,
    *,
    required_types: frozenset[str],
    label_markers: Sequence[str],
) -> bool:
    inferred_types = source_label_document_types(supplier_label)
    compact_label = re.sub(
        r"\s+", "", normalise_source_gap(supplier_label)
    ).casefold()
    if len(inferred_types) != 1:
        return False
    inferred_type = inferred_types[0]
    effective_type = (
        inferred_type if supplier_type in {"", "OTHER"} else supplier_type
    )
    return bool(
        effective_type in required_types
        and effective_type == inferred_type
        and any(marker in compact_label for marker in label_markers)
    )


def _gap_is_covered_by_siblings(
    gap: str,
    *,
    source: _SourceDocument,
    sibling_sources: Sequence[_SourceDocument],
    quantitative_supplier_sources: Sequence[_SourceDocument] = (),
) -> bool:
    available_types = {
        candidate.data.document_type
        for candidate in sibling_sources
        if candidate.data is not None
    }
    sibling_document_labels = {
        normalise_source_gap(
            _normalise_text(candidate.version.source_payload.get("source_label"))
        )
        for candidate in sibling_sources
        if isinstance(candidate.version.source_payload, dict)
        and candidate.version.source_payload.get("source_label")
    }
    quantitative_suppliers = {
        candidate.version.id: candidate
        for candidate in (*sibling_sources, *quantitative_supplier_sources)
        if _source_supplies_quantitative_table(candidate)
    }.values()
    validated_quantitative_supplier_roles = {
        (
            candidate.data.document_type,
            normalise_source_gap(
                _normalise_text(candidate.version.source_payload.get("source_label"))
            ),
        )
        for candidate in quantitative_suppliers
        if candidate.data is not None
        if isinstance(candidate.version.source_payload, dict)
        and candidate.version.source_payload.get("source_label")
    }
    matched_type = next(
        (
            document_type
            for pattern, document_type in _TYPED_SIBLING_GAP_RULES
            if pattern.fullmatch(gap)
        ),
        None,
    )
    return bool(
        (matched_type is not None and matched_type in available_types)
        or _gap_is_covered_by_aggregate_sources(
            gap,
            current_document_type=source.data.document_type,
            available_types=available_types,
            sibling_document_labels=sibling_document_labels,
            validated_quantitative_supplier_roles=(
                validated_quantitative_supplier_roles
            ),
        )
    )


def _source_can_join_effective_closure(
    source: _SourceDocument,
    *,
    sibling_sources: Sequence[_SourceDocument],
    quantitative_supplier_sources: Sequence[_SourceDocument] = (),
) -> bool:
    if not source.materializable or source.data is None:
        return False
    source_gaps = [
        _normalise_text(item) for item in source.data.missing_or_unreadable
    ]
    if not source_gaps:
        return False
    remaining_warnings = set(source.warnings) - {
        "SOURCE_MISSING_OR_UNREADABLE",
        "DOCUMENT_INCOMPLETE",
    }
    return not remaining_warnings and all(
        _gap_is_covered_by_siblings(
            gap,
            source=source,
            sibling_sources=sibling_sources,
            quantitative_supplier_sources=quantitative_supplier_sources,
        )
        for gap in source_gaps
    )


def _source_supplies_quantitative_table(source: _SourceDocument) -> bool:
    """Prove a usable table capability without treating an incomplete type as complete.

    This narrow capability seed breaks a legitimate NOTICE/RFP cross-reference
    cycle: an RFP may contain the quantitative table while its only remaining
    gap is the notice schedule.  It must not let two empty, mutually incomplete
    documents prove one another complete merely because their types are present.
    """

    if (
        not source.materializable
        or source.data is None
        or not source.version.document_complete
        or set(source.warnings)
        - {"SOURCE_MISSING_OR_UNREADABLE", "LOW_EXTRACTION_CONFIDENCE"}
    ):
        return False
    payload = (
        source.version.source_payload
        if isinstance(source.version.source_payload, dict)
        else {}
    )
    raw_record = payload.get("quantitative_validation_record")
    current_manifest_sha256 = payload.get("current_manifest_sha256")
    if not isinstance(raw_record, dict) or not isinstance(
        current_manifest_sha256, str
    ):
        return False
    try:
        record = ValidatedQuantitativeAttachmentRecord.model_validate(raw_record)
        fingerprint_valid = (
            record.validation_fingerprint_sha256
            == validated_quantitative_record_fingerprint(record)
        )
    except (ValidationError, TypeError, ValueError):
        return False
    raw_table_ids = {table.table_id for table in source.data.quantitative_tables}
    available_tables = [table for table in record.tables if table.status == "AVAILABLE"]
    quantitative_anchors = [
        *(table.total_evidence for table in available_tables if table.total_evidence),
        *(table.minimum_evidence for table in available_tables if table.minimum_evidence),
        *(candidate.evidence for candidate in record.available_candidates),
        *(
            bracket.evidence
            for candidate in record.available_candidates
            for bracket in candidate.brackets
        ),
        *(
            candidate.threshold.evidence
            for candidate in record.available_candidates
            if candidate.threshold is not None
        ),
        *(
            case.evidence
            for candidate in record.available_candidates
            for case in candidate.cases
        ),
        *(
            condition.evidence
            for candidate in record.available_candidates
            for condition in candidate.recognition_conditions
        ),
    ]
    if not (
        record.status in {"AVAILABLE", "REVIEW"}
        and record.attachment_id == source.attachment_id
        and record.document_sha256 == source.document_sha256
        and record.manifest_sha256 == current_manifest_sha256
        and quantitative_record_contract_is_usable(
            record, source_payload=payload, attachment_id=source.attachment_id,
            document_sha256=source.document_sha256,
            manifest_sha256=current_manifest_sha256,
        )
        and record.prompt_version == source.prompt_version
        and record.extraction_schema_version == source.schema_version
        and fingerprint_valid
        and available_tables
        and quantitative_anchors
        and all(anchor.confidence >= 0.90 for anchor in quantitative_anchors)
        and all(
            table.table_id in raw_table_ids and bool(table.available_criterion_ids)
            for table in available_tables
        )
    ):
        return False
    return not any(
        not is_quantitative_irrelevant_gap(gap)
        and _contains_any(_normalise_text(gap), _QUANTITATIVE_TABLE_GAP_TERMS)
        for gap in source.data.missing_or_unreadable
    )


def _aggregate_source_gaps(
    sources: Sequence[_SourceDocument],
) -> tuple[list[str], list[str]]:
    """Resolve only exact document-presence gaps using accepted typed siblings."""
    effective_source_ids = {
        source.version.id for source in sources if source.complete
    }
    quantitative_supplier_ids = {
        source.version.id for source in sources if _source_supplies_quantitative_table(source)
    }
    while True:
        newly_effective: set[str] = set()
        for source in sources:
            if source.version.id in effective_source_ids:
                continue
            sibling_sources = [
                candidate
                for candidate in sources
                if candidate is not source
                and candidate.version.id in effective_source_ids
            ]
            quantitative_supplier_sources = [
                candidate
                for candidate in sources
                if candidate is not source
                and candidate.version.id in quantitative_supplier_ids
            ]
            if _source_can_join_effective_closure(
                source,
                sibling_sources=sibling_sources,
                quantitative_supplier_sources=quantitative_supplier_sources,
            ):
                newly_effective.add(source.version.id)
        if not newly_effective:
            break
        effective_source_ids.update(newly_effective)

    unresolved: list[str] = []
    resolved: list[str] = []
    for source in sources:
        if source.data is None:
            continue
        sibling_sources = [
            candidate
            for candidate in sources
            if candidate is not source
            and candidate.version.id in effective_source_ids
        ]
        quantitative_supplier_sources = [
            candidate
            for candidate in sources
            if candidate is not source
            and candidate.version.id in quantitative_supplier_ids
        ]
        for raw_gap in source.data.missing_or_unreadable:
            gap = _normalise_text(raw_gap)
            if _gap_is_covered_by_siblings(
                gap,
                source=source,
                sibling_sources=sibling_sources,
                quantitative_supplier_sources=quantitative_supplier_sources,
            ):
                resolved.append(gap)
            else:
                unresolved.append(gap)
    return unresolved, resolved


def _source_effectively_complete(
    source: _SourceDocument,
    *,
    unresolved_gaps: set[str],
) -> bool:
    if source.complete:
        return True
    if not source.materializable or source.data is None:
        return False
    source_gaps = {_normalise_text(item) for item in source.data.missing_or_unreadable}
    if not source_gaps:
        return False
    if source_gaps & unresolved_gaps:
        return False
    remaining_warnings = set(source.warnings) - {
        "SOURCE_MISSING_OR_UNREADABLE",
        "DOCUMENT_INCOMPLETE",
    }
    return not remaining_warnings


def _pricing_profile_for_versions(versions: Sequence[NoticeVersion]) -> dict[str, Any] | None:
    manifest_basis = _current_pps_manifest_basis(versions, prompt_version=PROMPT_VERSION)
    if manifest_basis is not None:
        if (
            not manifest_basis["coverage_complete"]
            or manifest_basis["accepted_attachment_ids"]
            != manifest_basis["expected_attachment_ids"]
        ):
            return None
        accepted_ids = set(manifest_basis["accepted_attachment_ids"])
        current_digests = sorted(
            {
                version.file_sha256
                for version in versions
                if isinstance(version.source_payload, dict)
                and version.source_payload.get("kind") == SOURCE_KIND
                and version.id in set(manifest_basis["selected_attempt_ids"])
                and version.source_payload.get("attachment_id") in accepted_ids
                and version.source_payload.get("manifest_sha256")
                == {
                    item["attachment_id"]: item["manifest_sha256"]
                    for item in manifest_basis["expected_attachments"]
                }.get(version.source_payload.get("attachment_id"))
                and version.source_payload.get("status") == "ACCEPTED"
                and version.source_payload.get("processing_version")
                == PPS_PROCESSING_VERSION
                and version.document_complete
            }
        )
    else:
        current_digests = [
            version.file_sha256
            for version in sorted(versions, key=lambda item: item.version_no, reverse=True)
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind") == "PUBLIC_DOCUMENT_REFERENCE"
        ][:1]
    for digest in current_digests:
        profile = pricing_profile_for_document(digest)
        if profile is not None:
            return profile
    return None


def _system_bid_recommendation(
    *,
    eligibility: str,
    readiness_status: str,
    business_risk_band: str,
    quantitative_status: str,
    quantitative_band: str,
    competition_band: str | None,
) -> str:
    if eligibility == "FAIL":
        return "NO_GO"
    if eligibility == "REVIEW":
        return "HOLD"
    if business_risk_band == "NO_GO":
        return "NO_GO"
    if (
        eligibility == "PASS"
        and readiness_status == "GREEN"
        and quantitative_status in {"CONFIRMED", "ESTIMATED"}
        and quantitative_band in {"GREEN", "YELLOW"}
        and competition_band not in {"VERY_HIGH"}
    ):
        return "GO"
    return "HOLD"


def _run_result(run: AnalysisRun, *, reused: bool) -> AnalysisPipelineResult:
    summary = run.output_summary or {}
    return AnalysisPipelineResult(
        analysis_run_id=run.id,
        notice_id=run.notice_id,
        notice_version_id=str(run.notice_version_id or ""),
        evaluation_id=str(run.evaluation_id or ""),
        idempotency_key=run.idempotency_key,
        input_sha256=run.input_sha256,
        status=run.status,
        reused=reused,
        source_count=int(summary.get("source_count", 0)),
        accepted_source_count=int(summary.get("accepted_source_count", 0)),
        materialized_requirement_count=int(summary.get("materialized_requirement_count", 0)),
        requirement_snapshot_count=int(summary.get("requirement_snapshot_count", 0)),
        score_snapshot_count=int(summary.get("score_snapshot_count", 0)),
        recommendation_snapshot_count=int(summary.get("recommendation_snapshot_count", 0)),
        eligibility=str(summary.get("eligibility", "REVIEW")),
        reason_code=str(summary.get("reason_code", "R07")),
        warnings=tuple(str(item) for item in summary.get("warnings", [])),
    )


def run_analysis_pipeline(
    session: Session,
    *,
    notice_id: str,
    source_version_ids: Sequence[str] | None = None,
    prompt_version: str = PROMPT_VERSION,
    ruleset_version: str = RULESET_VERSION,
    company_profile: dict[str, Any] | None = None,
    _stage_hook: Callable[[str], None] | None = None,
) -> AnalysisPipelineResult:
    """Materialise, evaluate, and snapshot one notice in a single transaction.

    The caller passes a clean Session and this function owns its transaction.
    It never invokes OpenAI or PPS. Only the latest stored extraction per
    attachment and prompt is consumed. Repeated calls with the same document,
    prompt, reviewed profile, company-fact basis, and ruleset return the
    existing immutable AnalysisRun without creating duplicate rows.
    """

    if session.in_transaction():
        raise AnalysisPipelineTransactionError(
            "run_analysis_pipeline requires a Session without an active transaction"
        )
    profile = copy.deepcopy(company_profile) if company_profile is not None else load_public_company_profile()
    profile_version = str(profile.get("profile_version") or "UNVERSIONED")
    profile_sha256 = _digest(profile)
    idempotency_key: str | None = None

    try:
        with session.begin():
            notice = session.scalar(select(Notice).where(Notice.id == notice_id))
            if notice is None:
                raise AnalysisPipelineSourceError("notice was not found")
            if authoritative_pps_notice_is_cancelled(session, notice):
                raise AnalysisPipelineSourceError(
                    "authoritative PPS notice is cancelled; analysis is disabled"
                )

            selected_versions = _select_source_versions(
                session,
                notice_id=notice_id,
                prompt_version=prompt_version,
                source_version_ids=source_version_ids,
            )
            sources = [
                _parse_source(
                    version, prompt_version=prompt_version,
                    allow_compatible_pps=True,
                )
                for version in selected_versions
            ]
            merged = _merge_requirements(sources)
            policy_items = _policy_items(merged, notice=notice, profile=profile)
            materialized_policy_items = [
                pair for pair in policy_items if _is_materialized_policy_item(*pair)
            ]
            prospective_atomics = [
                _atomic_requirement(item, policy, sequence=sequence)
                for sequence, (item, policy) in enumerate(materialized_policy_items, start=1)
            ]

            company_facts = list(
                session.scalars(
                    select(CompanyFact).options(selectinload(CompanyFact.evidence))
                ).all()
            )
            performance_records = list(
                session.scalars(
                    select(CompanyPerformanceRecord).where(
                        CompanyPerformanceRecord.record_status != "ARCHIVED"
                    )
                ).all()
            )
            # A declared performance bidder gate whose recognition scope has
            # not been bound cannot consume a generic or fabricated Boolean
            # fact. Keep only those pending keys absent so the existing linked
            # missing-evidence REVIEW path applies; other facts and AND/OR
            # evaluation remain unchanged.
            pending_performance_fact_keys = {
                atomic.fact_key
                for atomic, (_item, policy) in zip(
                    prospective_atomics, materialized_policy_items, strict=True,
                )
                if policy.get("requires_performance_scope_binding") is True
            }
            eligibility_company_facts = [
                fact for fact in company_facts
                if fact.fact_key not in pending_performance_fact_keys
            ]
            fact_manifest = _selected_fact_manifest(
                company_facts,
                fact_keys=(
                    {item.fact_key for item in prospective_atomics}
                    | set(QUANTITATIVE_CANONICAL_FACT_KEYS)
                ),
                deadline=notice.deadline,
            )
            all_notice_versions = list(
                session.scalars(
                    select(NoticeVersion)
                    .where(NoticeVersion.notice_id == notice.id)
                    .order_by(NoticeVersion.version_no)
                ).all()
            )
            pps_manifest_basis = _current_pps_manifest_basis(
                all_notice_versions,
                prompt_version=prompt_version,
            )
            quantitative = (
                estimate_for_notice(notice, company_facts, performance_records)
                if performance_records
                else estimate_for_notice(notice, company_facts)
            )
            quantitative_catalog = load_quantitative_profile_catalog()
            dynamic_quantitative_profile = quantitative.ruleset_version.startswith(
                "dynamic-quantitative-rules-"
            )
            quantitative_profile_basis = (
                quantitative.ruleset_version
                if dynamic_quantitative_profile
                else str(
                    quantitative_catalog.get("profile_version") or "UNVERSIONED"
                )
            )
            department_catalog = load_department_keyword_profiles()
            department_rankings = rank_notice_across_departments(
                title=notice.title,
                agency=notice.agency,
                category=notice.category or "",
                limit=3,
            )
            award_history = list(
                session.scalars(
                    select(AwardHistoryItem)
                    .where(AwardHistoryItem.target_notice_id == notice.id)
                    .order_by(AwardHistoryItem.awarded_at, AwardHistoryItem.id)
                ).all()
            )
            award_as_of = notice.published_at or notice.deadline
            award_intelligence = build_award_intelligence(
                award_history,
                as_of=award_as_of,
                target_estimated_price=notice.estimated_amount,
            )
            pricing_profile = _pricing_profile_for_versions(all_notice_versions)
            reference_manifest, reference_versions = _active_reference_manifest(session)
            award_manifest = _award_history_manifest(award_history)
            source_semantics = [
                {
                    "attachment_id": source.attachment_id,
                    "document_sha256": source.document_sha256,
                    "prompt_version": source.prompt_version,
                    "schema_version": source.schema_version,
                    "processing_version": (
                        source.version.source_payload.get("processing_version")
                        if isinstance(source.version.source_payload, dict)
                        else None
                    ),
                    "manifest_sha256": (
                        source.version.source_payload.get("manifest_sha256")
                        if isinstance(source.version.source_payload, dict)
                        else None
                    ),
                    "status": source.status,
                    "error_code": source.error_code,
                    "result_sha256": source.result_sha256,
                    "document_complete": source.complete,
                }
                for source in sources
            ]
            notice_basis_sha256 = _digest(
                {
                    "notice_id": notice.id,
                    "deadline": notice.deadline,
                    "risk_dimensions": notice.risk_dimensions,
                }
            )
            input_sha256 = _digest(
                {
                    "pipeline_version": PIPELINE_VERSION,
                    "materialization_version": MATERIALIZATION_VERSION,
                    "snapshot_version": SNAPSHOT_VERSION,
                    "ruleset_version": ruleset_version,
                    "business_risk_version": RISK_METHOD_VERSION,
                    "policy_version": POLICY_VERSION,
                    "profile_version": profile_version,
                    "profile_sha256": profile_sha256,
                    "notice_basis_sha256": notice_basis_sha256,
                    "sources": source_semantics,
                    "pps_manifest_basis": pps_manifest_basis,
                    "company_facts": fact_manifest,
                    "reference_versions": reference_manifest,
                    "quantitative_output_sha256": _digest(
                        quantitative.model_dump(mode="json")
                    ),
                    "department_output_sha256": _digest(department_rankings),
                    "award_history": award_manifest,
                    "award_output_sha256": _digest(award_intelligence),
                    "pricing_profile_sha256": _digest(pricing_profile),
                }
            )
            idempotency_key = f"notice-analysis:{input_sha256}"
            existing = session.scalar(
                select(AnalysisRun).where(AnalysisRun.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return _run_result(existing, reused=True)

            warnings = sorted(
                {
                    warning
                    for source in sources
                    for warning in source.warnings
                }
            )
            unresolved_gaps, resolved_gaps = _aggregate_source_gaps(sources)
            unresolved_gap_set = set(unresolved_gaps)
            if resolved_gaps:
                warnings.append("SOURCE_LOCAL_GAP_RESOLVED_BY_TYPED_SIBLING")
            if unresolved_gaps:
                warnings.append("AGGREGATE_GAPS_UNRESOLVED")
            if not sources:
                warnings.append("NO_EXTRACTION_SOURCES")
            if pps_manifest_basis is not None and not pps_manifest_basis["coverage_complete"]:
                warnings.append("ATTACHMENT_COVERAGE_INCOMPLETE")
            accepted_source_count = sum(source.materializable for source in sources)
            no_blocking_requirements_verified = bool(
                policy_items
                and not materialized_policy_items
                and sources
                and accepted_source_count == len(sources)
                and all(
                    _source_effectively_complete(
                        source,
                        unresolved_gaps=unresolved_gap_set,
                    )
                    for source in sources
                )
                and (
                    pps_manifest_basis is None
                    or pps_manifest_basis["coverage_complete"]
                )
                and all(_has_verified_anchor(item) for item, _policy in policy_items)
            )
            if not materialized_policy_items:
                warnings.append(
                    "NO_BLOCKING_REQUIREMENTS_VERIFIED"
                    if no_blocking_requirements_verified
                    else "NO_ELIGIBILITY_OR_ACTION_REQUIREMENTS"
                )
            if not sources:
                run_status = "FAILED"
            elif accepted_source_count == 0:
                # A manifest-bound REVIEW/unsupported attempt is a truthful
                # audited partial outcome, not an infrastructure failure.
                run_status = "PARTIAL"
            elif (
                any(
                    not _source_effectively_complete(
                        source,
                        unresolved_gaps=unresolved_gap_set,
                    )
                    for source in sources
                )
                or (
                    not materialized_policy_items
                    and not no_blocking_requirements_verified
                )
                or (
                    pps_manifest_basis is not None
                    and not pps_manifest_basis["coverage_complete"]
                )
            ):
                run_status = "PARTIAL"
            else:
                run_status = "COMPLETED"
            warnings = sorted(set(warnings))
            gate_candidate_keys, _eligibility_gate_keys = _partial_gate_candidate_keys(
                run_status=run_status,
                sources=sources,
                policy_items=policy_items,
                pps_manifest_basis=pps_manifest_basis,
            )

            confidence_values = [
                source.version.extraction_confidence
                for source in sources
                if source.materializable
            ] + [
                anchor.confidence
                for item in merged
                for anchor in item.anchors
            ]
            extraction_confidence = min(confidence_values) if confidence_values else 0.0
            next_version_no = int(
                session.scalar(
                    select(func.max(NoticeVersion.version_no)).where(
                        NoticeVersion.notice_id == notice.id
                    )
                )
                or 0
            ) + 1
            materialized_version = NoticeVersion(
                notice_id=notice.id,
                version_no=next_version_no,
                file_sha256=input_sha256,
                document_complete=run_status == "COMPLETED",
                extraction_status=(
                    "ACCEPTED"
                    if run_status == "COMPLETED"
                    else "PARTIAL"
                    if run_status == "PARTIAL"
                    else "REVIEW"
                ),
                extraction_confidence=extraction_confidence,
                source_payload={
                    "kind": MATERIALIZED_KIND,
                    "pipeline_version": PIPELINE_VERSION,
                    "materialization_version": MATERIALIZATION_VERSION,
                    "idempotency_key": idempotency_key,
                    "input_sha256": input_sha256,
                    "status": run_status,
                    "review_code": None if run_status == "COMPLETED" else "R07",
                    "prompt_version": prompt_version,
                    "source_version_ids": [source.version.id for source in sources],
                    "source_document_sha256s": sorted(
                        {source.document_sha256 for source in sources}
                    ),
                    "attachment_ids": sorted({source.attachment_id for source in sources}),
                    "pps_manifest_sha256": (
                        pps_manifest_basis["manifest_sha256"]
                        if pps_manifest_basis is not None
                        else None
                    ),
                    "expected_attachment_ids": (
                        pps_manifest_basis["expected_attachment_ids"]
                        if pps_manifest_basis is not None
                        else []
                    ),
                    "attachment_coverage_complete": (
                        pps_manifest_basis["coverage_complete"]
                        if pps_manifest_basis is not None
                        else True
                    ),
                    "warnings": warnings,
                },
            )
            materialized_version.requirements.extend(prospective_atomics)
            session.add(materialized_version)
            session.flush()
            if _stage_hook:
                _stage_hook("after_materialization")

            provisional_evaluation = evaluate_notice(
                notice,
                materialized_version,
                prospective_atomics,
                eligibility_company_facts,
                verified_document_requirement_keys=gate_candidate_keys,
                no_blocking_requirements_verified=no_blocking_requirements_verified,
                risk_dimensions={},
            )
            # A known non-eligibility gap or an unrelated weak anchor must not
            # erase an independently verified eligibility clause. The complete
            # PPS path proves every current source before using per-clause
            # confidence; missing source and weak mandatory clauses still fail
            # closed in ``_partial_gate_candidate_keys``. Company evidence is
            # evaluated normally and may still require REVIEW or produce FAIL.
            eligibility_gate_applied = bool(gate_candidate_keys)
            verified_requirement_keys = (
                gate_candidate_keys if eligibility_gate_applied else frozenset()
            )
            if eligibility_gate_applied:
                confidence_gate_applied = _current_complete_pps_evidence(
                    sources, pps_manifest_basis,
                )
                warnings = sorted(
                    set(warnings) | {
                        "PER_REQUIREMENT_CONFIDENCE_GATE_APPLIED"
                        if confidence_gate_applied
                        else "NON_ELIGIBILITY_PARTIAL_GATE_APPLIED"
                    }
                )
                materialized_version.source_payload = {
                    **(materialized_version.source_payload or {}),
                    "review_code": None,
                    "eligibility_gate_applied": True,
                    "eligibility_gate_basis": (
                        "ALL_MANDATORY_ELIGIBILITY_ANCHORS_VERIFIED"
                    ),
                    "warnings": warnings,
                }

            derived_risk_dimensions, risk_axis_basis = _derive_risk_dimensions(
                notice=notice,
                evaluation=provisional_evaluation,
                policy_items=policy_items,
                sources=sources,
                run_status=run_status,
                eligibility_gate_applied=eligibility_gate_applied,
                award_intelligence=award_intelligence,
            )
            evaluation_result = evaluate_notice(
                notice,
                materialized_version,
                prospective_atomics,
                eligibility_company_facts,
                verified_document_requirement_keys=verified_requirement_keys,
                no_blocking_requirements_verified=no_blocking_requirements_verified,
                risk_dimensions=derived_risk_dimensions,
                risk_axis_basis=risk_axis_basis,
            )
            safe_atomics, safe_explanation = _sanitized_evaluation_payload(
                evaluation_result.atomic_results,
                evaluation_result.explanation,
            )
            safe_explanation["analysis_pipeline"] = {
                "pipeline_version": PIPELINE_VERSION,
                "materialization_version": MATERIALIZATION_VERSION,
                "snapshot_version": SNAPSHOT_VERSION,
                "idempotency_key": idempotency_key,
                "input_sha256": input_sha256,
                "status": run_status,
                "source_count": len(sources),
                "accepted_source_count": accepted_source_count,
                "attachment_coverage_complete": (
                    pps_manifest_basis["coverage_complete"]
                    if pps_manifest_basis is not None
                    else True
                ),
                "eligibility_gate_applied": eligibility_gate_applied,
                "warnings": warnings,
            }
            reason_code = evaluation_result.reason_code
            evaluation = Evaluation(
                notice_id=notice.id,
                notice_version_id=materialized_version.id,
                evaluated_at=datetime.now(timezone.utc),
                deadline_snapshot_at=notice.deadline,
                eligibility=evaluation_result.eligibility.value,
                reason_code=reason_code,
                readiness_score=evaluation_result.readiness_score,
                readiness_status=evaluation_result.readiness_status.value,
                evidence_coverage=evaluation_result.evidence_coverage,
                risk_score=evaluation_result.risk_score,
                risk_band=evaluation_result.risk_band.value,
                ruleset_version=ruleset_version,
                atomic_results=safe_atomics,
                explanation=safe_explanation,
            )
            session.add(evaluation)
            session.flush()
            if _stage_hook:
                _stage_hook("after_evaluation")

            output_summary = {
                "source_count": len(sources),
                "accepted_source_count": accepted_source_count,
                "attachment_coverage_complete": (
                    pps_manifest_basis["coverage_complete"]
                    if pps_manifest_basis is not None
                    else True
                ),
                "materialized_requirement_count": len(prospective_atomics),
                "requirement_snapshot_count": len(policy_items),
                "score_snapshot_count": 8,
                "recommendation_snapshot_count": len(department_rankings) + 1,
                "eligibility": evaluation.eligibility,
                "reason_code": reason_code,
                "eligibility_gate_applied": eligibility_gate_applied,
                "risk_status": safe_explanation.get("risk", {}).get("status"),
                "warnings": warnings,
            }
            model_names = sorted(
                {source.model_name for source in sources if source.model_name}
            )
            analysis_run = AnalysisRun(
                notice_id=notice.id,
                notice_version_id=materialized_version.id,
                evaluation_id=evaluation.id,
                run_kind=RUN_KIND,
                status=run_status,
                idempotency_key=idempotency_key,
                input_sha256=input_sha256,
                ruleset_version=ruleset_version,
                company_profile_version=reference_versions.get(
                    "company_public_profile", profile_version
                ),
                department_profile_version=reference_versions.get(
                    "department_keyword_profiles", str(department_catalog["version"])
                ),
                quantitative_profile_version=(
                    quantitative_profile_basis
                    if dynamic_quantitative_profile
                    else reference_versions.get(
                        "quantitative_notice_profiles",
                        quantitative_profile_basis,
                    )
                ),
                pricing_profile_version=reference_versions.get(
                    "pricing_method_profiles",
                    str(
                        (pricing_profile or {}).get("profile_id")
                        or "grounded-pricing-method-1.0.0"
                    ),
                ),
                analytics_version=ANALYTICS_VERSION,
                extraction_prompt_version=prompt_version,
                model_name=(
                    model_names[0]
                    if len(model_names) == 1
                    else "MULTIPLE"
                    if model_names
                    else None
                ),
                basis_versions={
                    "pipeline": PIPELINE_VERSION,
                    "materializer": MATERIALIZATION_VERSION,
                    "snapshot": SNAPSHOT_VERSION,
                    "ruleset": ruleset_version,
                    "requirement_policy": POLICY_VERSION,
                    "extraction_prompt": prompt_version,
                    "extraction_schema": SCHEMA_VERSION,
                    "extraction_read_policy": EXTRACTION_READ_POLICY_VERSION,
                    "source_extraction_contracts": sorted({
                        (source.prompt_version, source.schema_version) for source in sources
                    }),
                    "company_profile": profile_version,
                    "department_profile": str(department_catalog["version"]),
                    "quantitative_profile": quantitative_profile_basis,
                    "quantitative_engine": QUANTITATIVE_ENGINE_VERSION,
                    "pricing_profile": reference_versions.get(
                        "pricing_method_profiles",
                        str(
                            (pricing_profile or {}).get("profile_id")
                            or "grounded-pricing-method-1.0.0"
                        ),
                    ),
                    "award_analytics": ANALYTICS_VERSION,
                    "competition_risk": COMPETITION_RISK_VERSION,
                    "business_risk": RISK_METHOD_VERSION,
                    "active_reference_versions": reference_versions,
                },
                input_manifest={
                    "source_version_ids": [source.version.id for source in sources],
                    "source_document_sha256s": sorted(
                        {source.document_sha256 for source in sources}
                    ),
                    "attachment_ids": sorted({source.attachment_id for source in sources}),
                    "pps_manifest_sha256": (
                        pps_manifest_basis["manifest_sha256"]
                        if pps_manifest_basis is not None
                        else None
                    ),
                    "expected_attachment_ids": (
                        pps_manifest_basis["expected_attachment_ids"]
                        if pps_manifest_basis is not None
                        else []
                    ),
                    "audited_attachment_ids": (
                        pps_manifest_basis["audited_attachment_ids"]
                        if pps_manifest_basis is not None
                        else []
                    ),
                    "accepted_attachment_ids": (
                        pps_manifest_basis["accepted_attachment_ids"]
                        if pps_manifest_basis is not None
                        else []
                    ),
                    "attachment_coverage_complete": (
                        pps_manifest_basis["coverage_complete"]
                        if pps_manifest_basis is not None
                        else True
                    ),
                    "company_fact_ids": [item["company_fact_id"] for item in fact_manifest],
                    "company_fact_basis_sha256s": [
                        item["basis_sha256"] for item in fact_manifest
                    ],
                    "notice_basis_sha256": notice_basis_sha256,
                    "profile_sha256": profile_sha256,
                    "reference_ids": [item["reference_id"] for item in reference_manifest],
                    "reference_content_sha256s": [
                        item["content_sha256"] for item in reference_manifest
                    ],
                    "award_history_ids": [item["award_history_id"] for item in award_manifest],
                    "award_history_basis_sha256s": [
                        item["basis_sha256"] for item in award_manifest
                    ],
                    "quantitative_output_sha256": _digest(
                        quantitative.model_dump(mode="json")
                    ),
                    "department_output_sha256": _digest(department_rankings),
                    "award_output_sha256": _digest(award_intelligence),
                    "pricing_profile_sha256": _digest(pricing_profile),
                },
                output_summary=output_summary,
                error_code=None if reason_code != "R07" else "R07",
            )
            session.add(analysis_run)
            session.flush()

            atomic_by_key = {item["requirement_key"]: item for item in safe_atomics}
            for sequence, (item, policy) in enumerate(policy_items, start=1):
                atomic = atomic_by_key.get(item.requirement_key)
                if atomic is not None:
                    outcome = str(atomic["result"])
                    reason = str(atomic["reason_code"])
                    evidence_state = (
                        "VALID"
                        if atomic.get("evidence_valid")
                        else "MISSING"
                        if atomic.get("evidence_key") is None
                        else "INVALID"
                    )
                else:
                    outcome = str(policy.get("outcome") or "INFORMATION")
                    reason = None
                    evidence_state = str(policy.get("evidence_state") or "NOT_REQUIRED")
                blocking = bool(
                    item.requirement.mandatory
                    and policy.get("policy_class") in {"ELIGIBILITY", "ACTION_REQUIRED"}
                    and outcome != "PASS"
                )
                analysis_run.requirement_results.append(
                    RequirementResultSnapshot(
                        result_key=f"policy:{item.requirement_key}",
                        sequence=sequence,
                        requirement_key=item.requirement_key,
                        policy_class=str(policy.get("policy_class") or "INFORMATION"),
                        outcome=outcome,
                        reason_code=reason,
                        blocking=blocking,
                        evidence_state=evidence_state,
                        result_json={
                            "mandatory": item.requirement.mandatory,
                            "condition_sha256": _digest(
                                _normalise_text(item.requirement.normalized_condition)
                            ),
                            "source_document_sha256s": sorted(
                                item.source_document_sha256s
                            ),
                            "attachment_ids": sorted(item.attachment_ids),
                            "policy_outcome": policy.get("outcome"),
                            "performance_scope_binding_pending": (
                                policy.get("requires_performance_scope_binding") is True
                            ),
                            "performance_relation_unresolved": (
                                policy.get("performance_relation_unresolved") is True
                            ),
                            "parse_confidence": _parse_confidence(item),
                        },
                    )
                )

            score_status = "AVAILABLE" if run_status == "COMPLETED" else "REVIEW"
            score_confidence = extraction_confidence if sources else 0.0
            common_basis = {
                "input_sha256": input_sha256,
                "source_count": len(sources),
                "materialized_requirement_count": len(prospective_atomics),
            }
            # ``value`` represents an exact total only.  A confirmed subtotal
            # is not the total when the result is a range or REVIEW; those
            # results remain expressible through lower/upper bounds and the
            # explicit confirmed_points basis field.
            quantitative_value = quantitative.estimated_points
            public_quantitative_criteria = (
                build_public_quantitative_criteria_snapshot(quantitative)
            )
            if public_quantitative_criteria is None:
                raise AnalysisPipelineError(
                    "quantitative public criteria snapshot invariant failed"
                )
            competition = award_intelligence["competition_risk"]
            award_prediction = award_intelligence["prediction"]["award_rate"]
            submitted_prediction = award_intelligence["prediction"]["submitted_bid_rate"]
            analysis_run.scores.extend(
                [
                    ScoreSnapshot(
                        score_key="eligibility.readiness",
                        score_type="READINESS",
                        value=evaluation.readiness_score,
                        unit="PERCENT",
                        status=score_status,
                        band=evaluation.readiness_status,
                        confidence=score_confidence,
                        method_version=ruleset_version,
                        basis_json=common_basis,
                    ),
                    ScoreSnapshot(
                        score_key="eligibility.evidence_coverage",
                        score_type="EVIDENCE_COVERAGE",
                        value=evaluation.evidence_coverage,
                        unit="PERCENT",
                        status=score_status,
                        band=None,
                        confidence=score_confidence,
                        method_version=ruleset_version,
                        basis_json=common_basis,
                    ),
                    ScoreSnapshot(
                        score_key="business.risk",
                        score_type="BUSINESS_RISK",
                        value=evaluation.risk_score,
                        unit="POINTS_0_100",
                        status=(
                            "AVAILABLE"
                            if evaluation.risk_score is not None
                            else "UNSCORABLE"
                        ),
                        band=evaluation.risk_band,
                        confidence=score_confidence if evaluation.risk_score is not None else 0.0,
                        method_version=RISK_METHOD_VERSION,
                        basis_json={
                            **common_basis,
                            **safe_explanation.get("risk", {}),
                        },
                    ),
                    ScoreSnapshot(
                        score_key="quantitative.total",
                        score_type="QUANTITATIVE_ESTIMATE",
                        value=quantitative_value,
                        lower_value=quantitative.lower_points,
                        upper_value=quantitative.upper_points,
                        unit="POINTS",
                        status=quantitative.overall_status,
                        band=quantitative.readiness_band,
                        confidence=quantitative.confidence,
                        method_version=quantitative.engine_version,
                        basis_json={
                            "input_sha256": input_sha256,
                            "ruleset_version": quantitative.ruleset_version,
                            "rule_source_status": quantitative.rule_source_status,
                            "source_validation_status": quantitative.source_validation_status,
                            "activation_status": quantitative.activation_status,
                            "activation_reasons": quantitative.activation_reasons,
                            "total_max_points": quantitative.total_max_points,
                            "out_of_scope_points": quantitative.out_of_scope_points,
                            "confirmed_points": quantitative.confirmed_points,
                            "evidence_coverage_pct": quantitative.evidence_coverage_pct,
                            "profile_output_sha256": _digest(
                                quantitative.model_dump(mode="json")
                            ),
                            "public_criteria": public_quantitative_criteria,
                        },
                    ),
                    ScoreSnapshot(
                        score_key="competition.risk",
                        score_type="COMPETITION_RISK",
                        value=competition.get("score"),
                        unit="POINTS_0_100",
                        status=str(competition.get("status") or "UNKNOWN"),
                        band=competition.get("band"),
                        confidence=_confidence_value(competition.get("confidence")),
                        method_version=str(
                            competition.get("method_version")
                            or COMPETITION_RISK_VERSION
                        ),
                        basis_json={
                            "input_sha256": input_sha256,
                            "sample_count": competition.get("sample_count"),
                            "coverage_sha256": _digest(competition.get("coverage")),
                            "components_sha256": _digest(competition.get("components")),
                            "market_claim": competition.get("market_claim"),
                        },
                    ),
                    ScoreSnapshot(
                        score_key="pricing.award_rate_prediction",
                        score_type="AWARD_RATE_PREDICTION",
                        value=award_prediction.get("center"),
                        lower_value=award_prediction.get("range_low"),
                        upper_value=award_prediction.get("range_high"),
                        unit="PERCENT",
                        status=str(award_prediction.get("status") or "INSUFFICIENT_DATA"),
                        confidence=_confidence_value(award_prediction.get("confidence")),
                        method_version=ANALYTICS_VERSION,
                        basis_json={
                            "input_sha256": input_sha256,
                            "sample_count": award_prediction.get("sample_count"),
                            "method_sha256": _digest(award_prediction.get("method")),
                        },
                    ),
                    ScoreSnapshot(
                        score_key="pricing.submitted_bid_rate_prediction",
                        score_type="SUBMITTED_BID_RATE_PREDICTION",
                        value=submitted_prediction.get("center"),
                        lower_value=submitted_prediction.get("range_low"),
                        upper_value=submitted_prediction.get("range_high"),
                        unit="PERCENT",
                        status=str(
                            submitted_prediction.get("status") or "INSUFFICIENT_DATA"
                        ),
                        confidence=_confidence_value(submitted_prediction.get("confidence")),
                        method_version=ANALYTICS_VERSION,
                        basis_json={
                            "input_sha256": input_sha256,
                            "sample_count": submitted_prediction.get("sample_count"),
                            "method_sha256": _digest(submitted_prediction.get("method")),
                        },
                    ),
                    ScoreSnapshot(
                        score_key="pricing.method",
                        score_type="PRICING_METHOD",
                        value=None,
                        unit=None,
                        status="AVAILABLE" if pricing_profile is not None else "UNSCORABLE",
                        confidence=1.0 if pricing_profile is not None else 0.0,
                        method_version=(
                            str(pricing_profile.get("profile_id"))
                            if pricing_profile is not None
                            else analysis_run.pricing_profile_version
                        ),
                        basis_json={
                            "input_sha256": input_sha256,
                            "applicability": (
                                pricing_profile.get("applicability")
                                if pricing_profile is not None
                                else "NO_EXACT_DOCUMENT_SHA256_MATCH"
                            ),
                            "profile_sha256": _digest(pricing_profile),
                        },
                    ),
                ]
            )
            for rank, department in enumerate(department_rankings, start=1):
                analysis_run.recommendations.append(
                    RecommendationSnapshot(
                        recommendation_key=f"department:{department['department_id']}",
                        department_id=str(department["department_id"]),
                        rank=rank,
                        priority_score=float(department["score"]),
                        recommendation=str(department["priority"]),
                        confidence=None,
                        risk_band=None,
                        detail_json={
                            "profile_version": department["profile_version"],
                            "department_name": department["department_name"],
                            "group": department["group"],
                            "ranking_scope": department["ranking_scope"],
                            "department_score": department["department_score"],
                            "matched_keyword_sha256": _digest(
                                {
                                    "baseline": department["matched_baseline_keywords"],
                                    "department": department["matched_department_keywords"],
                                    "regions": department["matched_regions"],
                                }
                            ),
                        },
                    )
                )
            bid_basis = {
                "eligibility": evaluation.eligibility,
                "eligibility_reason": evaluation.reason_code,
                "readiness_status": evaluation.readiness_status,
                "readiness_score": evaluation.readiness_score,
                "quantitative_status": quantitative.overall_status,
                "quantitative_band": quantitative.readiness_band,
                "quantitative_output_sha256": _digest(
                    quantitative.model_dump(mode="json")
                ),
                "business_risk_band": evaluation.risk_band,
                "competition_status": competition.get("status"),
                "competition_band": competition.get("band"),
                "competition_output_sha256": _digest(competition),
            }
            system_recommendation = _system_bid_recommendation(
                eligibility=evaluation.eligibility,
                readiness_status=evaluation.readiness_status,
                business_risk_band=evaluation.risk_band,
                quantitative_status=quantitative.overall_status,
                quantitative_band=quantitative.readiness_band,
                competition_band=competition.get("band"),
            )
            analysis_run.recommendations.append(
                RecommendationSnapshot(
                    recommendation_key="bid:system",
                    department_id=None,
                    rank=0,
                    priority_score=None,
                    recommendation=system_recommendation,
                    confidence=None,
                    risk_band=evaluation.risk_band,
                    detail_json={
                        "basis_sha256": _digest(bid_basis),
                        "eligibility": evaluation.eligibility,
                        "readiness_status": evaluation.readiness_status,
                        "quantitative_status": quantitative.overall_status,
                        "quantitative_band": quantitative.readiness_band,
                        "competition_status": competition.get("status"),
                        "competition_band": competition.get("band"),
                        "decision_boundary": (
                            "SYSTEM_ADVISORY_ONLY; user_decisions remains the human decision record"
                        ),
                    },
                )
            )
            session.flush()
            if _stage_hook:
                _stage_hook("after_snapshots")
            result = _run_result(analysis_run, reused=False)
        return result
    except IntegrityError:
        session.rollback()
        if idempotency_key is not None:
            with session.begin():
                existing = session.scalar(
                    select(AnalysisRun).where(
                        AnalysisRun.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    return _run_result(existing, reused=True)
        raise
    except Exception:
        session.rollback()
        raise


__all__ = [
    "AnalysisPipelineError",
    "AnalysisPipelineResult",
    "AnalysisPipelineSourceError",
    "AnalysisPipelineTransactionError",
    "MATERIALIZATION_VERSION",
    "PIPELINE_VERSION",
    "SNAPSHOT_VERSION",
    "run_analysis_pipeline",
]
