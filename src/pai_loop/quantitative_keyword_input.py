"""Bounded, source-bound keyword input for a quantitative-only diagnostic.

Selection is never proof of full attachment coverage. XML below is transport
framing of original text, not a claim that a binary HWP was converted to HWPX.
"""
from __future__ import annotations

import hashlib
import re
from xml.etree import ElementTree as ET

_SIGNAL = re.compile(r"배점|정량|평가\s*기준|신용\s*평가|경영\s*상태|수행\s*실적|인력\s*보유|scoring|score\s*table", re.I)
_PAGE = re.compile(r"(?m)^\[PAGE [1-9]\d*\]\n")


def select_quantitative_keyword_input(source: str, *, maximum: int = 60_000,
                                      xml: bool = False) -> tuple[str, dict[str, object]]:
    if not source.strip() or len(source) > 2_000_000 or maximum < 256:
        raise ValueError("QUANTITATIVE_SOURCE_LIMIT")
    # Keep whole physical pages when available. Otherwise keep generous whole
    # line windows; never cut a row to squeeze under the budget.
    markers = list(_PAGE.finditer(source))
    spans: list[tuple[int, int]] = []
    if markers:
        boundaries = [0] + [m.start() for m in markers if m.start()] + [len(source)]
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            if _SIGNAL.search(source[start:end]):
                spans.append((boundaries[max(0, index - 1)], boundaries[min(len(boundaries) - 1, index + 2)]))
    else:
        lines = source.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        for index, line in enumerate(lines):
            if _SIGNAL.search(line):
                spans.append((offsets[max(0, index - 12)], offsets[min(len(lines), index + 61)]))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    if not merged:
        raise ValueError("QUANTITATIVE_KEYWORDS_NOT_FOUND")
    if xml:
        root = ET.Element("source_excerpts", {"coverage": "partial", "images": "excluded"})
        for start, end in merged:
            ET.SubElement(root, "excerpt", {"start": str(start), "end": str(end)}).text = source[start:end]
        text = ET.tostring(root, encoding="unicode")
    else:
        text = "\n[OMITTED SOURCE: NOT ANALYZED]\n".join(source[a:b] for a, b in merged)
    if len(text) > maximum:
        raise ValueError("QUANTITATIVE_SELECTION_TOO_LARGE")
    return text, {
        "purpose": "QUANTITATIVE_PROBE_ONLY", "persistence_eligible": False,
        "attachment_coverage_complete": False,
        "selection_method": "KEYWORD_XML" if xml else "KEYWORD_TEXT",
        "canonical_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "original_characters": len(source), "selected_display_characters": len(text),
        "source_ranges": [list(pair) for pair in merged], "images_excluded": True,
    }
