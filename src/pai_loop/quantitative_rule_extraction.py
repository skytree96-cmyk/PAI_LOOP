from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
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
    QuantitativeCaseLiteral,
    QuantitativeRuleCandidate,
    QuantitativeScoringMethod,
    QuantitativeTableCandidate,
    QuantitativeThresholdLiteral,
    SCHEMA_VERSION,
    evidence_quote_matches_source,
)
from .quantitative_formula import CaseTableRowLiteral, compile_case_table


QUANTITATIVE_CANDIDATE_PROFILE_VERSION = "pai-loop-quantitative-candidate-profile-0.6.0"
QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION = "pai-loop-quantitative-attachment-validator-0.5.0"

_ATTACHMENT_LOCAL_ABSENCE_TERMS = (
    "포함되지",
    "별도 첨부",
    "별도 문서",
    "별도 제공",
    "첨부되지",
    "제공되지",
    "미포함",
)
_QUANTITATIVE_GAP_TERMS = (
    "정량평가표",
    "정량 평가표",
    "평가배점표",
    "평가 배점표",
    "평가표",
    "배점표",
)
_UNREADABLE_GAP_TERMS = ("판독", "식별 불가", "불명확", "훼손", "흐림")
_PARTIAL_TABLE_GAP_TERMS = (
    "일부",
    "일부분",
    "페이지",
    "행",
    "열",
    "항목",
    "기준",
    "등급",
    "구간",
    "산식",
    "점수",
)
_ATTACHMENT_LOCAL_CONTEXT_TERMS = (
    "이 첨부",
    "해당 첨부",
    "공고문",
    "본문",
    "제안요청서",
    "과업지시서",
    "과업 지시서",
    "과업내용서",
    "과업 내용서",
    "규격서",
    "사양서",
    "세부사양",
    "시방서",
    "내역서",
)
_SIBLING_DOCUMENT_TARGETS = (
    (("제안요청서", "제안 요청서"), ("RFP",)),
    (("과업지시서", "과업 지시서", "과업내용서", "과업 내용서"), ("SCOPE",)),
    (("입찰공고", "공고문"), ("NOTICE",)),
    (("규격서", "사양서", "세부사양", "시방서", "내역서"), ("RFP", "SCOPE")),
)
_QUALITATIVE_ONLY_EXCLUSION_RE = re.compile(
    r"(?s)(?:가\.\s*)?평가\s*항목별\s*배점\s*표에서\s*"
    r"매우우수/우수/보통/미흡\s*등급의\s*실제\s*점수\s*구간\s*기준이\s*"
    r"정성\s*평가\s*항목에\s*대해\s*상세\s*서술되지\s*않음\s*"
    r"\(\s*정성\s*평가이므로\s*정량\s*테이블에서\s*제외\s*\)\s*\.?"
)

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
    required_sibling_document_types: tuple[
        Literal["NOTICE", "RFP", "SCOPE", "FORM", "OTHER"], ...
    ] = ()
    required_sibling_label_markers: tuple[str, ...] = ()
    source_gap_statement: str | None = Field(default=None, max_length=1000)
    source_gap_document_type: Literal[
        "NOTICE", "RFP", "SCOPE", "FORM", "OTHER"
    ] | None = None


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


class ImmutableQuantitativeCase(FrozenModel):
    literal: str
    operator: Literal["GTE", "EQ", "IN"]
    comparison_value: float | None
    category_values: tuple[str, ...]
    award_kind: Literal["POINTS", "PERCENT_OF_MAX"]
    award_value: float = Field(ge=0)
    row_order: int = Field(ge=1, le=100)
    evidence: ImmutableEvidenceAnchor


class ImmutableQuantitativeRecognitionCondition(FrozenModel):
    literal: str
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
    cases: tuple[ImmutableQuantitativeCase, ...] = ()
    recognition_conditions: tuple[ImmutableQuantitativeRecognitionCondition, ...] = ()
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
    document_type: Literal["NOTICE", "RFP", "SCOPE", "FORM", "OTHER"] | None = None
    source_label: str | None = Field(default=None, min_length=1, max_length=500)


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


