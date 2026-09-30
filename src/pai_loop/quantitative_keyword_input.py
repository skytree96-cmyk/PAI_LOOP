"""Bounded, source-bound keyword input for a quantitative-only diagnostic.

Selection is never proof of full attachment coverage. XML below is transport
framing of original text, not a claim that a binary HWP was converted to HWPX.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
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


def hwpx_quantitative_table_context(content: bytes, canonical_text: str, *,
                                    maximum: int = 20_000) -> str:
    """Native HWPX table structure as untrusted context, bound to exact text.

    This never converts a binary HWP, performs OCR, or replaces the canonical
    quote verifier. Existing HWPX archive guards run before XML is considered.
    """
    from .pps_enrichment import _extract_hwpx_text

    if len(content) > 8 * 1024 * 1024:
        raise ValueError("HWPX_CONTEXT_INPUT_LIMIT")
    if _extract_hwpx_text(content).replace("\x00", "") != canonical_text.strip():
        raise ValueError("HWPX_CONTEXT_SOURCE_MISMATCH")
    root = ET.Element("hwpx_scoring_tables", {"coverage": "partial", "images": "excluded"})
    allowed = {"tbl", "table", "tr", "row", "tc", "cell", "p", "run", "t",
               "cellAddr", "cellSpan", "subList", "footnote", "endnote"}
    count = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in sorted(archive.namelist()):
            if not re.fullmatch(r"Contents/section\d+\.xml", name):
                continue
            section = ET.fromstring(archive.read(name))
            covered: set[int] = set()
            for table in section.iter():
                if id(table) in covered:
                    continue
                if table.tag.rsplit("}", 1)[-1] not in {"tbl", "table"}:
                    continue
                words = " ".join(n.text or "" for n in table.iter()
                                 if n.tag.rsplit("}", 1)[-1] in {"t", "p"})
                if not _SIGNAL.search(words):
                    continue
                covered.update(id(n) for n in table.iter())
                target = ET.SubElement(root, "table", {"section": name})
                stack = [(table, target, 0)]
                while stack:
                    source, parent, depth = stack.pop()
                    count += 1
                    if count > 10_000 or depth > 64:
                        raise ValueError("HWPX_CONTEXT_STRUCTURE_LIMIT")
                    tag = source.tag.rsplit("}", 1)[-1]
                    if tag in {"pic", "image", "img", "binaryItem"}:
                        continue
                    if tag in allowed:
                        attrs = {k: v for k, v in source.attrib.items()
                                 if k in {"rowAddr", "colAddr", "rowSpan", "colSpan"}
                                 and re.fullmatch(r"\d{1,4}", v)}
                        node = ET.SubElement(parent, tag, attrs)
                        if tag in {"t", "p"} and source.text:
                            node.text = source.text
                        parent = node
                    stack.extend((child, parent, depth + 1) for child in reversed(list(source)))
    if not len(root):
        raise ValueError("HWPX_SCORING_TABLE_NOT_FOUND")
    result = ET.tostring(root, encoding="unicode")
    if len(result) > maximum:
        raise ValueError("HWPX_CONTEXT_SIZE_LIMIT")
    return result
