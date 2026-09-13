"""Synthetic source-ownership tests: geometry must never fabricate evidence."""
from __future__ import annotations

import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pai_loop import pdf_source_structure as structure


def _pdf(commands: str, *, rotation: int = 0, second_page: str | None = None) -> bytes:
    writer = PdfWriter()
    for source in ([commands] if second_page is None else [commands, second_page]):
        page = writer.add_blank_page(width=600, height=500)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(source.encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        if rotation:
            page.rotate(rotation)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _canonical(raw: bytes) -> str:
    parts = []
    for number, page in enumerate(PdfReader(io.BytesIO(raw)).pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            parts.append(f"\n[PAGE {number}]\n{text}")
    return "".join(parts).strip()


def _line(x0, y0, x1, y1):
    return f"0.5 w {x0} {y0} m {x1} {y1} l S\n"


def _text(text, x, y):
    return f"BT /F1 9 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET\n"


def _grid(*, opened=False, left=40, width=240, inconsistent=False):
    xs = [left + width * value / 3 for value in range(4)]
    ys = [100, 130, 160, 190]
    code = "".join(_line(xs[0], y, xs[-1] + (8 if inconsistent and y == 130 else 0), y) for y in ys)
    code += "".join(_line(x, ys[0], x, ys[-1]) for x in (xs[1:-1] if opened else xs))
    return code, xs, ys


def _run(commands, **kwargs):
    raw = _pdf(commands)
    source = _canonical(raw)
    result = structure.extract_pdf_source_structure(raw, source, **kwargs)
    assert result["canonical_text_preserved"]
    intervals = []
    for page in result["pages"]:
        for cell in page["cells"]:
            for span in cell["source_spans"]:
                assert source[span["start"]:span["end"]] == span["text"]
                intervals.append((span["start"], span["end"]))
    intervals.sort()
    assert all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:]))
    return source, result


def _owned(result):
    return [span["text"] for page in result["pages"] for cell in page["cells"] for span in cell["source_spans"]]


def test_repeated_text_is_bound_by_full_ordered_stream_not_first_hit():
    lines, xs, _ = _grid()
    source, result = _run(lines + _text("SAME", xs[2]+5, 171) + _text("SAME", xs[0]+5, 141)
                           + _text("SAME", xs[1]+5, 111))
    assert _owned(result) == ["SAME", "SAME", "SAME"]
    starts = [s["start"] for cell in result["pages"][0]["cells"] for s in cell["source_spans"]]
    assert starts == sorted(set(starts))
    assert structure._glyph_source_offsets("AA AA AA", [{"text": "AA"}, {"text": "AA"}], 0) is None
    assert structure._glyph_source_offsets("A0 100%", [{"text": "A0"}, {"text": "10%"}], 0) is None


def test_merged_cell_has_single_owner_and_never_fills_other_rows():
    commands = _line(40, 100, 280, 100) + _line(40, 160, 280, 160) + _line(120, 130, 280, 130)
    commands += "".join(_line(x, 100, x, 160) for x in [40, 120, 200, 280])
    commands += _text("shared", 50, 137) + _text("A", 130, 140) + _text("B", 130, 110)
    _, result = _run(commands)
    cells = result["pages"][0]["cells"]
    shared = [cell for cell in cells if any(span["text"] == "shared" for span in cell["source_spans"])]
    assert len(shared) == 1
    assert shared[0]["bbox"][3] - shared[0]["bbox"][1] == 60
    assert sum(cell["ownership_status"] == "EMPTY" for cell in cells) == 2
    assert all(region["semantic_relationships"] == "UNPROVEN" for region in result["pages"][0]["regions"])


def test_open_envelope_is_explicit_and_adjacent_prose_is_not_owned():
    commands, xs, _ = _grid(opened=True)
    commands += _text("grade", xs[0]+5, 141) + _text("95%", xs[1]+5, 141) + _text("5", xs[2]+5, 141)
    commands += _text("outside prose", xs[-1]+10, 141)
    _, result = _run(commands)
    assert _owned(result) == ["grade", "95%", "5"]
    page = result["pages"][0]
    assert page["regions"][0]["geometry_proof"]["kind"] == "OPEN_BORDER_ENVELOPE"
    assert page["regions"][0]["geometry_proof"]["inferred_sides"] == ["left", "right"]
    assert page["regions"][0]["geometry_proof"]["painted_outer_borders"] is False
    assert page["unassigned_nonwhite_characters"] == len("outsideprose")