def _normalize_case_category(value: str) -> str:
    """Match the execution DSL's NFKC/whitespace/case normalization."""

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _case_literal_contains_exact_category(literal: str, value: str) -> bool:
    """Require a category to occupy a source token/cell, never a substring."""

    source = unicodedata.normalize("NFKC", literal).casefold()
    target = _normalize_case_category(value)
    if not target:
        return False

    compact: list[str] = []
    whitespace_boundaries: set[int] = set()
    pending_whitespace = False
    for character in source:
        if character.isspace():
            pending_whitespace = True
            continue
        if pending_whitespace and compact:
            whitespace_boundaries.add(len(compact))
        compact.append(character)
        pending_whitespace = False
    compact_source = "".join(compact)

    def token_character(character: str) -> bool:
        return character.isalnum() or character in "+_-"

    start = compact_source.find(target)
    while start >= 0:
        end = start + len(target)
        left_is_boundary = (
            start == 0
            or start in whitespace_boundaries
            or not token_character(compact_source[start - 1])
        )
        right_is_boundary = (
            end == len(compact_source)
            or end in whitespace_boundaries
            or not token_character(compact_source[end])
            or compact_source.startswith(("배점", "평점", "점수", "점", "등급"), end)
        )
        if left_is_boundary and right_is_boundary:
            return True
        start = compact_source.find(target, start + 1)
    return False

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


