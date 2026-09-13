"""Explicitly reviewed quantitative input, separate from attachment coverage.

This module selects complete original PDF pages for a bounded diagnostic. It
does not classify an attachment as irrelevant, modify its canonical evidence,
or provide eligibility/manifest completion. No I/O or provider calls occur here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Iterable, Mapping


QUANTITATIVE_PROBE_PROMPT_VERSION = "pai-loop-quantitative-probe-0.1"
_PAGE = re.compile(r"(?m)^\[PAGE ([1-9]\d*)\]\n")
_MAX_SOURCE_CHARACTERS = 2_000_000
_MAX_STRUCTURE_BYTES = 512 * 1024


def _cell_boundaries(source: str, pages: tuple[ReviewedSourcePage, ...], proof: str,
                     pdf_sha256: str | None) -> tuple[int, ...]:
    """Rebind local geometry output; offsets alone never authorize formatting.

    This verifies source ownership, not PDF geometry independently. The caller
    supplies the result of extract_pdf_source_structure, never model output.
    Its use remains a non-persistent diagnostic with canonical quote validation.
    """
    if not proof:
        return ()
    if len(proof.encode("utf-8")) > _MAX_STRUCTURE_BYTES:
        raise ValueError("REVIEW_STRUCTURE_LIMIT")
    structure = json.loads(proof)
    if (not isinstance(pdf_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", pdf_sha256)
            or structure.get("pdf_sha256") != pdf_sha256):
        raise ValueError("REVIEW_STRUCTURE_PDF_MISMATCH")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if (structure.get("version") != "pdf-source-structure-1"
            or structure.get("source_sha256") != digest
            or structure.get("canonical_sha256") != digest
            or structure.get("canonical_text_preserved") is not True
            or structure.get("canonical_run_mapping_passed") is not True):
        raise ValueError("REVIEW_STRUCTURE_SOURCE_MISMATCH")
    selected = {page.page: page for page in pages}
    transitions: list[int] = []
    seen_pages: set[int] = set()
    for item in structure.get("pages", []):
        number = item.get("page_number")
        if type(number) is not int or number not in selected or number in seen_pages:
            raise ValueError("REVIEW_STRUCTURE_PAGE_MISMATCH")
        seen_pages.add(number)
        if item.get("geometry_status") != "PARTIAL_BOUND_CELLS":
            if item.get("cell_transition_offsets"):
                raise ValueError("REVIEW_STRUCTURE_UNPROVEN_BOUNDARY")
            continue
        if (item.get("run_mapping_status") != "EXACT_COMPLETE_STREAM"
                or item.get("glyph_mapping_status") != "EXACT_COMPLETE_NONWHITE_STREAM"):
            raise ValueError("REVIEW_STRUCTURE_UNPROVEN_BOUNDARY")
        page = selected[number]
        owners: dict[int, tuple[str, str]] = {}
        cell_ids: set[str] = set()
        for cell in item.get("cells", []):
            if cell.get("ownership_status") != "BOUND":
                if cell.get("source_spans"):
                    raise ValueError("REVIEW_STRUCTURE_UNPROVEN_BOUNDARY")
                continue
            cell_id, region_id = cell.get("id"), cell.get("region_id")
            if not isinstance(cell_id, str) or not isinstance(region_id, str) or cell_id in cell_ids:
                raise ValueError("REVIEW_STRUCTURE_CELL_ID_INVALID")
            cell_ids.add(cell_id)
            for span in cell.get("source_spans", []):
                start, end = span.get("start"), span.get("end")
                if (type(start) is not int or type(end) is not int
                        or not page.start <= start < end <= page.end
                        or source[start:end] != span.get("text")):
                    raise ValueError("REVIEW_STRUCTURE_SPAN_MISMATCH")
                for offset in range(start, end):
                    if source[offset].isspace():
                        continue
                    if offset in owners:
                        raise ValueError("REVIEW_STRUCTURE_OVERLAPPING_OWNERSHIP")
                    owners[offset] = (cell_id, region_id)
        ordered = sorted(owners)
        derived = [right for left, right in zip(ordered, ordered[1:])
                   if owners[left][0] != owners[right][0]
                   and owners[left][1] == owners[right][1]
                   and not source[left + 1:right].strip()]
        if derived != item.get("cell_transition_offsets", []):
            raise ValueError("REVIEW_STRUCTURE_BOUNDARY_MISMATCH")
        transitions.extend(derived)
    result = tuple(sorted(transitions))
    if list(result) != structure.get("cell_transition_offsets", []) or bool(result) != structure.get("boundaries_proven"):
        raise ValueError("REVIEW_STRUCTURE_BOUNDARY_MISMATCH")
    return result


@dataclass(frozen=True, slots=True)
class ReviewedSourcePage:
    page: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class QuantitativeReviewInput:
    canonical_text: str = field(repr=False)
    pages: tuple[ReviewedSourcePage, ...]
    all_pages: tuple[int, ...]
    expected_quotes: tuple[str, ...] = field(repr=False)
    structure_proof: str = field(default="", repr=False)
    cell_boundary_offsets: tuple[int, ...] = ()
    expected_pdf_sha256: str | None = None

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest()

    @property
    def selected_source(self) -> str:
        # Omission markers are data framing, never source evidence. Validation
        # always uses canonical_text, preventing a quote across an omitted page.
        parts = []
        previous_end = None
        for page in self.pages:
            if previous_end is not None and previous_end != page.start:
                parts.append("\n[OMITTED PAGES: NOT ANALYZED IN THIS PROBE]\n")
            cursor = page.start
            for offset in self.cell_boundary_offsets:
                if page.start < offset < page.end:
                    parts.append(self.canonical_text[cursor:offset])
                    parts.append("\n")
                    cursor = offset
            parts.append(self.canonical_text[cursor:page.end])
            previous_end = page.end
        return "".join(parts)

    def audit(self) -> dict[str, object]:
        selected = tuple(page.page for page in self.pages)
        source_characters = sum(page.end - page.start for page in self.pages)
        return {
            "purpose": "QUANTITATIVE_PROBE_ONLY",
            "persistence_eligible": False,
            "attachment_coverage_complete": False,
            "canonical_sha256": self.source_sha256,
            "pdf_sha256": self.expected_pdf_sha256,
            "selected_pages": list(selected),
            "omitted_pages": [page for page in self.all_pages if page not in selected],
            "original_characters": len(self.canonical_text),
            "selected_source_characters": source_characters,
            "expected_quote_count": len(self.expected_quotes),
            "all_expected_quotes_retained": True,
            "inserted_cell_boundary_newlines": len(self.cell_boundary_offsets),
            "selected_display_characters": len(self.selected_source),
            "structure_proof_sha256": hashlib.sha256(self.structure_proof.encode("utf-8")).hexdigest() if self.structure_proof else None,
        }

    def validate(self) -> None:
        rebuilt = build_quantitative_review_input(
            self.canonical_text, reviewed_pages=[p.page for p in self.pages],
            expected_quotes=self.expected_quotes,
            source_structure=json.loads(self.structure_proof) if self.structure_proof else None,
            expected_pdf_sha256=self.expected_pdf_sha256,
        )
        if rebuilt != self:
            raise ValueError("REVIEW_INPUT_SOURCE_BINDING_MISMATCH")


def build_quantitative_review_input(
    canonical_text: str, *, reviewed_pages: Iterable[int],
    expected_quotes: Iterable[str] = (),
    source_structure: Mapping[str, object] | None = None,
    expected_pdf_sha256: str | None = None,
) -> QuantitativeReviewInput:
    """Require an explicit page review; no keyword-driven deletion is allowed.

    expected_quotes is a reviewed checklist, not an assertion that every unknown
    requirement has been found. A missing quote prevents even a diagnostic call.
    """
    if not isinstance(canonical_text, str) or not canonical_text.strip():
        raise ValueError("EMPTY_REVIEW_SOURCE")
    if len(canonical_text) > _MAX_SOURCE_CHARACTERS:
        raise ValueError("REVIEW_SOURCE_CHARACTER_LIMIT")
    markers = list(_PAGE.finditer(canonical_text))
    if not markers or canonical_text[:markers[0].start()].strip():
        raise ValueError("REVIEW_SOURCE_REQUIRES_PAGE_MARKERS")
    numbers = tuple(int(marker[1]) for marker in markers)
    if numbers != tuple(sorted(set(numbers))):
        raise ValueError("REVIEW_SOURCE_PAGE_ORDER_INVALID")
    requested = tuple(reviewed_pages)
    if not requested or any(type(page) is not int for page in requested):
        raise ValueError("REVIEWED_PAGES_REQUIRED")
    if len(set(requested)) != len(requested) or set(requested) - set(numbers):
        raise ValueError("REVIEWED_PAGES_INVALID")
    selected = tuple(
        ReviewedSourcePage(int(marker[1]), marker.start(),
                           markers[i+1].start() if i+1 < len(markers) else len(canonical_text))
        for i, marker in enumerate(markers) if int(marker[1]) in requested
    )
    quotes = tuple(expected_quotes)
    for quote in quotes:
        if not isinstance(quote, str) or not quote.strip() or quote not in canonical_text:
            raise ValueError("REVIEW_QUOTE_NOT_IN_SOURCE")
        # Permit a quote spanning physically consecutive retained pages only.
        intervals: list[tuple[int, int]] = []
        for page in selected:
            if intervals and intervals[-1][1] == page.start:
                intervals[-1] = (intervals[-1][0], page.end)
            else:
                intervals.append((page.start, page.end))
        if not any(quote in canonical_text[start:end] for start, end in intervals):
            raise ValueError("REVIEW_SELECTION_OMITS_REQUIRED_QUOTE")
    proof = json.dumps(source_structure, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if source_structure is not None else ""
    boundaries = _cell_boundaries(canonical_text, selected, proof, expected_pdf_sha256)
    return QuantitativeReviewInput(canonical_text, selected, numbers, quotes, proof, boundaries, expected_pdf_sha256)


def quantitative_probe_instruction(review_input: QuantitativeReviewInput) -> str:
    review_input.validate()
    return (
        "This is an explicitly scoped QUANTITATIVE-ONLY DIAGNOSTIC. Extract only "
        "objective scoring rules and their own recognition conditions, references, "
        "footnotes, subtotals and formulas. A physical table is not required. "
        "Return requirements=[]; do not extract qualitative evaluation, general task "
        "instructions or submission checklists. Such omissions do not establish "
        "eligibility or complete attachment coverage. Preserve an applicable scoring "
        "condition even when it appears in a task description or certificate form. "
        "Do not claim that omitted pages contain no scoring conditions. Any reference "
        "whose applicable target is not present in SOURCE must remain an explicit "
        "missing source gap. Never calculate a company's score or use company facts. "
        "SOURCE may contain additional newlines at physically proven PDF cell "
        "transitions; its characters and original reading order are otherwise retained. "
        "These partial boundaries do not establish row semantics or missing operators. "
        "Source-selection audit: "
        + json.dumps(review_input.audit(), ensure_ascii=False, separators=(",", ":"))
    )