def test_two_up_independent_components_keep_source_order_and_gutter_empty():
    left, xs, _ = _grid(opened=True, left=20, width=210)
    right, xr, _ = _grid(opened=True, left=330, width=210)
    _, result = _run(left + right + _text("RIGHT", xr[0]+5, 141) + _text("LEFT", xs[0]+5, 141)
                     + _text("gutter", 250, 141))
    assert _owned(result) == ["RIGHT", "LEFT"]
    regions = result["pages"][0]["regions"]
    assert len(regions) == 2
    assert not structure._overlap(regions[0]["bbox"], regions[1]["bbox"])


def test_inconsistent_outer_endpoints_do_not_create_an_envelope():
    commands, xs, _ = _grid(opened=True, inconsistent=True)
    _, result = _run(commands + _text("outside-column", xs[2]+2, 141))
    assert all(region["geometry_proof"]["kind"] != "OPEN_BORDER_ENVELOPE" for region in result["pages"][0]["regions"])
    assert not _owned(result)


def test_regular_painted_micro_dashes_are_recovered_but_irregular_gaps_are_not():
    def edge(lo, hi):
        return {"object_type": "line", "stroke": True, "orientation": "h", "x0": lo, "x1": hi,
                "top": 40, "bottom": 40, "width": hi-lo, "height": 0, "dash": ([], 0)}
    regular = [edge(index * 1.2, index * 1.2 + .48) for index in range(12)]
    result = structure._rulings(regular)
    assert len(result) == 1 and result[0]["source_proof"] == "SEGMENTED_DASH_PATTERN"
    # A one-off missing border is not a regular dash pattern.
    pieces = structure._rulings([edge(0, 10), edge(10.7, 20)])
    assert [(part["x0"], part["x1"]) for part in pieces] == [(0, 10), (10.7, 20)]
    assert all(part["source_proof"] == "UNJOINED_PAINTED_PATH" for part in pieces)
    irregular = regular[:]
    irregular[4] = edge(4*1.2 + .1, 4*1.2 + .58)
    assert all(part["source_proof"] == "UNJOINED_PAINTED_PATH" for part in structure._rulings(irregular))


def test_rejected_join_keeps_painted_dividers_and_does_not_merge_neighboring_cells():
    commands = "".join(_line(40, y, 280, y) for y in [100, 130, 160, 190])
    commands += "".join(_line(x, 100, x, 190) for x in [40, 200, 280])
    commands += "".join(_line(120, lo, 120, hi) for lo, hi in [(100,129.6),(130.2,159.7),(160.1,190)])
    _, result = _run(commands + _text("LEFT", 50, 141) + _text("RIGHT", 130, 141))
    assert _owned(result) == ["LEFT", "RIGHT"]
    assert len(result["pages"][0]["cells"]) == 9
    assert len(result["cell_transition_offsets"]) == 1


@pytest.mark.parametrize("commands", ["", _text("Narrative: ratio = A / B * 100. See reference.", 40, 150)])
def test_narrative_formula_or_blank_page_is_preserved_without_claiming_no_quantitative_rules(commands):
    _, result = _run(commands)
    assert result["status"] == "UNPROVEN"
    assert result["semantic_validation"] == "NOT_PERFORMED"
    assert not _owned(result)


def test_rotated_page_keeps_source_but_cannot_supply_cells():
    commands, _, _ = _grid()
    raw = _pdf(commands + _text("R", 50, 141), rotation=90)
    result = structure.extract_pdf_source_structure(raw, _canonical(raw))
    assert result["canonical_text_preserved"]
    assert result["pages"][0]["diagnostics"] == ["ROTATED_PAGE_UNPROVEN"]
    assert not _owned(result)


def test_transformed_text_does_not_get_guessed_ownership():
    commands, _, _ = _grid()
    commands += "BT /F1 9 Tf 0 1 -1 0 50 140 Tm (ROTATE) Tj ET\n"
    _, result = _run(commands)
    assert result["pages"][0]["invalid_geometry_nonwhite_characters"] == len("ROTATE")
    assert not _owned(result)


def test_positive_scale_and_translation_keep_boxes_and_source_offsets_consistent():
    commands, xs, _ = _grid()
    _, result = _run("q 0.5 0 0 0.5 40 20 cm\n" + commands + _text("SCALE", xs[0]+5, 141) + "Q\n")
    assert _owned(result) == ["SCALE"]
    page = result["pages"][0]
    cell = next(cell for cell in page["cells"] if cell["ownership_status"] == "BOUND")
    assert cell["bbox"] == [60, 400, 100, 415]
    assert page["page_bbox"] == [0, 0, 600, 500]


def test_tampered_canonical_text_fails_closed_before_geometry():
    raw = _pdf(_text("A0 100%", 40, 140))
    result = structure.extract_pdf_source_structure(raw, _canonical(raw).replace("A0", "A1"))
    assert result["status"] == "UNPROVEN"
    assert result["diagnostics"] == ["CANONICAL_SOURCE_MISMATCH"]
    assert not result["pages"]