def _normalise_source_gap(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _is_explicit_qualitative_only_exclusion(value: str) -> bool:
    """Ignore only an explicit qualitative-only exclusion from quant extraction."""

    gap = _normalise_source_gap(value)
    return bool(gap and _QUALITATIVE_ONLY_EXCLUSION_RE.fullmatch(gap))


def _compact_document_label(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _attachment_local_quantitative_table_targets(
    value: str,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] | None:
    """Return explicit sibling targets, an empty tuple for unlinked local gaps, or None."""

    gap = _normalise_source_gap(value)
    if (
        not gap
        or not re.fullmatch(r"(?s).{1,1000}", gap)
        or any(term in gap for term in _UNREADABLE_GAP_TERMS)
    ):
        return None

    context_positions = [
        (term, match)
        for term in _ATTACHMENT_LOCAL_CONTEXT_TERMS
        for match in re.finditer(re.escape(term), gap)
    ]
    absence_positions = [
        match
        for term in _ATTACHMENT_LOCAL_ABSENCE_TERMS
        for match in re.finditer(re.escape(term), gap)
    ]
    table_positions = [
        match
        for term in _QUANTITATIVE_GAP_TERMS
        for match in re.finditer(re.escape(term), gap)
    ]
    if not context_positions or not absence_positions or not table_positions:
        return None

    qualifying_context_terms: set[str] = set()
    for context_term, context in context_positions:
        if any(
            0 <= absence.start() - context.end() <= 120
            and 0 <= table.start() - absence.end() <= 200
            and not any(
                term in gap[context.end() : table.end()]
                for term in _PARTIAL_TABLE_GAP_TERMS
            )
            for absence in absence_positions
            for table in table_positions
        ):
            qualifying_context_terms.add(context_term)

    # Or the table itself is explicitly absent from this attachment/body. Do not
    # promote a partial row/grade/formula gap into a sibling-resolvable absence.
    for context_term, context in context_positions:
        if any(
            0 <= absence.start() - table.end() <= 160
            and (
                table.end() <= context.start() <= absence.start()
                or context.end() <= table.start()
            )
            and not any(
                term in gap[table.end() : absence.start()]
                for term in _PARTIAL_TABLE_GAP_TERMS
            )
            for table in table_positions
            for absence in absence_positions
        ):
            qualifying_context_terms.add(context_term)

    if not qualifying_context_terms:
        return None

    compact_contexts = {
        _compact_document_label(term) for term in qualifying_context_terms
    }
    targets: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for markers, document_types in _SIBLING_DOCUMENT_TARGETS:
        compact_markers = tuple(
            sorted(
                {
                    _compact_document_label(marker)
                    for marker in markers
                    if _compact_document_label(marker) in compact_contexts
                }
            )
        )
        if compact_markers:
            targets.append((document_types, compact_markers))
    return tuple(targets)


def _source_label_document_types(
    value: str | None,
) -> tuple[Literal["NOTICE", "RFP", "SCOPE", "FORM"], ...]:
    """Return deterministic document roles declared by the manifest label."""

    if not isinstance(value, str) or not value.strip():
        return ()
    compact = _compact_document_label(value)
    matches: set[Literal["NOTICE", "RFP", "SCOPE", "FORM"]] = set()
    if any(marker in compact for marker in ("입찰공고", "공고문")):
        matches.add("NOTICE")
    if "제안요청서" in compact:
        matches.add("RFP")
    if any(marker in compact for marker in ("과업지시서", "과업내용서")):
        matches.add("SCOPE")
    if any(marker in compact for marker in ("작성양식", "제출서식", "서식", "양식")):
        matches.add("FORM")
    return tuple(
        item for item in ("NOTICE", "RFP", "SCOPE", "FORM") if item in matches
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
    required_sibling_document_types: tuple[
        Literal["NOTICE", "RFP", "SCOPE", "FORM", "OTHER"], ...
    ] = (),
    required_sibling_label_markers: tuple[str, ...] = (),
    source_gap_statement: str | None = None,
    source_gap_document_type: Literal[
        "NOTICE", "RFP", "SCOPE", "FORM", "OTHER"
    ] | None = None,
) -> QuantitativeValidationIssue:
    return QuantitativeValidationIssue(
        code=code,
        disposition=disposition,
        message=message,
        attachment_id=attachment_id,
        table_id=table_id,
        criterion_id=criterion_id,
        required_sibling_document_types=required_sibling_document_types,
        required_sibling_label_markers=required_sibling_label_markers,
        source_gap_statement=source_gap_statement,
        source_gap_document_type=source_gap_document_type,
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
    condition_keys: set[tuple[str, str]] = set()
    for condition in candidate.recognition_conditions:
        if not evidence_quote_matches_source(
            condition.literal,
            condition.evidence.quote,
        ):
            raise ValueError("AVAILABLE recognition condition is not bound to its anchor")
        key = (
            " ".join(condition.literal.split()).casefold(),
            condition.evidence.quote,
        )
        if key in condition_keys:
            raise ValueError("AVAILABLE recognition conditions contain a duplicate")
        condition_keys.add(key)

    if candidate.scoring_method == "BRACKET":
        if (
            not candidate.brackets
            or candidate.threshold is not None
            or candidate.formula_literal
            or candidate.cases
        ):
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
        if (
            candidate.threshold is None
            or candidate.brackets
            or candidate.formula_literal
            or candidate.cases
        ):
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
        if (
            candidate.brackets
            or candidate.threshold is not None
            or not candidate.formula_literal
            or candidate.cases
        ):
            raise ValueError("AVAILABLE FORMULA candidate shape is invalid")
        if not evidence_quote_matches_source(
            candidate.formula_literal,
            candidate.evidence.quote,
        ):
            raise ValueError("AVAILABLE formula literal is not bound to its anchor")
    elif candidate.scoring_method == "CASE_TABLE":
        if (
            not candidate.cases
            or candidate.brackets
            or candidate.threshold is not None
            or candidate.formula_literal
        ):
            raise ValueError("AVAILABLE CASE_TABLE candidate shape is invalid")
        rows: list[CaseTableRowLiteral] = []
        if [item.row_order for item in candidate.cases] != list(
            range(1, len(candidate.cases) + 1)
        ):
            raise ValueError("AVAILABLE CASE_TABLE row order is invalid")
        for case in candidate.cases:
            if not evidence_quote_matches_source(case.literal, case.evidence.quote):
                raise ValueError("AVAILABLE CASE literal is not bound to its anchor")
            values = [case.award_value]
            if case.comparison_value is not None:
                values.append(case.comparison_value)
            if any(not _literal_contains_number(value, case.literal) for value in values):
                raise ValueError("AVAILABLE CASE numbers do not match its literal")
            rows.append(
                CaseTableRowLiteral(
                    operator=case.operator,
                    comparison_value=case.comparison_value,
                    category_values=case.category_values,
                    award_kind=case.award_kind,
                    award_value=case.award_value,
                )
            )
        if compile_case_table(
            tuple(rows),
            value_kind=_case_value_kind(candidate.metric),
            maximum_points=candidate.max_points,
        ) is None:
            raise ValueError("AVAILABLE CASE_TABLE is not deterministic")


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
        for case in candidate.cases:
            yield case.evidence
        for condition in candidate.recognition_conditions:
            yield condition.evidence
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
    local_gap_groups: dict[
        tuple[str, str],
        list[tuple[tuple[str, ...], tuple[str, ...]]],
    ] = {}
    for issue in record.issues:
        sibling_target = (
            issue.required_sibling_document_types,
            issue.required_sibling_label_markers,
        )
        if issue.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT":
            if (
                issue.source_gap_statement is None
                or issue.source_gap_document_type is None
            ):
                raise ValueError("local quantitative gap provenance is missing")
            local_gap_groups.setdefault(
                (issue.source_gap_statement, issue.source_gap_document_type),
                [],
            ).append(sibling_target)
        elif (
            any(sibling_target)
            or issue.source_gap_statement is not None
            or issue.source_gap_document_type is not None
        ):
            raise ValueError("only local quantitative gaps may declare a sibling target")
    for (statement, document_type), actual_targets in local_gap_groups.items():
        derived_targets = _attachment_local_quantitative_table_targets(
            statement,
        )
        if derived_targets is None:
            raise ValueError("local quantitative gap provenance is not classifiable")
        expected_targets = set(derived_targets or (((), ()),))
        if len(actual_targets) != len(set(actual_targets)) or set(actual_targets) != expected_targets:
            raise ValueError("local quantitative gap sibling target is invalid")

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


def _case_value_kind(metric: KnownQuantitativeMetric) -> Literal[
    "NUMERIC", "DISCRETE", "CATEGORICAL"
]:
    if metric in {"CREDIT_RATING", "LOCAL_PRESENCE"}:
        return "CATEGORICAL"
    if metric in {
        "PERFORMANCE_COUNT",
        "PERSONNEL_COUNT",
        "CERTIFICATION_COUNT",
        "FACILITY_EQUIPMENT_COUNT",
        "AWARD_COUNT",
    }:
        return "DISCRETE"
    return "NUMERIC"


def _validate_cases(
    candidate: QuantitativeRuleCandidate,
    *,
    source: str,
    sources: Mapping[str, str],
    expected_attachment_ids: set[str],
    payload_attachment_id: str,
    table_id: str,
) -> tuple[list[QuantitativeValidationIssue], tuple[ImmutableQuantitativeCase, ...]]:
    issues: list[QuantitativeValidationIssue] = []
    frozen: list[ImmutableQuantitativeCase] = []
    context = {
        "attachment_id": payload_attachment_id,
        "table_id": table_id,
        "criterion_id": candidate.criterion_id,
    }
    expected_orders = list(range(1, len(candidate.cases) + 1))
    if [item.row_order for item in candidate.cases] != expected_orders:
        issues.append(
            _issue(
                "CASE_ROW_ORDER_INVALID",
                "INCOMPLETE",
                "CASE_TABLE 행 순서는 원문 순서의 연속된 번호여야 합니다.",
                **context,
            )
        )

    compiled_rows: list[CaseTableRowLiteral] = []
    for case in candidate.cases:
        issues.extend(
            _anchor_issues(
                case.evidence,
                sources=sources,
                expected_attachment_ids=expected_attachment_ids,
                payload_attachment_id=payload_attachment_id,
                table_id=table_id,
                criterion_id=candidate.criterion_id,
            )
        )
        if not _literal_is_anchored(case.literal, case.evidence, source):
            issues.append(
                _issue(
                    "CASE_LITERAL_MISMATCH",
                    "INCOMPLETE",
                    "CASE_TABLE 행 literal을 근거 인용문에서 확인할 수 없습니다.",
                    **context,
                )
            )
        numeric_values = [case.award_value]
        if case.comparison_value is not None:
            numeric_values.append(case.comparison_value)
        if any(not _literal_contains_number(value, case.literal) for value in numeric_values):
            issues.append(
                _issue(
                    "CASE_NUMBER_MISMATCH",
                    "INCOMPLETE",
                    "CASE_TABLE 조건값 또는 배점 숫자가 literal과 일치하지 않습니다.",
                    **context,
                )
            )
        if case.award_kind == "POINTS" and case.award_value > candidate.max_points:
            issues.append(
                _issue(
                    "CASE_POINTS_EXCEED_MAX",
                    "INCOMPLETE",
                    "CASE_TABLE 배점이 항목 만점을 초과합니다.",
                    **context,
                )
            )
        if case.operator == "GTE" and case.comparison_value is not None:
            comparison = _decimal(case.comparison_value)
            comparator_issue = _comparator_binding_issue(
                literal=case.literal,
                expected=((comparison, "GTE"),) if comparison is not None else (),
                mismatch_code="CASE_COMPARATOR_MISMATCH",
                mismatch_message="CASE_TABLE 원문의 비교 연산자와 구조화한 조건이 일치하지 않습니다.",
                context=context,
            )
            if comparator_issue is not None:
                issues.append(comparator_issue)
        elif case.operator == "EQ" and _COMPARATOR_MARKER_RE.search(case.literal):
            issues.append(
                _issue(
                    "CASE_COMPARATOR_MISMATCH",
                    "INCOMPLETE",
                    "정확값 CASE 행에는 다른 비교 연산자를 함께 사용할 수 없습니다.",
                    **context,
                )
            )
        if case.operator == "IN":
            normalized_values = [
                _normalize_case_category(value) for value in case.category_values
            ]
            if any(
                not value
                or not _case_literal_contains_exact_category(case.literal, source_value)
                for value, source_value in zip(
                    normalized_values, case.category_values, strict=True
                )
            ):
                issues.append(
                    _issue(
                        "CASE_CATEGORY_MISMATCH",
                        "REVIEW",
                        "CASE_TABLE 범주값을 해당 원문 행에서 모두 확인할 수 없습니다.",
                        **context,
                    )
                )
            if len(normalized_values) != len(set(normalized_values)):
                issues.append(
                    _issue(
                        "CASE_CATEGORY_DUPLICATE",
                        "REVIEW",
                        "CASE_TABLE 범주값이 NFKC 정규화 후 중복됩니다.",
                        **context,
                    )
                )
        try:
            compiled_row = CaseTableRowLiteral(
                operator=case.operator,
                comparison_value=case.comparison_value,
                category_values=tuple(case.category_values),
                award_kind=case.award_kind,
                award_value=case.award_value,
            )
        except ValidationError:
            issues.append(
                _issue(
                    "CASE_ROW_VALIDATION_FAILED",
                    "REVIEW",
                    "CASE_TABLE 행을 결정론 산식으로 검증하지 못했습니다.",
                    **context,
                )
            )
        else:
            compiled_rows.append(compiled_row)
        frozen.append(
            ImmutableQuantitativeCase(
                literal=case.literal,
                operator=case.operator,
                comparison_value=case.comparison_value,
                category_values=tuple(case.category_values),
                award_kind=case.award_kind,
                award_value=case.award_value,
                row_order=case.row_order,
                evidence=_frozen_anchor(case.evidence),
            )
        )

    if (
        len(compiled_rows) == len(candidate.cases)
        and candidate.cases
        and compile_case_table(
            tuple(compiled_rows),
            value_kind=_case_value_kind(candidate.metric),
            maximum_points=candidate.max_points,
        )
        is None
    ):
        issues.append(
            _issue(
                "CASE_TABLE_NOT_DETERMINISTIC",
                "REVIEW",
                "CASE_TABLE 행이 중복·역순·가림 없이 결정론적으로 실행되지 않습니다.",
                **context,
            )
        )
    return issues, tuple(frozen)


def _validate_recognition_conditions(
    candidate: QuantitativeRuleCandidate,
    *,
    source: str,
    sources: Mapping[str, str],
    expected_attachment_ids: set[str],
    payload_attachment_id: str,
    table_id: str,
) -> tuple[
    list[QuantitativeValidationIssue],
    tuple[ImmutableQuantitativeRecognitionCondition, ...],
]:
    issues: list[QuantitativeValidationIssue] = []
    frozen: list[ImmutableQuantitativeRecognitionCondition] = []
    seen: set[tuple[str, str]] = set()
    context = {
        "attachment_id": payload_attachment_id,
        "table_id": table_id,
        "criterion_id": candidate.criterion_id,
    }
    for condition in candidate.recognition_conditions:
        issues.extend(
            _anchor_issues(
                condition.evidence,
                sources=sources,
                expected_attachment_ids=expected_attachment_ids,
                payload_attachment_id=payload_attachment_id,
                table_id=table_id,
                criterion_id=candidate.criterion_id,
            )
        )
        if not _literal_is_anchored(condition.literal, condition.evidence, source):
            issues.append(
                _issue(
                    "RECOGNITION_CONDITION_LITERAL_MISMATCH",
                    "INCOMPLETE",
                    "실적 인정조건 literal을 해당 근거 인용문에서 확인할 수 없습니다.",
                    **context,
                )
            )
        key = (
            " ".join(condition.literal.split()).casefold(),
            condition.evidence.quote,
        )
        if key in seen:
            issues.append(
                _issue(
                    "RECOGNITION_CONDITION_DUPLICATE",
                    "REVIEW",
                    "동일한 실적 인정조건이 한 평가항목에 중복 연결되었습니다.",
                    **context,
                )
            )
        seen.add(key)
        frozen.append(
            ImmutableQuantitativeRecognitionCondition(
                literal=condition.literal,
                evidence=_frozen_anchor(condition.evidence),
            )
        )
    return issues, tuple(frozen)


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
    frozen_cases: tuple[ImmutableQuantitativeCase, ...] = ()
    condition_issues, frozen_conditions = _validate_recognition_conditions(
        candidate,
        source=source,
        sources=source_text_by_attachment_id,
        expected_attachment_ids=expected,
        payload_attachment_id=source_attachment_id,
        table_id=table_id,
    )
    issues.extend(condition_issues)
    if candidate.scoring_method == "BRACKET":
        if (
            not candidate.brackets
            or candidate.threshold is not None
            or candidate.formula_literal
            or candidate.cases
        ):
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
        if (
            candidate.threshold is None
            or candidate.brackets
            or candidate.formula_literal
            or candidate.cases
        ):
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
        if (
            candidate.brackets
            or candidate.threshold is not None
            or not candidate.formula_literal
            or candidate.cases
        ):
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
    elif candidate.scoring_method == "CASE_TABLE":
        if (
            not candidate.cases
            or candidate.brackets
            or candidate.threshold is not None
            or candidate.formula_literal
        ):
            issues.append(
                _issue(
                    "SCORING_METHOD_SHAPE_MISMATCH",
                    "INCOMPLETE",
                    "CASE_TABLE 방식에는 원문 순서의 case 행만 있어야 합니다.",
                    **context,
                )
            )
        case_issues, frozen_cases = _validate_cases(
            candidate,
            source=source,
            sources=source_text_by_attachment_id,
            expected_attachment_ids=expected,
            payload_attachment_id=source_attachment_id,
            table_id=table_id,
        )
        issues.extend(case_issues)
    elif (
        candidate.brackets
        or candidate.threshold is not None
        or candidate.formula_literal
        or candidate.cases
    ):
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
            cases=frozen_cases,
            recognition_conditions=frozen_conditions,
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
        source_gaps = [
            gap
            for gap in payload.missing_or_unreadable
            if not _is_explicit_qualitative_only_exclusion(gap)
        ]
        local_gap_targets = [
            (
                _normalise_source_gap(gap),
                _attachment_local_quantitative_table_targets(
                    gap,
                ),
            )
            for gap in source_gaps
        ]
        target_requirements: set[
            tuple[str, tuple[str, ...], tuple[str, ...]]
        ] = set()
        for gap, targets in local_gap_targets:
            if targets is None:
                continue
            if targets:
                target_requirements.update(
                    (gap, document_types, label_markers)
                    for document_types, label_markers in targets
                )
            else:
                target_requirements.add((gap, (), ()))
        for gap, document_types, label_markers in sorted(target_requirements):
            issues.append(
                _issue(
                    "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT",
                    "INCOMPLETE",
                    "이 첨부에는 정량평가표가 없다고 선언되어 다른 현재 첨부의 검증이 필요합니다.",
                    attachment_id=attachment_id,
                    required_sibling_document_types=document_types,
                    required_sibling_label_markers=label_markers,
                    source_gap_statement=gap,
                    source_gap_document_type=payload.document_type,
                )
            )
        if any(
            targets is None for _gap, targets in local_gap_targets
        ):
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
    attachment_profiles: Mapping[str, Mapping[str, object]] | None = None,
) -> QuantitativeCandidateProfile:
    """Merge persisted per-file records against the exact current manifest."""

    if not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    runtime_profiles_supplied = attachment_profiles is not None
    runtime_profiles = attachment_profiles or {}
    source_gaps_by_attachment_id: dict[str, tuple[str, ...] | None] = {}
    bindings_list: list[AttachmentDocumentBinding] = []
    for attachment_id, document_sha256 in sorted(expected_documents.items()):
        raw_profile = runtime_profiles.get(attachment_id, {})
        document_type = raw_profile.get("document_type")
        source_label = raw_profile.get("source_label")
        raw_source_gaps = raw_profile.get("missing_or_unreadable")
        source_gaps_by_attachment_id[attachment_id] = (
            tuple(_normalise_source_gap(item) for item in raw_source_gaps)
            if isinstance(raw_source_gaps, (list, tuple))
            and all(
                isinstance(item, str)
                and bool(_normalise_source_gap(item))
                and len(_normalise_source_gap(item)) <= 1000
                for item in raw_source_gaps
            )
            else None
        )
        bindings_list.append(
            AttachmentDocumentBinding(
                attachment_id=attachment_id,
                document_sha256=document_sha256,
                document_type=(
                    document_type
                    if document_type in {"NOTICE", "RFP", "SCOPE", "FORM", "OTHER"}
                    else None
                ),
                source_label=(
                    source_label.strip()
                    if (
                        isinstance(source_label, str)
                        and source_label.strip()
                        and len(source_label.strip()) <= 500
                    )
                    else None
                ),
            )
        )
    bindings = tuple(bindings_list)
    binding_by_attachment_id = {item.attachment_id: item for item in bindings}
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
    bound_records: dict[str, ValidatedQuantitativeAttachmentRecord] = {}

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
        source_gaps = source_gaps_by_attachment_id.get(attachment_id)
        gap_issue_codes = {
            item.code
            for item in record.issues
            if item.code
            in {
                "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT",
                "EXTRACTION_DECLARED_INCOMPLETE",
            }
        }
        if source_gaps is None:
            if runtime_profiles_supplied or gap_issue_codes:
                binding_errors.append(
                    _issue(
                        "SOURCE_GAP_BINDING_MISMATCH",
                        "INCOMPLETE",
                        "정량 검증 record의 결손 선언을 현재 추출 결과와 대조할 수 없습니다.",
                        attachment_id=attachment_id,
                    )
                )
        else:
            current_document_type = binding_by_attachment_id[
                attachment_id
            ].document_type
            expected_local_gap_statements: set[str] = set()
            expected_generic_gap = False
            if current_document_type is None and source_gaps:
                expected_generic_gap = True
            else:
                for gap in source_gaps:
                    if _is_explicit_qualitative_only_exclusion(gap):
                        continue
                    targets = _attachment_local_quantitative_table_targets(
                        gap,
                    )
                    if targets is None:
                        expected_generic_gap = True
                    else:
                        expected_local_gap_statements.add(gap)
            actual_local_gap_statements = {
                item.source_gap_statement
                for item in record.issues
                if item.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
                and item.source_gap_statement is not None
            }
            actual_generic_gap = (
                "EXTRACTION_DECLARED_INCOMPLETE" in gap_issue_codes
            )
            if (
                expected_local_gap_statements != actual_local_gap_statements
                or expected_generic_gap != actual_generic_gap
            ):
                binding_errors.append(
                    _issue(
                        "SOURCE_GAP_BINDING_MISMATCH",
                        "INCOMPLETE",
                        "정량 검증 record의 결손 선언이 현재 추출 결과와 일치하지 않습니다.",
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
        bound_records[attachment_id] = record
        tables.extend(record.tables)
        available.extend(record.available_candidates)
        review.extend(record.review_candidates)
        not_applicable.extend(record.not_applicable_evidence)

    supplying_attachment_ids = {
        attachment_id
        for attachment_id, record in bound_records.items()
        if any(table.status == "AVAILABLE" for table in record.tables)
    }
    local_gap_issue_counts = Counter(
        (attachment_id, item.source_gap_statement)
        for attachment_id, record in bound_records.items()
        for item in record.issues
        if item.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
        and item.source_gap_statement is not None
    )

    def local_absence_is_resolved(
        issue: QuantitativeValidationIssue,
        *,
        attachment_id: str,
    ) -> bool:
        if (
            issue.code != "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
            or not issue.required_sibling_document_types
            or not issue.required_sibling_label_markers
        ):
            return False
        source_binding = binding_by_attachment_id.get(attachment_id)
        if (
            source_binding is None
            or source_binding.source_label is None
            or source_binding.document_type != issue.source_gap_document_type
        ):
            return False
        source_label_types = _source_label_document_types(source_binding.source_label)
        if len(source_label_types) > 1:
            return False
        source_effective_type = (
            source_label_types[0]
            if source_label_types
            else source_binding.document_type
        )
        required_sibling_types = set(issue.required_sibling_document_types)
        if source_effective_type in required_sibling_types:
            if len(required_sibling_types) > 1:
                required_sibling_types.discard(source_effective_type)
            elif local_gap_issue_counts[
                (attachment_id, issue.source_gap_statement)
            ] > 1:
                # A multi-role statement may name the current attachment as
                # context (for example, "공고문에는 제안요청서 ... 없음").
                # Suppress only that current-role issue; another explicit role
                # from the same statement must still resolve independently.
                return True
            else:
                # A single same-role gap cannot borrow an unrelated document
                # merely because both were classified as the same type.
                return False
        if not required_sibling_types:
            return False
        for sibling_id in supplying_attachment_ids - {attachment_id}:
            binding = binding_by_attachment_id.get(sibling_id)
            if (
                binding is None
                or binding.source_label is None
            ):
                continue
            compact_label = _compact_document_label(binding.source_label)
            label_matches = any(
                marker in compact_label
                for marker in issue.required_sibling_label_markers
            )
            if not label_matches:
                continue
            label_types = _source_label_document_types(binding.source_label)
            if len(label_types) > 1:
                continue
            effective_type = binding.document_type
            if effective_type in {None, "OTHER"} and label_types:
                effective_type = label_types[0]
            if effective_type in required_sibling_types:
                return True
        return False

    for attachment_id, record in sorted(bound_records.items()):
        resolved_local_absence = any(
            local_absence_is_resolved(item, attachment_id=attachment_id)
            for item in record.issues
        )
        unresolved_record_issues = tuple(
            item
            for item in record.issues
            if not local_absence_is_resolved(item, attachment_id=attachment_id)
        )
        issues.extend(unresolved_record_issues)
        if (
            record.status == "INCOMPLETE"
            and not resolved_local_absence
            and not any(
                item.disposition == "INCOMPLETE"
                for item in unresolved_record_issues
            )
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
            item.disposition == "REVIEW" for item in unresolved_record_issues
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
