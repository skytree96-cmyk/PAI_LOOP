"""Differential guards for exact source-anchor indexing on document paragraphs."""
from __future__ import annotations

import random

from pai_loop.integrations.openai_extraction import evidence_quote_matches_source
from pai_loop.quantitative_rule_extraction import (
    _MAX_TABLE_CELL_WINDOW_CHARS,
    _MAX_TABLE_CELL_WINDOW_LINES,
    _anchor_line_spans,
)


def reference_spans(lines: tuple[str, ...], quote: str) -> tuple[tuple[int, int], ...]:
    """Original exhaustive predicate, retained only as an independent test oracle."""
    matches = []
    for start in range(len(lines)):
        for end in range(start + 1, min(len(lines), start + _MAX_TABLE_CELL_WINDOW_LINES) + 1):
            window = "\n".join(lines[start:end])
            if len(window) > _MAX_TABLE_CELL_WINDOW_CHARS:
                break
            if evidence_quote_matches_source(quote, window):
                matches.append((start, end))
                break
    return tuple((start, end) for start, end in matches if not any(
        other_start >= start and other_end <= end and (other_start, other_end) != (start, end)
        for other_start, other_end in matches
    ))


def test_anchor_index_preserves_unicode_boundaries_and_duplicate_locations() -> None:
    rng = random.Random(20260906)
    fragments = ["", " ", "\t", "SYN-등급", "AAA, AA0", "100%", "6", "6점", "문서 원문",
                 "문 서 원 문", "e\u0301", "é", "\u200b", "동일", "동일 동일", "기준\u200b값"]
    for _ in range(120):
        lines = tuple(rng.choice(fragments) for _ in range(rng.randint(1, 18)))
        start = rng.randrange(len(lines))
        quote = "\n".join(lines[start:start + rng.randint(1, 5)])
        for candidate in (quote, quote.replace(" ", ""), "SYN-존재하지않는인용", "", "6"):
            assert _anchor_line_spans(lines, candidate) == reference_spans(lines, candidate)


def test_anchor_index_keeps_original_raw_character_and_line_limits() -> None:
    lines = ("\u200b" * _MAX_TABLE_CELL_WINDOW_CHARS, "SYN-원문", "SYN-원문", "")
    assert _anchor_line_spans(lines, "SYN-원문") == reference_spans(lines, "SYN-원문")
    lines = ("SYN-시작",) + ("",) * _MAX_TABLE_CELL_WINDOW_LINES + ("SYN-끝",)
    assert _anchor_line_spans(lines, "SYN-시작 SYN-끝") == ()
    assert _anchor_line_spans(("동일", "동일"), "동일") == ((0, 1), (1, 2))
