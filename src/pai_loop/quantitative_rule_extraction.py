from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
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
    QuantitativeRecognitionCondition,
    QuantitativeRuleCandidate,
    QuantitativeScoringMethod,
    QuantitativeTableCandidate,
    QuantitativeThresholdLiteral,
    SCHEMA_VERSION,
    evidence_quote_matches_source,
)
from .quantitative_formula import CaseTableRowLiteral, compile_case_table


QUANTITATIVE_CANDIDATE_PROFILE_VERSION = "pai-loop-quantitative-candidate-profile-0.7.10"
QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION = "pai-loop-quantitative-attachment-validator-0.6.10"

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
SourcewideAmbiguityResolutionBlocker = Literal[
    "SOURCEWIDE_AMBIGUITY_REASON_NOT_STRUCTURAL",
    "SOURCEWIDE_AMBIGUITY_SCOPE_UNSUPPORTED",
    "SOURCEWIDE_AMBIGUITY_SOURCE_GAPS_PRESENT",
    "SOURCEWIDE_AMBIGUITY_BOUNDARY_SCAN_LIMIT",
    "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION",
    "SOURCEWIDE_AMBIGUITY_REBIND_INCOMPLETE",
    "SOURCEWIDE_AMBIGUITY_SIGNATURE_UNSUPPORTED",
    "SOURCEWIDE_AMBIGUITY_GEOMETRY_UNPROVEN",
    "SOURCEWIDE_AMBIGUITY_CASE_CENSUS_MISMATCH",
    "SOURCEWIDE_AMBIGUITY_TOTAL_EVIDENCE_UNPROVEN",
    "SOURCEWIDE_AMBIGUITY_CASE_EVIDENCE_UNPROVEN",
    "SOURCEWIDE_AMBIGUITY_SECTION_UNPROVEN",
    "SOURCEWIDE_AMBIGUITY_COMPETING_STRUCTURE",
    "SOURCEWIDE_AMBIGUITY_TOTAL_PROVENANCE_UNPROVEN",
    "SOURCEWIDE_AMBIGUITY_TOTAL_MISMATCH",
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
_MAX_TABLE_CELL_WINDOW_LINES = 4
_MAX_TABLE_CELL_WINDOW_CHARS = 500
_MAX_CRITERION_HEADER_FALLBACK_LINES = 64
_MAX_SOURCEWIDE_HEADER_MATCHES = 64
_MAX_SOURCEWIDE_BOUNDARY_SPANS = 512
_HWP_SECTION_LINE_RE = re.compile(r"^\[HWP SECTION \d+\]$")
_SOURCEWIDE_TABLE_MARKER_RE = re.compile(
    r"^(?:[가-힣]\.\s*)?(?:정량(?:적)?\s*평가(?:\s*세부\s*기준|\s*기준|표)?|"
    r"평가\s*배점표|평가\s*기준표)$"
)
_SOURCEWIDE_HEADER_NOUN_SUFFIX_RE = re.compile(
    r"(?:현황|상태|능력|실적|보유|평가|기준|항목|인력|시설|장비|경력|"
    r"재무구조|조직)$"
)
_HWP_FOOTNOTE_LINE_RE = re.compile(
    r"^(?:※|[*＊]|[①-⑳]|\[\s*주\s*\]|주\s*\d+\s*[.)]?)"
)
_AMOUNT_UNIT_SCALE = {
    "원": Decimal("1"),
    "천원": Decimal("1000"),
    "만원": Decimal("10000"),
    "백만원": Decimal("1000000"),
    "천만원": Decimal("10000000"),
    "억원": Decimal("100000000"),
}
_AMOUNT_KOREAN_BOUND_RE = re.compile(
    rf"(?P<num>{_NUM_PATTERN})\s*"
    r"(?P<unit>천\s*만\s*원|백\s*만\s*원|억\s*원|만\s*원|천\s*원|원)\s*"
    r"(?P<op>이상|초과|이하|미만)",
    re.IGNORECASE,
)
_AMOUNT_ASCII_DIRECT_BOUND_RE = re.compile(
    rf"(?P<op>>=|<=|==|>|<|=)\s*(?P<num>{_NUM_PATTERN})\s*"
    r"(?P<unit>천\s*만\s*원|백\s*만\s*원|억\s*원|만\s*원|천\s*원|원)",
    re.IGNORECASE,
)
_AMOUNT_ASCII_REVERSED_BOUND_RE = re.compile(
    rf"(?P<num>{_NUM_PATTERN})\s*"
    r"(?P<unit>천\s*만\s*원|백\s*만\s*원|억\s*원|만\s*원|천\s*원|원)\s*"
    r"(?P<op>>=|<=|==|>|<|=)\s*(?=[A-Za-z가-힣_(]|$)",
    re.IGNORECASE,
)


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


def _source_lines(source: str) -> tuple[str, ...]:
    """Return visible HWP paragraphs while preserving their exact order."""

    return tuple(line.strip() for line in source.splitlines())


def _normalise_anchor_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    visible = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return " ".join(visible.split())


def _overlapping_substring_count(source: str, needle: str) -> int:
    if not needle:
        return 0
    count = 0
    offset = 0
    while (match := source.find(needle, offset)) >= 0:
        count += 1
        offset = match + 1
    return count


def _anchor_occurrence_count(quote: str, source: str) -> int:
    normalized_quote = _normalise_anchor_text(quote)
    normalized_source = _normalise_anchor_text(source)
    if not normalized_quote:
        return 0
    compact_quote = "".join(normalized_quote.split())
    compact_source = "".join(normalized_source.split())
    return max(
        _overlapping_substring_count(normalized_source, normalized_quote),
        _overlapping_substring_count(compact_source, compact_quote),
    )


def _anchor_line_spans(
    lines: tuple[str, ...],
    quote: str,
) -> tuple[tuple[int, int], ...]:
    """Locate every minimal bounded line span containing the exact anchor."""

    matches: list[tuple[int, int, str]] = []
    for start in range(len(lines)):
        for end in range(
            start + 1,
            min(len(lines), start + _MAX_TABLE_CELL_WINDOW_LINES) + 1,
        ):
            window = "\n".join(lines[start:end])
            if len(window) > _MAX_TABLE_CELL_WINDOW_CHARS:
                break
            if evidence_quote_matches_source(quote, window):
                matches.append((start, end, window))
                break
    if not matches:
        return ()
    minimal = [
        (start, end)
        for start, end, _window in matches
        if not any(
            other_start >= start
            and other_end <= end
            and (other_start, other_end) != (start, end)
            for other_start, other_end, _other_window in matches
        )
    ]
    return tuple(minimal)


def _unique_anchor_line_span(
    lines: tuple[str, ...],
    quote: str,
) -> tuple[int, int] | None:
    spans = _anchor_line_spans(lines, quote)
    if len(spans) != 1:
        return None
    start, end = spans[0]
    return spans[0] if _anchor_occurrence_count(quote, "\n".join(lines[start:end])) == 1 else None


def _all_anchor_line_spans(
    lines: tuple[str, ...],
    values: Iterable[str],
) -> tuple[tuple[int, int], ...]:
    """Return every source occurrence span, deduplicated in source order."""

    return tuple(
        dict.fromkeys(
            span
            for value in values
            if value
            for span in _anchor_line_spans(lines, value)
        )
    )


def _spans_overlap(
    left: tuple[int, int],
    right: tuple[int, int],
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _minimal_anchor_window(
    lines: tuple[str, ...],
    *,
    anchor_span: tuple[int, int],
    predicate: Callable[[str], bool],
    maximum_lines: int = _MAX_TABLE_CELL_WINDOW_LINES,
    allow_before_anchor: bool = True,
) -> str | None:
    anchor_start, anchor_end = anchor_span
    lower = (
        max(0, anchor_start - maximum_lines + 1)
        if allow_before_anchor
        else anchor_start
    )
    upper = min(len(lines), anchor_end + maximum_lines - 1)
    matches: list[tuple[int, int, str]] = []
    for start in range(lower, anchor_start + 1):
        for end in range(anchor_end, upper + 1):
            if end - start > maximum_lines:
                continue
            window = "\n".join(lines[start:end])
            if (
                len(window) > _MAX_TABLE_CELL_WINDOW_CHARS
                or any(not line for line in lines[start:end])
                or any(_HWP_SECTION_LINE_RE.fullmatch(line) for line in lines[start:end])
            ):
                continue
            if predicate(window):
                matches.append((start, end, window))
    if not matches:
        return None
    best_size = min((end - start, len(window)) for start, end, window in matches)
    best = [
        window
        for start, end, window in matches
        if (end - start, len(window)) == best_size
    ]
    return best[0] if len(best) == 1 else None


def _score_cell_matches(
    line: str,
    *,
    value: float,
    percent: bool,
) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
    match = re.fullmatch(
        rf"(?:배점(?:의)?)?(?P<num>{_NUM_PATTERN})(?P<unit>점|%|퍼센트)?",
        compact,
    )
    if match is None:
        return False
    try:
        parsed = Decimal(match.group("num").replace(",", ""))
    except InvalidOperation:
        return False
    expected = _decimal(value)
    if parsed is None or expected is None or parsed != expected:
        return False
    unit = match.group("unit") or ""
    return unit in {"%", "퍼센트"} if percent else unit in {"", "점"}


def _is_score_cell(line: str) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
    return bool(
        re.fullmatch(
            rf"(?:배점(?:의)?)?{_NUM_PATTERN}(?:점|%|퍼센트)?",
            compact,
        )
    )


def _normalise_amount_unit(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _amount_gte_condition_matches(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
    case: QuantitativeCaseLiteral | ImmutableQuantitativeCase,
    condition: str,
) -> bool:
    expected = _decimal(case.comparison_value)
    if expected is None:
        return False
    if candidate.metric == "PERFORMANCE_AMOUNT":
        bound_matches = [
            (match, operator_map[match.group("op")])
            for regex, operator_map in (
                (_AMOUNT_KOREAN_BOUND_RE, _KOREAN_OPERATOR),
                (_AMOUNT_ASCII_DIRECT_BOUND_RE, _DIRECT_OPERATOR),
                (_AMOUNT_ASCII_REVERSED_BOUND_RE, _REVERSED_OPERATOR),
            )
            for match in regex.finditer(condition)
        ]
        if not bound_matches:
            # Preserve the existing explicit header-unit inheritance path for
            # truly unitless rows. A recognised currency unit must bind to the
            # comparator in this row and may never borrow another metric's term.
            if re.search(r"천\s*만\s*원|백\s*만\s*원|억\s*원|만\s*원|천\s*원|원", condition):
                return False
            return Counter(_comparator_terms(condition)) == Counter(
                ((expected, "GTE"),)
            )
        if (
            len(bound_matches) != 1
            or bound_matches[0][1] != "GTE"
            or len(_COMPARATOR_MARKER_RE.findall(condition)) != 1
        ):
            return False
        match = bound_matches[0][0]
        try:
            source_number = Decimal(match.group("num").replace(",", ""))
        except InvalidOperation:
            return False
        source_scale = _AMOUNT_UNIT_SCALE.get(
            _normalise_amount_unit(match.group("unit"))
        )
        candidate_scale = _AMOUNT_UNIT_SCALE.get(
            _normalise_amount_unit(candidate.unit or "")
        )
        return bool(
            source_scale is not None
            and candidate_scale is not None
            and source_number * source_scale == expected * candidate_scale
        )
    return bool(
        len(_NUMBER_RE.findall(condition)) == 1
        and Counter(_comparator_terms(condition)) == Counter(((expected, "GTE"),))
    )


def _case_condition_matches(
    candidate: QuantitativeRuleCandidate,
    case: QuantitativeCaseLiteral,
    condition: str,
) -> bool:
    if case.operator == "GTE":
        if candidate.metric == "PERFORMANCE_AMOUNT":
            return _amount_gte_condition_matches(candidate, case, condition)
        comparison = _decimal(case.comparison_value)
        return bool(
            comparison is not None
            and Counter(_comparator_terms(condition))
            == Counter(((comparison, "GTE"),))
        )
    if case.operator == "EQ":
        return bool(
            case.comparison_value is not None
            and len(_NUMBER_RE.findall(condition)) == 1
            and _literal_contains_number(case.comparison_value, condition)
            and not _COMPARATOR_MARKER_RE.search(condition)
        )
    return case.operator == "IN" and all(
        _case_literal_contains_exact_category(condition, value)
        for value in case.category_values
    )


def _case_comparison_matches(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
    case: QuantitativeCaseLiteral | ImmutableQuantitativeCase,
    literal: str,
) -> bool:
    if case.comparison_value is None:
        return True
    lines = literal.splitlines()
    condition = (
        "\n".join(lines[:-1])
        if len(lines) >= 2
        and _score_cell_matches(
            lines[-1],
            value=case.award_value,
            percent=case.award_kind == "PERCENT_OF_MAX",
        )
        else literal
    )
    if case.operator == "GTE":
        if candidate.metric == "PERFORMANCE_AMOUNT":
            return _amount_gte_condition_matches(candidate, case, condition)
        comparison = _decimal(case.comparison_value)
        return bool(
            comparison is not None
            and Counter(_comparator_terms(condition))
            == Counter(((comparison, "GTE"),))
        )
    return _literal_contains_number(case.comparison_value, condition)


def _case_row_window_matches(
    candidate: QuantitativeRuleCandidate,
    case: QuantitativeCaseLiteral,
    window: str,
) -> bool:
    row_lines = window.splitlines()
    if len(row_lines) < 2 or not _score_cell_matches(
        row_lines[-1],
        value=case.award_value,
        percent=case.award_kind == "PERCENT_OF_MAX",
    ):
        return False
    condition_lines = row_lines[:-1]
    # A score-like cell before the final cell proves a column-major or crossed
    # row layout, not one condition->award HWP row.
    if any(_is_score_cell(line) for line in condition_lines):
        return False
    condition = "\n".join(condition_lines)
    return _case_condition_matches(candidate, case, condition)


def _criterion_window_matches(
    candidate: QuantitativeRuleCandidate,
    window: str,
) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", window))
    expected = _decimal(candidate.max_points)
    descriptive_anchor = _normalise_anchor_text(candidate.evidence.quote)
    return bool(
        expected is not None
        and re.search(r"[A-Za-z가-힣]{2,}", descriptive_anchor)
        and evidence_quote_matches_source(candidate.evidence.quote, window)
        and any(
            Decimal(match.group("num").replace(",", "")) == expected
            for match in re.finditer(rf"(?P<num>{_NUM_PATTERN})\s*점", compact)
        )
    )


_METRIC_HEADER_TOKEN_GROUPS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    "PERFORMANCE_AMOUNT": (("실적", "금액"),),
    "PERFORMANCE_COUNT": (("실적", "건수"),),
    "CREDIT_RATING": (("경영상태",), ("신용평가등급",)),
}


def _metric_header_matches(
    candidate: QuantitativeRuleCandidate,
    window: str,
) -> bool:
    """Recognise only the supported metric words in one exact HWP header."""

    groups = _METRIC_HEADER_TOKEN_GROUPS.get(candidate.metric, ())
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", window))
    return bool(
        groups
        and any(all(token in compact for token in group) for group in groups)
    )


def _header_point_tokens(window: str) -> tuple[tuple[Decimal, int], ...]:
    """Return explicit ``N점`` values and offsets from one bounded window."""

    normalized = unicodedata.normalize("NFKC", window)
    tokens: list[tuple[Decimal, int]] = []
    for match in re.finditer(rf"(?P<num>{_NUM_PATTERN})\s*점", normalized):
        try:
            tokens.append(
                (Decimal(match.group("num").replace(",", "")), match.start())
            )
        except InvalidOperation:
            return ()
    return tuple(tokens)


def _metric_tokens_precede_point(
    metric: str,
    window: str,
    point_offset: int,
) -> bool:
    normalized = unicodedata.normalize("NFKC", window)
    return any(
        all(0 <= normalized.find(token) < point_offset for token in group)
        for group in _METRIC_HEADER_TOKEN_GROUPS.get(metric, ())
    )


def _metric_header_is_unique(
    candidate: QuantitativeRuleCandidate,
    window: str,
) -> bool:
    """Require one exact supported metric phrase, not repeated header text."""

    groups = _METRIC_HEADER_TOKEN_GROUPS.get(candidate.metric, ())
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", window))
    matching_groups = [
        group for group in groups if all(token in compact for token in group)
    ]
    return bool(
        matching_groups
        and all(
            compact.count(token) == 1
            for group in matching_groups
            for token in group
        )
    )


def _minimal_sourcewide_spans(
    matches: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Remove containing windows in linear time for the fixed four-line bound."""

    unique = tuple(dict.fromkeys(matches))
    lookup = set(unique)
    minimal: list[tuple[int, int]] = []
    for span in unique:
        contains_smaller = any(
            (inner_start, inner_end) in lookup
            and (inner_start, inner_end) != span
            for inner_start in range(span[0], span[1])
            for inner_end in range(inner_start + 1, span[1] + 1)
        )
        if not contains_smaller:
            minimal.append(span)
    return tuple(minimal)


def _candidate_metric_max_header_spans(
    lines: tuple[str, ...],
    candidate: QuantitativeRuleCandidate,
) -> tuple[tuple[int, int], ...]:
    """Return minimal source-wide metric+maximum HWP header slices.

    This fallback deliberately does not trust the model's criterion quote.  A
    production extraction can anchor a summary subtotal while its CASE_TABLE
    rows belong to a later detailed header.  Only one-to-four-line source
    slices with the supported metric words and exactly one ``N점`` token equal
    to the candidate maximum are eligible.  The owning rows and structural
    boundaries are checked separately before a slice can be rebound.
    """

    if (
        candidate.scoring_method != "CASE_TABLE"
        or not candidate.cases
        or candidate.metric not in _METRIC_HEADER_TOKEN_GROUPS
        or [case.row_order for case in candidate.cases]
        != list(range(1, len(candidate.cases) + 1))
    ):
        return ()
    expected = _decimal(candidate.max_points)
    if expected is None:
        return ()

    matches: list[tuple[int, int]] = []
    for start in range(len(lines)):
        for end in range(
            start + 1,
            min(len(lines), start + _MAX_TABLE_CELL_WINDOW_LINES) + 1,
        ):
            window_lines = lines[start:end]
            if any(not line for line in window_lines) or any(
                _HWP_SECTION_LINE_RE.fullmatch(line) for line in window_lines
            ):
                break
            window = "\n".join(window_lines)
            if len(window) > _MAX_TABLE_CELL_WINDOW_CHARS:
                break
            point_tokens = _header_point_tokens(window)
            metric_before_points = bool(
                len(point_tokens) == 1
                and _metric_tokens_precede_point(
                    candidate.metric,
                    window,
                    point_tokens[0][1],
                )
            )
            if (
                tuple(value for value, _offset in point_tokens) == (expected,)
                and metric_before_points
                and _metric_header_is_unique(candidate, window)
            ):
                matches.append((start, end))
                if len(matches) > _MAX_SOURCEWIDE_HEADER_MATCHES:
                    return ()

    # A split header can satisfy the predicate together with an unnecessary
    # adjacent line.  Retain only the exact minimal source slice.
    return _minimal_sourcewide_spans(matches)


def _clear_sourcewide_criterion_header(window: str) -> bool:
    """Recognise a noun-like criterion heading, never a value/score case row."""

    normalized = unicodedata.normalize("NFKC", window)
    match = re.fullmatch(
        rf"\s*(?:(?:\d+|[가-힣])\s*[.)]\s*|[❍○●■□▪▶]\s*)?"
        rf"(?P<label>[A-Za-z0-9가-힣]"
        rf"[A-Za-z0-9가-힣\s·/&()_,.\-]{{1,160}}?)\s*"
        rf"(?:\(\s*)?{_NUM_PATTERN}\s*점\s*\)?\s*",
        normalized,
    )
    if match is None:
        return False
    label = re.sub(r"[\s·/&()_,.\-]+$", "", match.group("label"))
    semantic_label = re.sub(
        r"\d+(?:\.\d+)?\s*(?:건|원|명|%|등급|점|년|개월|회|개)",
        "",
        label,
    )
    return bool(
        _SOURCEWIDE_HEADER_NOUN_SUFFIX_RE.search(label)
        and not re.search(r"(?:건|원|명|%|등급)$", label)
        and len(re.findall(r"[가-힣]", semantic_label)) >= 4
    )


_SOURCEWIDE_QUANTITATIVE_COLUMN_HEADER_CLUSTER = (
    "세부항목",
    "평가요소",
    "배점",
    "등급",
    "배점(점)",
)
_MAX_SOURCEWIDE_COLUMN_HEADER_LOOKBACK = 8
_MAX_SOURCEWIDE_COLUMN_DETAIL_PREFIX_LINES = 3


def _is_quantitative_column_detail_boundary(
    lines: tuple[str, ...],
    *,
    start: int,
    end: int,
) -> bool:
    """Identify a performance-detail cell beneath an explicit table header.

    HWP extraction flattens rows into one line per cell.  In that layout the
    detail description beneath the five quantitative column headings can look
    like a new, unnumbered criterion because it also ends in ``(N점)``.  Keep
    the exception tied to the exact ordered header cluster and the performance
    detail vocabulary so an independent noun-like criterion remains a hard
    source boundary.  The candidate window may begin at the final ``배점(점)``
    cell, so inspect the whole bounded lookback rather than only its start.
    """

    context_start = max(0, start - _MAX_SOURCEWIDE_COLUMN_HEADER_LOOKBACK)
    indexed = tuple(
        (
            index,
            re.sub(r"\s+", "", unicodedata.normalize("NFKC", lines[index])),
        )
        for index in range(context_start, end)
        if lines[index] and not _HWP_SECTION_LINE_RE.fullmatch(lines[index])
    )
    cluster_size = len(_SOURCEWIDE_QUANTITATIVE_COLUMN_HEADER_CLUSTER)
    for index in range(max(0, len(indexed) - cluster_size + 1)):
        cluster = indexed[index : index + cluster_size]
        if tuple(value for _line_index, value in cluster) != (
            _SOURCEWIDE_QUANTITATIVE_COLUMN_HEADER_CLUSTER
        ):
            continue
        cluster_start = cluster[0][0]
        cluster_end = cluster[-1][0] + 1
        if not (
            cluster_start
            <= start
            <= cluster_end + _MAX_SOURCEWIDE_COLUMN_DETAIL_PREFIX_LINES
        ):
            continue
        detail_window = "\n".join(lines[cluster_end:end])
        if re.match(
            r"\s*(?:(?:\d+|[가-힣])\s*[.)]\s*|[❍○●■□▪▶]\s*)",
            detail_window,
        ):
            continue
        detail_points = _header_point_tokens(detail_window)
        if len(detail_points) != 1:
            continue
        preceding_points: set[Decimal] = set()
        for header_start in range(
            max(0, cluster_start - _MAX_TABLE_CELL_WINDOW_LINES),
            cluster_start,
        ):
            header_lines = lines[header_start:cluster_start]
            if any(not line for line in header_lines) or any(
                _HWP_SECTION_LINE_RE.fullmatch(line) for line in header_lines
            ):
                continue
            header_window = "\n".join(header_lines)
            header_points = _header_point_tokens(header_window)
            if len(header_points) != 1:
                continue
            header_point, point_offset = header_points[0]
            if any(
                _metric_tokens_precede_point(metric, header_window, point_offset)
                for metric in ("PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT")
            ):
                preceding_points.add(header_point)
        if preceding_points != {detail_points[0][0]}:
            continue
        detail = "".join(
            value for line_index, value in indexed if cluster_end <= line_index < end
        )
        if all(
            token in detail
            for token in (
                "최근",
                "지자체",
                "공공기관",
                "교육",
                "취업",
                "행사",
                "용역",
                "수행완료",
                "실적",
            )
        ):
            return True
    return False


_COUNT_SOURCE_UNIT_RE = re.compile(
    rf"(?<![\d.])(?P<num>{_NUM_PATTERN})\s*(?P<unit>건|회|개)"
)
_SOURCEWIDE_SIMPLE_QUANTITATIVE_TOTAL_RE = re.compile(
    rf"^(?:[❍·•-])?(?:정량(?:적)?평가)?(?:총점|총배점|합계)"
    rf"[:：]?(?P<points>{_NUM_PATTERN})점(?:만점)?[.]?$"
)
_SOURCEWIDE_QUANTITATIVE_SUMMARY_TOTAL_RE = re.compile(
    rf"^(?:❍)?정량적평가\((?P<points>{_NUM_PATTERN})점\):.+평가$"
)
_BUSAN_QUANTITATIVE_DETAIL_MARKER_RE = re.compile(
    r"^[가-힣]\.\s*정량적\s*평가\s*세부\s*기준$"
)
_CREDIT_RATING_COLUMN_HEADER_CLUSTER = (
    "신용평가등급",
    "평점",
    "회사채",
    "기업어음",
    "기업신용평가등급",
)
_BUSAN_SOURCEWIDE_AMBIGUITY_SIGNATURE = (
    ("PERFORMANCE_AMOUNT", Decimal("6")),
    ("PERFORMANCE_COUNT", Decimal("4")),
    ("CREDIT_RATING", Decimal("10")),
)
_BUSAN_ENTERPRISE_CREDIT_CATEGORY_ROWS = (
    ("AAA", "AA+", "AA0", "AA-", "A+", "A0", "A-", "BBB+", "BBB0"),
    ("BBB-", "BB+", "BB0", "BB-"),
    ("B+", "B0", "B-"),
    ("CCC+ 이하",),
)
_BUSAN_CREDIT_RATING_FOOTNOTE_RE = re.compile(
    r"^\*등급별평점이소수점이하의숫자가있는경우"
    r"소수점다섯째자리에서반올림함[.]?$"
)
_SOURCEWIDE_LABELED_CASE_START_RE = re.compile(r"^(?P<label>[A-Z])[.]\s*\S.*$")
_COMPACT_COUNT_MINIMUM_RE = re.compile(
    r"실적건수\(\d[\d,]*(?:\.\d+)?"
    r"(?:천만원|백만원|억원|만원|천원|원|억|만|천)이상\)"
)
_PERFORMANCE_FOOTNOTE_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "ANCHOR": re.compile(r"^①.*최근\s*3년간.*입찰\s*공고일.*기준"),
    "CERTIFICATE": re.compile(
        r"^②.*증빙서류.*용역수행실적.*용역실적증명서"
    ),
    "SHARE": re.compile(
        r"^③.*공동계약.*참여\s*비율.*금액.*실적"
    ),
}


def _source_bound_recognition_condition(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    span: tuple[int, int],
) -> QuantitativeRecognitionCondition | None:
    """Build one exact HWP recognition condition, never reconstructed prose."""

    start, end = span
    if (
        end <= start
        or end - start > _MAX_TABLE_CELL_WINDOW_LINES
        or any(not line for line in lines[start:end])
        or any(_HWP_SECTION_LINE_RE.fullmatch(line) for line in lines[start:end])
    ):
        return None
    quote = "\n".join(lines[start:end])
    if (
        len(quote) > 500
        or _unique_anchor_line_span(lines, quote) != span
    ):
        return None
    return QuantitativeRecognitionCondition(
        literal=quote,
        evidence=candidate.evidence.model_copy(
            update={"page": None, "quote": quote}
        ),
    )


def _unique_quantitative_column_cluster(
    lines: tuple[str, ...],
    *,
    region: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if region is None:
        return None
    cluster_size = len(_SOURCEWIDE_QUANTITATIVE_COLUMN_HEADER_CLUSTER)
    matches: list[tuple[int, int]] = []
    for start in range(region[0], max(region[0], region[1] - cluster_size + 1)):
        end = start + cluster_size
        if end > region[1]:
            break
        compact = tuple(
            re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
            for line in lines[start:end]
        )
        if compact == _SOURCEWIDE_QUANTITATIVE_COLUMN_HEADER_CLUSTER:
            matches.append((start, end))
    return matches[0] if len(matches) == 1 else None


def _performance_detail_conditions(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    criterion_region: tuple[int, int] | None,
) -> tuple[QuantitativeRecognitionCondition, ...]:
    """Recover exact semantic cells between a unique HWP header and first row."""

    if (
        criterion_region is None
        or candidate.metric not in {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT"}
        or candidate.scoring_method != "CASE_TABLE"
        or not candidate.cases
    ):
        return ()
    cluster = _unique_quantitative_column_cluster(lines, region=criterion_region)
    first_case = _unique_anchor_line_span(lines, candidate.cases[0].literal)
    if (
        cluster is None
        or first_case is None
        or not _span_inside_region(first_case, criterion_region)
        or first_case[0] <= cluster[1]
    ):
        return ()

    detail_end = first_case[0]
    while detail_end > cluster[1] and _score_cell_matches(
        lines[detail_end - 1],
        value=candidate.max_points,
        percent=False,
    ):
        detail_end -= 1
    marker_tokens = (
        ("단일", "용역")
        if candidate.metric == "PERFORMANCE_AMOUNT"
        else ("실적", "건수")
    )
    marker_indexes = [
        index
        for index in range(cluster[1], detail_end)
        if all(
            token
            in re.sub(r"\s+", "", unicodedata.normalize("NFKC", lines[index]))
            for token in marker_tokens
        )
    ]
    if len(marker_indexes) != 1:
        return ()
    marker = marker_indexes[0]
    scope_span = (cluster[1], marker)
    detail_span = (marker, detail_end)
    if (
        scope_span[1] <= scope_span[0]
        or detail_span[1] <= detail_span[0]
        or not _is_quantitative_column_detail_boundary(
            lines,
            start=scope_span[0],
            end=scope_span[1],
        )
    ):
        return ()
    compact_detail = re.sub(
        r"\s+",
        "",
        unicodedata.normalize(
            "NFKC",
            "\n".join(lines[detail_span[0] : detail_span[1]]),
        ),
    )
    if candidate.metric == "PERFORMANCE_AMOUNT":
        detail_matches = bool(
            re.fullmatch(
                r"단일용역(?:의)?(?:최고|최대)(?:계약)?금액\(1건\)",
                compact_detail,
            )
        )
    else:
        detail_matches = bool(_COMPACT_COUNT_MINIMUM_RE.fullmatch(compact_detail))
    if not detail_matches:
        return ()

    conditions = tuple(
        _source_bound_recognition_condition(
            candidate,
            lines=lines,
            span=span,
        )
        for span in (scope_span, detail_span)
    )
    return (
        tuple(item for item in conditions if item is not None)
        if all(item is not None for item in conditions)
        else ()
    )


def _unique_performance_footnotes(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    footer_region: tuple[int, int] | None,
) -> Mapping[str, QuantitativeRecognitionCondition]:
    """Return the exact ordered 부산-style recognition block when unambiguous."""

    if footer_region is None:
        return {}
    markers = [
        index
        for index in range(footer_region[0], footer_region[1])
        if re.sub(
            r"\s+",
            "",
            unicodedata.normalize("NFKC", lines[index]),
        ).startswith("※실적인정기준")
    ]
    if len(markers) != 1:
        return {}
    resolved: dict[str, tuple[int, int]] = {}
    for key, pattern in _PERFORMANCE_FOOTNOTE_PATTERNS.items():
        matches = [
            (index, index + 1)
            for index in range(markers[0] + 1, footer_region[1])
            if pattern.search(lines[index])
        ]
        if len(matches) != 1:
            return {}
        resolved[key] = matches[0]
    ordered = [resolved[key][0] for key in ("ANCHOR", "CERTIFICATE", "SHARE")]
    if ordered != sorted(ordered) or any(
        not lines[index] or _HWP_SECTION_LINE_RE.fullmatch(lines[index])
        for index in range(markers[0], resolved["SHARE"][1])
    ):
        return {}
    output: dict[str, QuantitativeRecognitionCondition] = {}
    for key, span in resolved.items():
        condition = _source_bound_recognition_condition(
            candidate,
            lines=lines,
            span=span,
        )
        if condition is None:
            return {}
        output[key] = condition
    return output


def _source_bound_count_unit(
    candidate: QuantitativeRuleCandidate,
) -> str | None:
    if (
        candidate.metric != "PERFORMANCE_COUNT"
        or candidate.scoring_method != "CASE_TABLE"
        or not candidate.cases
    ):
        return None
    units: list[str] = []
    for case in candidate.cases:
        expected = _decimal(case.comparison_value)
        if expected is None or case.operator not in {"GTE", "EQ"}:
            return None
        matches = list(_COUNT_SOURCE_UNIT_RE.finditer(case.literal))
        valid: list[str] = []
        for match in matches:
            try:
                parsed = Decimal(match.group("num").replace(",", ""))
            except InvalidOperation:
                return None
            if parsed == expected:
                valid.append(match.group("unit"))
        if len(matches) != 1 or len(valid) != 1:
            return None
        units.append(valid[0])
    return units[0] if units and len(set(units)) == 1 else None


def _source_bound_credit_rating_case_region(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    criterion_region: tuple[int, int] | None,
) -> tuple[int, int] | None:
    """Fence the 부산 credit rows at their unique exact source footnote.

    The HWP extractor can leave the final criterion open through later forms,
    whose standalone numbers look like score cells.  Cropping at the last
    modeled case would trust the payload, so the only accepted end boundary is
    the unique published rounding footnote immediately following all owned case
    rows.  Missing/repeated footnotes and any intervening nonblank row fail
    closed.
    """

    if (
        candidate.metric != "CREDIT_RATING"
        or candidate.scoring_method != "CASE_TABLE"
        or not candidate.cases
        or criterion_region is None
    ):
        return None
    footnotes = [
        index
        for index, line in enumerate(lines)
        if _BUSAN_CREDIT_RATING_FOOTNOTE_RE.fullmatch(
            re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
        )
    ]
    if len(footnotes) != 1:
        return None
    footnote = footnotes[0]
    bounded_region = (criterion_region[0], footnote)
    if (
        footnote <= criterion_region[0]
        or footnote >= criterion_region[1]
    ):
        return None

    case_spans: list[tuple[int, int]] = []
    for case in sorted(candidate.cases, key=lambda item: item.row_order):
        literal_span = _unique_anchor_line_span(lines, case.literal)
        evidence_span = _unique_anchor_line_span(lines, case.evidence.quote)
        if (
            literal_span is None
            or evidence_span is None
            or literal_span != evidence_span
            or not _span_inside_region(literal_span, bounded_region)
        ):
            return None
        case_spans.append(literal_span)
    if any(
        left[1] > right[0]
        for left, right in zip(case_spans, case_spans[1:], strict=False)
    ):
        return None
    if any(lines[index] for index in range(case_spans[-1][1], footnote)):
        return None
    return bounded_region


def _source_bound_credit_rating_header_quote(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    criterion_region: tuple[int, int] | None,
) -> str | None:
    """Return one exact owned HWP header quote proving the rating unit."""

    if (
        candidate.metric != "CREDIT_RATING"
        or candidate.scoring_method != "CASE_TABLE"
        or not candidate.cases
        or criterion_region is None
        or not all(
            case.operator == "IN"
            and case.category_values
            and case.comparison_value is None
            for case in candidate.cases
        )
    ):
        return None

    source_region = criterion_region
    owned_region = _source_bound_credit_rating_case_region(
        candidate,
        lines=lines,
        criterion_region=source_region,
    )
    if owned_region is None:
        return None
    criterion_region = owned_region

    criterion_literal_span = _unique_anchor_line_span(
        lines, candidate.criterion_literal
    )
    criterion_evidence_span = _unique_anchor_line_span(
        lines, candidate.evidence.quote
    )
    if (
        criterion_literal_span is None
        or criterion_evidence_span is None
        or criterion_literal_span[0] != criterion_region[0]
        or criterion_evidence_span[0] != criterion_region[0]
        or not _spans_overlap(criterion_literal_span, criterion_evidence_span)
        or not _span_inside_region(criterion_literal_span, criterion_region)
        or not _span_inside_region(criterion_evidence_span, criterion_region)
    ):
        return None

    case_spans: list[tuple[int, int]] = []
    for case in sorted(candidate.cases, key=lambda item: item.row_order):
        literal_span = _unique_anchor_line_span(lines, case.literal)
        evidence_span = _unique_anchor_line_span(lines, case.evidence.quote)
        award_span = _unique_percent_score_line_span(
            lines,
            case=case,
            criterion_region=criterion_region,
        )
        literal_lines = case.literal.splitlines()
        if (
            literal_span is None
            or evidence_span is None
            or award_span is None
            or literal_span != evidence_span
            or literal_span[1] != award_span[1]
            or not _span_inside_region(literal_span, criterion_region)
            or not _span_inside_region(award_span, literal_span)
            or not _case_row_window_matches(candidate, case, case.literal)
            or len(literal_lines) < 2
        ):
            return None
        observed_cell = re.sub(
            r",+",
            ",",
            re.sub(
                r"\s+",
                "",
                unicodedata.normalize("NFKC", "\n".join(literal_lines[:-1])),
            ),
        ).strip(",")
        expected_cell = ",".join(
            re.sub(
                r"\s+",
                "",
                unicodedata.normalize("NFKC", value),
            ).strip(",")
            for value in case.category_values
        )
        if observed_cell != expected_cell:
            return None
        case_spans.append(literal_span)

    if any(
        left[1] > right[0]
        for left, right in zip(case_spans, case_spans[1:], strict=False)
    ):
        return None
    if not _sourcewide_case_census_matches(
        candidate,
        lines=lines,
        criterion_region=source_region,
    ):
        return None

    first_case_start = case_spans[0][0]
    cluster_size = len(_CREDIT_RATING_COLUMN_HEADER_CLUSTER)
    matches: list[tuple[int, int]] = []
    for start in range(
        criterion_region[0] + 1,
        max(criterion_region[0] + 1, first_case_start - cluster_size + 1),
    ):
        end = start + cluster_size
        if end > first_case_start:
            break
        observed = tuple(
            re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
            for line in lines[start:end]
        )
        if observed == _CREDIT_RATING_COLUMN_HEADER_CLUSTER:
            matches.append((start, end))
    if len(matches) != 1:
        return None
    # Keep the persisted evidence inside the ordinary bounded-anchor window.
    # The complete five-cell cluster was proved above; the title-through-first
    # `신용평가등급` cell is the smallest quote that binds both the criterion
    # maximum and the recovered categorical unit.
    bound_span = (criterion_region[0], matches[0][0] + 1)
    quote = "\n".join(lines[bound_span[0] : bound_span[1]])
    return (
        quote
        if len(quote) <= 500
        and _unique_anchor_line_span(lines, quote) == bound_span
        else None
    )


def _source_bound_performance_footer_region(
    candidates: list[QuantitativeRuleCandidate],
    *,
    lines: tuple[str, ...],
    criterion_regions: list[tuple[int, int] | None],
    boundary_spans: Iterable[tuple[int, int]],
) -> tuple[int, int] | None:
    """Fence shared footnotes to the final modeled performance criterion.

    부산형 표는 공통 실적인정 각주를 금액 항목이 아니라 마지막 건수
    행 뒤에 둔다.  그 좁은 footer만 공유하고, 다음 명시적 기준이나 다른
    표의 각주까지 가로질러 빌리지 않는다.
    """

    owners = [
        (index, region)
        for index, (candidate, region) in enumerate(
            zip(candidates, criterion_regions, strict=True)
        )
        if candidate.metric in {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT"}
        and candidate.scoring_method == "CASE_TABLE"
        and candidate.cases
        and region is not None
    ]
    if not owners:
        return None
    owner_index, owner_region = max(owners, key=lambda item: item[1][0])
    case_ends: list[int] = []
    for case in candidates[owner_index].cases:
        literal_span = _unique_anchor_line_span(lines, case.literal)
        evidence_span = _unique_anchor_line_span(lines, case.evidence.quote)
        if (
            literal_span is None
            or evidence_span is None
            or not _span_inside_region(literal_span, owner_region)
            or not _span_inside_region(evidence_span, owner_region)
        ):
            return None
        case_ends.append(max(literal_span[1], evidence_span[1]))
    if case_ends != sorted(case_ends):
        return None
    footer_start = case_ends[-1]
    footer_end = min(
        (
            owner_region[1],
            *(
                span[0]
                for span in boundary_spans
                if span[0] >= footer_start
            ),
        )
    )
    return (
        (footer_start, footer_end)
        if footer_end > footer_start
        else None
    )


def _repair_source_bound_candidate_unit(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    criterion_region: tuple[int, int] | None,
) -> QuantitativeRuleCandidate:
    normalized = _normalise_amount_unit(candidate.unit or "")
    if candidate.metric == "PERFORMANCE_COUNT":
        if normalized in {"건", "회", "개"}:
            return candidate
        repaired = _source_bound_count_unit(candidate)
        return (
            candidate.model_copy(update={"unit": repaired})
            if repaired is not None
            else candidate
        )
    if candidate.metric == "CREDIT_RATING":
        if normalized in {"등급", "신용등급", "rating"}:
            return candidate
        bound_quote = _source_bound_credit_rating_header_quote(
            candidate,
            lines=lines,
            criterion_region=criterion_region,
        )
        return (
            candidate.model_copy(
                update={
                    "unit": "등급",
                    "evidence": candidate.evidence.model_copy(
                        update={"page": None, "quote": bound_quote}
                    ),
                }
            )
            if bound_quote is not None
            else candidate
        )
    return candidate


def _augment_sourcewide_hwp_activation_context(
    candidates: list[QuantitativeRuleCandidate],
    *,
    lines: tuple[str, ...],
    criterion_regions: list[tuple[int, int] | None],
    boundary_spans: Iterable[tuple[int, int]],
    protected_structure_spans: Iterable[tuple[int, int]],
) -> list[QuantitativeRuleCandidate]:
    """Attach only exact table semantics needed by deterministic activation."""

    footer_region = _source_bound_performance_footer_region(
        candidates,
        lines=lines,
        criterion_regions=criterion_regions,
        boundary_spans=boundary_spans,
    )
    protected_spans = tuple(protected_structure_spans)
    output: list[QuantitativeRuleCandidate] = []
    for candidate, criterion_region in zip(
        candidates,
        criterion_regions,
        strict=True,
    ):
        candidate = _repair_source_bound_candidate_unit(
            candidate,
            lines=lines,
            criterion_region=criterion_region,
        )
        if candidate.metric not in {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT"}:
            output.append(candidate)
            continue
        detail_conditions = _performance_detail_conditions(
            candidate,
            lines=lines,
            criterion_region=criterion_region,
        )
        if not detail_conditions:
            output.append(candidate)
            continue
        footnotes = _unique_performance_footnotes(
            candidate,
            lines=lines,
            footer_region=footer_region,
        )
        additions = [
            *detail_conditions,
            *(footnotes[key] for key in ("ANCHOR", "CERTIFICATE") if key in footnotes),
        ]
        if candidate.metric == "PERFORMANCE_AMOUNT" and "SHARE" in footnotes:
            additions.append(footnotes["SHARE"])
        conditions = list(candidate.recognition_conditions)
        for condition in additions:
            # Claude may copy a short semantic recognition literal while its
            # evidence anchor contains that same HWP cell in full.
            # The source-derived condition below is the canonical full-cell
            # claim.  Keeping both shapes would make two different keys own
            # the same physical span and falsely trip the collision guard.
            # Replace exactly one condition whose provenance matches and whose
            # literal/evidence footprint is wholly covered by this independently
            # proven source cell.  A short literal may occur in another
            # criterion (for example, the same lookback phrase in both amount
            # and count rows), so uniqueness is proved inside the owned full
            # cell rather than across the entire document.  Multiple matches
            # inside the cell, broad evidence crossing cells, and multiple
            # model conditions must remain visible to the duplicate/collision
            # guards.
            addition_literal_span = _unique_anchor_line_span(
                lines, condition.literal
            )
            addition_evidence_span = _unique_anchor_line_span(
                lines, condition.evidence.quote
            )
            addition_span = (
                addition_literal_span
                if addition_literal_span is not None
                and addition_literal_span == addition_evidence_span
                else None
            )
            if addition_span is not None:
                addition_text = "\n".join(
                    lines[addition_span[0] : addition_span[1]]
                )
                replacement_indexes: list[int] = []
                for existing_index, existing in enumerate(conditions):
                    all_existing_literal_spans = _anchor_line_spans(
                        lines, existing.literal
                    )
                    all_existing_evidence_spans = _anchor_line_spans(
                        lines, existing.evidence.quote
                    )
                    existing_literal_spans = tuple(
                        span
                        for span in all_existing_literal_spans
                        if _span_inside_region(span, addition_span)
                    )
                    existing_evidence_spans = tuple(
                        span
                        for span in all_existing_evidence_spans
                        if _span_inside_region(span, addition_span)
                    )
                    outside_structure_collision = any(
                        not _span_inside_region(span, addition_span)
                        and any(
                            _spans_overlap(span, protected_span)
                            for protected_span in protected_spans
                        )
                        for span in (
                            *all_existing_literal_spans,
                            *all_existing_evidence_spans,
                        )
                    )
                    if (
                        existing.evidence.attachment_id
                        == condition.evidence.attachment_id
                        and len(existing_literal_spans) == 1
                        and len(existing_evidence_spans) == 1
                        and _anchor_occurrence_count(
                            existing.literal,
                            addition_text,
                        )
                        == 1
                        and _anchor_occurrence_count(
                            existing.evidence.quote,
                            addition_text,
                        )
                        == 1
                        and not outside_structure_collision
                        and _spans_overlap(
                            existing_literal_spans[0],
                            existing_evidence_spans[0],
                        )
                        and _literal_is_anchored(
                            existing.literal,
                            existing.evidence,
                            condition.literal,
                        )
                    ):
                        replacement_indexes.append(existing_index)
                if len(replacement_indexes) == 1:
                    conditions.pop(replacement_indexes[0])
            key = _recognition_key(condition.literal, condition.evidence.quote)
            if key not in {
                _recognition_key(item.literal, item.evidence.quote)
                for item in conditions
            }:
                conditions.append(condition)
        output.append(
            candidate.model_copy(update={"recognition_conditions": conditions})
        )
    return output


def _sourcewide_structural_boundary_spans(
    lines: tuple[str, ...],
) -> tuple[tuple[tuple[int, int], ...], bool]:
    """Find explicit physical table/criterion boundaries omitted by the model."""

    matches: list[tuple[int, int]] = [
        (index, index + 1)
        for index, line in enumerate(lines)
        if _SOURCEWIDE_TABLE_MARKER_RE.fullmatch(
            unicodedata.normalize("NFKC", line).strip()
        )
    ]
    if len(matches) > _MAX_SOURCEWIDE_BOUNDARY_SPANS:
        return (), True
    for start in range(len(lines)):
        for end in range(
            start + 1,
            min(len(lines), start + _MAX_TABLE_CELL_WINDOW_LINES) + 1,
        ):
            window_lines = lines[start:end]
            if any(not line for line in window_lines) or any(
                _HWP_SECTION_LINE_RE.fullmatch(line) for line in window_lines
            ):
                break
            window = "\n".join(window_lines)
            if len(window) > _MAX_TABLE_CELL_WINDOW_CHARS:
                break
            point_tokens = _header_point_tokens(window)
            if len(point_tokens) != 1 or point_tokens[0][0] <= 0:
                continue
            point_offset = point_tokens[0][1]
            supported_metric_header = any(
                _metric_tokens_precede_point(metric, window, point_offset)
                for metric in _METRIC_HEADER_TOKEN_GROUPS
            )
            explicit_criterion_header = bool(
                not _COMPARATOR_MARKER_RE.search(window)
                and _clear_sourcewide_criterion_header(window)
                and not _is_quantitative_column_detail_boundary(
                    lines,
                    start=start,
                    end=end,
                )
            )
            if supported_metric_header or explicit_criterion_header:
                matches.append((start, end))
                if len(matches) > _MAX_SOURCEWIDE_BOUNDARY_SPANS:
                    return (), True
    return _minimal_sourcewide_spans(matches), False


def _sourcewide_quantitative_total_candidates(
    lines: tuple[str, ...],
) -> tuple[tuple[tuple[int, int], Decimal], ...]:
    """Return only exact one-line objective-total declarations."""

    output: list[tuple[tuple[int, int], Decimal]] = []
    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
        match = _SOURCEWIDE_SIMPLE_QUANTITATIVE_TOTAL_RE.fullmatch(compact)
        if match is None:
            continue
        try:
            points = Decimal(match.group("points").replace(",", ""))
        except InvalidOperation:
            continue
        output.append(((index, index + 1), points))
    return tuple(output)


def _next_blank_or_section_boundaries(
    lines: tuple[str, ...],
) -> tuple[int, ...]:
    """Precompute the first blank/HWP section marker at or after each index."""

    next_boundary = len(lines)
    boundaries = [len(lines)] * (len(lines) + 1)
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index] or _HWP_SECTION_LINE_RE.fullmatch(lines[index]):
            next_boundary = index
        boundaries[index] = next_boundary
    return tuple(boundaries)


def _bounded_header_candidate_region(
    lines: tuple[str, ...],
    *,
    header_span: tuple[int, int],
    boundary_spans: Iterable[tuple[int, int]],
    table_fences: Iterable[tuple[int, int]],
    next_blank_or_section: tuple[int, ...],
) -> tuple[int, int] | None:
    """Fence a provisional header at the next structural or blank boundary."""

    start = header_span[0]
    boundary_starts = [
        span[0]
        for span in (*tuple(boundary_spans), *tuple(table_fences))
        if span[0] > start
    ]
    if header_span[1] < len(next_blank_or_section):
        boundary_starts.append(next_blank_or_section[header_span[1]])
    end = min(
        boundary_starts,
        default=min(len(lines), start + _MAX_CRITERION_HEADER_FALLBACK_LINES),
    )
    end = min(end, start + _MAX_CRITERION_HEADER_FALLBACK_LINES)
    return (start, end) if end > header_span[1] else None


def _unique_case_support_span(
    lines: tuple[str, ...],
    *,
    candidate: QuantitativeRuleCandidate,
    case: QuantitativeCaseLiteral,
    criterion_region: tuple[int, int],
) -> tuple[int, int] | None:
    """Resolve one exact condition-to-award row inside a provisional criterion."""

    matches: list[tuple[int, int]] = []
    evidence_spans = tuple(
        span
        for span in _anchor_line_spans(lines, case.evidence.quote)
        if _span_inside_region(span, criterion_region)
    )
    for literal_span in _anchor_line_spans(lines, case.literal):
        if not _span_inside_region(literal_span, criterion_region) or not any(
            _spans_overlap(literal_span, evidence_span)
            for evidence_span in evidence_spans
        ):
            continue
        if (
            _literal_contains_number(case.award_value, case.literal)
            and _case_comparison_matches(candidate, case, case.literal)
            and (
                case.operator != "IN"
                or all(
                    _case_literal_contains_exact_category(case.literal, value)
                    for value in case.category_values
                )
            )
        ):
            matches.append(literal_span)

    repair_anchor = evidence_spans[0] if len(evidence_spans) == 1 else None
    if repair_anchor is None:
        repair_anchor = _unique_percent_score_line_span(
            lines,
            case=case,
            criterion_region=criterion_region,
        )
    if repair_anchor is not None:
        window = _minimal_anchor_window(
            lines,
            anchor_span=repair_anchor,
            predicate=lambda value: bool(
                evidence_quote_matches_source(case.evidence.quote, value)
                and _case_row_window_matches(candidate, case, value)
            ),
        )
        if window is not None:
            window_spans = tuple(
                span
                for span in _anchor_line_spans(lines, window)
                if _span_inside_region(span, criterion_region)
            )
            if len(window_spans) == 1:
                matches.append(window_spans[0])

    unique_matches = tuple(dict.fromkeys(matches))
    return unique_matches[0] if len(unique_matches) == 1 else None


def _header_candidate_has_ordered_cases(
    lines: tuple[str, ...],
    *,
    candidate: QuantitativeRuleCandidate,
    criterion_region: tuple[int, int],
) -> bool:
    spans = tuple(
        _unique_case_support_span(
            lines,
            candidate=candidate,
            case=case,
            criterion_region=criterion_region,
        )
        for case in candidate.cases
    )
    return bool(
        spans
        and all(span is not None for span in spans)
        and spans[0] is not None
        and spans[0][0] > criterion_region[0]
        and all(
            left is not None
            and right is not None
            and left[1] <= right[0]
            for left, right in zip(spans, spans[1:], strict=False)
        )
    )


def _hwp_section_for_span(
    lines: tuple[str, ...],
    *,
    span: tuple[int, int],
    hwp_section_starts: tuple[int, ...],
) -> str | None:
    """Return the exact owning HWP section marker for a source span."""

    preceding = [index for index in hwp_section_starts if index < span[0]]
    if not preceding:
        return None
    marker = lines[preceding[-1]]
    return marker[1:-1] if _HWP_SECTION_LINE_RE.fullmatch(marker) else None


def _rebind_unique_sourcewide_case_table_headers(
    payload: ExtractionPayload,
    *,
    source: str,
    lines: tuple[str, ...],
    hwp_section_starts: tuple[int, ...],
    header_candidates: list[list[tuple[tuple[int, int], ...]]],
    sourcewide_boundary_spans: tuple[tuple[int, int], ...],
    next_blank_or_section: tuple[int, ...],
) -> tuple[ExtractionPayload, frozenset[tuple[int, int]]]:
    """Rebind one exact detailed header when a model anchored a subtotal.

    This is intentionally a narrow pre-pass for the three deterministic
    company metrics currently supported by production scoring.  Candidate
    numbers, metric, rows, and row values are never changed.  Zero or multiple
    structurally valid source headers leave the payload untouched so normal
    validation fails closed.
    """

    tables = payload.quantitative_tables
    all_header_spans = tuple(
        dict.fromkeys(
            span
            for candidates in header_candidates
            for spans in candidates
            for span in spans
        )
    )
    table_fences = [
        _all_anchor_line_spans(
            lines,
            (
                evidence.quote
                for evidence in (table.total_evidence, table.minimum_evidence)
                if evidence is not None
            ),
        )
        for table in tables
    ]
    criterion_evidence_spans = [
        [
            _all_anchor_line_spans(lines, (candidate.evidence.quote,))
            for candidate in table.criteria
        ]
        for table in tables
    ]
    candidate_non_criterion_spans = [
        [
            _all_anchor_line_spans(
                lines,
                (
                    candidate.criterion_literal,
                    *(case.evidence.quote for case in candidate.cases),
                    *(case.literal for case in candidate.cases),
                    *(
                        condition.evidence.quote
                        for condition in candidate.recognition_conditions
                    ),
                    *(
                        condition.literal
                        for condition in candidate.recognition_conditions
                    ),
                ),
            )
            for candidate in table.criteria
        ]
        for table in tables
    ]
    candidate_row_spans = [
        [
            _all_anchor_line_spans(
                lines,
                (
                    *(case.evidence.quote for case in candidate.cases),
                    *(case.literal for case in candidate.cases),
                    *(
                        condition.evidence.quote
                        for condition in candidate.recognition_conditions
                    ),
                    *(
                        condition.literal
                        for condition in candidate.recognition_conditions
                    ),
                ),
            )
            for candidate in table.criteria
        ]
        for table in tables
    ]

    selected: dict[tuple[int, int], tuple[int, int]] = {}
    for table_index, table in enumerate(tables):
        for candidate_index, candidate in enumerate(table.criteria):
            if (
                _literal_is_anchored(
                    candidate.criterion_literal,
                    candidate.evidence,
                    source,
                )
                and _literal_contains_number(
                    candidate.max_points,
                    candidate.criterion_literal,
                )
            ):
                continue

            foreign_spans: list[tuple[int, int]] = [
                span for spans in table_fences for span in spans
            ]
            for other_table_index, other_table in enumerate(tables):
                for other_candidate_index, other_candidate in enumerate(
                    other_table.criteria
                ):
                    if (
                        other_table_index == table_index
                        and other_candidate_index == candidate_index
                    ):
                        continue
                    other_non_criterion = candidate_non_criterion_spans[
                        other_table_index
                    ][other_candidate_index]
                    foreign_spans.extend(other_non_criterion)
                    shared_evidence = bool(
                        other_table_index == table_index
                        and _normalise_anchor_text(other_candidate.evidence.quote)
                        == _normalise_anchor_text(candidate.evidence.quote)
                    )
                    foreign_spans.extend(
                        span
                        for span in criterion_evidence_spans[other_table_index][
                            other_candidate_index
                        ]
                        if not shared_evidence
                        or any(
                            _spans_overlap(span, foreign_span)
                            for foreign_span in other_non_criterion
                        )
                    )

            foreign = tuple(dict.fromkeys(foreign_spans))
            blocked = tuple(
                dict.fromkeys(
                    (
                        *foreign,
                        *candidate_row_spans[table_index][candidate_index],
                    )
                )
            )
            valid_headers: list[tuple[int, int]] = []
            for header_span in header_candidates[table_index][candidate_index]:
                if any(
                    _spans_overlap(header_span, foreign_span)
                    for foreign_span in blocked
                ):
                    continue
                region = _bounded_header_candidate_region(
                    lines,
                    header_span=header_span,
                    boundary_spans=(
                        *all_header_spans,
                        *sourcewide_boundary_spans,
                        *foreign,
                    ),
                    table_fences=table_fences[table_index],
                    next_blank_or_section=next_blank_or_section,
                )
                if (
                    _hwp_section_for_span(
                        lines,
                        span=header_span,
                        hwp_section_starts=hwp_section_starts,
                    )
                    is not None
                    and region is not None
                    and _header_candidate_has_ordered_cases(
                        lines,
                        candidate=candidate,
                        criterion_region=region,
                    )
                ):
                    valid_headers.append(header_span)
            unique_headers = tuple(dict.fromkeys(valid_headers))
            if len(unique_headers) == 1:
                selected[(table_index, candidate_index)] = unique_headers[0]

    # One source slice may never become the header of two criteria.
    ambiguous_owners = {
        owner
        for owner, span in selected.items()
        if any(
            owner != other_owner and _spans_overlap(span, other_span)
            for other_owner, other_span in selected.items()
        )
    }
    if not selected or len(ambiguous_owners) == len(selected):
        return payload, frozenset()

    repaired_tables: list[QuantitativeTableCandidate] = []
    rebound_owners: set[tuple[int, int]] = set()
    for table_index, table in enumerate(tables):
        repaired_candidates: list[QuantitativeRuleCandidate] = []
        for candidate_index, candidate in enumerate(table.criteria):
            owner = (table_index, candidate_index)
            span = selected.get(owner)
            if span is None or owner in ambiguous_owners:
                repaired_candidates.append(candidate)
                continue
            source_slice = "\n".join(lines[span[0] : span[1]])
            section = _hwp_section_for_span(
                lines,
                span=span,
                hwp_section_starts=hwp_section_starts,
            )
            if section is None:
                repaired_candidates.append(candidate)
                continue
            repaired_candidate = candidate.model_copy(
                update={
                    "criterion_literal": source_slice,
                    "evidence": candidate.evidence.model_copy(
                        update={
                            "page": None,
                            "section": section,
                            "quote": source_slice,
                        }
                    ),
                }
            )
            repaired_candidates.append(repaired_candidate)
            if repaired_candidate != candidate:
                rebound_owners.add(owner)
        repaired_tables.append(
            table.model_copy(update={"criteria": repaired_candidates})
        )
    return (
        payload.model_copy(update={"quantitative_tables": repaired_tables}),
        frozenset(rebound_owners),
    )


def _unique_percent_score_line_span(
    lines: tuple[str, ...],
    *,
    case: QuantitativeCaseLiteral,
    criterion_region: tuple[int, int] | None,
) -> tuple[int, int] | None:
    """Resolve one exact percent award cell inside the owning criterion only."""

    if case.award_kind != "PERCENT_OF_MAX" or criterion_region is None:
        return None
    matches = [
        (index, index + 1)
        for index in range(criterion_region[0], criterion_region[1])
        if _score_cell_matches(
            lines[index],
            value=case.award_value,
            percent=True,
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _sourcewide_percent_award_spans(
    lines: tuple[str, ...],
    *,
    criterion_region: tuple[int, int] | None,
) -> tuple[tuple[int, int], ...]:
    """Return every exact percent award cell inside one owned criterion."""

    if criterion_region is None:
        return ()
    return tuple(
        (index, index + 1)
        for index in range(criterion_region[0], criterion_region[1])
        if re.fullmatch(
            rf"(?:배점(?:의)?)?{_NUM_PATTERN}(?:%|퍼센트)",
            re.sub(
                r"\s+",
                "",
                unicodedata.normalize("NFKC", lines[index]),
            ),
        )
    )


def _sourcewide_case_census_matches(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    criterion_region: tuple[int, int] | None,
) -> bool:
    """Prove that payload CASE rows exhaust the exact owned source rows."""

    if criterion_region is None or not candidate.cases:
        return False
    ordered_cases = sorted(candidate.cases, key=lambda item: item.row_order)

    if candidate.metric == "CREDIT_RATING":
        owned_region = _source_bound_credit_rating_case_region(
            candidate,
            lines=lines,
            criterion_region=criterion_region,
        )
        if owned_region is None:
            return False
        criterion_region = owned_region
        normalized_rows = tuple(
            tuple(
                re.sub(
                    r"\s+",
                    " ",
                    unicodedata.normalize("NFKC", value),
                ).strip()
                for value in case.category_values
            )
            for case in ordered_cases
        )
        if normalized_rows != _BUSAN_ENTERPRISE_CREDIT_CATEGORY_ROWS:
            return False
        claimed_awards = tuple(
            _unique_percent_score_line_span(
                lines,
                case=case,
                criterion_region=criterion_region,
            )
            for case in ordered_cases
        )
        claimed_rows: list[tuple[int, int]] = []
        for case, award_span in zip(ordered_cases, claimed_awards, strict=True):
            literal_span = _unique_anchor_line_span(lines, case.literal)
            evidence_span = _unique_anchor_line_span(lines, case.evidence.quote)
            if (
                award_span is None
                or literal_span is None
                or evidence_span is None
                or literal_span != evidence_span
                or literal_span[0] >= award_span[0]
                or literal_span[1] != award_span[1]
                or not _span_inside_region(literal_span, criterion_region)
                or not _span_inside_region(award_span, literal_span)
            ):
                return False
            claimed_rows.append(literal_span)
        source_award_cells = tuple(
            (index, index + 1)
            for index in range(criterion_region[0], criterion_region[1])
            if _is_score_cell(lines[index])
        )
        return bool(
            all(span is not None for span in claimed_awards)
            and tuple(span for span in claimed_awards if span is not None)
            == source_award_cells
            and source_award_cells
            == _sourcewide_percent_award_spans(
                lines, criterion_region=criterion_region
            )
            and all(
                left[1] <= right[0]
                for left, right in zip(claimed_rows, claimed_rows[1:], strict=False)
            )
        )

    if candidate.metric not in {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT"}:
        return False

    source_starts: list[tuple[int, str]] = []
    for index in range(criterion_region[0], criterion_region[1]):
        normalized = unicodedata.normalize("NFKC", lines[index]).strip()
        match = _SOURCEWIDE_LABELED_CASE_START_RE.fullmatch(normalized)
        if match is not None:
            source_starts.append((index, match.group("label")))
    if not source_starts or [label for _index, label in source_starts] != [
        chr(ord("A") + offset) for offset in range(len(source_starts))
    ]:
        return False

    source_rows: list[tuple[int, int]] = []
    for row_index, (start, _label) in enumerate(source_starts):
        next_start = (
            source_starts[row_index + 1][0]
            if row_index + 1 < len(source_starts)
            else criterion_region[1]
        )
        score_spans = [
            (index, index + 1)
            for index in range(start + 1, next_start)
            if _is_score_cell(lines[index])
        ]
        if len(score_spans) != 1:
            return False
        source_rows.append((start, score_spans[0][1]))

    claimed_rows: list[tuple[int, int]] = []
    for case in ordered_cases:
        literal_span = _unique_anchor_line_span(lines, case.literal)
        evidence_span = _unique_anchor_line_span(lines, case.evidence.quote)
        if (
            literal_span is None
            or evidence_span is None
            or not _spans_overlap(literal_span, evidence_span)
        ):
            return False
        claimed_rows.append(literal_span)
    return tuple(claimed_rows) == tuple(source_rows)


def _sourcewide_ambiguity_is_structural(reason: str | None) -> bool:
    """Allow only ambiguity text that the exact HWP rebind can resolve."""

    normalized = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", reason or ""),
    ).strip()
    return normalized in {
        "HWP 셀 구조상 행 연결 검토 필요",
        "요약 배점과 상세 배점 중 적용 표를 확인해야 함",
    }


def _recognition_key(literal: str, quote: str) -> tuple[str, str]:
    return (_normalise_anchor_text(literal).casefold(), _normalise_anchor_text(quote))


def _span_inside_region(
    span: tuple[int, int],
    region: tuple[int, int] | None,
) -> bool:
    return region is not None and region[0] <= span[0] and span[1] <= region[1]


def _is_explicit_hwp_footnote_span(
    lines: tuple[str, ...],
    span: tuple[int, int],
) -> bool:
    return bool(
        span[0] < len(lines)
        and _HWP_FOOTNOTE_LINE_RE.match(lines[span[0]].lstrip())
    )


def _rebind_candidate_table_cell_literals(
    candidate: QuantitativeRuleCandidate,
    *,
    source: str,
    lines: tuple[str, ...],
    foreign_structure_spans: tuple[tuple[int, int], ...] = (),
    foreign_shared_criterion_evidence_spans: tuple[tuple[int, int], ...] = (),
    foreign_recognition_spans: tuple[tuple[int, int], ...] = (),
    criterion_region: tuple[int, int] | None = None,
    criterion_anchor_span: tuple[int, int] | None = None,
    shared_recognition_keys: frozenset[tuple[str, str]] = frozenset(),
    external_recognition_keys: frozenset[tuple[str, str]] = frozenset(),
) -> QuantitativeRuleCandidate:
    """Repair exact HWP cell splits without changing structured rule values."""

    if criterion_region is None:
        return candidate

    candidate_span = criterion_anchor_span or _unique_anchor_line_span(
        lines,
        candidate.evidence.quote,
    )
    candidate_structure_spans = _all_anchor_line_spans(
        lines,
        (candidate.evidence.quote, candidate.criterion_literal),
    )
    case_anchor_spans = [
        _unique_anchor_line_span(lines, case.evidence.quote)
        for case in candidate.cases
    ]
    repair_anchor_spans = [
        span
        or _unique_percent_score_line_span(
            lines,
            case=case,
            criterion_region=criterion_region,
        )
        for case, span in zip(candidate.cases, case_anchor_spans, strict=True)
    ]
    recognition_anchor_spans = [
        _unique_anchor_line_span(lines, item.evidence.quote)
        for item in candidate.recognition_conditions
    ]
    case_anchor_spans_all = [
        _anchor_line_spans(lines, case.evidence.quote) for case in candidate.cases
    ]
    case_literal_spans_all = [
        _anchor_line_spans(lines, case.literal) for case in candidate.cases
    ]
    recognition_anchor_spans_all = [
        _anchor_line_spans(lines, item.evidence.quote)
        for item in candidate.recognition_conditions
    ]
    recognition_literal_spans_all = [
        _anchor_line_spans(lines, item.literal)
        for item in candidate.recognition_conditions
    ]
    case_anchors_are_ordered = bool(
        repair_anchor_spans
        and all(span is not None for span in repair_anchor_spans)
        and all(
            left is not None and right is not None and left[0] < right[0]
            for left, right in zip(
                repair_anchor_spans,
                repair_anchor_spans[1:],
                strict=False,
            )
        )
    )
    reserved_case_spans = [
        span
        for spans in (*case_anchor_spans_all, *case_literal_spans_all)
        for span in spans
    ]
    reserved_recognition_spans = [
        span
        for spans in (
            *recognition_anchor_spans_all,
            *recognition_literal_spans_all,
        )
        for span in spans
    ]
    criterion_literal = candidate.criterion_literal
    criterion_evidence = candidate.evidence
    if (
        candidate_span is not None
        and not (
            _literal_is_anchored(criterion_literal, criterion_evidence, source)
            and _literal_contains_number(candidate.max_points, criterion_literal)
        )
    ):
        window = _minimal_anchor_window(
            lines,
            anchor_span=candidate_span,
            maximum_lines=4,
            allow_before_anchor=False,
            predicate=lambda value: _criterion_window_matches(candidate, value),
        )
        if window is not None:
            span = _unique_anchor_line_span(lines, window)
            foreign_header_spans = tuple(
                reserved
                for reserved in foreign_structure_spans
                if not (
                    reserved in foreign_shared_criterion_evidence_spans
                    and _spans_overlap(reserved, candidate_span)
                )
            )
            if span is not None and not any(
                span[0] < reserved[1] and reserved[0] < span[1]
                for reserved in (
                    *reserved_case_spans,
                    *reserved_recognition_spans,
                    *foreign_header_spans,
                    *foreign_recognition_spans,
                )
            ) and _span_inside_region(span, criterion_region):
                criterion_literal = window
                criterion_evidence = candidate.evidence.model_copy(
                    update={"quote": window}
                )

    repaired_cases = list(candidate.cases)
    repaired_spans: list[tuple[int, int]] = []
    for index, case in enumerate(candidate.cases):
        if (
            _literal_is_anchored(case.literal, case.evidence, source)
            and _case_row_window_matches(candidate, case, case.literal)
        ):
            continue
        if not case_anchors_are_ordered:
            continue
        anchor_span = repair_anchor_spans[index]
        if anchor_span is None:
            continue
        window = _minimal_anchor_window(
            lines,
            anchor_span=anchor_span,
            predicate=lambda value, row=case: bool(
                evidence_quote_matches_source(row.evidence.quote, value)
                and _case_row_window_matches(candidate, row, value)
            ),
        )
        if window is None:
            continue
        span = _unique_anchor_line_span(lines, window)
        other_case_spans = [
            reserved
            for other_index, pair in enumerate(
                zip(case_anchor_spans_all, case_literal_spans_all, strict=True)
            )
            if other_index != index
            for spans in pair
            for reserved in spans
        ]
        non_case_spans = [
            span
            for span in (
                *candidate_structure_spans,
                *reserved_recognition_spans,
                *foreign_structure_spans,
                *foreign_recognition_spans,
            )
            if span is not None
        ]
        if span is None or not _span_inside_region(span, criterion_region) or any(
            span[0] < reserved[1] and reserved[0] < span[1]
            for reserved in (
                *other_case_spans,
                *non_case_spans,
                *repaired_spans,
            )
        ):
            continue
        repaired_spans.append(span)
        repaired_cases[index] = case.model_copy(
            update={
                "literal": window,
                "evidence": case.evidence.model_copy(update={"quote": window}),
            }
        )

    repaired_conditions = list(candidate.recognition_conditions)
    for index, condition in enumerate(candidate.recognition_conditions):
        if _literal_is_anchored(condition.literal, condition.evidence, source):
            continue
        anchor_span = recognition_anchor_spans[index]
        if anchor_span is None:
            continue
        window = _minimal_anchor_window(
            lines,
            anchor_span=anchor_span,
            predicate=lambda value, row=condition: bool(
                evidence_quote_matches_source(row.literal, value)
                and evidence_quote_matches_source(row.evidence.quote, value)
            ),
        )
        span = _unique_anchor_line_span(lines, window) if window is not None else None
        other_recognition_spans = [
            reserved
            for other_index, pair in enumerate(
                zip(
                    recognition_anchor_spans_all,
                    recognition_literal_spans_all,
                    strict=True,
                )
            )
            if other_index != index
            for spans in pair
            for reserved in spans
        ]
        blocked_spans = [
            reserved
            for reserved in (
                *candidate_structure_spans,
                *reserved_case_spans,
                *other_recognition_spans,
                *foreign_structure_spans,
                *foreign_recognition_spans,
            )
            if reserved is not None
        ]
        condition_key = _recognition_key(condition.literal, condition.evidence.quote)
        if (
            span is not None
            and (
                condition_key in shared_recognition_keys
                or condition_key in external_recognition_keys
                or _span_inside_region(span, criterion_region)
            )
            and not any(
                span[0] < reserved[1] and reserved[0] < span[1]
                for reserved in blocked_spans
            )
        ):
            repaired_conditions[index] = condition.model_copy(
                update={
                    "evidence": condition.evidence.model_copy(
                        update={"quote": window}
                    )
                }
            )

    return candidate.model_copy(
        update={
            "criterion_literal": criterion_literal,
            "evidence": criterion_evidence,
            "cases": repaired_cases,
            "recognition_conditions": repaired_conditions,
        }
    )


def _sourcewide_ambiguity_resolution_blocker(
    payload: ExtractionPayload,
    *,
    table_index: int,
    table: QuantitativeTableCandidate,
    criteria: list[QuantitativeRuleCandidate],
    lines: tuple[str, ...],
    rebound_owners: frozenset[tuple[int, int]],
    criterion_anchors: list[tuple[int, int] | None],
    criterion_regions: list[tuple[int, int] | None],
    table_region: tuple[int, int] | None,
    sourcewide_boundary_spans: tuple[tuple[int, int], ...],
    hwp_section_starts: tuple[int, ...],
    next_blank_or_section: tuple[int, ...],
    boundary_overflow: bool,
    ambiguous_case_claim: bool,
    ambiguous_recognition_claim: bool,
) -> SourcewideAmbiguityResolutionBlocker | None:
    """Return a safe reason code when exact sourcewide resolution is blocked.

    An extractor-provided ambiguity is decision-bearing and normally remains
    fail-closed.  The sole exception is a one-table HWP payload where every
    criterion was actually rebound from a unique source-wide header and the
    complete table geometry and total independently agree.  This deliberately
    excludes already-valid model headers, partial repairs, multi-table payloads,
    declared source gaps, and any later structural ambiguity.

    The returned value is an enum-only diagnostic code.  It contains no source
    text, model prose, attachment/table identifiers, or bidder facts, so the
    existing PIN-only diagnostics endpoint can safely aggregate it from profile
    issues. ``None`` means either that no ambiguity existed or that every exact
    proof gate succeeded.
    """

    if not table.ambiguity_reason:
        return None
    if not _sourcewide_ambiguity_is_structural(table.ambiguity_reason):
        return "SOURCEWIDE_AMBIGUITY_REASON_NOT_STRUCTURAL"
    if (
        len(payload.quantitative_tables) != 1
        or table_index != 0
        or not table.criteria
        or payload.quantitative_table_not_applicable is not None
    ):
        return "SOURCEWIDE_AMBIGUITY_SCOPE_UNSUPPORTED"
    if payload.missing_or_unreadable:
        return "SOURCEWIDE_AMBIGUITY_SOURCE_GAPS_PRESENT"
    if boundary_overflow:
        return "SOURCEWIDE_AMBIGUITY_BOUNDARY_SCAN_LIMIT"
    if ambiguous_case_claim or ambiguous_recognition_claim:
        return "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION"

    expected_owners = frozenset(
        (table_index, candidate_index)
        for candidate_index in range(len(table.criteria))
    )
    if rebound_owners != expected_owners:
        return "SOURCEWIDE_AMBIGUITY_REBIND_INCOMPLETE"

    signature = tuple(
        (candidate.metric, _decimal(candidate.max_points))
        for candidate in criteria
    )
    if signature != _BUSAN_SOURCEWIDE_AMBIGUITY_SIGNATURE:
        return "SOURCEWIDE_AMBIGUITY_SIGNATURE_UNSUPPORTED"

    if (
        table_region is None
        or len(criterion_anchors) != len(table.criteria)
        or len(criterion_regions) != len(table.criteria)
        or any(anchor is None for anchor in criterion_anchors)
        or any(region is None for region in criterion_regions)
    ):
        return "SOURCEWIDE_AMBIGUITY_GEOMETRY_UNPROVEN"
    resolved_anchors = [anchor for anchor in criterion_anchors if anchor is not None]
    resolved_regions = [region for region in criterion_regions if region is not None]
    if not all(
        region[0] < region[1]
        and region[0] == anchor[0]
        and _span_inside_region(anchor, region)
        and _span_inside_region(region, table_region)
        for anchor, region in zip(resolved_anchors, resolved_regions, strict=True)
    ):
        return "SOURCEWIDE_AMBIGUITY_GEOMETRY_UNPROVEN"
    if not all(
        left_anchor[0] < right_anchor[0] and left_region[1] <= right_region[0]
        for left_anchor, right_anchor, left_region, right_region in zip(
            resolved_anchors,
            resolved_anchors[1:],
            resolved_regions,
            resolved_regions[1:],
            strict=False,
        )
    ):
        return "SOURCEWIDE_AMBIGUITY_GEOMETRY_UNPROVEN"
    if not all(
        _sourcewide_case_census_matches(
            candidate,
            lines=lines,
            criterion_region=region,
        )
        for candidate, region in zip(criteria, resolved_regions, strict=True)
    ):
        return "SOURCEWIDE_AMBIGUITY_CASE_CENSUS_MISMATCH"

    total = _decimal(table.total_points)
    if total is None or table.total_evidence is None:
        return "SOURCEWIDE_AMBIGUITY_TOTAL_EVIDENCE_UNPROVEN"
    total_span = _unique_anchor_line_span(lines, table.total_evidence.quote)
    if (
        total_span is None
        or not _literal_contains_number(total, table.total_evidence.quote)
    ):
        return "SOURCEWIDE_AMBIGUITY_TOTAL_EVIDENCE_UNPROVEN"
    owned_claim_spans: list[tuple[int, int]] = list(resolved_anchors)
    for candidate, region in zip(criteria, resolved_regions, strict=True):
        for case in candidate.cases:
            literal_span = _unique_anchor_line_span(lines, case.literal)
            evidence_span = _unique_anchor_line_span(lines, case.evidence.quote)
            if (
                literal_span is None
                or evidence_span is None
                or not _spans_overlap(literal_span, evidence_span)
                or not _span_inside_region(literal_span, region)
                or not _span_inside_region(evidence_span, region)
            ):
                return "SOURCEWIDE_AMBIGUITY_CASE_EVIDENCE_UNPROVEN"
            owned_claim_spans.extend((literal_span, evidence_span))
    last_claim_end = max(span[1] for span in owned_claim_spans)
    owning_sections = [index for index in hwp_section_starts if index < table_region[0]]
    if not owning_sections:
        return "SOURCEWIDE_AMBIGUITY_SECTION_UNPROVEN"
    owning_section = owning_sections[-1]

    def section_for(span: tuple[int, int]) -> int | None:
        starts = [index for index in hwp_section_starts if index < span[0]]
        return starts[-1] if starts else None

    table_marker_spans = tuple(
        (index, index + 1)
        for index, line in enumerate(lines)
        if _SOURCEWIDE_TABLE_MARKER_RE.fullmatch(
            unicodedata.normalize("NFKC", line).strip()
        )
    )
    owner_markers = [
        span
        for span in table_marker_spans
        if section_for(span) == owning_section and span[0] < table_region[0]
    ]
    if len(owner_markers) > 1 or any(
        section_for(span) != owning_section or span[0] >= table_region[0]
        for span in table_marker_spans
        if span not in owner_markers
    ):
        return "SOURCEWIDE_AMBIGUITY_COMPETING_STRUCTURE"

    simple_totals = _sourcewide_quantitative_total_candidates(lines)
    if any(not _spans_overlap(span, total_span) for span, _points in simple_totals):
        return "SOURCEWIDE_AMBIGUITY_COMPETING_STRUCTURE"

    trailing_total = bool(
        _span_inside_region(total_span, table_region)
        and total_span[0] >= last_claim_end
        and len(simple_totals) == 1
        and _spans_overlap(simple_totals[0][0], total_span)
        and simple_totals[0][1] == total
        and (
            last_claim_end >= len(next_blank_or_section)
            or next_blank_or_section[last_claim_end] >= total_span[0]
        )
        and not any(
            last_claim_end <= span[0] < total_span[0]
            and not _spans_overlap(span, total_span)
            for span in sourcewide_boundary_spans
        )
    )

    summary_candidates: list[tuple[tuple[int, int], Decimal]] = []
    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
        match = _SOURCEWIDE_QUANTITATIVE_SUMMARY_TOTAL_RE.fullmatch(compact)
        if match is None:
            continue
        try:
            points = Decimal(match.group("points").replace(",", ""))
        except InvalidOperation:
            continue
        summary_candidates.append(((index, index + 1), points))
    detail_markers = [
        (index, index + 1)
        for index, line in enumerate(lines)
        if _BUSAN_QUANTITATIVE_DETAIL_MARKER_RE.fullmatch(
            unicodedata.normalize("NFKC", line)
        )
    ]
    summary_total = bool(
        len(summary_candidates) == 1
        and summary_candidates[0][0] == total_span
        and summary_candidates[0][1] == total
        and section_for(total_span) == owning_section
        and len(detail_markers) == 1
        and section_for(detail_markers[0]) == owning_section
        and total_span[1] <= detail_markers[0][0]
        and detail_markers[0][1] == resolved_anchors[0][0]
        and not simple_totals
    )
    if not (trailing_total or summary_total):
        return "SOURCEWIDE_AMBIGUITY_TOTAL_PROVENANCE_UNPROVEN"

    criterion_maxima = [_decimal(candidate.max_points) for candidate in criteria]
    if not all(value is not None for value in criterion_maxima) or sum(
        (value for value in criterion_maxima if value is not None),
        Decimal("0"),
    ) != total:
        return "SOURCEWIDE_AMBIGUITY_TOTAL_MISMATCH"
    return None


def _rebind_split_table_cell_literals(
    payload: ExtractionPayload,
    *,
    source: str,
) -> tuple[
    ExtractionPayload,
    tuple[SourcewideAmbiguityResolutionBlocker | None, ...],
]:
    """Repair exact HWP cell splits and return table-aligned safe blockers."""

    lines = _source_lines(source)
    if not payload.quantitative_tables:
        return payload, ()
    if not lines or not any(
        _HWP_SECTION_LINE_RE.fullmatch(line) for line in lines
    ):
        return payload, tuple(
            (
                "SOURCEWIDE_AMBIGUITY_SCOPE_UNSUPPORTED"
                if table.ambiguity_reason
                else None
            )
            for table in payload.quantitative_tables
        )

    hwp_section_starts = tuple(
        index
        for index, line in enumerate(lines)
        if _HWP_SECTION_LINE_RE.fullmatch(line)
    )
    next_blank_or_section = _next_blank_or_section_boundaries(lines)
    sourcewide_boundary_spans, boundary_overflow = (
        _sourcewide_structural_boundary_spans(lines)
    )
    header_cache: dict[
        tuple[str, Decimal], tuple[tuple[int, int], ...]
    ] = {}
    sourcewide_header_candidates: list[
        list[tuple[tuple[int, int], ...]]
    ] = []
    for table in payload.quantitative_tables:
        candidates_for_table: list[tuple[tuple[int, int], ...]] = []
        for candidate in table.criteria:
            expected = _decimal(candidate.max_points)
            valid_shape = bool(
                candidate.scoring_method == "CASE_TABLE"
                and candidate.cases
                and candidate.metric in _METRIC_HEADER_TOKEN_GROUPS
                and expected is not None
                and [case.row_order for case in candidate.cases]
                == list(range(1, len(candidate.cases) + 1))
            )
            if boundary_overflow or not valid_shape or expected is None:
                candidates_for_table.append(())
                continue
            cache_key = (candidate.metric, expected)
            spans = header_cache.get(cache_key)
            if spans is None:
                spans = _candidate_metric_max_header_spans(lines, candidate)
                header_cache[cache_key] = spans
            candidates_for_table.append(spans)
        sourcewide_header_candidates.append(candidates_for_table)
    payload, sourcewide_header_rebound_owners = (
        _rebind_unique_sourcewide_case_table_headers(
            payload,
            source=source,
            lines=lines,
            hwp_section_starts=hwp_section_starts,
            header_candidates=sourcewide_header_candidates,
            sourcewide_boundary_spans=sourcewide_boundary_spans,
            next_blank_or_section=next_blank_or_section,
        )
    )
    sourcewide_header_rebound_owners = set(sourcewide_header_rebound_owners)

    table_fences: list[tuple[tuple[int, int], ...]] = []
    candidate_structure_spans: list[list[tuple[tuple[int, int], ...]]] = []
    table_structure_spans: list[tuple[tuple[int, int], ...]] = []
    candidate_recognition_entries: list[
        list[tuple[tuple[tuple[str, str], tuple[tuple[int, int], ...]], ...]]
    ] = []
    criterion_anchor_spans: list[list[tuple[int, int] | None]] = []
    criterion_header_candidates: list[list[tuple[tuple[int, int], ...]]] = []

    for table_index, table in enumerate(payload.quantitative_tables):
        fence_values = [
            evidence.quote
            for evidence in (table.total_evidence, table.minimum_evidence)
            if evidence is not None
        ]
        fences = _all_anchor_line_spans(lines, fence_values)
        table_fences.append(fences)

        structures_for_table: list[tuple[tuple[int, int], ...]] = []
        recognition_for_table: list[
            tuple[tuple[tuple[str, str], tuple[tuple[int, int], ...]], ...]
        ] = []
        anchors_for_table: list[tuple[int, int] | None] = []
        header_candidates_for_table = list(
            sourcewide_header_candidates[table_index]
        )
        for candidate in table.criteria:
            structure_values = (
                candidate.evidence.quote,
                candidate.criterion_literal,
                *(item.evidence.quote for item in candidate.cases),
                *(item.literal for item in candidate.cases),
            )
            structures_for_table.append(
                _all_anchor_line_spans(lines, structure_values)
            )
            recognition_for_table.append(
                tuple(
                    (
                        _recognition_key(item.literal, item.evidence.quote),
                        _all_anchor_line_spans(
                            lines,
                            (item.evidence.quote, item.literal),
                        ),
                    )
                    for item in candidate.recognition_conditions
                )
            )
            anchors_for_table.append(
                _unique_anchor_line_span(lines, candidate.evidence.quote)
            )
        candidate_structure_spans.append(structures_for_table)
        candidate_recognition_entries.append(recognition_for_table)
        criterion_anchor_spans.append(anchors_for_table)
        criterion_header_candidates.append(header_candidates_for_table)
        table_structure_spans.append(
            tuple(
                dict.fromkeys(
                    (
                        *fences,
                        *(
                            span
                            for spans in structures_for_table
                            for span in spans
                        ),
                    )
                )
            )
        )

    for table_index, table in enumerate(payload.quantitative_tables):
        primary_anchors = criterion_anchor_spans[table_index]
        all_header_spans = tuple(
            dict.fromkeys(
                (
                    *(span for span in primary_anchors if span is not None),
                    *(
                        span
                        for spans in criterion_header_candidates[table_index]
                        for span in spans
                    ),
                )
            )
        )
        resolved: list[tuple[int, int] | None] = []
        for candidate_index, candidate in enumerate(table.criteria):
            primary = primary_anchors[candidate_index]
            if primary is not None:
                resolved.append(primary)
                continue
            valid_headers: list[tuple[int, int]] = []
            for header_span in criterion_header_candidates[table_index][
                candidate_index
            ]:
                region = _bounded_header_candidate_region(
                    lines,
                    header_span=header_span,
                    boundary_spans=(
                        *all_header_spans,
                        *sourcewide_boundary_spans,
                    ),
                    table_fences=table_fences[table_index],
                    next_blank_or_section=next_blank_or_section,
                )
                if region is not None and _header_candidate_has_ordered_cases(
                    lines,
                    candidate=candidate,
                    criterion_region=region,
                ):
                    valid_headers.append(header_span)
            resolved.append(valid_headers[0] if len(valid_headers) == 1 else None)
        criterion_anchor_spans[table_index] = resolved

    criterion_regions: list[list[tuple[int, int] | None]] = []
    for table_index, table in enumerate(payload.quantitative_tables):
        anchors = criterion_anchor_spans[table_index]
        ordered = bool(
            anchors
            and all(span is not None for span in anchors)
            and all(
                left is not None and right is not None and left[0] < right[0]
                for left, right in zip(anchors, anchors[1:], strict=False)
            )
        )
        if not ordered:
            criterion_regions.append([None for _candidate in table.criteria])
            continue

        resolved_anchors = [span for span in anchors if span is not None]
        regions_for_table: list[tuple[int, int] | None] = []
        for candidate_index, anchor_span in enumerate(resolved_anchors):
            start = anchor_span[0]
            boundary_starts = [
                span[0]
                for span in table_fences[table_index]
                if span[0] > start
            ]
            boundary_starts.extend(
                marker
                for marker in hwp_section_starts
                if marker > start
            )
            if candidate_index + 1 < len(resolved_anchors):
                boundary_starts.append(resolved_anchors[candidate_index + 1][0])
            boundary_starts.extend(
                span[0]
                for other_table_index, other_anchors in enumerate(
                    criterion_anchor_spans
                )
                if other_table_index != table_index
                for span in other_anchors
                if span is not None and span[0] > start
            )
            end = min(boundary_starts, default=len(lines))
            regions_for_table.append((start, end) if end > start else None)
        criterion_regions.append(regions_for_table)

    table_regions: list[tuple[int, int] | None] = []
    table_core_regions: list[tuple[int, int] | None] = []
    for table_index, regions in enumerate(criterion_regions):
        if not regions or any(region is None for region in regions):
            table_regions.append(None)
            table_core_regions.append(None)
            continue
        resolved_regions = [region for region in regions if region is not None]
        start = resolved_regions[0][0]
        boundary_starts = [
            marker for marker in hwp_section_starts if marker > start
        ]
        boundary_starts.extend(
            span[0]
            for other_table_index, other_anchors in enumerate(
                criterion_anchor_spans
            )
            if other_table_index != table_index
            for span in other_anchors
            if span is not None and span[0] > start
        )
        end = min(boundary_starts, default=len(lines))
        table_region = (start, end) if end > start else None
        table_regions.append(table_region)
        last_criterion_start = resolved_regions[-1][0]
        core_end = min(
            (
                span[0]
                for span in table_fences[table_index]
                if span[0] > last_criterion_start
            ),
            default=end,
        )
        table_core_regions.append(
            (start, core_end) if core_end > start else None
        )

    all_structure_spans = tuple(
        dict.fromkeys(
            span for spans in table_structure_spans for span in spans
        )
    )
    proven_repaired_structure_span_list = [
        span
        for candidates_for_table in sourcewide_header_candidates
        for spans in candidates_for_table
        for span in spans
    ]
    for table_index, table in enumerate(payload.quantitative_tables):
        for candidate_index, candidate in enumerate(table.criteria):
            criterion_region = criterion_regions[table_index][candidate_index]
            if criterion_region is None:
                continue
            for case in candidate.cases:
                case_span = _unique_case_support_span(
                    lines,
                    candidate=candidate,
                    case=case,
                    criterion_region=criterion_region,
                )
                if case_span is not None:
                    proven_repaired_structure_span_list.append(case_span)
    proven_repaired_structure_spans = tuple(
        dict.fromkeys(proven_repaired_structure_span_list)
    )
    globally_protected_structure_spans = tuple(
        dict.fromkeys(
            (
                *all_structure_spans,
                *sourcewide_boundary_spans,
                *proven_repaired_structure_spans,
            )
        )
    )
    repaired_tables: list[QuantitativeTableCandidate] = []
    ambiguity_resolution_blockers: list[
        SourcewideAmbiguityResolutionBlocker | None
    ] = []
    for table_index, table in enumerate(payload.quantitative_tables):
        recognition_owners: dict[tuple[str, str], set[int]] = {}
        for candidate_index, entries in enumerate(
            candidate_recognition_entries[table_index]
        ):
            for key, _spans in entries:
                recognition_owners.setdefault(key, set()).add(candidate_index)
        shared_keys = frozenset(
            key for key, owners in recognition_owners.items() if len(owners) > 1
        )

        repaired_candidates: list[QuantitativeRuleCandidate] = []
        for candidate_index, candidate in enumerate(table.criteria):
            candidate_keys = {
                key
                for key, _spans in candidate_recognition_entries[table_index][
                    candidate_index
                ]
            }
            shareable_candidate_keys = {
                _recognition_key(item.literal, item.evidence.quote)
                for item in candidate.recognition_conditions
                if (
                    (literal_span := _unique_anchor_line_span(lines, item.literal))
                    is not None
                )
                if (
                    (evidence_span := _unique_anchor_line_span(
                        lines, item.evidence.quote
                    ))
                    is not None
                )
                if _spans_overlap(literal_span, evidence_span)
                and _span_inside_region(
                    literal_span,
                    table_core_regions[table_index],
                )
                and _span_inside_region(
                    evidence_span,
                    table_core_regions[table_index],
                )
            }
            shared_candidate_keys = frozenset(
                candidate_keys & shared_keys & shareable_candidate_keys
            )
            external_candidate_keys = frozenset(
                _recognition_key(item.literal, item.evidence.quote)
                for item in candidate.recognition_conditions
                if (
                    (literal_span := _unique_anchor_line_span(lines, item.literal))
                    is not None
                )
                if (
                    (evidence_span := _unique_anchor_line_span(
                        lines, item.evidence.quote
                    ))
                    is not None
                )
                if _spans_overlap(literal_span, evidence_span)
                and _span_inside_region(literal_span, table_regions[table_index])
                and _span_inside_region(evidence_span, table_regions[table_index])
                and _is_explicit_hwp_footnote_span(lines, literal_span)
                and not any(
                    _spans_overlap(literal_span, structure_span)
                    or _spans_overlap(evidence_span, structure_span)
                    for structure_span in all_structure_spans
                )
            )
            foreign_structure = tuple(
                dict.fromkeys(
                    (
                        *table_fences[table_index],
                        *(
                            span
                            for other_candidate_index, spans in enumerate(
                                candidate_structure_spans[table_index]
                            )
                            if other_candidate_index != candidate_index
                            for span in spans
                        ),
                        *(
                            span
                            for other_table_index, spans in enumerate(
                                table_structure_spans
                            )
                            if other_table_index != table_index
                            for span in spans
                        ),
                    )
                )
            )
            shared_criterion_evidence_spans = tuple(
                dict.fromkeys(
                    span
                    for other_candidate_index, other_candidate in enumerate(
                        table.criteria
                    )
                    if other_candidate_index != candidate_index
                    if _normalise_anchor_text(other_candidate.evidence.quote)
                    == _normalise_anchor_text(candidate.evidence.quote)
                    for span in _anchor_line_spans(
                        lines,
                        other_candidate.evidence.quote,
                    )
                )
            )
            foreign_non_criterion_evidence_spans = tuple(
                dict.fromkeys(
                    span
                    for other_candidate_index, other_candidate in enumerate(
                        table.criteria
                    )
                    if other_candidate_index != candidate_index
                    for span in _all_anchor_line_spans(
                        lines,
                        (
                            other_candidate.criterion_literal,
                            *(
                                case.evidence.quote
                                for case in other_candidate.cases
                            ),
                            *(case.literal for case in other_candidate.cases),
                        ),
                    )
                )
            )
            safe_shared_criterion_evidence_spans = tuple(
                span
                for span in shared_criterion_evidence_spans
                if not any(
                    _spans_overlap(span, foreign_span)
                    for foreign_span in foreign_non_criterion_evidence_spans
                )
            )
            foreign_recognition = tuple(
                dict.fromkeys(
                    (
                        *(
                            span
                            for other_candidate_index, entries in enumerate(
                                candidate_recognition_entries[table_index]
                            )
                            if other_candidate_index != candidate_index
                            for key, spans in entries
                            if key
                            not in (
                                shared_candidate_keys | external_candidate_keys
                            )
                            for span in spans
                        ),
                        *(
                            span
                            for other_table_index, candidates in enumerate(
                                candidate_recognition_entries
                            )
                            if other_table_index != table_index
                            for entries in candidates
                            for _key, spans in entries
                            for span in spans
                        ),
                    )
                )
            )
            repaired_candidate = _rebind_candidate_table_cell_literals(
                candidate,
                source=source,
                lines=lines,
                foreign_structure_spans=foreign_structure,
                foreign_shared_criterion_evidence_spans=(
                    safe_shared_criterion_evidence_spans
                ),
                foreign_recognition_spans=foreign_recognition,
                criterion_region=criterion_regions[table_index][candidate_index],
                criterion_anchor_span=criterion_anchor_spans[table_index][
                    candidate_index
                ],
                shared_recognition_keys=shared_candidate_keys,
                external_recognition_keys=external_candidate_keys,
            )
            repaired_candidates.append(repaired_candidate)
            repaired_header_span = _unique_anchor_line_span(
                lines,
                repaired_candidate.criterion_literal,
            )
            criterion_region = criterion_regions[table_index][candidate_index]
            regional_metric_headers = tuple(
                span
                for span in sourcewide_header_candidates[table_index][
                    candidate_index
                ]
                if _span_inside_region(span, criterion_region)
            )
            if (
                (
                    repaired_candidate.criterion_literal
                    != candidate.criterion_literal
                    or repaired_candidate.evidence.quote
                    != candidate.evidence.quote
                )
                and repaired_header_span is not None
                and regional_metric_headers == (repaired_header_span,)
            ):
                # A partial HWP header can be proven only after criterion
                # regions are established.  Count that exact region-bounded
                # repair toward the sourcewide proof only when it is also the
                # sole supported metric+maximum header in that region.
                # Unchanged model headers remain deliberately excluded.
                sourcewide_header_rebound_owners.add(
                    (table_index, candidate_index)
                )

        repaired_current_structure_spans = _all_anchor_line_spans(
            lines,
            (
                value
                for candidate in repaired_candidates
                for value in (
                    candidate.evidence.quote,
                    candidate.criterion_literal,
                    *(case.evidence.quote for case in candidate.cases),
                    *(case.literal for case in candidate.cases),
                )
            ),
        )
        repaired_candidates = _augment_sourcewide_hwp_activation_context(
            repaired_candidates,
            lines=lines,
            criterion_regions=criterion_regions[table_index],
            boundary_spans=sourcewide_boundary_spans,
            protected_structure_spans=(
                *globally_protected_structure_spans,
                *repaired_current_structure_spans,
            ),
        )

        ambiguous_case_claim = False
        case_claims: list[
            tuple[tuple[int, int], tuple[int, int]]
        ] = []
        for candidate_index, candidate in enumerate(repaired_candidates):
            region = criterion_regions[table_index][candidate_index]
            for case_index, case in enumerate(candidate.cases):
                literal_spans = _anchor_line_spans(lines, case.literal)
                evidence_spans = _anchor_line_spans(lines, case.evidence.quote)
                if len(literal_spans) != 1 or len(evidence_spans) != 1:
                    ambiguous_case_claim = True
                if (
                    region is not None
                    and len(literal_spans) == 1
                    and _case_row_window_matches(candidate, case, case.literal)
                    and not _span_inside_region(literal_spans[0], region)
                ):
                    ambiguous_case_claim = True
                owner = (candidate_index, case_index)
                for span in dict.fromkeys((*literal_spans, *evidence_spans)):
                    case_claims.append((owner, span))

        for left_index, (left_owner, left_span) in enumerate(case_claims):
            if any(
                left_owner != right_owner and _spans_overlap(left_span, right_span)
                for right_owner, right_span in case_claims[left_index + 1 :]
            ):
                ambiguous_case_claim = True
                break

        repaired_recognition_owners: dict[tuple[str, str], set[int]] = {}
        for candidate_index, candidate in enumerate(repaired_candidates):
            for condition in candidate.recognition_conditions:
                repaired_recognition_owners.setdefault(
                    _recognition_key(
                        condition.literal,
                        condition.evidence.quote,
                    ),
                    set(),
                ).add(candidate_index)
        repaired_shared_keys = frozenset(
            key
            for key, owners in repaired_recognition_owners.items()
            if len(owners) > 1
        )
        ambiguous_recognition_claim = False
        recognition_claims: list[
            tuple[tuple[int, int], tuple[str, str], tuple[int, int]]
        ] = []
        for candidate_index, candidate in enumerate(repaired_candidates):
            criterion_region = criterion_regions[table_index][candidate_index]
            table_region = table_regions[table_index]
            for condition_index, condition in enumerate(
                candidate.recognition_conditions
            ):
                key = _recognition_key(
                    condition.literal,
                    condition.evidence.quote,
                )
                literal_spans = _anchor_line_spans(lines, condition.literal)
                evidence_spans = _anchor_line_spans(
                    lines,
                    condition.evidence.quote,
                )
                if len(literal_spans) != 1 or len(evidence_spans) != 1:
                    ambiguous_recognition_claim = True
                owner = (candidate_index, condition_index)
                claim_spans = tuple(
                    dict.fromkeys((*literal_spans, *evidence_spans))
                )
                for span in claim_spans:
                    recognition_claims.append((owner, key, span))
                if not literal_spans or not evidence_spans:
                    continue
                if not any(
                    _spans_overlap(literal_span, evidence_span)
                    for literal_span in literal_spans
                    for evidence_span in evidence_spans
                ):
                    ambiguous_recognition_claim = True
                if any(
                    _spans_overlap(span, structure_span)
                    for span in claim_spans
                    for structure_span in all_structure_spans
                ):
                    ambiguous_recognition_claim = True
                inside_criterion = all(
                    _span_inside_region(span, criterion_region)
                    for span in claim_spans
                )
                explicit_table_footnote = bool(
                    table_region is not None
                    and all(
                        _span_inside_region(span, table_region)
                        for span in claim_spans
                    )
                    and any(
                        _is_explicit_hwp_footnote_span(lines, span)
                        for span in claim_spans
                    )
                )
                shared_in_table = bool(
                    key in repaired_shared_keys
                    and table_core_regions[table_index] is not None
                    and all(
                        _span_inside_region(
                            span,
                            table_core_regions[table_index],
                        )
                        for span in claim_spans
                    )
                )
                if not (inside_criterion or explicit_table_footnote or shared_in_table):
                    ambiguous_recognition_claim = True

        for left_index, (left_owner, left_key, left_span) in enumerate(
            recognition_claims
        ):
            if any(
                left_owner != right_owner
                and left_key != right_key
                and _spans_overlap(left_span, right_span)
                for right_owner, right_key, right_span in recognition_claims[
                    left_index + 1 :
                ]
            ):
                ambiguous_recognition_claim = True
                break

        ambiguity_reason = table.ambiguity_reason
        ambiguity_resolution_blocker = _sourcewide_ambiguity_resolution_blocker(
            payload,
            table_index=table_index,
            table=table,
            criteria=repaired_candidates,
            lines=lines,
            rebound_owners=frozenset(sourcewide_header_rebound_owners),
            criterion_anchors=criterion_anchor_spans[table_index],
            criterion_regions=criterion_regions[table_index],
            table_region=table_regions[table_index],
            sourcewide_boundary_spans=sourcewide_boundary_spans,
            hwp_section_starts=hwp_section_starts,
            next_blank_or_section=next_blank_or_section,
            boundary_overflow=boundary_overflow,
            ambiguous_case_claim=ambiguous_case_claim,
            ambiguous_recognition_claim=ambiguous_recognition_claim,
        )
        if table.ambiguity_reason and ambiguity_resolution_blocker is None:
            ambiguity_reason = None
        if ambiguous_case_claim:
            ambiguity_reason = (
                ambiguity_reason
                or "정량평가 행 근거가 복수 평가항목에 중복 연결되었습니다."
            )
        if ambiguous_recognition_claim:
            ambiguity_reason = (
                ambiguity_reason
                or "정량평가 인식조건 근거가 평가항목 구조와 모호하게 연결되었습니다."
            )
        if (
            ambiguity_resolution_blocker is None
            and (ambiguous_case_claim or ambiguous_recognition_claim)
        ):
            ambiguity_resolution_blocker = (
                "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION"
            )
        ambiguity_resolution_blockers.append(
            ambiguity_resolution_blocker if ambiguity_reason else None
        )
        repaired_tables.append(
            table.model_copy(
                update={
                    "criteria": repaired_candidates,
                    "ambiguity_reason": ambiguity_reason,
                }
            )
        )
    return (
        payload.model_copy(update={"quantitative_tables": repaired_tables}),
        tuple(ambiguity_resolution_blockers),
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
            if not _literal_contains_number(
                case.award_value,
                case.literal,
            ) or not _case_comparison_matches(candidate, case, case.literal):
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
        if not _literal_contains_number(
            case.award_value,
            case.literal,
        ) or not _case_comparison_matches(candidate, case, case.literal):
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
        if (
            case.operator == "GTE"
            and case.comparison_value is not None
            and candidate.metric == "PERFORMANCE_AMOUNT"
        ):
            if not _case_comparison_matches(candidate, case, case.literal):
                issues.append(
                    _issue(
                        "CASE_COMPARATOR_MISMATCH",
                        "INCOMPLETE",
                        "CASE_TABLE 원문의 비교 연산자와 구조화한 조건이 일치하지 않습니다.",
                        **context,
                    )
                )
        elif case.operator == "GTE" and case.comparison_value is not None:
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
        payload, ambiguity_resolution_blockers = _rebind_split_table_cell_literals(
            extractions_by_attachment_id[attachment_id],
            source=source_text_by_attachment_id.get(attachment_id, ""),
        )
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

        for table_index, table in enumerate(payload.quantitative_tables):
            table_key = (attachment_id, table.table_id)
            table_issues = _validate_table_metadata(
                table,
                source_attachment_id=attachment_id,
                source_text_by_attachment_id=source_text_by_attachment_id,
                expected_attachment_ids=expected,
            )
            ambiguity_resolution_blocker = ambiguity_resolution_blockers[
                table_index
            ]
            if ambiguity_resolution_blocker is not None:
                table_issues.append(
                    _issue(
                        ambiguity_resolution_blocker,
                        "REVIEW",
                        "정량평가표 모호성의 정확한 원문 구조 해제 조건이 충족되지 않았습니다.",
                        attachment_id=attachment_id,
                        table_id=table.table_id,
                    )
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
