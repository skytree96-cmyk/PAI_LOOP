from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .integrations.openai_extraction import (
    ExtractionPayload,
    EvidenceAnchor,
    KNOWN_QUANTITATIVE_EVIDENCE_KEYS,
    KnownQuantitativeMetric,
    PROMPT_VERSION,
    QuantitativeBracketLiteral,
    QuantitativeRuleCandidate,
    QuantitativeScoringMethod,
    QuantitativeTableCandidate,
    QuantitativeThresholdLiteral,
    SCHEMA_VERSION,
    evidence_quote_matches_source,
)


QUANTITATIVE_CANDIDATE_PROFILE_VERSION = "pai-loop-quantitative-candidate-profile-0.1.0"
QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION = "pai-loop-quantitative-attachment-validator-0.1.0"

ProfileStatus = Literal["AVAILABLE", "REVIEW", "INCOMPLETE", "NOT_APPLICABLE"]
CandidateStatus = Literal["AVAILABLE", "REVIEW", "INCOMPLETE"]
IssueDisposition = Literal["REVIEW", "INCOMPLETE"]
AttachmentRecordStatus = Literal[
    "AVAILABLE",
    "REVIEW",
    "INCOMPLETE",
    "NO_TABLE",
    "NOT_APPLICABLE",
]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ImmutableEvidenceAnchor(FrozenModel):
    attachment_id: str
    page: int | None = Field(ge=1)
    section: str | None
    quote: str = Field(max_length=500)
    confidence: float = Field(ge=0, le=1)


class QuantitativeValidationIssue(FrozenModel):
    code: str
    disposition: IssueDisposition
    message: str
    attachment_id: str | None = None
    table_id: str | None = None
    criterion_id: str | None = None


class ImmutableQuantitativeBracket(FrozenModel):
    label: str
    literal: str
    min_value: float | None
    max_value: float | None
    min_inclusive: bool
    max_inclusive: bool
    points: float = Field(ge=0)
    evidence: ImmutableEvidenceAnchor


class ImmutableQuantitativeThreshold(FrozenModel):
    literal: str
    operator: Literal["GT", "GTE", "LT", "LTE", "EQ"]
    threshold_value: float
    points_if_met: float = Field(ge=0)
    points_if_not_met: float = Field(ge=0)
    evidence: ImmutableEvidenceAnchor


class ImmutableQuantitativeRuleCandidate(FrozenModel):
    status: Literal["AVAILABLE"] = "AVAILABLE"
    source_attachment_id: str
    table_id: str
    criterion_id: str
    label: str
    criterion_literal: str
    max_points: float = Field(gt=0)
    scoring_method: QuantitativeScoringMethod
    metric: KnownQuantitativeMetric
    unit: str | None
    brackets: tuple[ImmutableQuantitativeBracket, ...]
    threshold: ImmutableQuantitativeThreshold | None
    formula_literal: str | None
    required_evidence: tuple[str, ...]
    evidence: ImmutableEvidenceAnchor


class QuantitativeReviewCandidate(FrozenModel):
    status: Literal["REVIEW", "INCOMPLETE"]
    source_attachment_id: str
    table_id: str
    criterion_id: str
    label: str
    max_points: float
    scoring_method: QuantitativeScoringMethod
    metric: KnownQuantitativeMetric
    issue_codes: tuple[str, ...]


class ImmutableQuantitativeTable(FrozenModel):
    source_attachment_id: str
    table_id: str
    label: str
    status: CandidateStatus
    total_points: float | None
    total_evidence: ImmutableEvidenceAnchor | None
    minimum_score: float | None
    minimum_evidence: ImmutableEvidenceAnchor | None
    criterion_ids: tuple[str, ...]
    available_criterion_ids: tuple[str, ...]
    review_criterion_ids: tuple[str, ...]


class AttachmentDocumentBinding(FrozenModel):
    attachment_id: str
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class QuantitativeCandidateProfile(FrozenModel):
    schema_version: str = QUANTITATIVE_CANDIDATE_PROFILE_VERSION
    status: ProfileStatus
    manifest_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    document_bindings: tuple[AttachmentDocumentBinding, ...] = ()
    expected_attachment_ids: tuple[str, ...]
    processed_attachment_ids: tuple[str, ...]
    tables: tuple[ImmutableQuantitativeTable, ...]
    available_candidates: tuple[ImmutableQuantitativeRuleCandidate, ...]
    review_candidates: tuple[QuantitativeReviewCandidate, ...]
    not_applicable_evidence: tuple[ImmutableEvidenceAnchor, ...]
    issues: tuple[QuantitativeValidationIssue, ...]
class ValidatedQuantitativeAttachmentRecord(FrozenModel):
    """Persistable validation result that intentionally excludes raw source text."""

    validator_version: str = QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION
    extraction_schema_version: str
    prompt_version: str
    attachment_id: str
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: AttachmentRecordStatus
    tables: tuple[ImmutableQuantitativeTable, ...]
    available_candidates: tuple[ImmutableQuantitativeRuleCandidate, ...]
    review_candidates: tuple[QuantitativeReviewCandidate, ...]
    not_applicable_evidence: tuple[ImmutableEvidenceAnchor, ...]
    issues: tuple[QuantitativeValidationIssue, ...]
    validation_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_persisted_invariants(self) -> "ValidatedQuantitativeAttachmentRecord":
        _assert_validated_record_invariants(self)
        return self


_NUMBER_RE = re.compile(r"-?(?:\d[\d,]*)(?:\.\d+)?")
_PLACEHOLDER_NORMALISED = {
    "",
    "NA",
    "NONE",
    "NULL",
    "TBD",
    "TODO",
    "UNKNOWN",
    "MISSING",
    "PLACEHOLDER",
    "TOBECONFIRMED",
    "미정",
    "미확인",
    "확인필요",
    "추후확인",
    "없음",
}

_NUM_PATTERN = r"-?(?:\d[\d,]*)(?:\.\d+)?"
_UNIT_PATTERN = (
    r"(?:원|천\s*원|만\s*원|백만\s*원|천만\s*원|억\s*원|건|명|개|점|%|"
    r"퍼센트|년|개월|회|등급|㎡|m2|m²|㎥)"
)
_KOREAN_BOUND_RE = re.compile(
    rf"(?P<num>{_NUM_PATTERN})\s*(?:{_UNIT_PATTERN})?\s*"
    r"(?P<op>이상|초과|이하|미만)",
    re.IGNORECASE,
)
_ASCII_DIRECT_BOUND_RE = re.compile(
    rf"(?P<op>>=|<=|==|>|<|=)\s*(?P<num>{_NUM_PATTERN})"
)
_ASCII_REVERSED_BOUND_RE = re.compile(
    rf"(?P<num>{_NUM_PATTERN})\s*(?:{_UNIT_PATTERN})?\s*"
    r"(?P<op>>=|<=|==|>|<|=)\s*(?=[A-Za-z가-힣_(])",
    re.IGNORECASE,
)
_COMPARATOR_MARKER_RE = re.compile(r"이상|초과|이하|미만|>=|<=|==|>|<|=")

_KOREAN_OPERATOR = {
    "이상": "GTE",
    "초과": "GT",
    "이하": "LTE",
    "미만": "LT",
}
_DIRECT_OPERATOR = {">=": "GTE", ">": "GT", "<=": "LTE", "<": "LT", "=": "EQ", "==": "EQ"}
_REVERSED_OPERATOR = {"<=": "GTE", "<": "GT", ">=": "LTE", ">": "LT", "=": "EQ", "==": "EQ"}


