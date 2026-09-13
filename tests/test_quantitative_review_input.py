import copy
from dataclasses import replace
import hashlib

import pytest

from pai_loop.quantitative_review_input import (
    QuantitativeReviewInput, ReviewedSourcePage, build_quantitative_review_input,
    quantitative_probe_instruction,
)


def test_selection_keeps_original_pages_narrative_and_linked_form_quotes():
    source = "[PAGE 1]\nSYN scoring: 5 contracts = 5 points; see form 4.\n[PAGE 2]\nSYN task prose.\n[PAGE 3]\nSYN form 4: at least 50 participants."
    review = build_quantitative_review_input(
        source, reviewed_pages=[3, 1], expected_quotes=["5 contracts = 5 points", "at least 50 participants"],
    )
    assert [page.page for page in review.pages] == [1, 3]
    assert review.canonical_text == source
    assert "[PAGE 3]" in review.selected_source
    assert "SYN task prose" not in review.selected_source
    assert "OMITTED PAGES" in review.selected_source
    assert review.audit()["attachment_coverage_complete"] is False
    assert review.audit()["persistence_eligible"] is False
    assert "requirements=[]" in quantitative_probe_instruction(review)


def test_missing_recognition_quote_prevents_request_preparation():
    with pytest.raises(ValueError, match="OMITS_REQUIRED_QUOTE"):
        build_quantitative_review_input(
            "[PAGE 1]\nSYN score 5.\n[PAGE 2]\nSYN completed public contracts only.",
            reviewed_pages=[1], expected_quotes=["completed public contracts only"],
        )


@pytest.mark.parametrize("source,pages", [
    ("no page markers", [1]), ("[PAGE 2]\nx\n[PAGE 1]\ny", [1]),
    ("[PAGE 1]\nx\n[PAGE 1]\ny", [1]), ("[PAGE 1]\nx", []),
    ("[PAGE 1]\nx", [True]), ("[PAGE 1]\nx", [1, 1]),
    ("[PAGE 1]\nx", [2]),
])
def test_invalid_or_ambiguous_source_selection_is_rejected(source, pages):
    with pytest.raises(ValueError):
        build_quantitative_review_input(source, reviewed_pages=pages)


def test_no_table_is_required_and_even_full_selection_is_only_a_probe():
    review = build_quantitative_review_input(
        "[PAGE 1]\nSYN score = amount / base; 3 year completed contracts only.",
        reviewed_pages=[1], expected_quotes=["score = amount / base"],
    )
    assert review.audit()["omitted_pages"] == []
    assert review.audit()["attachment_coverage_complete"] is False
    assert review.selected_source == review.canonical_text


def test_forged_page_offsets_are_rejected_before_a_probe():
    forged = QuantitativeReviewInput("[PAGE 1]\nSYN evidence", (ReviewedSourcePage(1, 0, 8),), (1,), ())
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        quantitative_probe_instruction(forged)


def test_required_quote_cannot_be_forged_by_joining_omitted_pages():
    source = "[PAGE 1]\nSYN first\n[PAGE 2]\nSYN ignored\n[PAGE 3]\nSYN last"
    with pytest.raises(ValueError, match="NOT_IN_SOURCE"):
        build_quantitative_review_input(source, reviewed_pages=[1, 3], expected_quotes=["SYN firstSYN last"])


def _structure(source):
    start = source.index("SYN-BBB0")
    values = ["SYN-BBB0", "100%", "10.0"]
    cells = []
    offsets = []
    for index, value in enumerate(values):
        cells.append({"id": f"c{index}", "region_id": "r1", "ownership_status": "BOUND",
                      "source_spans": [{"start": start, "end": start + len(value), "text": value}]})
        if index:
            offsets.append(start)
        start += len(value)
    digest = hashlib.sha256(source.encode()).hexdigest()
    return {"version": "pdf-source-structure-1", "pdf_sha256": "a" * 64, "source_sha256": digest,
            "canonical_sha256": digest, "canonical_text_preserved": True,
            "canonical_run_mapping_passed": True, "boundaries_proven": True,
            "cell_transition_offsets": offsets,
            "pages": [{"page_number": 1, "geometry_status": "PARTIAL_BOUND_CELLS",
                       "run_mapping_status": "EXACT_COMPLETE_STREAM",
                       "glyph_mapping_status": "EXACT_COMPLETE_NONWHITE_STREAM",
                       "cells": cells, "cell_transition_offsets": offsets.copy()}]}


def test_proven_cell_boundaries_format_display_without_changing_canonical_or_quotes():
    source = "[PAGE 1]\nSYN-BBB0100%10.0\nSYN narrative"
    structure = _structure(source)
    review = build_quantitative_review_input(source, reviewed_pages=[1], source_structure=structure, expected_pdf_sha256="a" * 64,
                                             expected_quotes=["SYN-BBB0100%10.0"])
    assert "SYN-BBB0\n100%\n10.0" in review.selected_source
    assert review.canonical_text == source
    assert review.selected_source.replace("\n", "") == source.replace("\n", "")
    assert review.audit()["inserted_cell_boundary_newlines"] == 2
    # The passed mutable dictionary cannot change a frozen reviewed request.
    structure["pages"][0]["cells"].clear()
    review.validate()


@pytest.mark.parametrize("mutation", ["numeric_interior", "overlap", "wrong_source", "unproven", "wrong_span", "wrong_page"])
def test_unproven_or_unbound_boundary_cannot_format_source(mutation):
    source = "[PAGE 1]\nSYN-BBB0100%10.0"
    proof = _structure(source)
    page = proof["pages"][0]
    if mutation == "numeric_interior":
        offset = source.index("10.0") + 1
        page["cell_transition_offsets"].append(offset)
        proof["cell_transition_offsets"].append(offset)
    elif mutation == "overlap":
        cell = copy.deepcopy(page["cells"][0])
        cell["id"] = "overlap"
        page["cells"].append(cell)
    elif mutation == "wrong_source":
        proof["source_sha256"] = "0" * 64
    elif mutation == "unproven":
        page["cells"][0]["ownership_status"] = "UNPROVEN"
    elif mutation == "wrong_span":
        page["cells"][0]["source_spans"][0]["text"] = "SYN-AAA0"
    else:
        page["page_number"] = 2
    with pytest.raises(ValueError, match="REVIEW_STRUCTURE"):
        build_quantitative_review_input(source, reviewed_pages=[1], source_structure=proof, expected_pdf_sha256="a" * 64)


def test_dataclass_offset_tampering_is_rejected_and_input_size_is_bounded():
    source = "[PAGE 1]\nSYN-BBB0100%10.0"
    review = build_quantitative_review_input(source, reviewed_pages=[1], source_structure=_structure(source), expected_pdf_sha256="a" * 64)
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        replace(review, cell_boundary_offsets=(source.index("10.0") + 1,)).validate()
    with pytest.raises(ValueError, match="CHARACTER_LIMIT"):
        build_quantitative_review_input("[PAGE 1]\n" + "x" * 2_000_000, reviewed_pages=[1])


@pytest.mark.parametrize("pdf_sha", [None, "b" * 64, "invalid"])
def test_geometry_requires_binding_to_the_reviewed_pdf_bytes(pdf_sha):
    source = "[PAGE 1]\nSYN-BBB0100%10.0"
    with pytest.raises(ValueError, match="PDF_MISMATCH"):
        build_quantitative_review_input(source, reviewed_pages=[1], source_structure=_structure(source),
                                        expected_pdf_sha256=pdf_sha)
