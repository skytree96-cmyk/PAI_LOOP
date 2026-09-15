"""Pure, same-attachment reference tracing for a source-bound performance row.

The caller must supply criterion/table regions already established by its source
validator, including sibling regions. RESOLVED proves only the explicit reference
chain: it is not a rule-validation result, a company fact, or permission to restamp
a stored extraction. This module neither mutates candidates nor performs I/O.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata


MAX_SOURCE_CHARACTERS = 200_000
MAX_REFERENCE_MARKERS = 256
MAX_BLOCKS = 2
MAX_CONTEXT_CHARACTERS = 2_000
MAX_CONDITION_CHARACTERS = 500
MAX_CONDITIONS = 20
_PROOF_VERSION = "performance-explicit-form-chain-1"
_SHA = re.compile(r"[0-9a-f]{64}")
_REF = re.compile(
    r"(?P<open>[\[【〈<])\s*(?P<kind>별지\s*서식|서식|별첨|첨부)\s*"
    r"(?P<number>\d{1,3}(?:-\d{1,2})?)(?:\s*호)?\s*(?P<close>[\]】〉>])"
)
_PAIRS = {"[": "]", "【": "】", "〈": "〉", "<": ">"}
_ACTION = re.compile(r"참조|참고|제출|첨부|작성|기재")
_EXTERNAL = re.compile(r"다른\s*(?:첨부|문서)|별도\s*(?:파일|첨부파일)|외부\s*(?:문서|파일)|https?://|\.pdf\b", re.I)
_NON_BINDING = re.compile(r"예시|견본|(?:참조|참고|적용|제출)\s*(?:하지|안)|무관|미적용|삭제된|폐기된")
_SENTENCE_END = re.compile(r"(?:다|함|음|됨)[.!?](?=\s|$)")
_REFERENCE_HINT = re.compile(r"(?:별지\s*서식|서식|별첨|첨부)\s*\d+")


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class PerformanceReferenceOwner:
    attachment_id: str
    metric: str
    table_span: SourceSpan
    criterion_span: SourceSpan
    sibling_criterion_spans: tuple[SourceSpan, ...] = ()


@dataclass(frozen=True)
class ReferenceEdge:
    reference: SourceSpan
    target_heading: SourceSpan
    target_block: SourceSpan
    form_key: str


@dataclass(frozen=True)
class PerformanceReferenceResolution:
    status: str
    reason: str | None
    source_sha256: str
    owner: PerformanceReferenceOwner
    edges: tuple[ReferenceEdge, ...] = ()
    condition_spans: tuple[SourceSpan, ...] = ()
    proof_version: str = _PROOF_VERSION
    proof_sha256: str | None = None
    # Neither a resolved chain nor its digest authorizes persistent records.
    persistence_eligible: bool = False


def _compact(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _unique(compact_source: str, quote: str) -> bool:
    needle = _compact(quote)
    first = compact_source.find(needle)
    return bool(needle) and first >= 0 and compact_source.find(needle, first + 1) < 0


def _span(source: str, start: int, end: int) -> SourceSpan:
    return SourceSpan(start, end, source[start:end])


def _valid_span(source: str, span: SourceSpan) -> bool:
    return (isinstance(span, SourceSpan) and type(span.start) is int and type(span.end) is int
            and 0 <= span.start < span.end <= len(source)
            and isinstance(span.quote, str) and span.quote == source[span.start:span.end]
            and bool(span.quote.strip()))


def _inside(inner: SourceSpan, outer: SourceSpan) -> bool:
    return outer.start <= inner.start < inner.end <= outer.end


def _overlaps(left: SourceSpan, right: SourceSpan) -> bool:
    return max(left.start, right.start) < min(left.end, right.end)


def _key(match: re.Match[str]) -> str:
    return re.sub(r"\s+", "", match["kind"]) + ":" + match["number"]


def _heading(source: str, match: re.Match[str]) -> SourceSpan | None:
    """A standalone title, not an inline citation or a flattened document guess."""
    start = source.rfind("\n", 0, match.start()) + 1
    end = source.find("\n", match.end())
    end = len(source) if end < 0 else end
    title = source[match.end():end].strip()
    if (source[start:match.start()].strip() or not title or len(title) > 120
            or _REF.search(title) or _ACTION.search(title) or re.search(r"[.…]{2,}", title)):
        return None
    return _span(source, match.start(), end)


def _form_kind(heading: str) -> str | None:
    text = _compact(heading)
    if _NON_BINDING.search(text):
        return None
    if "실적증명서" in text or "실적증명원" in text:
        return "CERTIFICATE"
    if "실적" in text and any(word in text for word in ("수행", "이행", "사업")):
        return "PERFORMANCE"
    return None


def _actual_form_body(kind: str, body: str) -> bool:
    # Form titles also occur in contents lists. Require actual field structure;
    # a title's position or the word '실적' alone is never enough.
    text = _compact(body)
    required = (("사업명",), ("계약금액",), ("사업기간", "용역수행기간", "교육기간"), ("발주처", "발주기관"))
    if kind == "CERTIFICATE":
        required = (("신청인",), ("계약금액",), ("증명서발급기관", "증명서발급"), ("실적내용", "이행실적"))
    return all(any(word in text for word in alternatives) for alternatives in required)


def _chunks(source: str, block: SourceSpan) -> tuple[SourceSpan, ...] | None:
    """Retain the entire block; only close a chunk at a full sentence boundary."""
    output = []
    start = block.start
    while block.end - start > MAX_CONDITION_CHARACTERS:
        ends = [start + match.end() for match in _SENTENCE_END.finditer(source[start:block.end])
                if match.end() <= MAX_CONDITION_CHARACTERS]
        if not ends:
            return None
        end = max(ends)
        output.append(_span(source, start, end))
        start = end
    if source[start:block.end].strip():
        output.append(_span(source, start, block.end))
    elif output:
        # Whitespace also stays accounted for; an over-limit final chunk fails.
        previous = output.pop()
        output.append(_span(source, previous.start, block.end))
    if not output or any(len(span.quote) > MAX_CONDITION_CHARACTERS for span in output):
        return None
    return tuple(output)


def resolve_performance_reference_context(
    *, source_text: str, source_sha256: str, source_attachment_id: str,
    owner: PerformanceReferenceOwner,
) -> PerformanceReferenceResolution:
    """Resolve at most one form and its explicitly referenced certificate.

    Caller-established row bounds remain a prerequisite. Exact source slices,
    non-overlapping sibling regions, unique normalized row text, independently
    recognizable form bodies, and closed peer-title boundaries are rechecked.
    Any unresolved link drops *all* proposed additions, never just the bad tail.
    """
    def blocked(reason: str, status: str = "BLOCKED") -> PerformanceReferenceResolution:
        return PerformanceReferenceResolution(status, reason, source_sha256, owner)

    if not isinstance(source_text, str) or not source_text or len(source_text) > MAX_SOURCE_CHARACTERS:
        return blocked("SOURCE_SIZE_INVALID")
    if not isinstance(source_sha256, str) or not _SHA.fullmatch(source_sha256) or hashlib.sha256(source_text.encode("utf-8")).hexdigest() != source_sha256:
        return blocked("SOURCE_SHA256_MISMATCH")
    if (not isinstance(owner, PerformanceReferenceOwner) or not isinstance(owner.attachment_id, str)
            or not owner.attachment_id.strip() or len(owner.attachment_id) > 120
            or owner.attachment_id != source_attachment_id
            or owner.metric not in {"PERFORMANCE_COUNT", "PERFORMANCE_AMOUNT"}
            or not _valid_span(source_text, owner.table_span)
            or not _valid_span(source_text, owner.criterion_span)
            or not _inside(owner.criterion_span, owner.table_span)
            or len(owner.criterion_span.quote) > 2_000):
        return blocked("OWNER_SOURCE_BINDING_INVALID")
    compact_source = _compact(source_text)
    if (not re.search(r"실적|수행\s*능력|이행\s*능력", owner.criterion_span.quote)
            or not _unique(compact_source, owner.criterion_span.quote)
            or not isinstance(owner.sibling_criterion_spans, tuple)
            or len(owner.sibling_criterion_spans) > 100
            or any(not _valid_span(source_text, sibling) or not _inside(sibling, owner.table_span)
                   or _overlaps(sibling, owner.criterion_span) for sibling in owner.sibling_criterion_spans)):
        return blocked("OWNER_REGION_AMBIGUOUS")
    if any(_overlaps(left, right) for i, left in enumerate(owner.sibling_criterion_spans)
           for right in owner.sibling_criterion_spans[i + 1:]):
        return blocked("OWNER_REGION_AMBIGUOUS")
    markers = list(_REF.finditer(source_text))
    if len(markers) > MAX_REFERENCE_MARKERS:
        return blocked("REFERENCE_LIMIT")
    if any(_PAIRS[m["open"]] != m["close"] for m in markers):
        return blocked("REFERENCE_MARKER_MALFORMED")
    starts = [m for m in markers if owner.criterion_span.start <= m.start() and m.end() <= owner.criterion_span.end]
    if not starts:
        if _REFERENCE_HINT.search(owner.criterion_span.quote):
            return blocked("REFERENCE_MARKER_UNSUPPORTED")
        return blocked("NO_EXPLICIT_REFERENCE", "NO_EXPLICIT_REFERENCE")
    if len(starts) != 1:
        return blocked("OWNER_REFERENCE_AMBIGUOUS")
    if any(not any(m.start() <= hint.start() < hint.end() <= m.end() for m in starts)
           for hint in _REFERENCE_HINT.finditer(source_text, owner.criterion_span.start, owner.criterion_span.end)):
        return blocked("REFERENCE_MARKER_UNSUPPORTED")
    # Even an unrecognized standalone marker fences a block. Skipping it could
    # silently pull the next form's instructions into this recognition scope.
    standalone = [m for m in markers if not source_text[source_text.rfind("\n", 0, m.start()) + 1:m.start()].strip()]
    edges: list[ReferenceEdge] = []
    conditions: list[SourceSpan] = []
    seen: set[str] = set()
    pending = starts[0]
    container = owner.criterion_span
    while True:
        key = _key(pending)
        if key in seen or len(edges) >= MAX_BLOCKS:
            return blocked("REFERENCE_CYCLE_OR_DEPTH")
        seen.add(key)
        context = source_text[max(container.start, pending.start() - 60):min(container.end, pending.end() + 150)]
        if (_EXTERNAL.search(container.quote) or _NON_BINDING.search(container.quote) or not _ACTION.search(context)
                or edges and not re.search(r"실적\s*증명(?:서|원)", context)):
            return blocked("REFERENCE_ACTION_OR_ATTACHMENT_UNPROVEN")
        candidates = []
        for index, marker in enumerate(standalone):
            if _key(marker) != key:
                continue
            heading = _heading(source_text, marker)
            # A contents entry closes at the very next title and has no form
            # fields. Do not skip intervening headings to manufacture a body.
            next_marker = standalone[index + 1] if index + 1 < len(standalone) else None
            following = _heading(source_text, next_marker) if next_marker is not None else None
            end = next_marker.start() if next_marker is not None else len(source_text)
            heading_end = source_text.find("\n", marker.end())
            heading_end = len(source_text) if heading_end < 0 else heading_end
            body = source_text[heading_end:end]
            # Only an empty/page-number contents body can be skipped. An
            # incomplete form may contain stricter conditions than a later
            # complete form with the same number; never silently discard it.
            if not body.strip() or re.fullmatch(r"\s*-?\s*\d{1,3}\s*-?\s*", body):
                continue
            if heading is None or (kind := _form_kind(heading.quote)) is None:
                return blocked("FORM_HEADING_UNPROVEN")
            block = _span(source_text, heading.start, end)
            if not _actual_form_body(kind, body):
                return blocked("FORM_INCOMPLETE_OR_AMBIGUOUS")
            candidates.append((kind, heading, block, following))
        if len(candidates) != 1:
            return blocked("FORM_MISSING_OR_DUPLICATE")
        kind, heading, block, following = candidates[0]
        if (following is None or _overlaps(block, owner.table_span)
                or kind != ("PERFORMANCE" if not edges else "CERTIFICATE")):
            return blocked("FORM_BOUNDARY_OR_ROLE_UNPROVEN")
        # No truncation: the complete linked body must fit before chunking.
        if sum(len(edge.target_block.quote) for edge in edges) + len(block.quote) > MAX_CONTEXT_CHARACTERS:
            return blocked("REFERENCE_CONTEXT_TOO_LONG")
        chunks = _chunks(source_text, block)
        if (chunks is None or len(conditions) + len(chunks) > MAX_CONDITIONS
                or any(len(_compact(span.quote)) < 8 or not _unique(compact_source, span.quote) for span in chunks)):
            return blocked("CONDITION_BOUNDARY_OR_ANCHOR_UNPROVEN")
        edges.append(ReferenceEdge(_span(source_text, pending.start(), pending.end()), heading, block, key))
        conditions.extend(chunks)
        nested = [m for m in markers if heading.end <= m.start() and m.end() <= block.end]
        body_hints = list(_REFERENCE_HINT.finditer(source_text, heading.end, block.end))
        if any(not any(m.start() <= hint.start() < hint.end() <= m.end() for m in nested) for hint in body_hints):
            return blocked("REFERENCE_MARKER_UNSUPPORTED")
        if not nested:
            break
        if len(nested) != 1:
            return blocked("NESTED_REFERENCE_AMBIGUOUS")
        pending, container = nested[0], block
    result = PerformanceReferenceResolution("RESOLVED", None, source_sha256, owner, tuple(edges), tuple(conditions))
    proof = hashlib.sha256(json.dumps(asdict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return PerformanceReferenceResolution("RESOLVED", None, source_sha256, owner, tuple(edges), tuple(conditions), proof_sha256=proof)


def verify_performance_reference_context(
    result: PerformanceReferenceResolution, *, source_text: str, source_attachment_id: str,
) -> bool:
    """Recompute all source/edge/chunk proofs; a matching digest alone is not authority."""
    return (isinstance(result, PerformanceReferenceResolution) and result.status == "RESOLVED"
            and result == resolve_performance_reference_context(source_text=source_text,
                source_sha256=result.source_sha256, source_attachment_id=source_attachment_id, owner=result.owner))