def _comparator_terms(literal: str) -> tuple[tuple[Decimal, str], ...]:
    terms: list[tuple[Decimal, str]] = []
    for regex, operator_map in (
        (_KOREAN_BOUND_RE, _KOREAN_OPERATOR),
        (_ASCII_DIRECT_BOUND_RE, _DIRECT_OPERATOR),
        (_ASCII_REVERSED_BOUND_RE, _REVERSED_OPERATOR),
    ):
        for match in regex.finditer(literal):
            try:
                value = Decimal(match.group("num").replace(",", ""))
            except InvalidOperation:
                continue
            term = (value, operator_map[match.group("op")])
            terms.append(term)
    return tuple(terms)


def _expected_bracket_terms(
    bracket: QuantitativeBracketLiteral,
) -> tuple[tuple[Decimal, str], ...]:
    expected: list[tuple[Decimal, str]] = []
    if bracket.min_value is not None:
        value = _decimal(bracket.min_value)
        if value is not None:
            expected.append((value, "GTE" if bracket.min_inclusive else "GT"))
    if bracket.max_value is not None:
        value = _decimal(bracket.max_value)
        if value is not None:
            expected.append((value, "LTE" if bracket.max_inclusive else "LT"))
    return tuple(expected)


def _comparator_binding_issue(
    *,
    literal: str,
    expected: tuple[tuple[Decimal, str], ...],
    mismatch_code: str,
    mismatch_message: str,
    context: Mapping[str, str],
) -> QuantitativeValidationIssue | None:
    parsed = _comparator_terms(literal)
    if Counter(parsed) == Counter(expected):
        if expected or not _COMPARATOR_MARKER_RE.search(literal):
            return None
    if _COMPARATOR_MARKER_RE.search(literal) and not parsed:
        return _issue(
            "COMPARATOR_GRAMMAR_UNSUPPORTED",
            "REVIEW",
            "비교 연산 문구를 제한된 문법으로 정확히 해석할 수 없습니다.",
            **context,
        )
    return _issue(
        mismatch_code,
        "INCOMPLETE",
        mismatch_message,
        **context,
    )


def _frozen_anchor(anchor: EvidenceAnchor) -> ImmutableEvidenceAnchor:
    return ImmutableEvidenceAnchor.model_validate(anchor.model_dump(mode="python"))


def _issue(
    code: str,
    disposition: IssueDisposition,
    message: str,
    *,
    attachment_id: str | None = None,
    table_id: str | None = None,
    criterion_id: str | None = None,
) -> QuantitativeValidationIssue:
    return QuantitativeValidationIssue(
        code=code,
        disposition=disposition,
        message=message,
        attachment_id=attachment_id,
        table_id=table_id,
        criterion_id=criterion_id,
    )


def _candidate_status(issues: Iterable[QuantitativeValidationIssue]) -> CandidateStatus:
    dispositions = {item.disposition for item in issues}
    if "INCOMPLETE" in dispositions:
        return "INCOMPLETE"
    if "REVIEW" in dispositions:
        return "REVIEW"
    return "AVAILABLE"


def _decimal(value: float) -> Decimal | None:
    if not math.isfinite(value):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _literal_numbers(value: str) -> set[Decimal]:
    numbers: set[Decimal] = set()
    for match in _NUMBER_RE.finditer(value):
        try:
            numbers.add(Decimal(match.group(0).replace(",", "")))
        except InvalidOperation:
            continue
    return numbers


def _literal_contains_number(value: float, literal: str) -> bool:
    expected = _decimal(value)
    return expected is not None and expected in _literal_numbers(literal)


def _literal_is_anchored(literal: str, anchor: EvidenceAnchor, source: str) -> bool:
    return evidence_quote_matches_source(
        anchor.quote, source
    ) and evidence_quote_matches_source(literal, anchor.quote)


def _anchor_issues(
    anchor: EvidenceAnchor,
    *,
    sources: Mapping[str, str],
    expected_attachment_ids: set[str],
    payload_attachment_id: str,
    table_id: str | None = None,
    criterion_id: str | None = None,
) -> list[QuantitativeValidationIssue]:
    context = {
        "attachment_id": payload_attachment_id,
        "table_id": table_id,
        "criterion_id": criterion_id,
    }
    if anchor.attachment_id not in expected_attachment_ids:
        return [
            _issue(
                "UNKNOWN_ATTACHMENT",
                "INCOMPLETE",
                "근거가 현재 첨부 manifest 밖의 파일을 가리킵니다.",
                **context,
            )
        ]
    if anchor.attachment_id != payload_attachment_id:
        return [
            _issue(
                "CROSS_ATTACHMENT_ANCHOR",
                "INCOMPLETE",
                "첨부별 추출 결과가 다른 첨부의 근거를 가리킵니다.",
                **context,
            )
        ]
    source = sources.get(anchor.attachment_id)
    if source is None:
        return [
            _issue(
                "SOURCE_TEXT_MISSING",
                "INCOMPLETE",
                "근거 첨부의 원문 텍스트가 없습니다.",
                **context,
            )
        ]
    if not evidence_quote_matches_source(anchor.quote, source):
        return [
            _issue(
                "UNVERIFIED_QUOTE",
                "INCOMPLETE",
                "근거 인용문을 해당 첨부 원문에서 정확히 확인할 수 없습니다.",
                **context,
            )
        ]
    return []


def _placeholder_evidence_key(value: str) -> bool:
    normalized = re.sub(r"[^0-9A-Z가-힣]", "", value.strip().upper())
    return normalized in _PLACEHOLDER_NORMALISED or any(
        token in normalized
        for token in ("PLACEHOLDER", "TOBECONFIRMED", "추후확인", "확인필요")
    )


def _required_evidence_is_registered(
    metric: str,
    required_evidence: Iterable[str],
) -> bool:
    values = tuple(required_evidence)
    allowed = KNOWN_QUANTITATIVE_EVIDENCE_KEYS.get(metric)
    return (
        allowed is not None
        and len(values) == len(set(values))
        and set(values) == set(allowed)
    )


def _brackets_overlap(
    brackets: Iterable[QuantitativeBracketLiteral | ImmutableQuantitativeBracket],
) -> bool:
    ordered = sorted(
        brackets,
        key=lambda item: (
            float("-inf") if item.min_value is None else item.min_value,
            float("inf") if item.max_value is None else item.max_value,
        ),
    )
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.max_value is None or right.min_value is None:
            return True
        if right.min_value < left.max_value:
            return True
        if (
            right.min_value == left.max_value
            and left.max_inclusive
            and right.min_inclusive
        ):
            return True
    return False


