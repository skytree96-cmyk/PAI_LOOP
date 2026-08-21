from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
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
    load_public_company_profile,
)
from .evaluator import (
    MIN_EXTRACTION_CONFIDENCE,
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
    Evaluation,
    Notice,
    NoticeVersion,
    RecommendationSnapshot,
    ReferenceDataVersion,
    RequirementResultSnapshot,
    ScoreSnapshot,
)
from .pricing_profiles import pricing_profile_for_document
from .quantitative_scoring import (
    QUANTITATIVE_ENGINE_VERSION,
    estimate_for_notice,
    load_quantitative_profile_catalog,
)
from .pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_PROCESSING_VERSION,
    _validated_manifest_attachments,
)


PIPELINE_VERSION = "analysis-pipeline-0.4.0"
MATERIALIZATION_VERSION = "atomic-materializer-0.2.0"
SNAPSHOT_VERSION = "analysis-snapshot-0.2.0"
SOURCE_KIND = "OPENAI_REQUIREMENT_EXTRACTION"
MATERIALIZED_KIND = "ANALYSIS_PIPELINE_MATERIALIZATION"
RUN_KIND = "FULL_REVIEW"

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
    "자격",
    "참가",
    "면허",
    "등록",
    "인증",
    "직접생산",
    "지역",
    "소재",
    "실적",
    "인력",
    "시설",
    "공동수급",
    "컨소시엄",
    "부정당",
    "제재",
    "업종",
    "업체",
    "사업자",
    "법인",
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
                and isinstance(version.source_payload.get("attachment_manifest"), list)
            ),
            None,
        )
        if latest_metadata is not None:
            allowed_pps_sources = {
                str(item.get("attachment_id")): _digest(item)
                for item in latest_metadata.source_payload.get("attachment_manifest", [])
                if isinstance(item, dict) and isinstance(item.get("attachment_id"), str)
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
                )
            ]

    latest_by_attachment: dict[str, NoticeVersion] = {}
    for version in versions:
        payload = version.source_payload
        if not isinstance(payload, dict) or payload.get("kind") != SOURCE_KIND:
            if source_version_ids is not None and version.id in requested:
                raise AnalysisPipelineSourceError(
                    "an explicitly selected version is not an OpenAI requirement extraction"
                )
            continue
        if payload.get("prompt_version") != prompt_version:
            if source_version_ids is not None and version.id in requested:
                raise AnalysisPipelineSourceError(
                    "an explicitly selected source has a different prompt version"
                )
            continue
        if (
            payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
            and payload.get("processing_version") != PPS_PROCESSING_VERSION
        ):
            if source_version_ids is not None and version.id in requested:
                raise AnalysisPipelineSourceError(
                    "an explicitly selected PPS source has a different processing version"
                )
            continue
        attachment_id = _attachment_identity(payload, version)
        previous = latest_by_attachment.get(attachment_id)
        if previous is None or previous.version_no < version.version_no:
            latest_by_attachment[attachment_id] = version
    return sorted(
        latest_by_attachment.values(),
        key=lambda item: (_attachment_identity(item.source_payload or {}, item), item.version_no),
    )


