"""Differential guards for exact source-anchor indexing on document paragraphs."""
from __future__ import annotations

import random

from pai_loop.integrations.openai_extraction import evidence_quote_matches_source
from pai_loop.quantitative_rule_extraction import (
    _MAX_TABLE_CELL_WINDOW_CHARS,
    _MAX_TABLE_CELL_WINDOW_LINES,
    _anchor_line_spans,
    _SourceLines,
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
            expected = reference_spans(lines, candidate)
            assert _anchor_line_spans(lines, candidate) == expected
            indexed = _SourceLines(lines)
            assert _anchor_line_spans(indexed, candidate) == expected
            assert _anchor_line_spans(indexed, candidate) == expected


def test_anchor_index_keeps_original_raw_character_and_line_limits() -> None:
    lines = ("\u200b" * _MAX_TABLE_CELL_WINDOW_CHARS, "SYN-원문", "SYN-원문", "")
    assert _anchor_line_spans(lines, "SYN-원문") == reference_spans(lines, "SYN-원문")
    lines = ("SYN-시작",) + ("",) * _MAX_TABLE_CELL_WINDOW_LINES + ("SYN-끝",)
    assert _anchor_line_spans(lines, "SYN-시작 SYN-끝") == ()
    assert _anchor_line_spans(("동일", "동일"), "동일") == ((0, 1), (1, 2))


def test_document_local_cache_is_bounded_and_does_not_cross_sources() -> None:
    first = _SourceLines(("SYN-first",))
    second = _SourceLines(("SYN-second",))
    assert _anchor_line_spans(first, "SYN-first") == ((0, 1),)
    assert _anchor_line_spans(second, "SYN-first") == ()
    for index in range(300):
        assert _anchor_line_spans(first, f"SYN-missing-{index}") == ()
    assert len(first.anchor_spans) == 256
    assert first.cached_span_count <= 4096


def test_document_local_cache_retains_window_bounds(monkeypatch) -> None:
    from pai_loop import quantitative_rule_extraction as rules

    lines = _SourceLines(("SYN-first", "SYN-second"))
    assert _anchor_line_spans(lines, "SYN-first SYN-second") == ((0, 2),)
    monkeypatch.setattr(rules, "_MAX_TABLE_CELL_WINDOW_LINES", 1)
    assert _anchor_line_spans(lines, "SYN-first SYN-second") == ()
    monkeypatch.setattr(rules, "_MAX_TABLE_CELL_WINDOW_LINES", 2)
    monkeypatch.setattr(rules, "_MAX_TABLE_CELL_WINDOW_CHARS", 4)
    assert _anchor_line_spans(lines, "SYN-first SYN-second") == ()