def _assert_available_candidate_invariants(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> None:
    if candidate.metric == "UNKNOWN" or candidate.scoring_method == "UNKNOWN":
        raise ValueError("AVAILABLE candidate cannot use UNKNOWN metric or method")
    if not _required_evidence_is_registered(
        candidate.metric,
        candidate.required_evidence,
    ):
        raise ValueError("AVAILABLE candidate evidence keys are not registered")
    if not evidence_quote_matches_source(
        candidate.criterion_literal,
        candidate.evidence.quote,
    ) or not _literal_contains_number(
        candidate.max_points,
        candidate.criterion_literal,
    ):
        raise ValueError("AVAILABLE candidate literal is not bound to its anchor")

    if candidate.scoring_method == "BRACKET":
        if not candidate.brackets or candidate.threshold is not None or candidate.formula_literal:
            raise ValueError("AVAILABLE BRACKET candidate shape is invalid")
        for bracket in candidate.brackets:
            if not evidence_quote_matches_source(bracket.literal, bracket.evidence.quote):
                raise ValueError("AVAILABLE bracket literal is not bound to its anchor")
            values = [bracket.points]
            if bracket.min_value is not None:
                values.append(bracket.min_value)
            if bracket.max_value is not None:
                values.append(bracket.max_value)
            if any(
                not _literal_contains_number(value, bracket.literal)
                for value in values
            ):
                raise ValueError("AVAILABLE bracket numbers do not match its literal")
            if bracket.points > candidate.max_points:
                raise ValueError("AVAILABLE bracket points exceed criterion maximum")
            if (
                bracket.min_value is not None
                and bracket.max_value is not None
                and bracket.min_value >= bracket.max_value
            ):
                raise ValueError("AVAILABLE bracket bounds are invalid")
            if Counter(_comparator_terms(bracket.literal)) != Counter(
                _expected_bracket_terms(bracket)
            ):
                raise ValueError("AVAILABLE bracket comparator binding is invalid")
        if _brackets_overlap(candidate.brackets):
            raise ValueError("AVAILABLE bracket ranges overlap")
    elif candidate.scoring_method == "THRESHOLD":
        if candidate.threshold is None or candidate.brackets or candidate.formula_literal:
            raise ValueError("AVAILABLE THRESHOLD candidate shape is invalid")
        threshold = candidate.threshold
        if not evidence_quote_matches_source(
            threshold.literal,
            threshold.evidence.quote,
        ):
            raise ValueError("AVAILABLE threshold literal is not bound to its anchor")
        if any(
            not _literal_contains_number(value, threshold.literal)
            for value in (
                threshold.threshold_value,
                threshold.points_if_met,
                threshold.points_if_not_met,
            )
        ):
            raise ValueError("AVAILABLE threshold numbers do not match its literal")
        if (
            threshold.points_if_met > candidate.max_points
            or threshold.points_if_not_met > candidate.max_points
        ):
            raise ValueError("AVAILABLE threshold points exceed criterion maximum")
        threshold_value = _decimal(threshold.threshold_value)
        expected = (
            ((threshold_value, threshold.operator),)
            if threshold_value is not None
            else ()
        )
        if Counter(_comparator_terms(threshold.literal)) != Counter(expected):
            raise ValueError("AVAILABLE threshold comparator binding is invalid")
    elif candidate.scoring_method == "FORMULA":
        if candidate.brackets or candidate.threshold is not None or not candidate.formula_literal:
            raise ValueError("AVAILABLE FORMULA candidate shape is invalid")
        if not evidence_quote_matches_source(
            candidate.formula_literal,
            candidate.evidence.quote,
        ):
            raise ValueError("AVAILABLE formula literal is not bound to its anchor")


def _record_anchors(
    record: ValidatedQuantitativeAttachmentRecord,
) -> Iterable[ImmutableEvidenceAnchor]:
    for table in record.tables:
        if table.total_evidence is not None:
            yield table.total_evidence
        if table.minimum_evidence is not None:
            yield table.minimum_evidence
    for candidate in record.available_candidates:
        yield candidate.evidence
        for bracket in candidate.brackets:
            yield bracket.evidence
        if candidate.threshold is not None:
            yield candidate.threshold.evidence
    yield from record.not_applicable_evidence


def _assert_validated_record_invariants(
    record: ValidatedQuantitativeAttachmentRecord,
) -> None:
    attachment_id = record.attachment_id
    if any(anchor.attachment_id != attachment_id for anchor in _record_anchors(record)):
        raise ValueError("all persisted anchors must match record attachment_id")
    if any(table.source_attachment_id != attachment_id for table in record.tables):
        raise ValueError("all persisted tables must match record attachment_id")
    if any(
        candidate.source_attachment_id != attachment_id
        for candidate in (*record.available_candidates, *record.review_candidates)
    ):
        raise ValueError("all persisted candidates must match record attachment_id")
    if any(issue.attachment_id != attachment_id for issue in record.issues):
        raise ValueError("all persisted issues must match record attachment_id")

    table_ids = [table.table_id for table in record.tables]
    if len(table_ids) != len(set(table_ids)):
        raise ValueError("persisted table IDs must be unique per attachment")
    known_table_ids = set(table_ids)
    if any(
        candidate.table_id not in known_table_ids
        for candidate in (*record.available_candidates, *record.review_candidates)
    ):
        raise ValueError("persisted candidate references an unknown table")

    for candidate in record.available_candidates:
        _assert_available_candidate_invariants(candidate)

    for table in record.tables:
        available = [
            item for item in record.available_candidates if item.table_id == table.table_id
        ]
        review = [
            item for item in record.review_candidates if item.table_id == table.table_id
        ]
        if Counter(table.available_criterion_ids) != Counter(
            item.criterion_id for item in available
        ):
            raise ValueError("table AVAILABLE criterion linkage is inconsistent")
        if Counter(table.review_criterion_ids) != Counter(
            item.criterion_id for item in review
        ):
            raise ValueError("table REVIEW criterion linkage is inconsistent")
        if Counter(table.criterion_ids) != Counter(
            [
                *(item.criterion_id for item in available),
                *(item.criterion_id for item in review),
            ]
        ):
            raise ValueError("table criterion linkage is incomplete")

        table_issues = [item for item in record.issues if item.table_id == table.table_id]
        signals = [
            *table_issues,
            *(
                _issue("REVIEW_CANDIDATE", item.status, "", attachment_id=attachment_id)
                for item in review
            ),
        ]
        if table.status != _candidate_status(signals):
            raise ValueError("table status is inconsistent with nested candidates/issues")
        if table.status != "INCOMPLETE":
            if table.total_points is None or table.total_evidence is None:
                raise ValueError("complete/review table must retain total and evidence")
            all_rows = [*available, *review]
            if sum(
                (Decimal(str(item.max_points)) for item in all_rows),
                Decimal("0"),
            ) != Decimal(str(table.total_points)):
                raise ValueError("complete/review table total does not reconcile")
            if not _literal_contains_number(
                table.total_points,
                table.total_evidence.quote,
            ):
                raise ValueError("table total is not present in its anchor")
            if (table.minimum_score is None) != (table.minimum_evidence is None):
                raise ValueError("table minimum and evidence must be paired")
            if table.minimum_score is not None and table.minimum_evidence is not None:
                if not _literal_contains_number(
                    table.minimum_score,
                    table.minimum_evidence.quote,
                ):
                    raise ValueError("table minimum is not present in its anchor")
                if table.minimum_score > table.total_points:
                    raise ValueError("table minimum exceeds total")

    has_incomplete = any(
        item.disposition == "INCOMPLETE" for item in record.issues
    ) or any(table.status == "INCOMPLETE" for table in record.tables) or any(
        item.status == "INCOMPLETE" for item in record.review_candidates
    )
    has_review = any(item.disposition == "REVIEW" for item in record.issues) or any(
        table.status == "REVIEW" for table in record.tables
    ) or any(item.status == "REVIEW" for item in record.review_candidates)

    if record.status == "NO_TABLE":
        if any(
            (
                record.tables,
                record.available_candidates,
                record.review_candidates,
                record.not_applicable_evidence,
                record.issues,
            )
        ):
            raise ValueError("NO_TABLE record must have an empty nested shape")
    elif record.status == "NOT_APPLICABLE":
        if (
            record.tables
            or record.available_candidates
            or record.review_candidates
            or not record.not_applicable_evidence
            or record.issues
        ):
            raise ValueError("NOT_APPLICABLE record shape is inconsistent")
    elif record.status == "AVAILABLE":
        if (
            not record.tables
            or not record.available_candidates
            or record.review_candidates
            or record.not_applicable_evidence
            or record.issues
            or any(table.status != "AVAILABLE" for table in record.tables)
        ):
            raise ValueError("AVAILABLE record shape is inconsistent")
    elif record.status == "REVIEW":
        if has_incomplete or not has_review or not record.tables:
            raise ValueError("REVIEW record shape/status is inconsistent")
    elif record.status == "INCOMPLETE" and not has_incomplete:
        raise ValueError("INCOMPLETE record lacks an incomplete nested signal")


def _validate_brackets(
    candidate: QuantitativeRuleCandidate,
    *,
    source: str,
    sources: Mapping[str, str],
    expected_attachment_ids: set[str],
    payload_attachment_id: str,
    table_id: str,
) -> tuple[list[QuantitativeValidationIssue], tuple[ImmutableQuantitativeBracket, ...]]:
    issues: list[QuantitativeValidationIssue] = []
    frozen: list[ImmutableQuantitativeBracket] = []
    context = {
        "attachment_id": payload_attachment_id,
        "table_id": table_id,
        "criterion_id": candidate.criterion_id,
    }
    for bracket in candidate.brackets:
        issues.extend(
            _anchor_issues(
                bracket.evidence,
                sources=sources,
                expected_attachment_ids=expected_attachment_ids,
                payload_attachment_id=payload_attachment_id,
                table_id=table_id,
                criterion_id=candidate.criterion_id,
            )
        )
        if not _literal_is_anchored(bracket.literal, bracket.evidence, source):
            issues.append(
                _issue(
                    "BRACKET_LITERAL_MISMATCH",
                    "INCOMPLETE",
                    "배점 구간 literal을 근거 인용문에서 확인할 수 없습니다.",
                    **context,
                )
            )
        values = [bracket.points]
        if bracket.min_value is not None:
            values.append(bracket.min_value)
        if bracket.max_value is not None:
            values.append(bracket.max_value)
        if any(not _literal_contains_number(value, bracket.literal) for value in values):
            issues.append(
                _issue(
                    "BRACKET_NUMBER_MISMATCH",
                    "INCOMPLETE",
                    "구조화한 배점 구간 숫자가 literal과 일치하지 않습니다.",
                    **context,
                )
            )
        if bracket.points > candidate.max_points:
            issues.append(
                _issue(
                    "BRACKET_POINTS_EXCEED_MAX",
                    "INCOMPLETE",
                    "배점 구간 점수가 항목 만점을 초과합니다.",
                    **context,
                )
            )
        if (
            bracket.min_value is not None
            and bracket.max_value is not None
            and bracket.min_value >= bracket.max_value
        ):
            issues.append(
                _issue(
                    "INVALID_BRACKET_BOUNDS",
                    "INCOMPLETE",
                    "배점 구간 하한은 상한보다 작아야 합니다.",
                    **context,
                )
            )
        comparator_issue = _comparator_binding_issue(
            literal=bracket.literal,
            expected=_expected_bracket_terms(bracket),
            mismatch_code="BRACKET_COMPARATOR_MISMATCH",
            mismatch_message=(
                "배점 구간의 원문 비교 연산자와 하한·상한 포함 여부가 일치하지 않습니다."
            ),
            context=context,
        )
        if comparator_issue is not None:
            issues.append(comparator_issue)
        frozen.append(
            ImmutableQuantitativeBracket(
                label=bracket.label,
                literal=bracket.literal,
                min_value=bracket.min_value,
                max_value=bracket.max_value,
                min_inclusive=bracket.min_inclusive,
                max_inclusive=bracket.max_inclusive,
                points=bracket.points,
                evidence=_frozen_anchor(bracket.evidence),
            )
        )

    ordered = sorted(
        candidate.brackets,
        key=lambda item: (
            float("-inf") if item.min_value is None else item.min_value,
            float("inf") if item.max_value is None else item.max_value,
        ),
    )
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.max_value is None:
            overlaps = True
        elif right.min_value is None:
            overlaps = True
        elif right.min_value < left.max_value:
            overlaps = True
        else:
            overlaps = (
                right.min_value == left.max_value
                and left.max_inclusive
                and right.min_inclusive
            )
        if overlaps:
            issues.append(
                _issue(
                    "OVERLAPPING_BRACKETS",
                    "INCOMPLETE",
                    "배점 구간이 서로 겹칩니다.",
                    **context,
                )
            )
            break
    return issues, tuple(frozen)


def _validate_threshold(
    candidate: QuantitativeRuleCandidate,
    threshold: QuantitativeThresholdLiteral,
    *,
    source: str,
    sources: Mapping[str, str],
    expected_attachment_ids: set[str],
    payload_attachment_id: str,
    table_id: str,
) -> tuple[list[QuantitativeValidationIssue], ImmutableQuantitativeThreshold | None]:
    context = {
        "attachment_id": payload_attachment_id,
        "table_id": table_id,
        "criterion_id": candidate.criterion_id,
    }
    issues = _anchor_issues(
        threshold.evidence,
        sources=sources,
        expected_attachment_ids=expected_attachment_ids,
        payload_attachment_id=payload_attachment_id,
        table_id=table_id,
        criterion_id=candidate.criterion_id,
    )
    if not _literal_is_anchored(threshold.literal, threshold.evidence, source):
        issues.append(
            _issue(
                "THRESHOLD_LITERAL_MISMATCH",
                "INCOMPLETE",
                "임계값 literal을 근거 인용문에서 확인할 수 없습니다.",
                **context,
            )
        )
    values = [threshold.threshold_value, threshold.points_if_met]
    if threshold.points_if_not_met is not None:
        values.append(threshold.points_if_not_met)
    if any(not _literal_contains_number(value, threshold.literal) for value in values):
        issues.append(
            _issue(
                "THRESHOLD_NUMBER_MISMATCH",
                "INCOMPLETE",
                "구조화한 임계값 또는 배점 숫자가 literal과 일치하지 않습니다.",
                **context,
            )
        )
    if threshold.points_if_met > candidate.max_points or (
        threshold.points_if_not_met is not None
        and threshold.points_if_not_met > candidate.max_points
    ):
        issues.append(
            _issue(
                "THRESHOLD_POINTS_EXCEED_MAX",
                "INCOMPLETE",
                "임계값 배점이 항목 만점을 초과합니다.",
                **context,
            )
        )
    if threshold.points_if_not_met is None:
        issues.append(
            _issue(
                "THRESHOLD_ELSE_POINTS_MISSING",
                "INCOMPLETE",
                "임계값을 충족하지 못할 때의 배점이 원문에서 확인되지 않았습니다.",
                **context,
            )
        )
        return issues, None
    threshold_decimal = _decimal(threshold.threshold_value)
    expected_terms = (
        ((threshold_decimal, threshold.operator),)
        if threshold_decimal is not None
        else ()
    )
    comparator_issue = _comparator_binding_issue(
        literal=threshold.literal,
        expected=expected_terms,
        mismatch_code="THRESHOLD_COMPARATOR_MISMATCH",
        mismatch_message=(
            "임계값 원문의 비교 연산자와 구조화한 threshold operator가 일치하지 않습니다."
        ),
        context=context,
    )
    if comparator_issue is not None:
        issues.append(comparator_issue)
    return issues, ImmutableQuantitativeThreshold(
        literal=threshold.literal,
        operator=threshold.operator,
        threshold_value=threshold.threshold_value,
        points_if_met=threshold.points_if_met,
        points_if_not_met=threshold.points_if_not_met,
        evidence=_frozen_anchor(threshold.evidence),
    )


def validate_quantitative_rule_candidate(
    candidate: QuantitativeRuleCandidate,
    *,
    source_attachment_id: str,
    table_id: str,
    source_text_by_attachment_id: Mapping[str, str],
    expected_attachment_ids: Iterable[str],
) -> tuple[
    ImmutableQuantitativeRuleCandidate | None,
    QuantitativeReviewCandidate | None,
    tuple[QuantitativeValidationIssue, ...],
]:
    """Validate one literal rule without using bidder/company facts."""

    expected = set(expected_attachment_ids)
    source = source_text_by_attachment_id.get(source_attachment_id, "")
    context = {
        "attachment_id": source_attachment_id,
        "table_id": table_id,
        "criterion_id": candidate.criterion_id,
    }
    issues = _anchor_issues(
        candidate.evidence,
        sources=source_text_by_attachment_id,
        expected_attachment_ids=expected,
        payload_attachment_id=source_attachment_id,
        table_id=table_id,
        criterion_id=candidate.criterion_id,
    )
    if not _literal_is_anchored(candidate.criterion_literal, candidate.evidence, source):
        issues.append(
            _issue(
                "CRITERION_LITERAL_MISMATCH",
                "INCOMPLETE",
                "평가항목 literal을 근거 인용문에서 확인할 수 없습니다.",
                **context,
            )
        )
    if not _literal_contains_number(candidate.max_points, candidate.criterion_literal):
        issues.append(
            _issue(
                "MAX_POINTS_LITERAL_MISMATCH",
                "INCOMPLETE",
                "항목 만점 숫자가 평가항목 literal에 없습니다.",
                **context,
            )
        )
    if candidate.metric == "UNKNOWN":
        issues.append(
            _issue(
                "UNKNOWN_METRIC",
                "REVIEW",
                "정량 지표를 알려진 지표 enum에 확정적으로 연결할 수 없습니다.",
                **context,
            )
        )
    if candidate.scoring_method == "UNKNOWN":
        issues.append(
            _issue(
                "UNKNOWN_SCORING_METHOD",
                "REVIEW",
                "배점 산식을 확정적으로 구조화할 수 없습니다.",
                **context,
            )
        )
    if candidate.ambiguity_reason:
        issues.append(
            _issue(
                "AMBIGUOUS_RULE",
                "REVIEW",
                "평가항목 원문에 해소되지 않은 모호성이 있습니다.",
                **context,
            )
        )
    if not candidate.required_evidence or any(
        _placeholder_evidence_key(value) for value in candidate.required_evidence
    ):
        issues.append(
            _issue(
                "REQUIRED_EVIDENCE_INCOMPLETE",
                "INCOMPLETE",
                "필요 증빙 키가 없거나 placeholder입니다.",
                **context,
            )
        )
    elif candidate.metric != "UNKNOWN" and not _required_evidence_is_registered(
        candidate.metric,
        candidate.required_evidence,
    ):
        issues.append(
            _issue(
                "UNREGISTERED_REQUIRED_EVIDENCE",
                "REVIEW",
                "필요 증빙 키가 알려진 정량 지표의 canonical registry와 일치하지 않습니다.",
                **context,
            )
        )

    bracket_issues: list[QuantitativeValidationIssue] = []
    frozen_brackets: tuple[ImmutableQuantitativeBracket, ...] = ()
    frozen_threshold: ImmutableQuantitativeThreshold | None = None
    if candidate.scoring_method == "BRACKET":
        if not candidate.brackets or candidate.threshold is not None or candidate.formula_literal:
            issues.append(
                _issue(
                    "SCORING_METHOD_SHAPE_MISMATCH",
                    "INCOMPLETE",
                    "BRACKET 방식에는 배점 구간만 있어야 합니다.",
                    **context,
                )
            )
        bracket_issues, frozen_brackets = _validate_brackets(
            candidate,
            source=source,
            sources=source_text_by_attachment_id,
            expected_attachment_ids=expected,
            payload_attachment_id=source_attachment_id,
            table_id=table_id,
        )
        issues.extend(bracket_issues)
    elif candidate.scoring_method == "THRESHOLD":
        if candidate.threshold is None or candidate.brackets or candidate.formula_literal:
            issues.append(
                _issue(
                    "SCORING_METHOD_SHAPE_MISMATCH",
                    "INCOMPLETE",
                    "THRESHOLD 방식에는 하나의 임계값만 있어야 합니다.",
                    **context,
                )
            )
        if candidate.threshold is not None:
            threshold_issues, frozen_threshold = _validate_threshold(
                candidate,
                candidate.threshold,
                source=source,
                sources=source_text_by_attachment_id,
                expected_attachment_ids=expected,
                payload_attachment_id=source_attachment_id,
                table_id=table_id,
            )
            issues.extend(threshold_issues)
    elif candidate.scoring_method == "FORMULA":
        if candidate.brackets or candidate.threshold is not None or not candidate.formula_literal:
            issues.append(
                _issue(
                    "SCORING_METHOD_SHAPE_MISMATCH",
                    "INCOMPLETE",
                    "FORMULA 방식에는 원문 산식만 있어야 합니다.",
                    **context,
                )
            )
        elif not evidence_quote_matches_source(
            candidate.formula_literal, candidate.evidence.quote
        ):
            issues.append(
                _issue(
                    "FORMULA_LITERAL_MISMATCH",
                    "INCOMPLETE",
                    "산식 literal을 근거 인용문에서 확인할 수 없습니다.",
                    **context,
                )
            )
    elif candidate.brackets or candidate.threshold is not None or candidate.formula_literal:
        issues.append(
            _issue(
                "UNKNOWN_METHOD_HAS_DERIVED_STRUCTURE",
                "REVIEW",
                "UNKNOWN 산식에 확정적 구조를 함께 사용할 수 없습니다.",
                **context,
            )
        )

    status = _candidate_status(issues)
    if status == "AVAILABLE":
        available = ImmutableQuantitativeRuleCandidate(
            source_attachment_id=source_attachment_id,
            table_id=table_id,
            criterion_id=candidate.criterion_id,
            label=candidate.label,
            criterion_literal=candidate.criterion_literal,
            max_points=candidate.max_points,
            scoring_method=candidate.scoring_method,
            metric=candidate.metric,
            unit=candidate.unit,
            brackets=frozen_brackets,
            threshold=frozen_threshold,
            formula_literal=candidate.formula_literal,
            required_evidence=tuple(candidate.required_evidence),
            evidence=_frozen_anchor(candidate.evidence),
        )
        return available, None, tuple(issues)
    review = QuantitativeReviewCandidate(
        status=status,
        source_attachment_id=source_attachment_id,
        table_id=table_id,
        criterion_id=candidate.criterion_id,
        label=candidate.label,
        max_points=candidate.max_points,
        scoring_method=candidate.scoring_method,
        metric=candidate.metric,
        issue_codes=tuple(sorted({item.code for item in issues})),
    )
    return None, review, tuple(issues)


def _validate_table_metadata(
    table: QuantitativeTableCandidate,
    *,
    source_attachment_id: str,
    source_text_by_attachment_id: Mapping[str, str],
    expected_attachment_ids: set[str],
) -> list[QuantitativeValidationIssue]:
    issues: list[QuantitativeValidationIssue] = []
    context = {"attachment_id": source_attachment_id, "table_id": table.table_id}
    source = source_text_by_attachment_id.get(source_attachment_id, "")
    if table.ambiguity_reason:
        issues.append(
            _issue(
                "AMBIGUOUS_TABLE",
                "REVIEW",
                "정량평가표에 해소되지 않은 모호성이 있습니다.",
                **context,
            )
        )
    if not table.criteria:
        issues.append(
            _issue(
                "TABLE_CRITERIA_MISSING",
                "INCOMPLETE",
                "정량평가표에 평가항목이 없습니다.",
                **context,
            )
        )
    if table.total_points is None or table.total_evidence is None:
        issues.append(
            _issue(
                "TABLE_TOTAL_INCOMPLETE",
                "INCOMPLETE",
                "정량평가표 총점 또는 총점 근거가 없습니다.",
                **context,
            )
        )
    else:
        issues.extend(
            _anchor_issues(
                table.total_evidence,
                sources=source_text_by_attachment_id,
                expected_attachment_ids=expected_attachment_ids,
                payload_attachment_id=source_attachment_id,
                table_id=table.table_id,
            )
        )
        if not _literal_contains_number(table.total_points, table.total_evidence.quote):
            issues.append(
                _issue(
                    "TABLE_TOTAL_LITERAL_MISMATCH",
                    "INCOMPLETE",
                    "표 총점 숫자를 총점 근거에서 확인할 수 없습니다.",
                    **context,
                )
            )
        criteria_total = sum((Decimal(str(item.max_points)) for item in table.criteria), Decimal("0"))
        if criteria_total != Decimal(str(table.total_points)):
            issues.append(
                _issue(
                    "TABLE_TOTAL_MISMATCH",
                    "INCOMPLETE",
                    "평가항목 만점 합계가 표 총점과 일치하지 않습니다.",
                    **context,
                )
            )
    if (table.minimum_score is None) != (table.minimum_evidence is None):
        issues.append(
            _issue(
                "MINIMUM_SCORE_INCOMPLETE",
                "INCOMPLETE",
                "최저점과 그 근거는 함께 있어야 합니다.",
                **context,
            )
        )
    elif table.minimum_score is not None and table.minimum_evidence is not None:
        issues.extend(
            _anchor_issues(
                table.minimum_evidence,
                sources=source_text_by_attachment_id,
                expected_attachment_ids=expected_attachment_ids,
                payload_attachment_id=source_attachment_id,
                table_id=table.table_id,
            )
        )
        if not _literal_contains_number(table.minimum_score, table.minimum_evidence.quote):
            issues.append(
                _issue(
                    "MINIMUM_SCORE_LITERAL_MISMATCH",
                    "INCOMPLETE",
                    "최저점 숫자를 최저점 근거에서 확인할 수 없습니다.",
                    **context,
                )
            )
        if table.total_points is not None and table.minimum_score > table.total_points:
            issues.append(
                _issue(
                    "MINIMUM_SCORE_EXCEEDS_TOTAL",
                    "INCOMPLETE",
                    "최저점이 표 총점을 초과합니다.",
                    **context,
                )
            )
    if table.total_evidence is not None and table.total_evidence.attachment_id == source_attachment_id:
        # This extra source lookup keeps table validation independent of the
        # OpenAI client and rejects stale/cross-source evidence deterministically.
        if not evidence_quote_matches_source(table.total_evidence.quote, source):
            issues.append(
                _issue(
                    "UNVERIFIED_TABLE_TOTAL_QUOTE",
                    "INCOMPLETE",
                    "표 총점 근거를 첨부 원문에서 확인할 수 없습니다.",
                    **context,
                )
            )
    return issues


def build_quantitative_candidate_profile(
    extractions_by_attachment_id: Mapping[str, ExtractionPayload],
    source_text_by_attachment_id: Mapping[str, str],
    *,
    expected_attachment_ids: Iterable[str],
    incomplete_attachment_ids: Iterable[str] = (),
) -> QuantitativeCandidateProfile:
    """Build a deterministic, immutable, manifest-sized rule candidate profile.

    The function never accepts company facts and never computes attained
    points. Callers must supply the current manifest IDs; manifest persistence
    and correction invalidation intentionally remain outside this pure module.
    """

    expected = set(expected_attachment_ids)
    processed = set(extractions_by_attachment_id)
    incomplete = set(incomplete_attachment_ids)
    issues: list[QuantitativeValidationIssue] = []
    available_candidates: list[ImmutableQuantitativeRuleCandidate] = []
    review_candidates: list[QuantitativeReviewCandidate] = []
    tables: list[ImmutableQuantitativeTable] = []
    not_applicable: list[ImmutableEvidenceAnchor] = []

    for attachment_id in sorted(expected - processed):
        issues.append(
            _issue(
                "ATTACHMENT_EXTRACTION_MISSING",
                "INCOMPLETE",
                "현재 manifest 첨부의 추출 결과가 없습니다.",
                attachment_id=attachment_id,
            )
        )
    for attachment_id in sorted(processed - expected):
        issues.append(
            _issue(
                "EXTRACTION_OUTSIDE_MANIFEST",
                "INCOMPLETE",
                "현재 manifest 밖의 추출 결과가 포함되었습니다.",
                attachment_id=attachment_id,
            )
        )
    for attachment_id in sorted(incomplete):
        issues.append(
            _issue(
                "ATTACHMENT_INCOMPLETE",
                "INCOMPLETE",
                "첨부 원문 추출이 완전하지 않습니다.",
                attachment_id=attachment_id,
            )
        )
    for attachment_id in sorted(expected):
        if attachment_id not in source_text_by_attachment_id:
            issues.append(
                _issue(
                    "SOURCE_TEXT_MISSING",
                    "INCOMPLETE",
                    "현재 manifest 첨부의 원문 텍스트가 없습니다.",
                    attachment_id=attachment_id,
                )
            )

    seen_table_ids: set[tuple[str, str]] = set()
    for attachment_id in sorted(processed & expected):
        payload = extractions_by_attachment_id[attachment_id]
        if payload.missing_or_unreadable:
            issues.append(
                _issue(
                    "EXTRACTION_DECLARED_INCOMPLETE",
                    "INCOMPLETE",
                    "모델 추출 결과가 누락 또는 판독 불가 원문을 선언했습니다.",
                    attachment_id=attachment_id,
                )
            )

        absence = payload.quantitative_table_not_applicable
        if absence is not None:
            absence_issues = _anchor_issues(
                absence.evidence,
                sources=source_text_by_attachment_id,
                expected_attachment_ids=expected,
                payload_attachment_id=attachment_id,
            )
            source = source_text_by_attachment_id.get(attachment_id, "")
            if not _literal_is_anchored(absence.reason_literal, absence.evidence, source):
                absence_issues.append(
                    _issue(
                        "NOT_APPLICABLE_LITERAL_MISMATCH",
                        "INCOMPLETE",
                        "정량평가 비적용 문구를 근거 인용문에서 확인할 수 없습니다.",
                        attachment_id=attachment_id,
                    )
                )
            issues.extend(absence_issues)
            if not absence_issues:
                not_applicable.append(_frozen_anchor(absence.evidence))

        if absence is not None and payload.quantitative_tables:
            issues.append(
                _issue(
                    "TABLE_AND_NOT_APPLICABLE_CONFLICT",
                    "REVIEW",
                    "같은 첨부에서 정량평가표와 비적용 선언이 함께 추출되었습니다.",
                    attachment_id=attachment_id,
                )
            )

        for table in payload.quantitative_tables:
            table_key = (attachment_id, table.table_id)
            table_issues = _validate_table_metadata(
                table,
                source_attachment_id=attachment_id,
                source_text_by_attachment_id=source_text_by_attachment_id,
                expected_attachment_ids=expected,
            )
            if table_key in seen_table_ids:
                table_issues.append(
                    _issue(
                        "DUPLICATE_TABLE_ID",
                        "INCOMPLETE",
                        "같은 첨부 안에서 정량평가표 ID가 중복되었습니다.",
                        attachment_id=attachment_id,
                        table_id=table.table_id,
                    )
                )
            seen_table_ids.add(table_key)

            local_available: list[ImmutableQuantitativeRuleCandidate] = []
            local_review: list[QuantitativeReviewCandidate] = []
            seen_criterion_ids: set[str] = set()
            for candidate in table.criteria:
                available, review, candidate_issues = validate_quantitative_rule_candidate(
                    candidate,
                    source_attachment_id=attachment_id,
                    table_id=table.table_id,
                    source_text_by_attachment_id=source_text_by_attachment_id,
                    expected_attachment_ids=expected,
                )
                if candidate.criterion_id in seen_criterion_ids:
                    duplicate = _issue(
                        "DUPLICATE_CRITERION_ID",
                        "INCOMPLETE",
                        "같은 표 안에서 평가항목 ID가 중복되었습니다.",
                        attachment_id=attachment_id,
                        table_id=table.table_id,
                        criterion_id=candidate.criterion_id,
                    )
                    candidate_issues = (*candidate_issues, duplicate)
                    available = None
                    review = QuantitativeReviewCandidate(
                        status="INCOMPLETE",
                        source_attachment_id=attachment_id,
                        table_id=table.table_id,
                        criterion_id=candidate.criterion_id,
                        label=candidate.label,
                        max_points=candidate.max_points,
                        scoring_method=candidate.scoring_method,
                        metric=candidate.metric,
                        issue_codes=tuple(
                            sorted({item.code for item in candidate_issues})
                        ),
                    )
                seen_criterion_ids.add(candidate.criterion_id)
                issues.extend(candidate_issues)
                if available is not None:
                    local_available.append(available)
                if review is not None:
                    local_review.append(review)

            candidate_table_issues = [
                item
                for item in issues
                if item.attachment_id == attachment_id
                and item.table_id == table.table_id
            ]
            table_status = _candidate_status([*table_issues, *candidate_table_issues])
            if table_status == "INCOMPLETE":
                # A broken total/metadata binding prevents otherwise valid rows
                # from being exported as AVAILABLE rule candidates.
                for candidate in local_available:
                    local_review.append(
                        QuantitativeReviewCandidate(
                            status="INCOMPLETE",
                            source_attachment_id=candidate.source_attachment_id,
                            table_id=candidate.table_id,
                            criterion_id=candidate.criterion_id,
                            label=candidate.label,
                            max_points=candidate.max_points,
                            scoring_method=candidate.scoring_method,
                            metric=candidate.metric,
                            issue_codes=tuple(
                                sorted(
                                    {
                                        item.code
                                        for item in [
                                            *table_issues,
                                            *candidate_table_issues,
                                        ]
                                        if item.disposition == "INCOMPLETE"
                                    }
                                    or {"TABLE_CONTAINS_INCOMPLETE_RULE"}
                                )
                            ),
                        )
                    )
                local_available = []

            issues.extend(table_issues)
            available_candidates.extend(local_available)
            review_candidates.extend(local_review)
            tables.append(
                ImmutableQuantitativeTable(
                    source_attachment_id=attachment_id,
                    table_id=table.table_id,
                    label=table.label,
                    status=table_status,
                    total_points=table.total_points,
                    total_evidence=(
                        _frozen_anchor(table.total_evidence)
                        if table.total_evidence is not None
                        else None
                    ),
                    minimum_score=table.minimum_score,
                    minimum_evidence=(
                        _frozen_anchor(table.minimum_evidence)
                        if table.minimum_evidence is not None
                        else None
                    ),
                    criterion_ids=tuple(item.criterion_id for item in table.criteria),
                    available_criterion_ids=tuple(
                        item.criterion_id for item in local_available
                    ),
                    review_criterion_ids=tuple(item.criterion_id for item in local_review),
                )
            )

    has_tables = bool(tables)
    aggregate_status = _candidate_status(issues)
    if not has_tables:
        if not_applicable and aggregate_status == "AVAILABLE":
            status: ProfileStatus = "NOT_APPLICABLE"
        else:
            if not not_applicable:
                issues.append(
                    _issue(
                        "QUANTITATIVE_TABLE_NOT_ESTABLISHED",
                        "INCOMPLETE",
                        "정량평가표도 비적용 원문 근거도 확인되지 않았습니다.",
                    )
                )
            status = "INCOMPLETE"
    elif aggregate_status == "AVAILABLE":
        status = "AVAILABLE"
    else:
        status = aggregate_status

    issue_key = lambda item: (
        item.attachment_id or "",
        item.table_id or "",
        item.criterion_id or "",
        item.code,
    )
    return QuantitativeCandidateProfile(
        status=status,
        expected_attachment_ids=tuple(sorted(expected)),
        processed_attachment_ids=tuple(sorted(processed & expected)),
        tables=tuple(
            sorted(tables, key=lambda item: (item.source_attachment_id, item.table_id))
        ),
        available_candidates=tuple(
            sorted(
                available_candidates,
                key=lambda item: (
                    item.source_attachment_id,
                    item.table_id,
                    item.criterion_id,
                ),
            )
        ),
        review_candidates=tuple(
            sorted(
                review_candidates,
                key=lambda item: (
                    item.source_attachment_id,
                    item.table_id,
                    item.criterion_id,
                ),
            )
        ),
        not_applicable_evidence=tuple(
            sorted(
                not_applicable,
                key=lambda item: (item.attachment_id, item.page or 0, item.quote),
            )
        ),
        issues=tuple(sorted(issues, key=issue_key)),
    )


def _record_fingerprint_data(data: Mapping[str, object]) -> str:
    canonical = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validated_quantitative_record_fingerprint(
    record: ValidatedQuantitativeAttachmentRecord,
) -> str:
    """Recompute the canonical integrity fingerprint of a persisted record."""

    return _record_fingerprint_data(
        record.model_dump(
            mode="json",
            exclude={"validation_fingerprint_sha256"},
        )
    )


def validate_quantitative_attachment_extraction(
    payload: ExtractionPayload,
    *,
    source_text: str,
    attachment_id: str,
    document_sha256: str,
    manifest_sha256: str,
    prompt_version: str = PROMPT_VERSION,
    extraction_schema_version: str = SCHEMA_VERSION,
) -> ValidatedQuantitativeAttachmentRecord:
    """Validate one attachment while its transient raw text is available.

    The returned frozen record includes literal anchors and exact source/digest
    bindings, but never the source document text. It can therefore be persisted
    between durable continuation chunks and merged later without downloading or
    storing the raw document again.
    """

    binding = AttachmentDocumentBinding(
        attachment_id=attachment_id,
        document_sha256=document_sha256,
    )
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    profile = build_quantitative_candidate_profile(
        {attachment_id: payload},
        {attachment_id: source_text},
        expected_attachment_ids={attachment_id},
    )
    issues = tuple(
        item
        for item in profile.issues
        if item.code != "QUANTITATIVE_TABLE_NOT_ESTABLISHED"
    )
    if payload.quantitative_tables:
        status: AttachmentRecordStatus = _candidate_status(issues)
    elif payload.quantitative_table_not_applicable is not None and not issues:
        status = "NOT_APPLICABLE"
    elif issues:
        status = _candidate_status(issues)
    else:
        # A document that simply contains no table is neutral. Only the final
        # manifest aggregate can determine whether a table was never found.
        status = "NO_TABLE"

    data: dict[str, object] = {
        "validator_version": QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION,
        "extraction_schema_version": extraction_schema_version,
        "prompt_version": prompt_version,
        "attachment_id": binding.attachment_id,
        "document_sha256": binding.document_sha256,
        "manifest_sha256": manifest_sha256,
        "status": status,
        "tables": profile.tables,
        "available_candidates": profile.available_candidates,
        "review_candidates": profile.review_candidates,
        "not_applicable_evidence": profile.not_applicable_evidence,
        "issues": issues,
    }
    fingerprint = _record_fingerprint_data(
        ValidatedQuantitativeAttachmentRecord.model_construct(
            **data,
            validation_fingerprint_sha256="0" * 64,
        ).model_dump(
            mode="json",
            exclude={"validation_fingerprint_sha256"},
        )
    )
    return ValidatedQuantitativeAttachmentRecord(
        **data,
        validation_fingerprint_sha256=fingerprint,
    )


def merge_validated_quantitative_records(
    records: Iterable[
        ValidatedQuantitativeAttachmentRecord | Mapping[str, object]
    ],
    *,
    expected_documents: Mapping[str, str],
    manifest_sha256: str,
    incomplete_attachment_ids: Iterable[str] = (),
) -> QuantitativeCandidateProfile:
    """Merge persisted per-file records against the exact current manifest."""

    if not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    bindings = tuple(
        AttachmentDocumentBinding(
            attachment_id=attachment_id,
            document_sha256=document_sha256,
        )
        for attachment_id, document_sha256 in sorted(expected_documents.items())
    )
    expected = {item.attachment_id: item.document_sha256 for item in bindings}
    issues: list[QuantitativeValidationIssue] = []
    grouped: dict[str, list[ValidatedQuantitativeAttachmentRecord]] = {}
    for raw_record in records:
        raw_data = (
            raw_record.model_dump(mode="python")
            if isinstance(raw_record, ValidatedQuantitativeAttachmentRecord)
            else dict(raw_record)
        )
        attachment_hint = raw_data.get("attachment_id")
        try:
            record = ValidatedQuantitativeAttachmentRecord.model_validate(raw_data)
        except ValidationError:
            issues.append(
                _issue(
                    "RECORD_INVARIANT_VIOLATION",
                    "INCOMPLETE",
                    "정량 검증 record의 status·shape·source binding 불변식이 깨졌습니다.",
                    attachment_id=(
                        attachment_hint if isinstance(attachment_hint, str) else None
                    ),
                )
            )
            continue
        grouped.setdefault(record.attachment_id, []).append(record)

    tables: list[ImmutableQuantitativeTable] = []
    available: list[ImmutableQuantitativeRuleCandidate] = []
    review: list[QuantitativeReviewCandidate] = []
    not_applicable: list[ImmutableEvidenceAnchor] = []
    processed: set[str] = set()

    for attachment_id in sorted(set(grouped) - set(expected)):
        issues.append(
            _issue(
                "RECORD_OUTSIDE_MANIFEST",
                "INCOMPLETE",
                "현재 manifest 밖의 정량 검증 record가 포함되었습니다.",
                attachment_id=attachment_id,
            )
        )
    for attachment_id, document_sha256 in sorted(expected.items()):
        candidates = grouped.get(attachment_id, [])
        if not candidates:
            issues.append(
                _issue(
                    "VALIDATED_RECORD_MISSING",
                    "INCOMPLETE",
                    "현재 manifest 첨부의 정량 검증 record가 없습니다.",
                    attachment_id=attachment_id,
                )
            )
            continue
        if len(candidates) != 1:
            issues.append(
                _issue(
                    "DUPLICATE_VALIDATED_RECORD",
                    "INCOMPLETE",
                    "같은 첨부에 둘 이상의 정량 검증 record가 있습니다.",
                    attachment_id=attachment_id,
                )
            )
            continue
        record = candidates[0]
        binding_errors: list[QuantitativeValidationIssue] = []
        if record.document_sha256 != document_sha256:
            binding_errors.append(
                _issue(
                    "DOCUMENT_BINDING_MISMATCH",
                    "INCOMPLETE",
                    "정량 검증 record의 문서 digest가 현재 첨부와 다릅니다.",
                    attachment_id=attachment_id,
                )
            )
        if record.manifest_sha256 != manifest_sha256:
            binding_errors.append(
                _issue(
                    "MANIFEST_BINDING_MISMATCH",
                    "INCOMPLETE",
                    "정량 검증 record가 현재 첨부 manifest에 바인딩되지 않았습니다.",
                    attachment_id=attachment_id,
                )
            )
        if record.validator_version != QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION:
            binding_errors.append(
                _issue(
                    "VALIDATOR_VERSION_MISMATCH",
                    "INCOMPLETE",
                    "정량 검증 record의 validator 버전이 현재 버전과 다릅니다.",
                    attachment_id=attachment_id,
                )
            )
        if record.prompt_version != PROMPT_VERSION or (
            record.extraction_schema_version != SCHEMA_VERSION
        ):
            binding_errors.append(
                _issue(
                    "EXTRACTION_VERSION_MISMATCH",
                    "INCOMPLETE",
                    "정량 검증 record의 추출 prompt/schema 버전이 현재 버전과 다릅니다.",
                    attachment_id=attachment_id,
                )
            )
        if (
            record.validation_fingerprint_sha256
            != validated_quantitative_record_fingerprint(record)
        ):
            binding_errors.append(
                _issue(
                    "VALIDATION_FINGERPRINT_MISMATCH",
                    "INCOMPLETE",
                    "정량 검증 record의 무결성 fingerprint가 일치하지 않습니다.",
                    attachment_id=attachment_id,
                )
            )
        if binding_errors:
            issues.extend(binding_errors)
            continue

        processed.add(attachment_id)
        issues.extend(record.issues)
        tables.extend(record.tables)
        available.extend(record.available_candidates)
        review.extend(record.review_candidates)
        not_applicable.extend(record.not_applicable_evidence)
        if record.status == "INCOMPLETE" and not any(
            item.disposition == "INCOMPLETE" for item in record.issues
        ):
            issues.append(
                _issue(
                    "ATTACHMENT_RECORD_INCOMPLETE",
                    "INCOMPLETE",
                    "첨부 정량 검증 record가 불완전합니다.",
                    attachment_id=attachment_id,
                )
            )
        elif record.status == "REVIEW" and not any(
            item.disposition == "REVIEW" for item in record.issues
        ):
            issues.append(
                _issue(
                    "ATTACHMENT_RECORD_REVIEW",
                    "REVIEW",
                    "첨부 정량 검증 record에 사람 검토가 필요합니다.",
                    attachment_id=attachment_id,
                )
            )

    for attachment_id in sorted(set(incomplete_attachment_ids)):
        issues.append(
            _issue(
                "ATTACHMENT_INCOMPLETE",
                "INCOMPLETE",
                "현재 manifest 첨부 처리가 완료되지 않았습니다.",
                attachment_id=attachment_id,
            )
        )

    if tables and not_applicable:
        issues.append(
            _issue(
                "TABLE_AND_NOT_APPLICABLE_CONFLICT",
                "REVIEW",
                "manifest 전체에서 정량평가표와 비적용 선언이 함께 확인되었습니다.",
            )
        )
    aggregate = _candidate_status(issues)
    if tables:
        status: ProfileStatus = aggregate
    elif not_applicable and aggregate == "AVAILABLE":
        status = "NOT_APPLICABLE"
    elif aggregate != "AVAILABLE":
        status = aggregate
    else:
        issues.append(
            _issue(
                "QUANTITATIVE_TABLE_NOT_ESTABLISHED",
                "INCOMPLETE",
                "현재 manifest 전체에서 정량평가표나 명시적 비적용 근거를 확인하지 못했습니다.",
            )
        )
        status = "INCOMPLETE"

    issue_key = lambda item: (
        item.attachment_id or "",
        item.table_id or "",
        item.criterion_id or "",
        item.code,
    )
    candidate_key = lambda item: (
        item.source_attachment_id,
        item.table_id,
        item.criterion_id,
    )
    return QuantitativeCandidateProfile(
        status=status,
        manifest_sha256=manifest_sha256,
        document_bindings=bindings,
        expected_attachment_ids=tuple(sorted(expected)),
        processed_attachment_ids=tuple(sorted(processed)),
        tables=tuple(
            sorted(tables, key=lambda item: (item.source_attachment_id, item.table_id))
        ),
        available_candidates=tuple(sorted(available, key=candidate_key)),
        review_candidates=tuple(sorted(review, key=candidate_key)),
        not_applicable_evidence=tuple(
            sorted(
                not_applicable,
                key=lambda item: (item.attachment_id, item.page or 0, item.quote),
            )
        ),
        issues=tuple(sorted(issues, key=issue_key)),
    )