def _parse_source(version: NoticeVersion, *, prompt_version: str) -> _SourceDocument:
    payload = version.source_payload if isinstance(version.source_payload, dict) else {}
    attachment_id = _attachment_identity(payload, version)
    document_sha256 = str(payload.get("document_sha256") or version.file_sha256).casefold()
    stored_prompt = str(payload.get("prompt_version") or "")
    schema_version = str(payload.get("schema_version") or "")
    status = str(payload.get("status") or version.extraction_status or "REVIEW").upper()
    model_name = payload.get("model") if isinstance(payload.get("model"), str) else None
    error_code = payload.get("error_code") if isinstance(payload.get("error_code"), str) else None
    warnings: list[str] = []

    if document_sha256 != version.file_sha256.casefold():
        warnings.append("DOCUMENT_SHA_MISMATCH")
    if stored_prompt != prompt_version:
        warnings.append("PROMPT_VERSION_MISMATCH")
    if schema_version != SCHEMA_VERSION:
        warnings.append("UNSUPPORTED_SCHEMA_VERSION")
    if (
        payload.get("source_kind") == PPS_ATTACHMENT_SOURCE
        and payload.get("processing_version") != PPS_PROCESSING_VERSION
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
        and stored_prompt == prompt_version
        and schema_version == SCHEMA_VERSION
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
    by_key = {str(item.get("requirement_id")): item for item in classified["items"]}
    return [(item, by_key[item.requirement_key]) for item in merged]


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
    values = [anchor.confidence for anchor in item.anchors] + item.source_confidences
    return min(values) if values else 0.0


def _has_verified_anchor(item: _MergedRequirement) -> bool:
    """Trust only exact anchors already accepted by the extraction boundary."""

    if (
        not item.anchors
        or not item.attachment_ids
        or not item.source_document_sha256s
        or _parse_confidence(item) < MIN_EXTRACTION_CONFIDENCE
    ):
        return False
    return all(
        bool(anchor.quote.strip())
        and anchor.attachment_id in item.attachment_ids
        and anchor.confidence >= MIN_EXTRACTION_CONFIDENCE
        for anchor in item.anchors
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


def _partial_gate_candidate_keys(
    *,
    run_status: str,
    sources: Sequence[_SourceDocument],
    policy_items: Sequence[tuple[_MergedRequirement, dict[str, Any]]],
) -> tuple[frozenset[str], frozenset[str]]:
    if run_status != "PARTIAL" or not _known_non_eligibility_gaps_only(sources):
        return frozenset(), frozenset()

    eligibility_items = [
        item
        for item, policy in policy_items
        if item.requirement.mandatory and policy.get("policy_class") == "ELIGIBILITY"
    ]
    if not eligibility_items or not all(_has_verified_anchor(item) for item in eligibility_items):
        return frozenset(), frozenset()

    verified_materialized_keys = frozenset(
        item.requirement_key
        for item, policy in policy_items
        if _is_materialized_policy_item(item, policy) and _has_verified_anchor(item)
    )
    return verified_materialized_keys, frozenset(
        item.requirement_key for item in eligibility_items
    )


def _company_eligibility_verdict_complete(
    evaluation: EvaluationResult,
    *,
    requirements: Sequence[AtomicRequirement],
    eligibility_keys: frozenset[str],
) -> bool:
    if not eligibility_keys:
        return False
    by_key = {str(item.get("requirement_key")): item for item in evaluation.atomic_results}
    requirement_by_key = {item.requirement_key: item for item in requirements}
    for key in eligibility_keys:
        requirement = requirement_by_key.get(key)
        result = by_key.get(key)
        if requirement is None or result is None:
            return False
        if result.get("result") not in {"PASS", "FAIL"} or result.get("actual_value") is None:
            return False
        if requirement.evidence_required and not result.get("evidence_valid"):
            return False
    return True


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
    if eligibility_items:
        qualification = {"PASS": 10.0, "REVIEW": 60.0, "FAIL": 100.0}[
            evaluation.eligibility.value
        ]
        dimensions["qualification"] = qualification
        basis["qualification"] = {
            "source": "DETERMINISTIC_ELIGIBILITY_EVALUATION",
            "method": "PASS=10; REVIEW=60; FAIL=100",
            "requirement_count": len(eligibility_items),
            "outcome": evaluation.eligibility.value,
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
        mapped_fact = policy.get("company_fact_key")
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
        operator="eq",
        required_value=True,
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
        review_trigger_value="__MISSING__",
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
            and isinstance(version.source_payload.get("attachment_manifest"), list)
        ),
        None,
    )
    if metadata is None or not isinstance(metadata.source_payload, dict):
        return None
    raw_manifest = [
        dict(item)
        for item in metadata.source_payload.get("attachment_manifest", [])
        if isinstance(item, dict)
    ]
    validated_manifest, invalid_count = _validated_manifest_attachments(raw_manifest)
    expected = [
        {
            "attachment_id": str(item.get("attachment_id") or ""),
            "manifest_sha256": _digest(item),
        }
        for item in validated_manifest
    ]
    expected_by_id = {item["attachment_id"]: item["manifest_sha256"] for item in expected}
    attempts: dict[str, NoticeVersion] = {}
    for version in sorted(versions, key=lambda item: item.version_no):
        payload = version.source_payload
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != SOURCE_KIND
            or payload.get("source_kind") != "PPS_PUBLIC_ATTACHMENT"
            or payload.get("prompt_version") != prompt_version
            or payload.get("processing_version") != PPS_PROCESSING_VERSION
        ):
            continue
        attachment_id = str(payload.get("attachment_id") or "")
        if payload.get("manifest_sha256") != expected_by_id.get(attachment_id):
            continue
        attempts[attachment_id] = version
    accepted_ids = sorted(
        attachment_id
        for attachment_id, version in attempts.items()
        if isinstance(version.source_payload, dict)
        and version.source_payload.get("status") == "ACCEPTED"
        and version.extraction_status in {"ACCEPTED", "COMPLETE"}
        and version.document_complete
    )
    audited_ids = sorted(attempts)
    expected_ids = sorted(expected_by_id)
    coverage_complete = (
        bool(expected)
        and invalid_count == 0
        and len(expected) == len(raw_manifest)
        and len(expected_by_id) == len(expected)
        and audited_ids == expected_ids
    )
    return {
        "metadata_version_id": metadata.id,
        "manifest_sha256": _digest(raw_manifest),
        "expected_attachments": sorted(
            expected,
            key=lambda item: (item["attachment_id"], item["manifest_sha256"]),
        ),
        "expected_attachment_ids": expected_ids,
        "audited_attachment_ids": audited_ids,
        "accepted_attachment_ids": accepted_ids,
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


def _aggregate_source_gaps(
    sources: Sequence[_SourceDocument],
) -> tuple[list[str], list[str]]:
    """Resolve only exact document-presence gaps using accepted typed siblings."""

    available_types = {
        source.data.document_type
        for source in sources
        if source.materializable and source.data is not None
    }
    unresolved: list[str] = []
    resolved: list[str] = []
    for source in sources:
        if source.data is None:
            continue
        for raw_gap in source.data.missing_or_unreadable:
            gap = _normalise_text(raw_gap)
            matched_type = next(
                (
                    document_type
                    for pattern, document_type in _TYPED_SIBLING_GAP_RULES
                    if pattern.fullmatch(gap)
                ),
                None,
            )
            if matched_type is not None and matched_type in available_types:
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
    if eligibility == "FAIL" or business_risk_band == "NO_GO":
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

            selected_versions = _select_source_versions(
                session,
                notice_id=notice_id,
                prompt_version=prompt_version,
                source_version_ids=source_version_ids,
            )
            sources = [
                _parse_source(version, prompt_version=prompt_version)
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
            fact_manifest = _selected_fact_manifest(
                company_facts,
                fact_keys={item.fact_key for item in prospective_atomics},
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
            quantitative = estimate_for_notice(notice)
            quantitative_catalog = load_quantitative_profile_catalog()
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
            if not materialized_policy_items:
                warnings.append("NO_ELIGIBILITY_OR_ACTION_REQUIREMENTS")
            if not sources or accepted_source_count == 0:
                run_status = "FAILED"
            elif (
                any(
                    not _source_effectively_complete(
                        source,
                        unresolved_gaps=unresolved_gap_set,
                    )
                    for source in sources
                )
                or not materialized_policy_items
                or (
                    pps_manifest_basis is not None
                    and not pps_manifest_basis["coverage_complete"]
                )
            ):
                run_status = "PARTIAL"
            else:
                run_status = "COMPLETED"
            warnings = sorted(set(warnings))
            gate_candidate_keys, eligibility_gate_keys = _partial_gate_candidate_keys(
                run_status=run_status,
                sources=sources,
                policy_items=policy_items,
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
                company_facts,
                verified_document_requirement_keys=gate_candidate_keys,
                risk_dimensions={},
            )
            eligibility_gate_applied = bool(
                gate_candidate_keys
                and _company_eligibility_verdict_complete(
                    provisional_evaluation,
                    requirements=prospective_atomics,
                    eligibility_keys=eligibility_gate_keys,
                )
            )
            verified_requirement_keys = (
                gate_candidate_keys if eligibility_gate_applied else frozenset()
            )
            if gate_candidate_keys and not eligibility_gate_applied:
                provisional_evaluation = evaluate_notice(
                    notice,
                    materialized_version,
                    prospective_atomics,
                    company_facts,
                    risk_dimensions={},
                )
            if eligibility_gate_applied:
                warnings = sorted(
                    set(warnings) | {"NON_ELIGIBILITY_PARTIAL_GATE_APPLIED"}
                )
                materialized_version.source_payload = {
                    **(materialized_version.source_payload or {}),
                    "review_code": None,
                    "eligibility_gate_applied": True,
                    "eligibility_gate_basis": (
                        "ALL_MANDATORY_ELIGIBILITY_ANCHORS_VERIFIED_AND_COMPANY_VERDICT_COMPLETE"
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
                company_facts,
                verified_document_requirement_keys=verified_requirement_keys,
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
                quantitative_profile_version=reference_versions.get(
                    "quantitative_notice_profiles",
                    str(quantitative_catalog.get("profile_version") or "UNVERSIONED"),
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
                    "company_profile": profile_version,
                    "department_profile": str(department_catalog["version"]),
                    "quantitative_profile": str(
                        quantitative_catalog.get("profile_version") or "UNVERSIONED"
                    ),
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
                            "total_max_points": quantitative.total_max_points,
                            "confirmed_points": quantitative.confirmed_points,
                            "evidence_coverage_pct": quantitative.evidence_coverage_pct,
                            "profile_output_sha256": _digest(
                                quantitative.model_dump(mode="json")
                            ),
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
