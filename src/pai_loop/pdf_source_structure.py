"""Conservative PDF geometry hints bound to the unchanged canonical source.

This is not a scoring parser. Cells have no semantic row/column inheritance,
and open outside borders are explicitly distinguished from painted rulings.
The only text anchors are exact, ordered slices of the caller's source text.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from collections.abc import Iterable
from itertools import islice
from statistics import median
from typing import Any


STRUCTURE_VERSION = "pdf-source-structure-1"
_SNAP = 0.5  # PDF points; never a word- or column-sized gap.
_INTERSECTION = 0.75
_EPS = 1e-6
_MAX_PAGES = 120
_MAX_PDF_BYTES = 8 * 1024 * 1024
_MAX_DECODED_PAGE_BYTES = 4 * 1024 * 1024
_MAX_DECODED_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_CHARACTERS = 500_000
_MAX_SELECTED_PAGES = 16
_MAX_PAGE_CHARACTERS = 10_000
_MAX_PAGE_EDGES = 25_000
_MAX_PAGE_RULINGS = 500
_MAX_PAGE_INTERSECTIONS = 2_500
_MAX_PAGE_CELLS = 500
_MAX_PAYLOAD_BYTES = 512 * 1024


def _sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _nonwhite(text: str) -> str:
    return "".join(char for char in text if not char.isspace())


def _finite(values: Any, length: int) -> bool:
    try:
        return len(values) == length and all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError, OverflowError):
        return False


def _assemble(texts: list[str]) -> tuple[str, list[tuple[int, int] | None]]:
    parts, ranges = [], []
    cursor = 0
    for number, text in enumerate(texts, 1):
        if not text.strip():
            ranges.append(None)
            continue
        prefix = f"\n[PAGE {number}]\n"
        parts.append(prefix + text)
        ranges.append((cursor + len(prefix), cursor + len(prefix) + len(text)))
        cursor += len(prefix) + len(text)
    raw = "".join(parts)
    left, right = len(raw) - len(raw.lstrip()), len(raw.rstrip())
    return raw.strip(), [None if item is None else (max(item[0], left) - left,
                            min(item[1], right) - left) for item in ranges]


def _glyph_source_offsets(text: str, chars: list[dict], start: int) -> list[list[int]] | None:
    """A full ordered stream proves the cumulative partition, even with repeats.

    PDF whitespace inference differs between libraries. Only non-whitespace
    glyph characters are mapped, without replacing any source characters. No
    substring search, first-hit, normalization or partial-prefix fallback.
    """
    if any(not isinstance(char.get("text"), str) for char in chars):
        return None
    source = [index + start for index, char in enumerate(text) if not char.isspace()]
    lengths = [len(_nonwhite(char["text"])) for char in chars]
    if _nonwhite(text) != "".join(_nonwhite(char["text"]) for char in chars):
        return None
    result, cursor = [], 0
    for length in lengths:
        result.append(source[cursor:cursor + length])
        cursor += length
    return result


def _edge_intersects(a: dict, b: dict) -> bool:
    if a["orientation"] == b["orientation"]:
        return False
    h, v = (a, b) if a["orientation"] == "h" else (b, a)
    return (h["x0"] - _INTERSECTION <= v["x0"] <= h["x1"] + _INTERSECTION
            and v["top"] - _INTERSECTION <= h["top"] <= v["bottom"] + _INTERSECTION)


def _components(edges: list[dict]) -> list[list[dict]]:
    # Parallel or nearby rulings alone do not connect independent 2-up panels.
    remaining = set(range(len(edges)))
    components = []
    while remaining:
        active = [min(remaining)]
        remaining.remove(active[0])
        members = []
        while active:
            index = active.pop()
            members.append(edges[index])
            neighbors = {other for other in remaining if _edge_intersects(edges[index], edges[other])}
            remaining -= neighbors
            active.extend(sorted(neighbors))
        components.append(members)
    return components


def _rulings(raw_edges: list[dict]) -> list[dict]:
    from pdfplumber.table import merge_edges

    original = [dict(edge) for edge in raw_edges
                if edge.get("object_type") in {"line", "rect_edge"}
                and edge.get("stroke") and edge.get("orientation") in {"h", "v"}
                and _finite([edge.get(key) for key in ("x0", "top", "x1", "bottom")], 4)]
    merged = merge_edges(original, _SNAP, _SNAP, 1.0, 1.0)
    proven = []
    for edge in merged:
        horizontal = edge["orientation"] == "h"
        coordinate = "top" if horizontal else "x0"
        lower, upper = ("x0", "x1") if horizontal else ("top", "bottom")
        if edge[upper] - edge[lower] < 0.1:
            continue
        parts = [item for item in original if item["orientation"] == edge["orientation"]
                 and abs(item[coordinate] - edge[coordinate]) <= _SNAP + _EPS
                 and item[upper] >= edge[lower] - _EPS and item[lower] <= edge[upper] + _EPS]
        union: list[list[float]] = []
        for lo, hi in sorted((item[lower], item[upper]) for item in parts):
            if union and lo <= union[-1][1] + 0.01:
                union[-1][1] = max(union[-1][1], hi)
            else:
                union.append([lo, hi])
        gaps = [right[0] - left[1] for left, right in zip(union, union[1:])]
        proof = "SOLID_PATH"
        if gaps:
            lengths = [hi - lo for lo, hi in union]
            # Preserve a repeated painted dash pattern, not arbitrary missing
            # strokes. The short terminal dash may be clipped at the endpoint.
            if (len(gaps) < 3 or max(gaps) > 1.0 + _EPS
                    or max(gaps) - min(gaps) > 0.03
                    or median(lengths) > 1.0
                    or max(gaps) > 2.0 * median(lengths) + 0.03
                    or any(abs(value - median(lengths)) > 0.03 for value in lengths[1:-1])):
                # Reject the proposed connection, not the painted strokes.
                # Dropping the original pieces would merge genuine adjacent
                # cells and overstate their character ownership.
                for lo, hi in union:
                    if hi - lo < 0.1:
                        continue
                    piece = dict(edge, source_proof="UNJOINED_PAINTED_PATH", source_edge_count=1)
                    piece[lower], piece[upper] = lo, hi
                    piece["width" if horizontal else "height"] = hi - lo
                    proven.append(piece)
                continue
            proof = "SEGMENTED_DASH_PATTERN"
        elif any(item.get("dash") and item["dash"][0] for item in parts):
            proof = "DASHED_PATH"
        proven.append(dict(edge, source_proof=proof, source_edge_count=len(parts)))
    return proven


def _open_outer_bounds(component: list[dict]) -> tuple[list[dict], dict | None]:
    hs = [edge for edge in component if edge["orientation"] == "h"]
    vs = [edge for edge in component if edge["orientation"] == "v"]
    if len(hs) < 3 or len(vs) < 2:
        return [], None
    repeated = [h for h in hs if sum(abs(h["x0"] - item["x0"]) <= _SNAP
                   and abs(h["x1"] - item["x1"]) <= _SNAP for item in hs) >= 3]
    if not repeated:
        return [], None
    widest = max(repeated, key=lambda edge: edge["x1"] - edge["x0"])
    full = [h for h in repeated if abs(h["x0"] - widest["x0"]) <= _SNAP
            and abs(h["x1"] - widest["x1"]) <= _SNAP]
    if len({round(h["top"], 3) for h in full}) < 3:
        return [], None
    x0, x1 = min(h["x0"] for h in full), max(h["x1"] for h in full)
    top, bottom = min(h["top"] for h in full), max(h["top"] for h in full)
    interior = [v for v in vs if x0 + _INTERSECTION < v["x0"] < x1 - _INTERSECTION
                and sum(_edge_intersects(v, h) for h in hs) >= 3]
    if len(interior) < 2:
        return [], None
    # A wider adjacent rule, a continuation or inconsistent endpoints cannot
    # be silently clipped into this envelope.
    if any(h["x0"] < x0 - _SNAP or h["x1"] > x1 + _SNAP
           or h["top"] < top - _INTERSECTION or h["top"] > bottom + _INTERSECTION for h in hs):
        return [], None
    if any(v["x0"] < x0 - _INTERSECTION or v["x0"] > x1 + _INTERSECTION
           or v["top"] < top - _INTERSECTION or v["bottom"] > bottom + _INTERSECTION for v in vs):
        return [], None
    inferred = []
    for side, coordinate in (("left", x0), ("right", x1)):
        if not any(abs(v["x0"] - coordinate) <= _INTERSECTION
                   and v["top"] <= top + _INTERSECTION and v["bottom"] >= bottom - _INTERSECTION for v in vs):
            inferred.append({"object_type": "line", "orientation": "v", "x0": coordinate,
                             "x1": coordinate, "top": top, "bottom": bottom, "width": 0,
                             "height": bottom - top, "inferred_outer_side": side})
    if not inferred:
        return [], None
    return inferred, {"kind": "OPEN_BORDER_ENVELOPE", "bbox": [x0, top, x1, bottom],
                      "full_width_ruling_count": len(full), "internal_ruling_count": len(interior),
                      "inferred_sides": [edge["inferred_outer_side"] for edge in inferred],
                      "painted_outer_borders": False}


def _candidate_cells(page: Any, rulings: list[dict]) -> tuple[list[dict], list[dict]]:
    cells, regions = [], []
    for component_number, component in enumerate(_components(rulings)):
        hs = [edge for edge in component if edge["orientation"] == "h"]
        vs = [edge for edge in component if edge["orientation"] == "v"]
        inferred, proof = _open_outer_bounds(component)
        if len(hs) < 2 or len(vs + inferred) < 2:
            continue
        tables = page.find_tables({"vertical_strategy": "explicit", "horizontal_strategy": "explicit",
                                  "explicit_vertical_lines": vs + inferred, "explicit_horizontal_lines": hs,
                                  "snap_tolerance": _SNAP, "join_tolerance": 0,
                                  "intersection_tolerance": _INTERSECTION, "edge_min_length": 0.1})
        for table_number, table in enumerate(tables):
            region_id = f"p{page.page_number}-r{component_number}-{table_number}"
            regions.append({"id": region_id, "bbox": list(table.bbox),
                            "geometry_proof": proof or {"kind": "PAINTED_RULING_COMPONENT"},
                            "reading_order": "UNPROVEN", "semantic_relationships": "UNPROVEN"})
            for bbox in table.cells:
                cells.append({"region_id": region_id, "bbox": list(bbox),
                              "geometry_status": "OPEN_BORDER_ENVELOPE" if inferred else "RULED_CELL"})
    return cells, regions


def _overlap(a: list[float], b: list[float]) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > _EPS and min(a[3], b[3]) - max(a[1], b[1]) > _EPS


def _inside(a: list[float], b: list[float]) -> bool:
    return a[0] >= b[0] - _EPS and a[1] >= b[1] - _EPS and a[2] <= b[2] + _EPS and a[3] <= b[3] + _EPS


def _source_spans(indices: list[int], text: str) -> list[dict]:
    ranges: list[list[int]] = []
    for index in sorted(indices):
        if ranges and not text[ranges[-1][1]:index].strip():
            ranges[-1][1] = index + 1
        else:
            ranges.append([index, index + 1])
    return [{"start": start, "end": end, "text": text[start:end]} for start, end in ranges]


def _bind_cells(cells: list[dict], chars: list[dict], offsets: list[list[int]], source: str) -> dict:
    bad = {index for index, cell in enumerate(cells)
           if any(index != other and _overlap(cell["bbox"], candidate["bbox"])
                  for other, candidate in enumerate(cells))}
    owned: list[list[int]] = [[] for _ in cells]
    invalid_geometry = 0
    for char, indices in zip(chars, offsets, strict=True):
        if not indices:
            continue
        bbox = [char.get(key) for key in ("x0", "top", "x1", "bottom")]
        matrix = char.get("matrix")
        if (not _finite(bbox, 4) or not _finite(matrix, 6) or not char.get("upright")
                or abs(matrix[1]) > _EPS or abs(matrix[2]) > _EPS or matrix[0] <= 0 or matrix[3] <= 0):
            # Its location is not trustworthy; no cell on this page can claim
            # complete character ownership when such a source glyph exists.
            invalid_geometry += len(indices)
            continue
        touching = [index for index, cell in enumerate(cells) if _overlap(bbox, cell["bbox"])]
        containing = [index for index in touching if _inside(bbox, cells[index]["bbox"])]
        if len(touching) == len(containing) == 1:
            owned[containing[0]].extend(indices)
        else:
            bad.update(touching)
    if invalid_geometry:
        bad.update(range(len(cells)))
    bound = 0
    for index, cell in enumerate(cells):
        cell["id"] = f"{cell['region_id']}-c{index}"
        if index in bad:
            cell.update(ownership_status="UNPROVEN", source_spans=[], source_nonwhite_characters=0)
        else:
            spans = _source_spans(owned[index], source)
            cell.update(ownership_status="BOUND" if spans else "EMPTY", source_spans=spans,
                        source_nonwhite_characters=len(owned[index]))
            bound += len(owned[index])
        cell["source_order"] = cell["source_spans"][0]["start"] if cell["source_spans"] else None
    # Consumers must not turn this into visual/global reading-order sorting.
    cells.sort(key=lambda cell: (cell["source_order"] is None, cell["source_order"] or 0))
    owners = sorted((index, cell["id"], cell["region_id"]) for cell in cells
                    for span in cell["source_spans"] for index in range(span["start"], span["end"])
                    if not source[index].isspace())
    transitions = [right[0] for left, right in zip(owners, owners[1:])
                   if left[1] != right[1] and left[2] == right[2]
                   and not source[left[0] + 1:right[0]].strip()]
    return {"bound_nonwhite_characters": bound, "unproven_cells": len(bad),
            "invalid_geometry_nonwhite_characters": invalid_geometry,
            "cell_transition_offsets": transitions}


def extract_pdf_source_structure(
    pdf_bytes: bytes, canonical_text: str, *, selected_pages: Iterable[int] | None = None,
) -> dict:
    """Return private, versioned hints; any uncertain mapping fails closed.

    Caller must retain the ordinary PDF security/completeness checks. Geometry
    never replaces canonical text, source quotes, attachment coverage or any
    deterministic scoring check. Exceptions yield diagnostic codes, not text.
    Decoded-stream limits are checked after library decoding, so this helper
    is not a replacement for process-level memory/time isolation of hostile PDFs.
    """
    result: dict = {"version": STRUCTURE_VERSION, "pdf_sha256": _sha(pdf_bytes),
                    "canonical_sha256": _sha(canonical_text), "status": "UNPROVEN",
                    "source_sha256": _sha(canonical_text),
                    "canonical_text_preserved": False, "pages": [], "diagnostics": [],
                    "mapping_scope": "ALL_PAGES" if selected_pages is None else "SELECTED_PAGES",
                    "cell_transition_offsets": [], "boundaries_proven": False,
                    "boundary_coverage": "PARTIAL",
                    "coordinate_system": "PDFPLUMBER_PAGE_POINTS_X0_TOP_X1_BOTTOM", "semantic_validation": "NOT_PERFORMED"}
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        result["diagnostics"].append("PDF_BYTE_LIMIT")
        return result
    if len(canonical_text) > _MAX_CHARACTERS:
        result["diagnostics"].append("SOURCE_CHARACTER_LIMIT")
        return result
    try:
        selected = None if selected_pages is None else list(islice(selected_pages, _MAX_SELECTED_PAGES + 1))
        if selected is not None and (len(selected) > _MAX_SELECTED_PAGES
                or any(type(number) is not int or number < 1 for number in selected)
                or len(set(selected)) != len(selected)):
            result["diagnostics"].append("INVALID_SELECTED_PAGES")
            return result
        import pdfplumber
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
        if reader.is_encrypted or len(reader.pages) > _MAX_PAGES:
            result["diagnostics"].append("PDF_UNSUPPORTED_FOR_STRUCTURE")
            return result
        if selected is not None and any(number > len(reader.pages) for number in selected):
            result["diagnostics"].append("INVALID_SELECTED_PAGES")
            return result
        selected_set = set(range(1, len(reader.pages) + 1) if selected is None else selected)
        result["selected_pages"] = sorted(selected_set)
        result["skipped_geometry_pages"] = [number for number in range(1, len(reader.pages) + 1) if number not in selected_set]
        texts, runs_equal = [], []
        total_decoded = 0
        for page in reader.pages:
            content = page.get_contents()
            decoded_size = len(content.get_data()) if content is not None else 0
            total_decoded += decoded_size
            if decoded_size > _MAX_DECODED_PAGE_BYTES or total_decoded > _MAX_DECODED_TOTAL_BYTES:
                result["diagnostics"].append("DECODED_STREAM_LIMIT")
                return result
            runs: list[str] = []
            text = page.extract_text(visitor_text=lambda text, *_: runs.append(text)) or ""
            texts.append(text)
            runs_equal.append("".join(runs) == text)
            if sum(len(value) for value in texts) > _MAX_CHARACTERS:
                result["diagnostics"].append("SOURCE_CHARACTER_LIMIT")
                return result
        assembled, ranges = _assemble(texts)
        if assembled != canonical_text:
            result["diagnostics"].append("CANONICAL_SOURCE_MISMATCH")
            return result
        result["canonical_text_preserved"] = True
        result["canonical_pages_verified"] = len(texts)
        result["canonical_run_mapping_passed"] = all(runs_equal)
        result["geometry_engine"] = f"pdfplumber-{pdfplumber.__version__}"
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) != len(texts):
                result["diagnostics"].append("PAGE_COUNT_MISMATCH")
                return result
            for index, page in enumerate(pdf.pages):
                if index + 1 not in selected_set:
                    continue
                body_range = ranges[index]
                item: dict = {"page_number": index + 1, "canonical_body_range": body_range,
                              "page_bbox": list(page.bbox), "crop_box": list(page.cropbox),
                              "rotation": page.rotation,
                              "run_mapping_status": "EXACT_COMPLETE_STREAM" if runs_equal[index] else "UNPROVEN",
                              "glyph_mapping_status": "UNPROVEN", "geometry_status": "UNPROVEN",
                              "regions": [], "cells": [], "diagnostics": []}
                result["pages"].append(item)
                body = canonical_text[slice(*body_range)] if body_range is not None else ""
                item["source_nonwhite_characters"] = len(_nonwhite(body))
                chars = page.chars
                if len(chars) > _MAX_PAGE_CHARACTERS:
                    item["diagnostics"].append("GEOMETRY_CHARACTER_LIMIT")
                    continue
                offsets = _glyph_source_offsets(body, chars, body_range[0] if body_range else 0)
                if not runs_equal[index] or offsets is None:
                    item["diagnostics"].append("SOURCE_STREAM_MISMATCH")
                    continue
                item["glyph_mapping_status"] = "EXACT_COMPLETE_NONWHITE_STREAM"
                if page.rotation or len(page.edges) > _MAX_PAGE_EDGES:
                    item["diagnostics"].append("ROTATED_PAGE_UNPROVEN" if page.rotation else "GEOMETRY_EDGE_LIMIT")
                    continue
                rulings = _rulings(page.edges)
                if len(rulings) > _MAX_PAGE_RULINGS:
                    item["diagnostics"].append("GEOMETRY_RULING_LIMIT")
                    continue
                horizontal_count = sum(edge["orientation"] == "h" for edge in rulings)
                if horizontal_count * (len(rulings) - horizontal_count + 2) > _MAX_PAGE_INTERSECTIONS:
                    item["diagnostics"].append("GEOMETRY_INTERSECTION_LIMIT")
                    continue
                cells, regions = _candidate_cells(page, rulings)
                if len(cells) > _MAX_PAGE_CELLS:
                    item["diagnostics"].append("GEOMETRY_CELL_LIMIT")
                    continue
                item["regions"], item["cells"] = regions, cells
                item.update(_bind_cells(cells, chars, offsets, canonical_text))
                item["unassigned_nonwhite_characters"] = item["source_nonwhite_characters"] - item["bound_nonwhite_characters"]
                item["geometry_status"] = "PARTIAL_BOUND_CELLS" if item["bound_nonwhite_characters"] else "UNPROVEN"
                item["ruling_count"] = len(rulings)
                item["segmented_dash_ruling_count"] = sum(edge["source_proof"] == "SEGMENTED_DASH_PATTERN" for edge in rulings)
                if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
                    result.update(status="UNPROVEN", pages=[])
                    result["diagnostics"].append("STRUCTURE_PAYLOAD_LIMIT")
                    return result
        if any(page["geometry_status"] == "PARTIAL_BOUND_CELLS" for page in result["pages"]):
            result["status"] = "PARTIAL_BOUND_CELLS"
        result["cell_transition_offsets"] = sorted(offset for page in result["pages"]
            for offset in page.get("cell_transition_offsets", []))
        result["boundaries_proven"] = bool(result["cell_transition_offsets"])
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            result.update(status="UNPROVEN", pages=[], cell_transition_offsets=[], boundaries_proven=False)
            result["diagnostics"].append("STRUCTURE_PAYLOAD_LIMIT")
    except ImportError:
        result["diagnostics"].append("STRUCTURE_DEPENDENCY_UNAVAILABLE")
    except Exception:
        # An incomplete helper must not become source evidence or a hidden
        # successful geometry result, even if earlier pages were processed.
        result.update(status="UNPROVEN", pages=[])
        result["diagnostics"].append("STRUCTURE_EXTRACTION_FAILED")
    return result