def test_selected_pages_have_original_page_numbers_hash_and_explicit_partial_scope():
    commands, xs, _ = _grid()
    raw = _pdf(_text("unselected narrative", 40, 140), second_page=commands+_text("same", xs[0]+5, 141))
    source = _canonical(raw)
    result = structure.extract_pdf_source_structure(raw, source, selected_pages=[2])
    assert result["mapping_scope"] == "SELECTED_PAGES"
    assert result["selected_pages"] == [2] and result["skipped_geometry_pages"] == [1]
    assert result["canonical_pages_verified"] == 2
    assert [page["page_number"] for page in result["pages"]] == [2]
    assert _owned(result) == ["same"]
    assert result["source_sha256"] == structure._sha(source)


@pytest.mark.parametrize("selection", [[0], [True], [2], [1, 1], list(range(1, 18))])
def test_invalid_page_selection_is_not_silently_clamped(selection):
    raw = _pdf("")
    result = structure.extract_pdf_source_structure(raw, "", selected_pages=selection)
    assert result["diagnostics"] == ["INVALID_SELECTED_PAGES"]
    assert not result["pages"]


def test_budgets_produce_visible_unproven_results(monkeypatch):
    raw = _pdf(_text("text", 40, 140))
    source = _canonical(raw)
    monkeypatch.setattr(structure, "_MAX_PDF_BYTES", len(raw)-1)
    assert structure.extract_pdf_source_structure(raw, source)["diagnostics"] == ["PDF_BYTE_LIMIT"]
    monkeypatch.setattr(structure, "_MAX_PDF_BYTES", len(raw)+1)
    monkeypatch.setattr(structure, "_MAX_DECODED_PAGE_BYTES", 1)
    assert structure.extract_pdf_source_structure(raw, source)["diagnostics"] == ["DECODED_STREAM_LIMIT"]


def test_payload_overflow_does_not_leave_a_partial_success(monkeypatch):
    commands, xs, _ = _grid()
    raw = _pdf(commands + _text("text", xs[0]+5, 141))
    monkeypatch.setattr(structure, "_MAX_PAYLOAD_BYTES", 100)
    result = structure.extract_pdf_source_structure(raw, _canonical(raw))
    assert result["diagnostics"] == ["STRUCTURE_PAYLOAD_LIMIT"]
    assert result["status"] == "UNPROVEN" and result["pages"] == []


def test_overlapping_cells_and_border_crossing_glyphs_never_get_source_spans():
    cells = [{"region_id": "r1", "bbox": [0, 0, 20, 20]}, {"region_id": "r2", "bbox": [10, 0, 30, 20]}]
    chars = [{"text": "A", "x0": 12, "top": 5, "x1": 15, "bottom": 10,
              "upright": True, "matrix": (1, 0, 0, 1, 0, 0)}]
    stats = structure._bind_cells(cells, chars, [[0]], "A")
    assert stats["unproven_cells"] == 2
    assert all(cell["source_spans"] == [] for cell in cells)
    cells = [{"region_id": "r1", "bbox": [0, 0, 14, 20]}]
    stats = structure._bind_cells(cells, chars, [[0]], "A")
    assert stats["unproven_cells"] == 1 and cells[0]["source_spans"] == []


def test_transition_offsets_split_only_adjacent_proven_cells_not_grade_zero_or_missing_glyph():
    cells = [{"region_id": "r1", "bbox": [0, 0, 20, 20]}, {"region_id": "r1", "bbox": [20, 0, 40, 20]},
             {"region_id": "r1", "bbox": [40, 0, 60, 20]}]
    source = "BBB0100%10.0"
    chars = [{"text": text, "x0": lo+2, "top": 5, "x1": lo+15, "bottom": 10,
              "upright": True, "matrix": (1, 0, 0, 1, 0, 0)}
             for lo, text in [(0, "BBB0"), (20, "100%"), (40, "10.0")]]
    stats = structure._bind_cells(cells, chars, [list(range(4)),list(range(4,8)),list(range(8,12))], source)
    assert stats["cell_transition_offsets"] == [4, 8]
    assert "\n".join(source[a:b] for a,b in zip([0,4,8],[4,8,12])) == "BBB0\n100%\n10.0"
    cells = [{"region_id": "r1", "bbox": [0, 0, 20, 20]}, {"region_id": "r1", "bbox": [40, 0, 60, 20]}]
    stats = structure._bind_cells(cells, chars, [list(range(4)),list(range(4,8)),list(range(8,12))], source)
    assert stats["cell_transition_offsets"] == []  # Unowned 100% cannot be jumped over.
