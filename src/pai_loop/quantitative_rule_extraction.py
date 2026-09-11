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
from .quantitative_formula import (
    CREDIT_RATING_ORDER,
    CaseTableRowLiteral,
    compile_case_table,
    compile_credit_rating_values,
    normalize_credit_rating_text,
)
from .source_gap_policy import (
    is_explicit_qualitative_only_exclusion as _shared_qualitative_only_exclusion,
    is_quantitative_irrelevant_gap,
    normalise_source_gap as _shared_normalise_source_gap,
    quantitative_table_local_absence_targets as _shared_quantitative_table_local_absence_targets,
    source_label_document_types as _shared_source_label_document_types,
)


QUANTITATIVE_CANDIDATE_PROFILE_VERSION = "pai-loop-quantitative-candidate-profile-0.7.15"
from .extraction_contracts import CURRENT_EXTRACTION_CONTRACT, classify_record_contract

QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION = CURRENT_EXTRACTION_CONTRACT.validator
MIN_QUANTITATIVE_EVIDENCE_CONFIDENCE = 0.90

# Issue-only proof changes use targeted fingerprint revisions below.  Changes to
# executable scoring semantics, such as the credit-range DSL above, intentionally
# bump the global validator version so an older AVAILABLE record cannot be reused.
_TARGETED_RECORD_FINGERPRINT_REVISIONS = {
    # v3: the gap gate now classifies the declaration instead of transcribing
    # observed sentences, so a record that stored this issue must be revalidated
    # before its gap can be trusted either way.
    "EXTRACTION_DECLARED_INCOMPLETE": "typed-notice-reference-gaps-v3",
    "MINIMUM_SCORE_EXCEEDS_TOTAL": "overall-cutoff-source-census-v2",
    "MAX_POINTS_LITERAL_MISMATCH": "own-criterion-maximum-suffix-v1",
    # A bracket award stated as a score anywhere in its own criterion is now
    # provable, so a record that stored this issue must be revalidated. The
    # separate 배점의 content trigger below keeps its own revision.
    "BRACKET_NUMBER_MISMATCH": "criterion-scored-award-proof-v1",
    "BRACKET_COMPARATOR_MISMATCH": "inline-binary-bracket-proof-v1",
    "SOURCEWIDE_AMBIGUITY_SIGNATURE_UNSUPPORTED": (
        "sourcewide-structural-signature-v1"
    ),
    # A recognition condition whose anchor quotes only part of its own exact
    # source literal is now provable, so a record that stored this issue must be
    # revalidated. Records without the issue keep their fingerprint and remain
    # reusable, and no executable scoring semantics change.
    "RECOGNITION_CONDITION_LITERAL_MISMATCH": (
        "recognition-condition-source-anchor-v1"
    ),
}

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
    operator: Literal["GTE", "EQ", "IN", "LTE", "LT"]
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
# 엔진이 배율을 아는 단위는 조건 쪽에서도 읽을 수 있어야 한다. 배점 쪽
# 어휘와 unit_scales 는 이미 인·억·천·만 같은 맨 단위를 받는데 이 목록만
# 빠져 있어서, ``A. 5인이상`` 이나 ``5억 이상`` 이 조건으로 인식되지 않았다.
_UNIT_PATTERN = (
    r"(?:원|천\s*원|만\s*원|백만\s*원|천만\s*원|억\s*원|건|명|인|개교|개|점|%|"
    r"퍼센트|년|개월|회|등급|억|천만|백만|만|천|㎡|m2|m²|㎥)"
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
_SOURCEWIDE_HEADING_PREFIX_PATTERN = (
    r"(?:(?:\d+|[가-힣])\s*[.)]\s*|[❍○●■□▪▶]\s*)?"
)
_SOURCEWIDE_TABLE_MARKER_RE = re.compile(
    rf"^{_SOURCEWIDE_HEADING_PREFIX_PATTERN}"
    r"(?:(?:정량(?:적)?|객관(?:적)?|계량(?:적)?|계량화)\s*"
    r"(?:지표(?:별)?\s*)?평가\s*"
    r"(?:세부\s*(?:기준(?:표)?|배점표|평가표)|"
    r"항목(?:별)?(?:\s*및\s*배점)?|항목별\s*배점|"
    r"기준(?:표)?|표|배점표|평가표)|"
    r"평가\s*배점표|평가\s*기준표)"
    rf"(?:\s*\(\s*{_NUM_PATTERN}\s*점\s*\))?$"
)


def _sourcewide_table_marker_spans(
    lines: tuple[str, ...],
) -> tuple[tuple[int, int], ...]:
    """Return exact objective-table headings, including split HWP cells.

    HWP extraction may place ``객관적``, ``평가`` and ``세부기준`` on
    separate physical lines. Treat at most four adjacent non-empty lines as
    one heading so a competing table cannot disappear merely because its title
    was split across cells. The anchored heading grammar still requires both
    the objective-evaluation noun and a table/criteria suffix.
    """

    matches: set[tuple[int, int]] = set()
    for start in range(len(lines)):
        for end in range(
            start + 1,
            min(len(lines), start + _MAX_TABLE_CELL_WINDOW_LINES) + 1,
        ):
            window_lines = lines[start:end]
            if any(not line.strip() for line in window_lines) or any(
                _HWP_SECTION_LINE_RE.fullmatch(line.strip())
                for line in window_lines
            ):
                break
            window = "\n".join(window_lines)
            if len(window) > _MAX_TABLE_CELL_WINDOW_CHARS:
                break
            if _SOURCEWIDE_TABLE_MARKER_RE.fullmatch(
                unicodedata.normalize("NFKC", window).strip()
            ):
                matches.add((start, end))
    return tuple(sorted(matches))
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

    return re.sub(r"\s+", "", normalize_credit_rating_text(value)).casefold()


def _case_literal_contains_exact_category(literal: str, value: str) -> bool:
    """Require a category to occupy a source token/cell, never a substring."""

    source = normalize_credit_rating_text(literal).casefold()
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


def _invalid_bracket_bounds(
    bracket: QuantitativeBracketLiteral | ImmutableQuantitativeBracket,
) -> bool:
    """Match the scoring engine's nonempty interval contract, including [x, x]."""
    return (
        bracket.min_value is not None
        and bracket.max_value is not None
        and (
            bracket.min_value > bracket.max_value
            or (
                bracket.min_value == bracket.max_value
                and not (bracket.min_inclusive and bracket.max_inclusive)
            )
        )
    )


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


class _SourceLines(tuple):
    """An immutable paragraph sequence with bounded, validation-local indexes.

    The object is owned by one document validation and is never stored in a
    process-global cache. Cached answers retain every original source bound.
    """

    def __new__(cls, values: Iterable[str]):
        instance = super().__new__(cls, values)
        instance.normalized = tuple(_normalise_anchor_text(line) for line in instance)
        instance.compact = tuple("".join(line.split()) for line in instance.normalized)
        instance.anchor_spans = {}
        instance.cached_span_count = 0
        return instance


def _source_lines(source: str) -> tuple[str, ...]:
    """Return visible HWP paragraphs while preserving their exact order."""

    return _SourceLines(
        "".join(
            character
            for character in unicodedata.normalize("NFC", line).strip()
            if unicodedata.category(character) != "Cf"
        )
        for line in source.splitlines()
    )


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

    # Normalizing every overlapping window for every anchor made long HWP
    # documents spend minutes in Unicode processing. Normalization is separable
    # across newline boundaries, so normalize each line and the anchor once.
    # Keep the original character/window bounds and the exact same contiguous
    # and >=8-character whitespace-free matching rules as the shared predicate.
    normalized_quote = _normalise_anchor_text(quote)
    if not normalized_quote:
        return ()
    cache_key = (_MAX_TABLE_CELL_WINDOW_LINES, _MAX_TABLE_CELL_WINDOW_CHARS, normalized_quote)
    indexed = isinstance(lines, _SourceLines)
    if indexed:
        cached = lines.anchor_spans.get(cache_key)
        if cached is not None:
            return cached
    compact_quote = "".join(normalized_quote.split())
    normalized_lines = lines.normalized if indexed else tuple(_normalise_anchor_text(line) for line in lines)
    compact_lines = lines.compact if indexed else tuple("".join(line.split()) for line in normalized_lines)
    matches: list[tuple[int, int]] = []
    for start in range(len(lines)):
        raw_length = 0
        normalized_window = ""
        compact_window = ""
        for end in range(
            start + 1,
            min(len(lines), start + _MAX_TABLE_CELL_WINDOW_LINES) + 1,
        ):
            index = end - 1
            raw_length += len(lines[index]) + (1 if index > start else 0)
            if raw_length > _MAX_TABLE_CELL_WINDOW_CHARS:
                break
            line = normalized_lines[index]
            if line:
                normalized_window += (" " if normalized_window else "") + line
            compact_window += compact_lines[index]
            if normalized_quote in normalized_window or (
                len(compact_quote) >= 8 and compact_quote in compact_window
            ):
                matches.append((start, end))
                break
    # One earliest-ending match per start, in ascending start order. A match
    # is nonminimal exactly when a later start has an equal or smaller end.
    minimal: list[tuple[int, int]] = []
    smallest_end = len(lines) + 1
    for start, end in reversed(matches):
        if end < smallest_end:
            minimal.append((start, end))
            smallest_end = end
    result = tuple(reversed(minimal))
    if indexed and len(lines.anchor_spans) < 256 and lines.cached_span_count + len(result) <= 4096:
        lines.anchor_spans[cache_key] = result
        lines.cached_span_count += len(result)
    return result



def _unique_anchor_line_span(
    lines: tuple[str, ...],
    quote: str,
) -> tuple[int, int] | None:
    spans = _anchor_line_spans(lines, quote)
    if len(spans) != 1:
        return None
    start, end = spans[0]
    return spans[0] if _anchor_occurrence_count(quote, "\n".join(lines[start:end])) == 1 else None


def _flat_cell_line_spans(
    lines: tuple[str, ...],
    quote: str,
) -> tuple[tuple[int, int], ...]:
    """Locate exact normalized whole-cell sequences for flat HWPX repair only."""

    quote_cells = tuple(
        _normalise_anchor_text(normalize_credit_rating_text(cell))
        for cell in quote.splitlines()
    )
    if not quote_cells or any(not cell for cell in quote_cells):
        return ()
    normalized_lines = tuple(
        _normalise_anchor_text(normalize_credit_rating_text(line)) for line in lines
    )
    width = len(quote_cells)
    return tuple(
        (start, start + width)
        for start in range(len(lines) - width + 1)
        if normalized_lines[start : start + width] == quote_cells
    )


def _unique_flat_cell_line_span(
    lines: tuple[str, ...],
    quote: str,
) -> tuple[int, int] | None:
    spans = _flat_cell_line_spans(lines, quote)
    return spans[0] if len(spans) == 1 else None


def _all_flat_cell_line_spans(
    lines: tuple[str, ...],
    values: Iterable[str],
) -> tuple[tuple[int, int], ...]:
    return tuple(
        dict.fromkeys(
            span
            for value in values
            if value
            for span in _flat_cell_line_spans(lines, value)
        )
    )


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
    if case.operator in {"GTE", "LTE", "LT"}:
        if case.operator == "GTE" and candidate.metric == "PERFORMANCE_AMOUNT":
            return _amount_gte_condition_matches(candidate, case, condition)
        comparison = _decimal(case.comparison_value)
        return bool(
            comparison is not None
            and Counter(_comparator_terms(condition))
            == Counter(((comparison, case.operator),))
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
    if case.operator in {"GTE", "LTE", "LT"}:
        if case.operator == "GTE" and candidate.metric == "PERFORMANCE_AMOUNT":
            return _amount_gte_condition_matches(candidate, case, condition)
        comparison = _decimal(case.comparison_value)
        return bool(
            comparison is not None
            and Counter(_comparator_terms(condition))
            == Counter(((comparison, case.operator),))
        )
    return _literal_contains_number(case.comparison_value, condition)




# Exact numeric unit aliases already supported by the scoring registry. Keep
# unknown words out of the suffix grammar; metric-specific scale checks still
# run independently. A regression checks this finite vocabulary against it.
_CASE_AWARD_CONDITION_UNIT_PATTERN = (
    rf"(?:{_UNIT_PATTERN}|krw|천|만|백만|천만|억|인|year|대)"
)


def _condition_numbers(text: str) -> list[float]:
    """Every number the condition side already states, commas and all."""

    values: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        try:
            values.append(float(match.group(0).replace(",", "")))
        except ValueError:
            continue
    return values


# 배점 칸이 조건보다 앞에 오는 표가 있다. ``10점 : 5개교 이상`` 은 배점 열을
# 먼저 인쇄한 원문을 그대로 옮긴 것이고, 순서를 바꾸면 인용문의 부분집합이라는
# 앵커가 깨지므로 추출 쪽에서 고칠 수 있는 형태가 아니다. 한 행만 보면 뒤집힌
# 추출과 구분할 수 없지만, 표는 행마다 열 순서가 같다. 그래서 증거는 항목 전체가
# 쥐고 있다: 모든 행이 같은 모양일 때만 그것을 열 순서로 읽는다.
_LEADING_AWARD_RE = re.compile(
    rf"^\s*(?:배점\s*(?:의\s*)?)?(?P<award>{_NUM_PATTERN})\s*(?P<unit>점|%|퍼센트)"
    r"(?!\s*(?:이상|이하|미만|초과))\s*[:：|/]?\s*"
)


def _leading_award_split(literal: str) -> tuple[str, str] | None:
    """앞자리 배점과 나머지 조건으로 가른다. 비교어가 붙으면 조건이므로 제외한다."""

    text = " ".join(literal.split())
    match = _LEADING_AWARD_RE.match(text)
    if match is None:
        return None
    condition = text[match.end() :].strip()
    if not condition:
        return None
    return match.group("award") + match.group("unit"), condition


def _table_prints_the_award_first(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
) -> bool:
    """이 항목의 모든 행이 배점으로 시작하는가.

    한 행만 뒤집혀 있다면 열 순서가 아니라 그 행의 추출 오류다.
    """

    cases = list(candidate.cases or ())
    if len(cases) < 2:
        return False
    return all(_leading_award_split(case.literal) is not None for case in cases)


def _case_award_matches_literal(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
    case: QuantitativeCaseLiteral | ImmutableQuantitativeCase,
    literal: str,
) -> bool:
    """Bind the award to a separate terminal score token or table cell.

    A comparison/category number is never also an award. Split rows may use a
    bare terminal numeric cell; inline rows require an explicit score unit or award label.
    The remainder must independently retain the declared condition. This check
    uses only persisted literal/evidence structure and also protects old proofs.
    """
    value = unicodedata.normalize("NFKC", literal).strip()
    if not value or len(value) > 1_000:
        return False
    lines = value.splitlines()
    if any(not line.strip() for line in lines):
        return False

    def condition_matches(condition: str) -> bool:
        # A source row number is not a comparison value. Strip only an explicit
        # leading enumeration; never strip a decimal or a number in the body.
        condition = re.sub(
            r"^\s*(?:[A-Za-z가-힣]\s*[.)]\s*|\d{1,3}\s*\)\s*|\d{1,3}\.\s+)",
            "", condition, count=1,
        ).strip().rstrip(":：").rstrip()
        if re.search(
            rf"{_NUM_PATTERN}\s*점|배점\s*(?:의\s*)?{_NUM_PATTERN}",
            condition,
        ) or not _case_condition_matches(candidate, case, condition):
            return False
        if case.operator == "IN":
            # Bare dates/another column cannot follow the cited categories and
            # become their award. Exact category phrases may be separated only
            # by table/list punctuation or the explicit grade unit.
            remainder = _normalize_case_category(condition)
            for category in sorted(case.category_values, key=len, reverse=True):
                token = _normalize_case_category(category)
                if not token or token not in remainder:
                    return False
                remainder = remainder.replace(token, "", 1)
            remainder = remainder.replace("등급", "")
            return not re.sub(r"[,，、/|;:·ㆍ()\[\]{}]+", "", remainder)
        numbers = list(_NUMBER_RE.finditer(condition))
        if len(numbers) != 1:
            return False
        if case.operator == "EQ":
            return re.fullmatch(
                rf"\s*(?:{_CASE_AWARD_CONDITION_UNIT_PATTERN})?\s*", condition[numbers[0].end():],
                re.IGNORECASE,
            ) is not None
        # The comparison must finish the condition cell. Labels such as a
        # subsequent total/date/share field cannot be borrowed across to its
        # following number, even if that number equals the declared award.
        if any(not condition[match.end():].strip() for match in _KOREAN_BOUND_RE.finditer(condition)):
            return True
        if any(re.fullmatch(rf"\s*(?:{_CASE_AWARD_CONDITION_UNIT_PATTERN})?\s*", condition[match.end():],
                re.IGNORECASE) for match in _ASCII_DIRECT_BOUND_RE.finditer(condition)):
            return True
        return any(re.fullmatch(r"\s*[A-Za-z가-힣_]+\s*", condition[match.end():])
            for match in _ASCII_REVERSED_BOUND_RE.finditer(condition))

    if len(lines) >= 2 and _score_cell_matches(
        lines[-1], value=case.award_value,
        percent=case.award_kind == "PERCENT_OF_MAX",
    ):
        condition_lines = lines[:-1]
        # A complete numeric condition may span number/unit/comparator cells;
        # its exact grammar, not the bare final number, proves the separation.
        return condition_matches("\n".join(condition_lines))

    if len(lines) == 1 and _table_prints_the_award_first(candidate):
        # 열 순서가 배점 먼저인 표. 나머지 증거는 뒤에 붙은 경우와 똑같이 요구한다.
        split = _leading_award_split(value)
        if split is not None:
            head, condition = split
            if (
                case.award_value is not None
                and _score_cell_matches(
                    head,
                    value=case.award_value,
                    percent=case.award_kind == "PERCENT_OF_MAX",
                )
                and condition_matches(condition)
            ):
                # The table order is already proven by every row leading with
                # its award, so a leading value that happens to equal the
                # comparison needs no separate guard here. 5점 : 5명 이상 is a
                # score column that coincides with its own threshold, not an
                # ambiguity; the trailing reading keeps that guard because it
                # has no table-level proof to lean on.
                return True

    if len(lines) == 1:
        # A flattened table row puts the award in the same line as its
        # condition -- ``C.2~3개교 11`` -- with the cell boundary reduced to a
        # space. The split branch above proves such a row by two facts: the
        # last cell reads as this row's award, and the remainder stands alone
        # as a complete condition. Both are available here too, but a space is
        # weaker evidence of a boundary than a line break, so a third fact is
        # required: the award must not repeat a number the condition already
        # states. ``A. 7명 이상 7`` therefore stays unproven, because nothing
        # in the row distinguishes a restated comparison from a score cell.
        head, separator, tail = value.rpartition(" ")
        if (
            separator
            and head.strip()
            and case.award_value is not None
            and _score_cell_matches(
                tail,
                value=case.award_value,
                percent=case.award_kind == "PERCENT_OF_MAX",
            )
            and not any(
                found == float(case.award_value)
                for found in _condition_numbers(head)
            )
            and condition_matches(head)
        ):
            # Only a proof returns here. A row this branch cannot prove still
            # has the inline-award grammar below to answer for it.
            return True

    explicit_award = (
        rf"(?<![\d.,+\-])(?:배점\s*(?:의\s*)?{_NUM_PATTERN}\s*(?:점|%|퍼센트)?"
        rf"|{_NUM_PATTERN}\s*(?:점|%|퍼센트))"
    )
    match = re.fullmatch(
        rf"(?P<condition>.+?)(?:\((?P<wrapped>{explicit_award})\)"
        rf"|(?P<plain>{explicit_award}))",
        value, re.DOTALL,
    )
    return bool(
        match is not None
        and _score_cell_matches(
            match.group("wrapped") or match.group("plain"), value=case.award_value,
            percent=case.award_kind == "PERCENT_OF_MAX",
        )
        and condition_matches(match.group("condition").strip())
    )


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


def _bracket_row_window_matches(
    bracket: QuantitativeBracketLiteral,
    window: str,
) -> bool:
    """Prove one split condition cell followed by its exact award cell."""

    row_lines = window.splitlines()
    if len(row_lines) < 2 or not _score_cell_matches(
        row_lines[-1],
        value=bracket.points,
        percent=False,
    ):
        return False
    condition_lines = row_lines[:-1]
    if any(_is_score_cell(line) for line in condition_lines):
        return False
    condition = "\n".join(condition_lines)
    values = tuple(
        value
        for value in (bracket.min_value, bracket.max_value)
        if value is not None
    )
    return bool(
        all(_literal_contains_number(value, condition) for value in values)
        and Counter(_comparator_terms(condition))
        == Counter(_expected_bracket_terms(bracket))
    )


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
_SOURCEWIDE_QUANTITATIVE_DETAIL_MARKER_RE = re.compile(
    rf"^{_SOURCEWIDE_HEADING_PREFIX_PATTERN}"
    r"정량적\s*평가\s*세부\s*기준$"
)
_OVERALL_POINT_BOUND_RE = re.compile(
    rf"(?P<points>{_NUM_PATTERN})\s*점\s*(?P<op>이상|초과|이하|미만)"
)
_CREDIT_RATING_COLUMN_HEADER_CLUSTER = (
    "신용평가등급",
    "평점",
    "회사채",
    "기업어음",
    "기업신용평가등급",
)
_SOURCEWIDE_AMBIGUITY_SUPPORTED_METRICS = frozenset(
    {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT", "CREDIT_RATING"}
)
_SOURCEWIDE_RESOLVED_SUMMARY_DETAIL_REASON_RE = re.compile(
    rf"^정량적평가(?P<total>{_NUM_PATTERN})점은"
    rf"용역수행실적\((?P<performance>{_NUM_PATTERN})점="
    rf"금액(?P<amount>{_NUM_PATTERN})점\+건수(?P<count>{_NUM_PATTERN})점\)과"
    rf"경영상태\((?P<credit>{_NUM_PATTERN})점\)로세분화되며[,]?"
    r"요약행과상세행이동일소계를가지므로"
    r"상세행만채점항목으로반영함[.]?$"
)
_SOURCEWIDE_RESOLVED_AMOUNT_LOWER_DOMAIN_REASON = (
    "표상최저구간(1억원미만)에대한점수/등급이명시되지않아"
    "하한규칙은확인불가"
)
_SOURCEWIDE_RESOLVED_COUNT_LOWER_DOMAIN_REASON = (
    "1건미만(0건)에대한점수규정은표에없음"
)
_SOURCEWIDE_RESOLVED_ENTERPRISE_CREDIT_COLUMN_REASON = (
    "표열이회사채/기업어음/기업신용평가등급3개항목으로병렬구성되어있어"
    ",본항목은기업신용평가등급열만반영함"
)
# Deliberate flattened-HWP limitation: parallel company-bond, commercial-paper,
# and enterprise-credit columns have no native cell coordinates.  The exact
# enterprise header cluster, source-bound rows, terminal footnote, and complete
# canonical rating registry below jointly prove column ownership.  Do not union
# aliases from either neighboring instrument column.
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


def _source_bound_amount_case_unit(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
) -> str | None:
    """Recover only an exact, uniformly owned currency unit from CASE rows.

    Claude can preserve ``2 / 1.5 / 1`` as the structured values while the
    source rows spell ``2억 원 / 1.5억 원 / 1억 원`` and leave the criterion
    ``unit`` null.  The numeric values are still useful, but only when every
    ordered row independently yields the same unique allowlisted scale.  Thus
    either ``2``+``억원`` or ``200000000``+``원`` may be proven from the same
    exact source cell; no comparison value or operator is changed.
    """

    if (
        candidate.metric != "PERFORMANCE_AMOUNT"
        or candidate.scoring_method != "CASE_TABLE"
        or not candidate.cases
        or [case.row_order for case in candidate.cases]
        != list(range(1, len(candidate.cases) + 1))
    ):
        return None

    row_spans: list[tuple[int, int]] = []
    inferred_units: list[str] = []
    nonzero_source_units: set[str] = set()
    zero_source_units: list[str] = []
    for case in candidate.cases:
        expected = _decimal(case.comparison_value)
        literal_span = _unique_anchor_line_span(lines, case.literal)
        evidence_span = _unique_anchor_line_span(lines, case.evidence.quote)
        matches = [
            (match, operator_map[match.group("op")])
            for regex, operator_map in (
                (_AMOUNT_KOREAN_BOUND_RE, _KOREAN_OPERATOR),
                (_AMOUNT_ASCII_DIRECT_BOUND_RE, _DIRECT_OPERATOR),
                (_AMOUNT_ASCII_REVERSED_BOUND_RE, _REVERSED_OPERATOR),
            )
            for match in regex.finditer(case.literal)
        ]
        if (
            expected is None
            or expected < 0
            or literal_span is None
            or evidence_span is None
            or not _spans_overlap(literal_span, evidence_span)
            or len(matches) != 1
            or matches[0][1] != case.operator
            or case.operator != "GTE"
            or len(_COMPARATOR_MARKER_RE.findall(case.literal)) != 1
        ):
            return None
        match = matches[0][0]
        try:
            source_number = Decimal(match.group("num").replace(",", ""))
        except InvalidOperation:
            return None
        source_unit = _normalise_amount_unit(match.group("unit"))
        source_scale = _AMOUNT_UNIT_SCALE.get(source_unit)
        if source_scale is None or source_number < 0:
            return None
        if expected == 0 or source_number == 0:
            if expected != 0 or source_number != 0:
                return None
            zero_source_units.append(source_unit)
        else:
            inferred_scale = source_number * source_scale / expected
            matching_units = [
                unit
                for unit, scale in _AMOUNT_UNIT_SCALE.items()
                if scale == inferred_scale
            ]
            if len(matching_units) != 1:
                return None
            inferred_units.append(matching_units[0])
            nonzero_source_units.add(source_unit)
        row_spans.append(
            (
                min(literal_span[0], evidence_span[0]),
                max(literal_span[1], evidence_span[1]),
            )
        )
    # A zero threshold cannot establish a scale by division.  Accept it only
    # after at least one non-zero row proves a unique target unit and the zero
    # row spells the same source unit as every non-zero row.
    if (
        not inferred_units
        or len(set(inferred_units)) != 1
        or (
            zero_source_units
            and (
                len(nonzero_source_units) != 1
                or any(
                    unit not in nonzero_source_units
                    for unit in zero_source_units
                )
            )
        )
    ):
        return None

    if (
        any(
            left[1] > right[0]
            for left, right in zip(row_spans, row_spans[1:], strict=False)
        )
    ):
        return None
    return inferred_units[0]


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
    if candidate.metric == "PERFORMANCE_AMOUNT":
        if normalized in _AMOUNT_UNIT_SCALE:
            return candidate
        repaired = _source_bound_amount_case_unit(candidate, lines=lines)
        return (
            candidate.model_copy(update={"unit": repaired})
            if repaired is not None
            else candidate
        )
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


def _source_condition_replacement_indexes(
    conditions: list[QuantitativeRecognitionCondition],
    *,
    canonical_condition: QuantitativeRecognitionCondition,
    lines: tuple[str, ...],
    protected_structure_spans: tuple[tuple[int, int], ...],
    reject_canonical_structure_collision: bool = False,
) -> tuple[int, ...]:
    """Return model claims wholly owned by one exact source-derived cell."""

    canonical_literal_span = _unique_anchor_line_span(
        lines, canonical_condition.literal
    )
    canonical_evidence_span = _unique_anchor_line_span(
        lines, canonical_condition.evidence.quote
    )
    canonical_span = (
        canonical_literal_span
        if canonical_literal_span is not None
        and canonical_literal_span == canonical_evidence_span
        else None
    )
    if canonical_span is None or (
        reject_canonical_structure_collision
        and any(
            _spans_overlap(canonical_span, protected_span)
            for protected_span in protected_structure_spans
        )
    ):
        return ()

    canonical_text = "\n".join(
        lines[canonical_span[0] : canonical_span[1]]
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
            if _span_inside_region(span, canonical_span)
        )
        existing_evidence_spans = tuple(
            span
            for span in all_existing_evidence_spans
            if _span_inside_region(span, canonical_span)
        )
        outside_structure_collision = any(
            not _span_inside_region(span, canonical_span)
            and any(
                _spans_overlap(span, protected_span)
                for protected_span in protected_structure_spans
            )
            for span in (
                *all_existing_literal_spans,
                *all_existing_evidence_spans,
            )
        )
        if (
            existing.evidence.attachment_id
            == canonical_condition.evidence.attachment_id
            and len(existing_literal_spans) == 1
            and len(existing_evidence_spans) == 1
            and _anchor_occurrence_count(existing.literal, canonical_text) == 1
            and _anchor_occurrence_count(
                existing.evidence.quote,
                canonical_text,
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
                canonical_condition.literal,
            )
        ):
            replacement_indexes.append(existing_index)
    return tuple(replacement_indexes)


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
    amount_share_owners: list[
        tuple[QuantitativeRuleCandidate, QuantitativeRecognitionCondition]
    ] = []
    for sibling, sibling_region in zip(
        candidates,
        criterion_regions,
        strict=True,
    ):
        if (
            sibling.metric != "PERFORMANCE_AMOUNT"
            or sibling.scoring_method != "CASE_TABLE"
            or not sibling.cases
            or sibling.ambiguity_reason is not None
            or [case.row_order for case in sibling.cases]
            != list(range(1, len(sibling.cases) + 1))
            or not _performance_detail_conditions(
                sibling,
                lines=lines,
                criterion_region=sibling_region,
            )
        ):
            continue
        sibling_footnotes = _unique_performance_footnotes(
            sibling,
            lines=lines,
            footer_region=footer_region,
        )
        share_condition = sibling_footnotes.get("SHARE")
        if share_condition is not None:
            amount_share_owners.append((sibling, share_condition))
    unique_amount_share_owner = (
        amount_share_owners[0] if len(amount_share_owners) == 1 else None
    )

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
        if (
            candidate.metric == "PERFORMANCE_COUNT"
            and unique_amount_share_owner is not None
        ):
            amount_owner, amount_share_condition = unique_amount_share_owner
            if (
                candidate.evidence.attachment_id
                == amount_owner.evidence.attachment_id
                == amount_share_condition.evidence.attachment_id
            ):
                stray_share_indexes = _source_condition_replacement_indexes(
                    conditions,
                    canonical_condition=amount_share_condition,
                    lines=lines,
                    protected_structure_spans=protected_spans,
                    reject_canonical_structure_collision=True,
                )
                if len(stray_share_indexes) == 1:
                    # The source-derived SHARE footnote belongs only to the
                    # unique amount sibling.  Dropping the count copy avoids
                    # both a false collision and an incorrect APPLY_SHARE
                    # scope; it must never be canonicalized into count.
                    conditions.pop(stray_share_indexes[0])
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
            replacement_indexes = _source_condition_replacement_indexes(
                conditions,
                canonical_condition=condition,
                lines=lines,
                protected_structure_spans=protected_spans,
            )
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

    matches: list[tuple[int, int]] = list(_sourcewide_table_marker_spans(lines))
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
            _case_award_matches_literal(candidate, case, case.literal)
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
        normalized_rows_list: list[tuple[str, ...]] = []
        for case in ordered_cases:
            compiled_values = compile_credit_rating_values(
                case.category_values,
                source_literal=case.literal,
            )
            if compiled_values is None:
                return False
            normalized_rows_list.append(compiled_values)
        normalized_rows = tuple(normalized_rows_list)
        if tuple(
            value for row in normalized_rows for value in row
        ) != CREDIT_RATING_ORDER:
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


def _compact_ambiguity_reason(reason: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", reason or "")
    visible = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return re.sub(r"\s+", "", visible).strip()


def _sourcewide_candidate_ambiguity_is_resolved(
    candidate: QuantitativeRuleCandidate,
    *,
    lines: tuple[str, ...],
    criterion_region: tuple[int, int] | None,
) -> bool:
    """Clear only non-decision notes backed by an exhaustive source census.

    The model sometimes records a faithful description of an unprinted lower
    domain or of the explicitly selected enterprise-credit column as an
    ``ambiguity_reason``.  Neither note creates a score/default or a competing
    interpretation.  They can be removed from the repaired copy only when the
    bounded HWP proof independently establishes every printed row.  Values
    outside those rows remain unscorable in the deterministic evaluator.
    """

    if not candidate.ambiguity_reason or not _sourcewide_case_census_matches(
        candidate,
        lines=lines,
        criterion_region=criterion_region,
    ):
        return False

    compact = _compact_ambiguity_reason(candidate.ambiguity_reason)
    ordered_cases = sorted(candidate.cases, key=lambda item: item.row_order)
    if candidate.metric == "PERFORMANCE_AMOUNT":
        return bool(
            compact == _SOURCEWIDE_RESOLVED_AMOUNT_LOWER_DOMAIN_REASON
            and candidate.scoring_method == "CASE_TABLE"
            and len(ordered_cases) == 3
            and all(case.operator == "GTE" for case in ordered_cases)
            and re.search(
                r"1\s*억\s*원\s*이상",
                ordered_cases[-1].literal,
            )
        )
    if candidate.metric == "PERFORMANCE_COUNT":
        return bool(
            compact == _SOURCEWIDE_RESOLVED_COUNT_LOWER_DOMAIN_REASON
            and candidate.scoring_method == "CASE_TABLE"
            and [case.operator for case in ordered_cases]
            == ["GTE", "EQ", "EQ", "EQ", "EQ"]
            and [
                _decimal(case.comparison_value)
                for case in ordered_cases
            ]
            == [
                Decimal("5"),
                Decimal("4"),
                Decimal("3"),
                Decimal("2"),
                Decimal("1"),
            ]
        )
    if candidate.metric == "CREDIT_RATING":
        cluster_size = len(_CREDIT_RATING_COLUMN_HEADER_CLUSTER)
        cluster_count = 0
        if criterion_region is not None:
            for start in range(
                criterion_region[0],
                criterion_region[1] - cluster_size + 1,
            ):
                observed = tuple(
                    re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
                    for line in lines[start : start + cluster_size]
                )
                if observed == _CREDIT_RATING_COLUMN_HEADER_CLUSTER:
                    cluster_count += 1
        return bool(
            compact == _SOURCEWIDE_RESOLVED_ENTERPRISE_CREDIT_COLUMN_REASON
            and cluster_count == 1
        )
    return False


def _sourcewide_ambiguity_is_structural(reason: str | None) -> bool:
    """Allow only ambiguity text that the exact HWP rebind can resolve."""

    normalized = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", reason or ""),
    ).strip()
    if normalized in {
        "HWP 셀 구조상 행 연결 검토 필요",
        "요약 배점과 상세 배점 중 적용 표를 확인해야 함",
    }:
        return True
    return bool(
        _SOURCEWIDE_RESOLVED_SUMMARY_DETAIL_REASON_RE.fullmatch(
            _compact_ambiguity_reason(reason)
        )
    )


def _sourcewide_structural_reason_totals_match(
    reason: str | None,
    *,
    table: QuantitativeTableCandidate,
    criteria: list[QuantitativeRuleCandidate],
) -> bool:
    """Bind the production summary/detail explanation to this table's maxima."""

    match = _SOURCEWIDE_RESOLVED_SUMMARY_DETAIL_REASON_RE.fullmatch(
        _compact_ambiguity_reason(reason)
    )
    if match is None:
        return True
    try:
        stated = {
            name: Decimal(value.replace(",", ""))
            for name, value in match.groupdict().items()
        }
    except InvalidOperation:
        return False
    maxima = {
        candidate.metric: _decimal(candidate.max_points)
        for candidate in criteria
    }
    total = _decimal(table.total_points)
    amount = maxima.get("PERFORMANCE_AMOUNT")
    count = maxima.get("PERFORMANCE_COUNT")
    credit = maxima.get("CREDIT_RATING")
    return bool(
        total is not None
        and amount is not None
        and count is not None
        and credit is not None
        and stated["total"] == total
        and stated["amount"] == amount
        and stated["count"] == count
        and stated["credit"] == credit
        and stated["performance"] == amount + count
        and stated["total"] == stated["performance"] + credit
    )


def _recognition_key(literal: str, quote: str) -> tuple[str, str]:
    return (_normalise_anchor_text(literal).casefold(), _normalise_anchor_text(quote))


def _span_inside_region(
    span: tuple[int, int],
    region: tuple[int, int] | None,
) -> bool:
    return region is not None and region[0] <= span[0] and span[1] <= region[1]


def _overall_evaluation_minimum_context_matches(
    lines: tuple[str, ...],
    *,
    span: tuple[int, int],
    minimum_score: Decimal,
    allowed_operators: tuple[str, ...] = ("이상", "초과"),
) -> bool:
    """Prove one minimum anchor occurrence is an overall-evaluation cutoff."""

    literal = "\n".join(lines[span[0] : span[1]])
    point_bounds: list[tuple[Decimal, str]] = []
    for match in _OVERALL_POINT_BOUND_RE.finditer(literal):
        try:
            point_bounds.append(
                (
                    Decimal(match.group("points").replace(",", "")),
                    match.group("op"),
                )
            )
        except InvalidOperation:
            return False
    if (
        len(point_bounds) != 1
        or point_bounds[0][0] != minimum_score
        or point_bounds[0][1] not in allowed_operators
    ):
        return False

    def context_matches(value: str) -> bool:
        compact = re.sub(
            r"\s+",
            "",
            unicodedata.normalize("NFKC", value),
        )
        return bool(
            "정량" not in compact
            and any(
                marker in compact
                for marker in ("제안서평가", "기술평가", "종합평가")
            )
            and any(marker in compact for marker in ("적격", "선정", "협상"))
        )

    if context_matches(literal):
        return True

    # A repeated score on the following line must not borrow ``제안서 평가``
    # or ``선정`` from the previous sentence. The only source-local
    # continuation accepted here is the explicit two-stage procurement form
    # used by the Busan RFP (for example, ``2단계-1단계 결과 85점 이상자 중
    # 최저가격 제안자``). Every ownership token is therefore present on the
    # same physical line as the bound.
    compact_literal = re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", literal),
    )
    return bool(
        re.fullmatch(
            rf"\d+단계[-:]\d+단계결과{_NUM_PATTERN}점"
            r"(?:이상|초과)(?:인)?(?:자|업체)?중최저가격(?:을)?"
            r"(?:제안한)?제안자",
            compact_literal,
        )
    )


def _legacy_has_unclaimed_quantitative_point_language(
    lines: tuple[str, ...],
    *,
    total_span: tuple[int, int],
    overall_minimum_spans: tuple[tuple[int, int], ...],
    minimum_score: Decimal,
    criteria: list[QuantitativeRuleCandidate],
) -> bool:
    """Census cutoff language outside exact, structurally owned table claims."""

    protected_lines = set(range(total_span[0], total_span[1]))
    for index in range(len(lines)):
        span = (index, index + 1)
        if _overall_evaluation_minimum_context_matches(
            lines, span=span, minimum_score=minimum_score
        ):
            protected_lines.add(index)
    for span in overall_minimum_spans:
        if _overall_evaluation_minimum_context_matches(
            lines, span=span, minimum_score=minimum_score
        ):
            protected_lines.update(range(span[0], span[1]))

    claimed_literals: list[str] = []
    for candidate in criteria:
        claimed_literals.extend((candidate.criterion_literal, candidate.evidence.quote))
        for bracket in candidate.brackets:
            claimed_literals.extend((bracket.literal, bracket.evidence.quote))
        if candidate.threshold is not None:
            claimed_literals.extend(
                (candidate.threshold.literal, candidate.threshold.evidence.quote)
            )
        for case in candidate.cases:
            claimed_literals.extend((case.literal, case.evidence.quote))
        for condition in candidate.recognition_conditions:
            claimed_literals.extend((condition.literal, condition.evidence.quote))
    for literal in claimed_literals:
        span = _unique_anchor_line_span(lines, literal)
        if span is not None:
            protected_lines.update(range(span[0], span[1]))

    summary_markers = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(
            rf"{_SOURCEWIDE_HEADING_PREFIX_PATTERN}정량(?:적)?\s*평가\s*요약",
            unicodedata.normalize("NFKC", line).strip(),
        )
    ]
    detail_markers = [
        index
        for index, line in enumerate(lines)
        if _SOURCEWIDE_QUANTITATIVE_DETAIL_MARKER_RE.fullmatch(
            unicodedata.normalize("NFKC", line).strip()
        )
    ]
    if (
        len(summary_markers) == 1
        and len(detail_markers) == 1
        and summary_markers[0] < detail_markers[0]
    ):
        summary_start = summary_markers[0]
        summary_end = detail_markers[0]

        def semantic_key(value: str) -> str:
            normalized = unicodedata.normalize("NFKC", value).replace("요약", "")
            normalized = re.sub(rf"{_NUM_PATTERN}\s*점", "", normalized)
            return re.sub(r"[^0-9A-Za-z가-힣]+", "", normalized).casefold()

        summary_rows: set[int] = set()
        summary_shape_valid = True
        for candidate in criteria:
            maximum = _decimal(candidate.max_points)
            label_key = semantic_key(candidate.label)
            matches: list[int] = []
            for index in range(summary_start + 1, summary_end):
                line = unicodedata.normalize("NFKC", lines[index])
                point_values = []
                for raw in re.findall(rf"({_NUM_PATTERN})\s*점", line):
                    try:
                        point_values.append(Decimal(raw.replace(",", "")))
                    except InvalidOperation:
                        summary_shape_valid = False
                if (
                    maximum is not None
                    and point_values == [maximum]
                    and label_key
                    and label_key in semantic_key(line)
                ):
                    matches.append(index)
            if len(matches) != 1 or matches[0] in summary_rows:
                summary_shape_valid = False
                break
            summary_rows.add(matches[0])
        unowned_summary_text = "\n".join(
            lines[index]
            for index in range(summary_start + 1, summary_end)
            if index not in summary_rows
        )
        if re.search(
            rf"{_NUM_PATTERN}\s*(?:점|%|퍼센트)", unowned_summary_text
        ):
            summary_shape_valid = False
        if summary_shape_valid and len(summary_rows) == len(criteria):
            protected_lines.add(summary_start)
            protected_lines.update(summary_rows)

    # Protected source claims are separators rather than deleted glue.  This
    # prevents a normal table footnote such as "최저점으로 처리" from being
    # paired with an unrelated score cell hundreds of characters away, while
    # still catching split, unclaimed cutoff prose in the same local segment.
    segments: list[list[str]] = [[]]
    for index, line in enumerate(lines):
        if index in protected_lines or _HWP_SECTION_LINE_RE.fullmatch(line):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(line)

    score_value_pattern = rf"{_NUM_PATTERN}\s*(?:점|%|퍼센트)"
    quantitative_score_same_line_re = re.compile(
        rf"(?:정량[^\n]{{0,160}}?{score_value_pattern}|"
        rf"{score_value_pattern}[^\n]{{0,160}}?정량)"
    )
    quantitative_score_context_re = re.compile(
        rf"(?:정량.*?{score_value_pattern}|{score_value_pattern}.*?정량)",
        re.DOTALL,
    )
    decision_term_pattern = (
        r"(?:획득|선정|협상|도달|낮|넘|인정|후순위|제외|부적격|"
        r"합격|통과|탈락|미달|미치|득점|적격|충족|부여|처리|"
        r"한하여|해야|하지\s*않|못한|대상)"
    )
    score_decision_line_re = re.compile(
        rf"(?:{score_value_pattern}[^\n]{{0,160}}?{decision_term_pattern}|"
        rf"{decision_term_pattern}[^\n]{{0,160}}?{score_value_pattern})"
    )
    percent_bound_re = re.compile(
        rf"{_NUM_PATTERN}\s*(?:%|퍼센트)\s*(?:이상|초과|이하|미만)"
    )
    cutoff_term_pattern = (
        r"(?:최저\s*점|최소\s*점수|기준\s*점수|합격\s*선|"
        r"통과(?:\s*기준)?|탈락|부적격|미달|못\s*미치|제외|"
        r"득점률|득점하지\s*못)"
    )
    cutoff_near_score_re = re.compile(
        rf"(?:{cutoff_term_pattern}.{{0,160}}?{score_value_pattern}|"
        rf"{score_value_pattern}.{{0,160}}?{cutoff_term_pattern})",
        re.DOTALL,
    )
    for segment in segments:
        value = unicodedata.normalize("NFKC", "\n".join(segment))
        if (
            quantitative_score_same_line_re.search(value)
            or quantitative_score_context_re.search(value)
            or score_decision_line_re.search(value)
            or _OVERALL_POINT_BOUND_RE.search(value)
            or percent_bound_re.search(value)
            or cutoff_near_score_re.search(value)
        ):
            return True
    return False


_EXTERNAL_MIN_NEXT_SECTION_RE = re.compile(
    r"^\d+\s*[.]\s*제안서\s*평가$"
)
_EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE = re.compile(
    r"(?:정량(?:적)?|객관(?:적)?|계량(?:적)?|계량화)\s*"
    r"(?:지표(?:별)?\s*)?평가?"
)
_EXTERNAL_MIN_DECISION_RE = re.compile(
    r"(?:최저\s*점|최소\s*점수|기준\s*점수|합격\s*선|과락|"
    r"획득|취득|얻|선정|협상|도달|낮|넘|인정|유효|후순위|제외|"
    r"부적격|합격|통과|탈락|미달|미치|득점|적격|한하여|진행|"
    r"해야|하지\s*않|못한|대상|업체만|자만|실격|적으|밑돌|"
    r"이르지\s*아니|채우지\s*못)"
)
_EXTERNAL_MIN_STRONG_CUTOFF_RE = re.compile(
    r"(?:최저\s*점|최소\s*점수|기준\s*점수|합격\s*선|과락|미달|"
    r"못\s*미치|득점하지\s*못)"
)
_EXTERNAL_MIN_SPLIT_PREFIX_RE = re.compile(
    r"(?:(?:\d+|[가-힣])[.)]|[❍○●■□▪▶])?"
    r"(?:정량(?:적)?|객관(?:적)?|계량(?:적)?|계량화)(?:지표(?:별)?)?평가"
    r"(?:결과|점수|세부점수|득점|배점(?:의)?|총배점|통과기준|최저점(?:수)?|"
    r"최소점수|기준점수|합격선|과락기준|안내|기준)?"
    r"(?:은|는|의|이|가|을|를)?"
)
_EXTERNAL_MIN_SPLIT_SUFFIX_RE = re.compile(
    r"(?:"
    r"이상|초과|이하|미만|미달|"
    r"(?:을|를)?(?:획득|취득|얻|득점)|"
    r"에(?:도달|못미치|이르지아니)|보다(?:낮|적으)|"
    r"(?:을|를)?(?:넘지못|밑돌|채우지못)|"
    r"(?:이어야|여야|이다|인|일때|일경우|시)"
    r")"
)
_EXTERNAL_MIN_ASCII_POINT_BOUND_RE = re.compile(
    rf"(?:<=|>=|<|>)\s*{_NUM_PATTERN}\s*점"
)


def _external_min_split_window_is_coherent(value: str) -> bool:
    """Accept only one syntactic cutoff split across adjacent HWP cells."""

    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
    score_re = re.compile(rf"{_NUM_PATTERN}(?:점|%|퍼센트)")
    scores = list(score_re.finditer(compact))
    if len(scores) != 1:
        return False
    score = scores[0]
    prefix = compact[: score.start()]
    suffix = compact[score.end() :]
    return bool(
        _EXTERNAL_MIN_SPLIT_PREFIX_RE.fullmatch(prefix)
        and (
            not suffix
            or _EXTERNAL_MIN_SPLIT_SUFFIX_RE.match(suffix)
        )
    )


def _minimum_scope_source_headers(
    lines: tuple[str, ...],
    criteria: list[QuantitativeRuleCandidate],
) -> tuple[tuple[int, int], ...] | None:
    """Prove original inner-cell anchors belong to unique metric/max headers.

    Used only to establish whole-proposal minimum ownership. Original literals,
    recognition conditions and executable criteria remain untouched.
    """
    boundaries, overflow = _sourcewide_structural_boundary_spans(lines)
    if overflow:
        return None
    header_groups = [_candidate_metric_max_header_spans(lines, item) for item in criteria]
    all_headers = tuple(span for spans in header_groups for span in spans)
    next_boundary = _next_blank_or_section_boundaries(lines)
    owners: list[tuple[int, int]] = []
    for candidate, headers in zip(criteria, header_groups, strict=True):
        anchor_span = _unique_anchor_line_span(lines, candidate.evidence.quote)
        if anchor_span is None:
            return None
        matches: list[tuple[int, int]] = []
        for header in headers:
            header_text = "\n".join(lines[header[0]:header[1]])
            if (
                not re.search(r"점\s*\)?\s*$", header_text)
                or _COMPARATOR_MARKER_RE.search(header_text)
                or _EXTERNAL_MIN_STRONG_CUTOFF_RE.search(header_text)
                or _EXTERNAL_MIN_DECISION_RE.search(header_text)
            ):
                continue
            region = _bounded_header_candidate_region(
                lines, header_span=header, boundary_spans=(*boundaries, *all_headers),
                table_fences=(), next_blank_or_section=next_boundary,
            )
            if (
                region is not None
                and _span_inside_region(anchor_span, region)
                and _sourcewide_case_census_matches(candidate, lines=lines, criterion_region=region)
            ):
                matches.append(header)
        if len(matches) != 1:
            return None
        owners.append(matches[0])
    if any(left[1] > right[0] for left, right in zip(owners, owners[1:])):
        return None
    return tuple(owners)


def _has_unclaimed_quantitative_point_language(
    lines: tuple[str, ...],
    *,
    total_span: tuple[int, int],
    overall_minimum_spans: tuple[tuple[int, int], ...],
    minimum_score: Decimal,
    criteria: list[QuantitativeRuleCandidate],
    proven_header_spans: tuple[tuple[int, int], ...] = (),
) -> bool:
    """Fail closed on unowned score language without scanning unrelated prose.

    The source can contain a 100-point combined evaluation, qualitative rows,
    later instructions and an appendix.  Only the detailed objective table is
    exhaustively censused.  Outside that fenced table, a score blocks detachment
    only when it is tied to explicit objective/cutoff language.  Exact model
    literals are treated as untrusted: a claimed row containing an extra cutoff
    is rejected before its physical lines can be masked.
    """

    score_value_re = re.compile(rf"{_NUM_PATTERN}\s*(?:점|%|퍼센트)")
    point_value_re = re.compile(rf"(?P<points>{_NUM_PATTERN})\s*점")
    percent_bound_re = re.compile(
        rf"{_NUM_PATTERN}\s*(?:%|퍼센트)\s*(?:이상|초과|이하|미만)"
    )
    detail_markers = [
        index
        for index, line in enumerate(lines)
        if _SOURCEWIDE_QUANTITATIVE_DETAIL_MARKER_RE.fullmatch(
            unicodedata.normalize("NFKC", line).strip()
        )
    ]
    if len(detail_markers) != 1:
        return True
    detail_start = detail_markers[0]

    atomic_literals: list[str] = []
    for candidate in criteria:
        atomic_literals.append(candidate.criterion_literal)
        atomic_literals.extend(bracket.literal for bracket in candidate.brackets)
        if candidate.threshold is not None:
            atomic_literals.append(candidate.threshold.literal)
        atomic_literals.extend(case.literal for case in candidate.cases)
        for condition in candidate.recognition_conditions:
            normalized = unicodedata.normalize("NFKC", condition.literal)
            if point_value_re.search(normalized) and (
                _EXTERNAL_MIN_STRONG_CUTOFF_RE.search(normalized)
                or (
                    _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(normalized)
                    and _EXTERNAL_MIN_DECISION_RE.search(normalized)
                )
            ):
                return True
            if (
                _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(normalized)
                and percent_bound_re.search(normalized)
            ):
                return True
            atomic_literals.append(condition.literal)

    protected_detail_lines: set[int] = {
        index for span in proven_header_spans for index in range(span[0], span[1])
    }
    owned_spans: list[tuple[int, int]] = []
    for literal in atomic_literals:
        normalized = unicodedata.normalize("NFKC", literal)
        if (
            _OVERALL_POINT_BOUND_RE.search(normalized)
            or (
                point_value_re.search(normalized)
                and _EXTERNAL_MIN_STRONG_CUTOFF_RE.search(normalized)
            )
            or (
                _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(normalized)
                and percent_bound_re.search(normalized)
            )
            or (
                point_value_re.search(normalized)
                and _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(normalized)
                and _EXTERNAL_MIN_DECISION_RE.search(normalized)
            )
        ):
            return True
        span = _unique_anchor_line_span(lines, literal)
        if span is None:
            continue
        owned_spans.append(span)
        protected_detail_lines.update(range(span[0], span[1]))
    if not owned_spans:
        return True

    last_owned_end = max(span[1] for span in owned_spans)
    fence_candidates = [
        index
        for index in range(max(detail_start + 1, last_owned_end), len(lines))
        if _EXTERNAL_MIN_NEXT_SECTION_RE.fullmatch(
            unicodedata.normalize("NFKC", lines[index]).strip()
        )
        or _HWP_SECTION_LINE_RE.fullmatch(lines[index])
    ]
    detail_end = min(fence_candidates, default=len(lines))

    # Every remaining explicit score token inside the physical objective table
    # is unowned. This catches novel verbs as well as a cutoff smuggled beside a
    # broad evidence anchor.
    detail_segments: list[list[str]] = [[]]
    for index in range(detail_start, detail_end):
        if index in protected_detail_lines:
            if detail_segments[-1]:
                detail_segments.append([])
            continue
        detail_segments[-1].append(lines[index])
    if any(
        score_value_re.search(unicodedata.normalize("NFKC", "\n".join(segment)))
        for segment in detail_segments
        if segment
    ):
        return True

    overall_maximum_values: set[Decimal] = set()

    def overall_line_is_clean(index: int) -> bool:
        line = unicodedata.normalize("NFKC", lines[index])
        if re.search(rf"{_NUM_PATTERN}\s*(?:%|퍼센트)", line):
            return False
        matches = list(point_value_re.finditer(line))
        if not matches:
            return True
        for match in matches:
            try:
                value = Decimal(match.group("points").replace(",", ""))
            except InvalidOperation:
                return False
            tail = line[match.end() : match.end() + 20]
            if value == minimum_score and re.match(r"\s*(?:이상|초과)", tail):
                continue
            if re.match(r"\s*만점", tail) and value >= minimum_score:
                overall_maximum_values.add(value)
                continue
            return False
        return True

    safe_outside_lines = set(range(total_span[0], total_span[1]))
    for index in range(len(lines)):
        span = (index, index + 1)
        if _overall_evaluation_minimum_context_matches(
            lines,
            span=span,
            minimum_score=minimum_score,
        ):
            if not overall_line_is_clean(index):
                return True
            safe_outside_lines.add(index)
    for span in overall_minimum_spans:
        for index in range(span[0], span[1]):
            if not overall_line_is_clean(index):
                return True
            safe_outside_lines.add(index)
    if len(overall_maximum_values) != 1:
        return True

    quantitative_total_values = [_decimal(candidate.max_points) for candidate in criteria]
    if any(value is None for value in quantitative_total_values):
        return True
    expected_quantitative_total = sum(
        (value for value in quantitative_total_values if value is not None),
        Decimal("0"),
    )
    overall_maximum = next(iter(overall_maximum_values))
    axis_points_re = {
        axis: re.compile(
            rf"{axis}(?:적)?(?:평가)?(?:점수|배점)?"
            rf"(?:은|는|이|가)?[:：(]?(?P<points>{_NUM_PATTERN})점\)?"
        )
        for axis in ("정량", "정성")
    }

    def overall_allocation_line(value: str) -> bool | None:
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
        if not (
            "정량" in compact
            and "정성" in compact
            and any(marker in compact for marker in ("만점", "총점", "실시"))
            and not _OVERALL_POINT_BOUND_RE.search(compact)
            and not _EXTERNAL_MIN_STRONG_CUTOFF_RE.search(compact)
        ):
            return None
        values: list[Decimal] = []
        for match in point_value_re.finditer(compact):
            try:
                values.append(Decimal(match.group("points").replace(",", "")))
            except InvalidOperation:
                return False
        if not values:
            return True
        if len(values) == 1:
            return values[0] == overall_maximum
        axis_values: dict[str, Decimal] = {}
        for axis, pattern in axis_points_re.items():
            matches = list(pattern.finditer(compact))
            if len(matches) != 1:
                return False
            try:
                axis_values[axis] = Decimal(
                    matches[0].group("points").replace(",", "")
                )
            except InvalidOperation:
                return False
        largest = max(values, default=Decimal("0"))
        remaining = list(values)
        if largest in remaining:
            remaining.remove(largest)
        return bool(
            remaining
            and largest == overall_maximum
            and largest == sum(remaining, Decimal("0"))
            and axis_values["정량"] == expected_quantitative_total
        )

    for index, line in enumerate(lines):
        if index < detail_start or index >= detail_end:
            allocation_status = overall_allocation_line(line)
            if allocation_status is False:
                return True
            if allocation_status is True:
                safe_outside_lines.add(index)

    # Outside the detailed table, keep exact overall/allocation lines as hard
    # separators and inspect only bounded local windows. This avoids joining an
    # unrelated event count to a distant word such as "탈락".
    outside_segments: list[list[str]] = [[]]
    for index, line in enumerate(lines):
        if detail_start <= index < detail_end:
            if outside_segments[-1]:
                outside_segments.append([])
            continue
        if (
            index in safe_outside_lines
            or _HWP_SECTION_LINE_RE.fullmatch(line)
        ):
            if outside_segments[-1]:
                outside_segments.append([])
            continue
        outside_segments[-1].append(line)

    for segment in outside_segments:
        for start in range(len(segment)):
            for width in range(1, min(4, len(segment) - start) + 1):
                value = unicodedata.normalize(
                    "NFKC", "\n".join(segment[start : start + width])
                )
                if len(value) > _MAX_TABLE_CELL_WINDOW_CHARS:
                    break
                has_score = bool(score_value_re.search(value))
                if not has_score:
                    continue
                if width > 1 and not _external_min_split_window_is_coherent(value):
                    continue
                if (
                    _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(value)
                    and _OVERALL_POINT_BOUND_RE.search(value)
                ):
                    return True
                if (
                    _EXTERNAL_MIN_STRONG_CUTOFF_RE.search(value)
                    and point_value_re.search(value)
                    and (
                        _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(value)
                        or (width == 1 and "정성" not in value)
                    )
                ):
                    return True
                if (
                    width == 1
                    and "정성" not in value
                    and _EXTERNAL_MIN_ASCII_POINT_BOUND_RE.search(value)
                    and _EXTERNAL_MIN_DECISION_RE.search(value)
                ):
                    return True
                if (
                    _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(value)
                    and _EXTERNAL_MIN_DECISION_RE.search(value)
                ):
                    return True
                if (
                    _EXTERNAL_MIN_QUANTITATIVE_CONTEXT_RE.search(value)
                    and percent_bound_re.search(value)
                ):
                    return True
    return False


def _drop_source_bound_external_overall_minimum(
    table: QuantitativeTableCandidate,
    *,
    attachment_id: str,
    table_count: int,
    criteria: list[QuantitativeRuleCandidate],
    lines: tuple[str, ...],
    criterion_regions: list[tuple[int, int] | None],
    table_region: tuple[int, int] | None,
    hwp_section_starts: tuple[int, ...],
) -> QuantitativeTableCandidate:
    """Detach a whole-proposal cutoff only after proving the HWP table boundary.

    A value larger than the objective subtotal is still fail-closed by default.
    The sole normalisation is the source structure used by documents such as the
    부산 RFP: an exact quantitative subtotal summary, a separate whole-proposal
    eligibility sentence, then one detailed quantitative-table heading and a
    completely enumerated table.  The raw model response remains persisted for
    audit; only the immutable executable record drops the unrelated minimum.
    """

    if (
        table_count != 1
        or table.ambiguity_reason is not None
        or table.total_points is None
        or table.minimum_score is None
        or table.total_evidence is None
        or table.minimum_evidence is None
        or table_region is None
        or len(criteria) != len(criterion_regions)
        or any(region is None for region in criterion_regions)
    ):
        return table
    structural_anchors = [
        table.total_evidence,
        table.minimum_evidence,
        *(candidate.evidence for candidate in criteria),
        *(case.evidence for candidate in criteria for case in candidate.cases),
        *(
            condition.evidence
            for candidate in criteria
            for condition in candidate.recognition_conditions
        ),
    ]
    if any(anchor.attachment_id != attachment_id for anchor in structural_anchors):
        return table
    total = _decimal(table.total_points)
    minimum = _decimal(table.minimum_score)
    if total is None or minimum is None or minimum <= total:
        return table

    resolved_regions = [region for region in criterion_regions if region is not None]
    if not resolved_regions or not all(
        _span_inside_region(region, table_region) for region in resolved_regions
    ):
        return table
    maxima = [_decimal(candidate.max_points) for candidate in criteria]
    if any(value is None or value <= 0 for value in maxima) or sum(
        (value for value in maxima if value is not None),
        Decimal("0"),
    ) != total:
        return table

    proven_headers = _minimum_scope_source_headers(lines, criteria)
    if not proven_headers:
        return table
    total_span = _unique_anchor_line_span(lines, table.total_evidence.quote)
    minimum_spans = _anchor_line_spans(lines, table.minimum_evidence.quote)
    detail_markers = tuple(
        (index, index + 1)
        for index, line in enumerate(lines)
        if _SOURCEWIDE_QUANTITATIVE_DETAIL_MARKER_RE.fullmatch(
            unicodedata.normalize("NFKC", line)
        )
    )
    table_markers = _sourcewide_table_marker_spans(lines)
    simple_totals = _sourcewide_quantitative_total_candidates(lines)
    sourcewide_point_bounds: list[tuple[tuple[int, int], Decimal]] = []
    for index, line in enumerate(lines):
        for match in _OVERALL_POINT_BOUND_RE.finditer(line):
            try:
                value = Decimal(match.group("points").replace(",", ""))
            except InvalidOperation:
                return table
            if value == minimum:
                sourcewide_point_bounds.append(((index, index + 1), value))
            elif _overall_evaluation_minimum_context_matches(
                lines,
                span=(index, index + 1),
                minimum_score=value,
                allowed_operators=("이상", "초과", "이하", "미만"),
            ):
                # A second overall/technical/combined evaluation cutoff cannot
                # be discarded as if the model's 85-point value were the only
                # document-wide decision boundary. Unrelated training or KPI
                # thresholds do not match this same-line ownership grammar.
                return table
    summary_totals: list[tuple[tuple[int, int], Decimal]] = []
    for index, line in enumerate(lines):
        compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
        match = _SOURCEWIDE_QUANTITATIVE_SUMMARY_TOTAL_RE.fullmatch(compact)
        if match is None:
            continue
        try:
            summary_totals.append(
                ((index, index + 1), Decimal(match.group("points").replace(",", "")))
            )
        except InvalidOperation:
            return table
    if (
        total_span is None
        or not minimum_spans
        or len(detail_markers) != 1
        or table_markers != detail_markers
        or simple_totals
        or _has_unclaimed_quantitative_point_language(
            lines,
            total_span=total_span,
            overall_minimum_spans=minimum_spans,
            minimum_score=minimum,
            criteria=criteria,
            proven_header_spans=proven_headers,
        )
        or len(summary_totals) != 1
        or summary_totals[0] != (total_span, total)
    ):
        return table

    detail_marker = detail_markers[0]
    physical_detail_end = min(
        (
            index
            for index in range(detail_marker[1], len(lines))
            if _EXTERNAL_MIN_NEXT_SECTION_RE.fullmatch(
                unicodedata.normalize("NFKC", lines[index]).strip()
            )
            or _HWP_SECTION_LINE_RE.fullmatch(lines[index])
        ),
        default=len(lines),
    )
    first_region = resolved_regions[0]
    total_section = _hwp_section_for_span(
        lines,
        span=total_span,
        hwp_section_starts=hwp_section_starts,
    )
    detail_section = _hwp_section_for_span(
        lines,
        span=detail_marker,
        hwp_section_starts=hwp_section_starts,
    )
    if (
        total_section is None
        or total_section != detail_section
        or detail_marker[1] != proven_headers[0][0]
        or total_span[1] > detail_marker[0]
        or not any(
            total_span[1] <= span[0] < detail_marker[0]
            and _hwp_section_for_span(
                lines,
                span=span,
                hwp_section_starts=hwp_section_starts,
            )
            == total_section
            # The model may quote the same whole-proposal cutoff from the
            # document overview. Its own anchor is still checked below; the
            # independent source census must also prove that cutoff between
            # the objective subtotal and the detailed table in this section.
            for span, value in sourcewide_point_bounds
            if value == minimum
        )
        or any(span[0] >= detail_marker[0] for span in minimum_spans)
        or not all(
            _overall_evaluation_minimum_context_matches(
                lines,
                span=span,
                minimum_score=minimum,
            )
            for span in minimum_spans
        )
        or not sourcewide_point_bounds
        or any(
            value != minimum
            or not _overall_evaluation_minimum_context_matches(
                lines,
                span=span,
                minimum_score=minimum,
            )
            for span, value in sourcewide_point_bounds
        )
        or _OVERALL_POINT_BOUND_RE.search(
            "\n".join(lines[detail_marker[0] : physical_detail_end])
        )
    ):
        return table

    return table.model_copy(
        update={"minimum_score": None, "minimum_evidence": None}
    )


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


def _rebind_flat_split_table_cell_literals(
    payload: ExtractionPayload,
    *,
    source: str,
    lines: tuple[str, ...],
) -> ExtractionPayload:
    """Repair exact row-major HWPX cell splits without table coordinates.

    HWPX extraction preserves paragraph order but does not emit the synthetic
    ``[HWP SECTION N]`` markers used by the legacy HWP reader.  This fallback
    therefore accepts only one table with unique, ordered criterion and row
    anchors, and only joins an exact condition cell to the immediately
    following exact score cell.  It never changes a structured bound, category,
    award, maximum, or total.
    """

    if (
        len(payload.quantitative_tables) != 1
        or not lines
        or any(_HWP_SECTION_LINE_RE.fullmatch(line) for line in lines)
    ):
        return payload
    table = payload.quantitative_tables[0]
    criterion_spans = [
        _unique_flat_cell_line_span(lines, candidate.evidence.quote)
        for candidate in table.criteria
    ]
    if (
        not criterion_spans
        or any(span is None for span in criterion_spans)
        or any(
            left is not None and right is not None and left[1] > right[0]
            for left, right in zip(
                criterion_spans,
                criterion_spans[1:],
                strict=False,
            )
        )
    ):
        return payload

    resolved_criteria = [span for span in criterion_spans if span is not None]
    protected_spans = _all_flat_cell_line_spans(
        lines,
        (
            value
            for candidate in table.criteria
            for value in (
                candidate.criterion_literal,
                candidate.evidence.quote,
                *(item.literal for item in candidate.brackets),
                *(item.evidence.quote for item in candidate.brackets),
                *(item.literal for item in candidate.cases),
                *(item.evidence.quote for item in candidate.cases),
                *(item.literal for item in candidate.recognition_conditions),
                *(
                    item.evidence.quote
                    for item in candidate.recognition_conditions
                ),
            )
        ),
    )
    repaired_candidates: list[QuantitativeRuleCandidate] = []
    for candidate_index, candidate in enumerate(table.criteria):
        criterion_span = resolved_criteria[candidate_index]
        region_end = (
            resolved_criteria[candidate_index + 1][0]
            if candidate_index + 1 < len(resolved_criteria)
            else len(lines)
        )
        region = (criterion_span[0], region_end)

        criterion_literal = candidate.criterion_literal
        literal_span = _unique_flat_cell_line_span(lines, criterion_literal)
        if (
            not _literal_contains_number(candidate.max_points, criterion_literal)
            and literal_span is not None
            and _spans_overlap(literal_span, criterion_span)
            and _criterion_window_matches(candidate, candidate.evidence.quote)
        ):
            criterion_literal = candidate.evidence.quote

        def repair_rows(
            rows: list[QuantitativeBracketLiteral]
            | list[QuantitativeCaseLiteral],
            *,
            is_bracket: bool,
        ) -> list[QuantitativeBracketLiteral] | list[QuantitativeCaseLiteral]:
            row_spans: list[tuple[int, int] | None] = []
            for row in rows:
                literal = _unique_flat_cell_line_span(lines, row.literal)
                evidence = _unique_flat_cell_line_span(lines, row.evidence.quote)
                row_spans.append(
                    (
                        (min(literal[0], evidence[0]), max(literal[1], evidence[1]))
                        if literal is not None
                        and evidence is not None
                        and _spans_overlap(literal, evidence)
                        else None
                    )
                )
            if (
                not row_spans
                or any(span is None for span in row_spans)
                or any(
                    span is not None and not _span_inside_region(span, region)
                    for span in row_spans
                )
                or any(
                    left is not None
                    and right is not None
                    and left[1] > right[0]
                    for left, right in zip(row_spans, row_spans[1:], strict=False)
                )
            ):
                return rows

            output = list(rows)
            claimed_scores: list[tuple[int, int]] = []
            for row_index, (row, row_span) in enumerate(
                zip(rows, row_spans, strict=True)
            ):
                if row_span is None or row_span[1] >= region[1]:
                    continue
                score_span = (row_span[1], row_span[1] + 1)
                if any(
                    _spans_overlap(score_span, protected)
                    for protected in protected_spans
                ) or any(
                    _spans_overlap(score_span, claimed)
                    for claimed in claimed_scores
                ):
                    continue
                window = "\n".join(lines[row_span[0] : score_span[1]])
                matches = (
                    _bracket_row_window_matches(row, window)
                    if is_bracket
                    else _case_row_window_matches(candidate, row, window)
                )
                if not matches:
                    continue
                claimed_scores.append(score_span)
                output[row_index] = row.model_copy(
                    update={
                        "literal": window,
                        "evidence": row.evidence.model_copy(
                            update={"quote": window}
                        ),
                    }
                )
            return output

        repaired_candidates.append(
            candidate.model_copy(
                update={
                    "criterion_literal": criterion_literal,
                    "brackets": repair_rows(
                        list(candidate.brackets), is_bracket=True
                    ),
                    "cases": repair_rows(
                        list(candidate.cases), is_bracket=False
                    ),
                }
            )
        )
    return payload.model_copy(
        update={
            "quantitative_tables": [
                table.model_copy(update={"criteria": repaired_candidates})
            ]
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
    if not _sourcewide_structural_reason_totals_match(
        table.ambiguity_reason,
        table=table,
        criteria=criteria,
    ):
        return "SOURCEWIDE_AMBIGUITY_SIGNATURE_UNSUPPORTED"
    if (
        len(payload.quantitative_tables) != 1
        or table_index != 0
        or not table.criteria
        or payload.quantitative_table_not_applicable is not None
    ):
        return "SOURCEWIDE_AMBIGUITY_SCOPE_UNSUPPORTED"
    if any(
        not is_quantitative_irrelevant_gap(gap)
        for gap in payload.missing_or_unreadable
    ):
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

    # Resolve source-structure ambiguity from source proof, never from one
    # notice's point signature.  Keep the exception deliberately narrow: only
    # metrics with an independent exhaustive case census below are supported,
    # and one canonical company fact may own at most one criterion in the
    # ambiguous table.  All header, geometry, row, evidence, and total gates
    # still have to succeed before the model's ambiguity can be cleared.
    metrics = tuple(candidate.metric for candidate in criteria)
    maxima = tuple(_decimal(candidate.max_points) for candidate in criteria)
    if (
        any(metric not in _SOURCEWIDE_AMBIGUITY_SUPPORTED_METRICS for metric in metrics)
        or len(set(metrics)) != len(metrics)
        or any(maximum is None or maximum <= 0 for maximum in maxima)
    ):
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

    table_marker_spans = _sourcewide_table_marker_spans(lines)
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
        if _SOURCEWIDE_QUANTITATIVE_DETAIL_MARKER_RE.fullmatch(
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
    attachment_id: str,
) -> tuple[
    ExtractionPayload,
    tuple[SourcewideAmbiguityResolutionBlocker | None, ...],
]:
    """Repair exact HWP cell splits and return table-aligned safe blockers."""

    lines = _source_lines(source)
    if not payload.quantitative_tables:
        return payload, ()
    original_units = {
        (table_index, candidate_index): candidate.unit
        for table_index, table in enumerate(payload.quantitative_tables)
        for candidate_index, candidate in enumerate(table.criteria)
    }
    payload = _rebind_flat_split_table_cell_literals(
        payload,
        source=source,
        lines=lines,
    )
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

    # Unitless numeric CASE rows cannot prove their condition-to-score windows
    # until the source-unit scale is known.  Recover only units that every
    # unique ordered row states verbatim; categorical/header-dependent repairs
    # remain deferred until criterion regions have been fenced below.
    early_repaired_tables: list[QuantitativeTableCandidate] = []
    for table_index, table in enumerate(payload.quantitative_tables):
        early_repaired_candidates: list[QuantitativeRuleCandidate] = []
        for candidate_index, candidate in enumerate(table.criteria):
            repaired_candidate = (
                _repair_source_bound_candidate_unit(
                    candidate,
                    lines=lines,
                    criterion_region=None,
                )
                if candidate.metric
                in {"PERFORMANCE_AMOUNT", "PERFORMANCE_COUNT"}
                else candidate
            )
            early_repaired_candidates.append(repaired_candidate)
        early_repaired_tables.append(
            table.model_copy(update={"criteria": early_repaired_candidates})
        )
    payload = payload.model_copy(
        update={"quantitative_tables": early_repaired_tables}
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

        # The early unit repair is needed to parse and rebind split HWP rows,
        # but it is not decision-bearing until the bounded criterion region
        # proves that the payload exhausts every labeled source row and its
        # independent score cell.  Restore the exact model unit and retain a
        # criterion ambiguity when that census fails so partial/cross-section
        # tables stay non-executable even for metrics parsed from row literals.
        repaired_candidates = [
            (
                candidate.model_copy(
                    update={
                        "unit": original_units[(table_index, candidate_index)],
                        "ambiguity_reason": candidate.ambiguity_reason
                        or "원문 평가행 전체 일치 여부를 확인해야 함",
                    }
                )
                if candidate.unit
                != original_units[(table_index, candidate_index)]
                and not _sourcewide_case_census_matches(
                    candidate,
                    lines=lines,
                    criterion_region=criterion_regions[table_index][
                        candidate_index
                    ],
                )
                else candidate
            )
            for candidate_index, candidate in enumerate(repaired_candidates)
        ]

        # A model note is not automatically an unresolved scoring rule.  Clear
        # only the three source-proven, non-decision explanations supported by
        # the bounded row census; every other free-text reason stays fail-closed.
        repaired_candidates = [
            (
                candidate.model_copy(update={"ambiguity_reason": None})
                if _sourcewide_candidate_ambiguity_is_resolved(
                    candidate,
                    lines=lines,
                    criterion_region=criterion_regions[table_index][
                        candidate_index
                    ],
                )
                else candidate
            )
            for candidate_index, candidate in enumerate(repaired_candidates)
        ]

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
        repaired_table = table.model_copy(
            update={
                "criteria": repaired_candidates,
                "ambiguity_reason": ambiguity_reason,
            }
        )
        repaired_tables.append(
            _drop_source_bound_external_overall_minimum(
                repaired_table,
                attachment_id=attachment_id,
                table_count=len(payload.quantitative_tables),
                criteria=repaired_candidates,
                lines=lines,
                criterion_regions=criterion_regions[table_index],
                table_region=table_regions[table_index],
                hwp_section_starts=hwp_section_starts,
            )
        )
    return (
        payload.model_copy(update={"quantitative_tables": repaired_tables}),
        tuple(ambiguity_resolution_blockers),
    )


def _normalise_source_gap(value: str) -> str:
    return _shared_normalise_source_gap(value)


def _is_explicit_qualitative_only_exclusion(value: str) -> bool:
    """Ignore only an explicit qualitative-only exclusion from quant extraction."""

    return _shared_qualitative_only_exclusion(value)


def _compact_document_label(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _attachment_local_quantitative_table_targets(
    value: str,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] | None:
    """Delegate local-gap semantics to the shared extraction/pipeline policy."""

    return _shared_quantitative_table_local_absence_targets(value)


def _source_label_document_types(
    value: str | None,
) -> tuple[Literal["NOTICE", "RFP", "SCOPE", "FORM"], ...]:
    """Return deterministic roles from the shared manifest-label policy."""

    matches = set(_shared_source_label_document_types(value))
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


def _literal_owns_its_source_anchor(
    literal: str,
    anchor: EvidenceAnchor,
    source: str,
) -> bool:
    """Prove a literal whose anchor quotes only part of that same source text.

    ``_literal_is_anchored`` requires the cited quote to contain the literal.
    Providers frequently transcribe a complete source clause into ``literal``
    but cite it without its terminal punctuation, so the anchor becomes a
    contiguous part of the literal instead of its container. Both strings are
    then exact attachment text pointing at one place, which is a complete
    source proof rather than a relaxation: the literal must appear verbatim in
    the attachment, the quote must appear verbatim inside that literal, and
    both must occur exactly once so the anchor identifies a single location.

    A paraphrased literal, a literal that is absent from the attachment, a
    repeated literal or quote, and a quote taken from a different row all keep
    failing closed. No punctuation list, edit distance, or notice-specific
    token is involved, and no extracted value is altered.
    """

    # The literal-scoped check runs first so an unrelated anchor never pays for
    # a full-document scan of a large attachment.
    return (
        evidence_quote_matches_source(anchor.quote, literal)
        and evidence_quote_matches_source(literal, source)
        and _anchor_occurrence_count(literal, source) == 1
        and _anchor_occurrence_count(anchor.quote, source) == 1
    )


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
    if anchor.confidence < MIN_QUANTITATIVE_EVIDENCE_CONFIDENCE:
        return [
            _issue(
                "LOW_CONFIDENCE_QUANTITATIVE_EVIDENCE",
                "INCOMPLETE",
                "정량 산정 근거의 추출 신뢰도가 자동 적용 기준보다 낮습니다.",
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
        # A persisted record is also revalidated without its attachment text,
        # so this guard can only assert that the qualifier and its anchor are
        # the same contiguous text. Either containment direction satisfies
        # that; the source-side proof (verbatim, single occurrence) stays in
        # ``_validate_recognition_conditions`` where the attachment is known.
        if not evidence_quote_matches_source(
            condition.literal,
            condition.evidence.quote,
        ) and not evidence_quote_matches_source(
            condition.evidence.quote,
            condition.literal,
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
        inline_binary = _uses_inline_binary_brackets(candidate)
        if inline_binary and not _inline_binary_bracket_proof(candidate):
            raise ValueError("AVAILABLE inline binary brackets are not completely source bound")
        for bracket in candidate.brackets:
            if not evidence_quote_matches_source(bracket.literal, bracket.evidence.quote):
                raise ValueError("AVAILABLE bracket literal is not bound to its anchor")
            values = []
            if bracket.min_value is not None:
                values.append(bracket.min_value)
            if bracket.max_value is not None:
                values.append(bracket.max_value)
            if not _bracket_points_match_literal(candidate, bracket) or any(
                not _literal_contains_number(value, bracket.literal)
                for value in values
            ):
                raise ValueError("AVAILABLE bracket numbers do not match its literal")
            if bracket.points > candidate.max_points:
                raise ValueError("AVAILABLE bracket points exceed criterion maximum")
            if _invalid_bracket_bounds(bracket):
                raise ValueError("AVAILABLE bracket bounds are invalid")
            if not inline_binary and Counter(_comparator_terms(bracket.literal)) != Counter(
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
            if not _case_award_matches_literal(
                candidate, case, case.literal,
            ) or not _case_comparison_matches(candidate, case, case.literal):
                raise ValueError("AVAILABLE CASE numbers do not match its literal")
            rows.append(
                CaseTableRowLiteral(
                    operator=case.operator,
                    comparison_value=case.comparison_value,
                    category_values=case.category_values,
                    source_literal=case.literal,
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


def _available_candidate_anchors(
    candidate: ImmutableQuantitativeRuleCandidate,
) -> Iterable[ImmutableEvidenceAnchor]:
    yield candidate.evidence
    for bracket in candidate.brackets:
        yield bracket.evidence
    if candidate.threshold is not None:
        yield candidate.threshold.evidence
    for case in candidate.cases:
        yield case.evidence
    for condition in candidate.recognition_conditions:
        yield condition.evidence


def _record_anchors(
    record: ValidatedQuantitativeAttachmentRecord,
) -> Iterable[ImmutableEvidenceAnchor]:
    for table in record.tables:
        if table.total_evidence is not None:
            yield table.total_evidence
        if table.minimum_evidence is not None:
            yield table.minimum_evidence
    for candidate in record.available_candidates:
        yield from _available_candidate_anchors(candidate)
    yield from record.not_applicable_evidence


def _available_table_confidence_is_sufficient(
    record: ValidatedQuantitativeAttachmentRecord,
    table: ImmutableQuantitativeTable,
) -> bool:
    table_anchors = tuple(
        anchor
        for anchor in (table.total_evidence, table.minimum_evidence)
        if anchor is not None
    )
    candidates = tuple(
        candidate
        for candidate in record.available_candidates
        if candidate.table_id == table.table_id
    )
    return bool(
        table.status == "AVAILABLE"
        and table.total_evidence is not None
        and all(
            anchor.confidence >= MIN_QUANTITATIVE_EVIDENCE_CONFIDENCE
            for anchor in table_anchors
        )
        and all(
            anchor.confidence >= MIN_QUANTITATIVE_EVIDENCE_CONFIDENCE
            for candidate in candidates
            for anchor in _available_candidate_anchors(candidate)
        )
    )


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
    if any(
        table.status == "AVAILABLE"
        and not _available_table_confidence_is_sufficient(record, table)
        for table in record.tables
    ):
        raise ValueError("AVAILABLE tables require high-confidence quantitative evidence")
    if any(
        anchor.confidence < MIN_QUANTITATIVE_EVIDENCE_CONFIDENCE
        for anchor in record.not_applicable_evidence
    ):
        raise ValueError("NOT_APPLICABLE evidence must meet the confidence threshold")
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
        if _inline_binary_bracket_claim_conflict(candidate, record.available_candidates):
            raise ValueError("AVAILABLE inline binary clause has multiple criterion owners")

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


def _criterion_literal_with_own_maximum(
    candidate: QuantitativeRuleCandidate, source: str,
) -> QuantitativeRuleCandidate:
    """Extend a label only by its own exact, immediately following maximum."""
    if _literal_contains_number(candidate.max_points, candidate.criterion_literal):
        return candidate
    if not _literal_is_anchored(candidate.criterion_literal, candidate.evidence, source):
        return candidate
    compact = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
    literal, quote = compact(candidate.criterion_literal), compact(candidate.evidence.quote)
    if not literal or not quote.startswith(literal):
        return candidate
    suffix = quote[len(literal):].removesuffix("점")
    if _NUMBER_RE.fullmatch(suffix) and Decimal(suffix.replace(",", "")) == _decimal(candidate.max_points):
        return candidate.model_copy(update={"criterion_literal": candidate.evidence.quote})
    return candidate


def _has_inline_binary_bracket_marker(literal: str) -> bool:
    # Detection is deliberately broader than acceptance: malformed or partial
    # two-arm clauses must not fall back to a one-arm number-membership check.
    return bool(re.search(
        r"점\s*[,;]\s*(?:이상|초과|이하|미만)",
        unicodedata.normalize("NFKC", literal),
    ))


def _uses_inline_binary_brackets(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
) -> bool:
    return any(_has_inline_binary_bracket_marker(row.literal) for row in candidate.brackets)


def _inline_binary_bracket_proof(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
) -> bool:
    """Prove both unchanged comparator/award pairs in one owned ratio clause.

    This proof is shared by initial validation and persisted-record invariants.
    Attachment uniqueness is checked separately while source text is available.
    """
    if (candidate.scoring_method != "BRACKET" or candidate.metric != "FINANCIAL_RATIO"
            or candidate.unit != "%" or len(candidate.brackets) != 2):
        return False
    literal = candidate.brackets[0].literal
    if literal.splitlines() != [literal]:
        return False
    if any(row.literal != literal or row.evidence.quote != literal for row in candidate.brackets):
        return False
    number = r"(?:[0-9]+(?:\.[0-9]+)?)"
    match = re.fullmatch(
        rf"(?:기준비율\s*)?(?P<bound>{number})\s*%\s*"
        rf"(?P<first>이상|초과|이하|미만)\s*(?P<first_points>{number})\s*점\s*,\s*"
        rf"(?P<second>이상|초과|이하|미만)\s*(?P<second_points>{number})\s*점",
        unicodedata.normalize("NFKC", literal).strip(),
    )
    if match is None:
        return False
    first, second = match.group("first", "second")
    if {first, second} not in ({"이상", "미만"}, {"초과", "이하"}):
        return False
    bound = Decimal(match.group("bound"))
    expected = Counter((
        (((bound, _KOREAN_OPERATOR[first]),), Decimal(match.group("first_points"))),
        (((bound, _KOREAN_OPERATOR[second]),), Decimal(match.group("second_points"))),
    ))
    actual = Counter((_expected_bracket_terms(row), _decimal(row.points)) for row in candidate.brackets)
    if actual != expected:
        return False
    criterion = candidate.criterion_literal
    if criterion.splitlines() != [criterion]:
        return False
    if _normalise_anchor_text(criterion) != _normalise_anchor_text(candidate.evidence.quote):
        return False
    # The full clause must end its own criterion, immediately after that
    # criterion's explicit maximum and colon. Never borrow a sibling's clause.
    normalized = unicodedata.normalize("NFKC", criterion).strip()
    clause = unicodedata.normalize("NFKC", literal).strip()
    if not normalized.endswith(clause) or normalized.count(clause) != 1:
        return False
    prefix = normalized[:-len(clause)]
    maximum = re.search(rf"(?:\(\s*)?(?P<points>{number})\s*점\s*\)?\s*:\s*$", prefix)
    return bool(maximum and Decimal(maximum.group("points")) == _decimal(candidate.max_points))


def _inline_binary_bracket_source_owned(
    candidate: QuantitativeRuleCandidate,
    source: str,
) -> bool:
    if not _inline_binary_bracket_proof(candidate):
        return False
    lines = _source_lines(source)
    span = _unique_anchor_line_span(lines, candidate.criterion_literal)
    return bool(
        span is not None and span[1] - span[0] == 1
        and _normalise_anchor_text(lines[span[0]]) == _normalise_anchor_text(candidate.criterion_literal)
        and _anchor_occurrence_count(candidate.brackets[0].literal, source) == 1
    )


def _inline_binary_bracket_claim_conflict(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
    others: Iterable[QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate],
) -> bool:
    if not _uses_inline_binary_brackets(candidate):
        return False
    literals = {row.literal for row in candidate.brackets if _has_inline_binary_bracket_marker(row.literal)}

    def quoted_claim_overlaps_criterion(claimed: str, quote: str) -> bool:
        # Align the claimed arm inside both the quote and its owning criterion.
        # A quote may start in a preceding paragraph or end in a following one;
        # the whole intersecting span must still agree. Matching only the arm
        # would also reject a separate criterion with the same numbers.
        values = tuple(_normalise_anchor_text(value) for value in
                       (candidate.criterion_literal, quote, claimed))
        for compact in (False, True):
            owner, quoted, claim = (
                tuple("".join(value.split()) for value in values) if compact else values
            )
            if not claim:
                continue
            owner_positions = [match.start() for match in re.finditer(
                f"(?={re.escape(claim)})", owner)]
            quote_positions = [match.start() for match in re.finditer(
                f"(?={re.escape(claim)})", quoted)]
            for owner_position in owner_positions:
                for quote_position in quote_positions:
                    offset = owner_position - quote_position
                    start, end = max(0, offset), min(len(owner), offset + len(quoted))
                    # Preserve the shared anchor predicate's minimum length
                    # when only whitespace-free matching establishes overlap.
                    if compact and end - start < 8:
                        continue
                    if owner[start:end] == quoted[start - offset:end - offset]:
                        return True
        return False

    for other in others:
        if other is candidate:
            continue
        claims = [(other.criterion_literal, other.evidence.quote)]
        claims.extend((row.literal, row.evidence.quote) for row in other.brackets)
        claims.extend((row.literal, row.evidence.quote) for row in other.cases)
        claims.extend((row.literal, row.evidence.quote) for row in other.recognition_conditions)
        if other.threshold is not None:
            claims.append((other.threshold.literal, other.threshold.evidence.quote))
        if other.formula_literal:
            claims.append((other.formula_literal, other.evidence.quote))
        for literal in literals:
            for claimed, quote in claims:
                if (evidence_quote_matches_source(literal, claimed)
                        or evidence_quote_matches_source(literal, quote)
                        or (evidence_quote_matches_source(claimed, literal)
                            and quoted_claim_overlaps_criterion(claimed, quote))):
                    return True
    return False



def _points_are_score_marked(points: float, text: str) -> bool:
    """True when ``points`` appears in ``text`` stated as a score.

    A bare digit match is not enough here. Criterion prose routinely carries
    unrelated small numbers (``최근 3년``, ``단일건 3천만원``), so any of those
    would prove a 3-point bracket by coincidence. Require the value to sit
    against a score marker: ``9점``, ``배점 9``, or a ``(9)`` scoring cell.
    """

    value = _decimal(points)
    if value is None or not text:
        return False
    digits = re.escape(format(value.normalize(), "f"))
    return bool(
        re.search(
            rf"배\s*점\s*[:：]?\s*{digits}(?![0-9.])"
            rf"|(?<![0-9.]){digits}\s*점"
            rf"|\(\s*{digits}\s*\)",
            unicodedata.normalize("NFKC", text),
        )
    )


def _bracket_points_match_literal(
    candidate: QuantitativeRuleCandidate | ImmutableQuantitativeRuleCandidate,
    bracket: QuantitativeBracketLiteral | ImmutableQuantitativeBracket,
) -> bool:
    literal = unicodedata.normalize("NFKC", bracket.literal)
    if re.search(r"배점\s*의", literal):
        awards = list(re.finditer(r"배점\s*의\s*(\d+(?:\.\d+)?)\s*(?:%|퍼센트)\s*$", literal))
        if len(awards) != 1 or len(re.findall(r"배점\s*의", literal)) != 1:
            return False
        award = awards[0]
        rate = Decimal(award.group(1))
        maximum, points = _decimal(candidate.max_points), _decimal(bracket.points)
        return bool(literal[:award.start()].strip() and rate is not None
                    and Decimal(0) <= rate <= Decimal(100) and maximum is not None
                    and points == maximum * rate / Decimal(100))
    if _literal_contains_number(bracket.points, bracket.literal):
        return True
    # A scoring table keeps the condition and its award in separate cells, so a
    # row literal is frequently the condition alone (``5건 이상``) while the award
    # sits one column over. Accept the award when another source-bound string
    # for the same criterion states it as a score.
    return any(
        _points_are_score_marked(bracket.points, text)
        for text in (
            candidate.criterion_literal,
            bracket.evidence.quote,
            candidate.evidence.quote,
        )
    )


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
    inline_binary = _uses_inline_binary_brackets(candidate)
    inline_proved = inline_binary and _inline_binary_bracket_source_owned(candidate, source)
    if inline_binary and not inline_proved:
        issues.append(_issue(
            "BRACKET_COMPARATOR_MISMATCH", "INCOMPLETE",
            "한 문장의 두 구간·배점과 단일 평가항목의 원문 소유권이 일치하지 않습니다.",
            **context,
        ))
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
        values = []
        if bracket.min_value is not None:
            values.append(bracket.min_value)
        if bracket.max_value is not None:
            values.append(bracket.max_value)
        if not _bracket_points_match_literal(candidate, bracket) or any(
            not _literal_contains_number(value, bracket.literal) for value in values
        ):
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
        if _invalid_bracket_bounds(bracket):
            issues.append(
                _issue(
                    "INVALID_BRACKET_BOUNDS",
                    "INCOMPLETE",
                    "배점 구간 하한은 상한보다 작거나, 같은 값이면 양쪽 경계를 포함해야 합니다.",
                    **context,
                )
            )
        comparator_issue = None if inline_binary else _comparator_binding_issue(
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
    "NUMERIC", "DISCRETE", "CATEGORICAL", "CREDIT_RATING"
]:
    if metric == "CREDIT_RATING":
        return "CREDIT_RATING"
    if metric == "LOCAL_PRESENCE":
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
        if not _case_award_matches_literal(
            candidate, case, case.literal,
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
        elif case.operator in {"GTE", "LTE", "LT"} and case.comparison_value is not None:
            comparison = _decimal(case.comparison_value)
            comparator_issue = _comparator_binding_issue(
                literal=case.literal,
                expected=((comparison, case.operator),) if comparison is not None else (),
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
                source_literal=case.literal,
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
        # A recognition condition is a source-quoted qualifier, not a scoring
        # row: accepting the inverse containment proven above never changes an
        # operator, threshold, award, or maximum. The exact HWP window repair
        # still runs first and remains the preferred anchor.
        if not _literal_is_anchored(
            condition.literal, condition.evidence, source
        ) and not _literal_owns_its_source_anchor(
            condition.literal, condition.evidence, source
        ):
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
    candidate = _criterion_literal_with_own_maximum(candidate, source)
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
            attachment_id=attachment_id,
        )
        source_gaps = [
            gap
            for gap in payload.missing_or_unreadable
            if not is_quantitative_irrelevant_gap(gap)
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

        attachment_candidates = tuple(
            candidate for table in payload.quantitative_tables for candidate in table.criteria
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
                repeated_id = candidate.criterion_id in seen_criterion_ids
                inline_claim_conflict = _inline_binary_bracket_claim_conflict(candidate, attachment_candidates)
                if repeated_id or inline_claim_conflict:
                    duplicate = _issue(
                        "DUPLICATE_CRITERION_ID" if repeated_id else "INLINE_BRACKET_CLAIM_COLLISION",
                        "INCOMPLETE",
                        ("같은 표 안에서 평가항목 ID가 중복되었습니다." if repeated_id else
                         "동일한 이진 구간 원문을 서로 다른 평가항목이 함께 소유합니다."),
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
                        "첨부 추출·검증 결과에서 정량평가표나 명시적 비적용 근거를 확보하지 못했습니다.",
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


def _targeted_record_fingerprint_revisions(
    data: Mapping[str, object],
) -> tuple[str, ...]:
    raw_issues = data.get("issues")
    if not isinstance(raw_issues, (list, tuple)):
        return ()
    issue_codes = {
        str(item.get("code") or "")
        for item in raw_issues
        if isinstance(item, Mapping)
    }
    revisions = {
        revision for code, revision in _TARGETED_RECORD_FINGERPRINT_REVISIONS.items()
        if code in issue_codes
    }
    candidates = data.get("available_candidates")
    if isinstance(candidates, (list, tuple)) and any(
        isinstance(candidate, Mapping) and any(
            isinstance(row, Mapping) and _has_inline_binary_bracket_marker(str(row.get("literal") or ""))
            for row in (candidate.get("brackets") or ())
        )
        for candidate in candidates
    ):
        revisions.add("inline-binary-bracket-proof-v1")
    if isinstance(candidates, (list, tuple)) and any(
        isinstance(candidate, Mapping) and any(
            isinstance(row, Mapping) and re.search(r"배점\s*의", str(row.get("literal") or ""))
            for row in (candidate.get("brackets") or ())
        )
        for candidate in candidates
    ):
        revisions.add("bracket-percent-award-proof-v1")
    return tuple(sorted(revisions))


def _record_fingerprint_data(data: Mapping[str, object]) -> str:
    canonical_data = dict(data)
    targeted_revisions = _targeted_record_fingerprint_revisions(canonical_data)
    if targeted_revisions:
        canonical_data["_targeted_validator_revisions"] = targeted_revisions
    canonical = json.dumps(
        canonical_data,
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

    if prompt_version != PROMPT_VERSION or extraction_schema_version != SCHEMA_VERSION:
        raise ValueError("new quantitative validation requires the current extraction contract")
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


def quantitative_record_contract_is_usable(
    record: ValidatedQuantitativeAttachmentRecord,
    *,
    source_payload: Mapping[str, object] | None,
    attachment_id: str,
    document_sha256: str,
    manifest_sha256: str,
) -> bool:
    """Check stored proof integrity and one exact predecessor feature contract.

    This rechecks the stored proof, not absent original document text. Legacy
    REVIEW candidates omit their CASE rows, so their original extraction is
    mandatory: checking only executable AVAILABLE candidates would be unsafe.
    """
    if not (
        record.attachment_id == attachment_id
        and record.document_sha256 == document_sha256
        and record.manifest_sha256 == manifest_sha256
        and record.validation_fingerprint_sha256
        == validated_quantitative_record_fingerprint(record)
    ):
        return False
    raw = record.model_dump(mode="json")
    if source_payload is None:
        # Preserve detached current-record callers. A legacy record always
        # needs its original extraction context; never synthesize that proof.
        return (
            record.prompt_version == PROMPT_VERSION
            and record.extraction_schema_version == SCHEMA_VERSION
            and record.validator_version == QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION
        )
    if source_payload.get("source_kind") != "PPS_PUBLIC_ATTACHMENT":
        return (
            record.prompt_version == PROMPT_VERSION
            and record.extraction_schema_version == SCHEMA_VERSION
            and record.validator_version == QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION
        )
    kind = classify_record_contract(source_payload, raw)
    if kind == "UNSUPPORTED":
        return False
    if kind == "CURRENT":
        # Existing current records are validated against caller-owned bindings;
        # optional redundant payload digest fields do not change that contract.
        return True
    if not (
        source_payload.get("attachment_id") == attachment_id
        and source_payload.get("document_sha256") == document_sha256
        and source_payload.get("current_manifest_sha256") == manifest_sha256
    ):
        return False
    if not (
        source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
        and source_payload.get("source_kind") == "PPS_PUBLIC_ATTACHMENT"
        and source_payload.get("status") == "ACCEPTED"
        and isinstance(source_payload.get("result"), dict)
        and isinstance(source_payload.get("quantitative_validation_record"), dict)
    ):
        return False
    try:
        original = ExtractionPayload.model_validate(source_payload["result"])
        original_record = ValidatedQuantitativeAttachmentRecord.model_validate(
            source_payload["quantitative_validation_record"]
        )
    except (ValidationError, TypeError, ValueError):
        return False
    if original_record != record:
        return False
    original_candidates = {
        (table.table_id, candidate.criterion_id): candidate
        for table in original.quantitative_tables for candidate in table.criteria
    }
    original_keys = [
        (table.table_id, candidate.criterion_id)
        for table in original.quantitative_tables for candidate in table.criteria
    ]
    stored_keys = [
        (candidate.table_id, candidate.criterion_id)
        for candidate in (*record.available_candidates, *record.review_candidates)
    ]
    if (
        len(original_keys) != len(set(original_keys))
        or Counter(original_keys) != Counter(stored_keys)
        or Counter(table.table_id for table in original.quantitative_tables)
        != Counter(table.table_id for table in record.tables)
    ):
        return False

    def old_case_shape(case: QuantitativeCaseLiteral | ImmutableQuantitativeCase) -> bool:
        if case.operator in {"GTE", "EQ"}:
            return case.comparison_value is not None and not case.category_values
        return (
            case.operator == "IN" and case.comparison_value is None
            and bool(case.category_values)
        )

    if any(
        not old_case_shape(case)
        for candidate in original_candidates.values() for case in candidate.cases
    ) or any(
        not old_case_shape(case)
        for candidate in record.available_candidates for case in candidate.cases
    ):
        return False
    # Source repair can expand an anchor or recover a missing source unit; it
    # cannot change extracted CASE operators, values, awards, or row order.
    for candidate in record.available_candidates:
        prior = original_candidates[(candidate.table_id, candidate.criterion_id)]
        if (
            (candidate.metric, candidate.scoring_method, candidate.max_points)
            != (prior.metric, prior.scoring_method, prior.max_points)
            or len(candidate.cases) != len(prior.cases)
        ):
            return False
        for current_case, prior_case in zip(candidate.cases, prior.cases):
            if (
                current_case.operator, current_case.comparison_value,
                tuple(current_case.category_values), current_case.award_kind,
                current_case.award_value, current_case.row_order,
            ) != (
                prior_case.operator, prior_case.comparison_value,
                tuple(prior_case.category_values), prior_case.award_kind,
                prior_case.award_value, prior_case.row_order,
            ):
                return False

    def anchors_belong(value: object) -> bool:
        if isinstance(value, dict):
            return (
                ("attachment_id" not in value or value["attachment_id"] == attachment_id)
                and all(anchors_belong(item) for item in value.values())
            )
        if isinstance(value, list):
            return all(anchors_belong(item) for item in value)
        return True

    return anchors_belong(original.model_dump(mode="json"))


def merge_validated_quantitative_records(
    records: Iterable[
        ValidatedQuantitativeAttachmentRecord | Mapping[str, object]
    ],
    *,
    expected_documents: Mapping[str, str],
    manifest_sha256: str,
    incomplete_attachment_ids: Iterable[str] = (),
    attachment_profiles: Mapping[str, Mapping[str, object]] | None = None,
    source_payloads: Mapping[str, Mapping[str, object]] | None = None,
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
        contract_usable = quantitative_record_contract_is_usable(
            record, source_payload=(source_payloads or {}).get(attachment_id),
            attachment_id=attachment_id, document_sha256=document_sha256,
            manifest_sha256=manifest_sha256,
        )
        if not contract_usable and record.validator_version != QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION:
            binding_errors.append(
                _issue(
                    "VALIDATOR_VERSION_MISMATCH",
                    "INCOMPLETE",
                    "정량 검증 record의 validator 버전이 현재 버전과 다릅니다.",
                    attachment_id=attachment_id,
                )
            )
        if not contract_usable and (
            record.prompt_version != PROMPT_VERSION
            or record.extraction_schema_version != SCHEMA_VERSION
        ):
            binding_errors.append(
                _issue(
                    "EXTRACTION_VERSION_MISMATCH",
                    "INCOMPLETE",
                    "정량 검증 record의 추출 prompt/schema 버전이 현재 버전과 다릅니다.",
                    attachment_id=attachment_id,
                )
            )
        if not contract_usable and source_payloads is not None:
            binding_errors.append(_issue(
                "EXTRACTION_CONTRACT_PROOF_INVALID", "INCOMPLETE",
                "정량 검증 record의 원본 추출 계약 또는 기능 근거를 확인할 수 없습니다.",
                attachment_id=attachment_id,
            ))
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
                    if is_quantitative_irrelevant_gap(gap):
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
        if record.status in {"AVAILABLE", "REVIEW"}
        and any(
            _available_table_confidence_is_sufficient(record, table)
            for table in record.tables
        )
    }
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
        if len(source_label_types) != 1:
            return False
        source_inferred_type = source_label_types[0]
        # The declaring attachment may itself have been misclassified by the
        # extractor. Its exact singleton filename role is used only to prevent
        # self-supply; the persisted type equality above still binds the issue
        # to the record that produced it.
        source_effective_type = source_inferred_type
        required_sibling_types = set(issue.required_sibling_document_types)
        if source_effective_type in required_sibling_types:
            # A local gap can never be satisfied by the document that declared
            # it, even when the prose repeats that document's role.
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
            if len(label_types) != 1:
                continue
            inferred_type = label_types[0]
            effective_type = binding.document_type
            if effective_type in {None, "OTHER"}:
                effective_type = inferred_type
            if effective_type != inferred_type:
                continue
            if effective_type in required_sibling_types:
                return True
        return False

    # Grow table capability only from independent AVAILABLE/REVIEW seeds.
    # A record whose sole hard issues are local absences may join after those
    # absences are satisfied by the current seed set. Batch updates make this a
    # monotone fixed point: valid A <- B <- C chains resolve, while two mutually
    # incomplete records can never bootstrap one another without a seed.
    while True:
        newly_supplying: set[str] = set()
        for attachment_id, record in bound_records.items():
            if attachment_id in supplying_attachment_ids or not any(
                _available_table_confidence_is_sufficient(record, table)
                for table in record.tables
            ):
                continue
            local_issues = tuple(
                item
                for item in record.issues
                if item.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
            )
            has_other_hard_issue = any(
                item.disposition == "INCOMPLETE"
                and item.code != "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
                for item in record.issues
            )
            if (
                local_issues
                and not has_other_hard_issue
                and all(
                    local_absence_is_resolved(item, attachment_id=attachment_id)
                    for item in local_issues
                )
            ):
                newly_supplying.add(attachment_id)
        if not newly_supplying:
            break
        supplying_attachment_ids.update(newly_supplying)

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
    else:
        # Match the direct build's diagnostic for an unestablished table in
        # extraction/validation results, not a claim about physical source
        # contents. Earlier attachment issues remain visible and blocking.
        if not not_applicable:
            issues.append(
                _issue(
                    "QUANTITATIVE_TABLE_NOT_ESTABLISHED",
                    "INCOMPLETE",
                    "현재 manifest의 추출·검증 결과에서 정량평가표나 명시적 비적용 근거를 확보하지 못했습니다.",
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
