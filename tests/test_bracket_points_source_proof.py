"""Where a bracket award may be proven from the source.

A scoring table keeps the condition and its award in separate cells, so a row
literal is usually the condition alone (``5건 이상``) while the award sits one
column over. Requiring the award inside that same literal failed 1,826 of the
brackets production had already extracted. Accepting any digit from the
criterion prose instead would prove a 3-point bracket from ``최근 3년``, so the
award has to appear stated as a score.
"""

from __future__ import annotations

import pytest

from pai_loop.quantitative_rule_extraction import _points_are_score_marked


@pytest.mark.parametrize(
    ("points", "text"),
    [
        (9.0, "구분 배점 9"),
        (9.0, "배점: 9"),
        (9.0, "5건 이상 9점"),
        (3.5, "자기자본비율 3.5점"),
        (10.0, "배점 10"),
        (2.0, "수행실적 (2)"),
        (20.0, "수행실적 20점"),
    ],
)
def test_award_stated_as_a_score_is_proven(points: float, text: str) -> None:
    assert _points_are_score_marked(points, text) is True


@pytest.mark.parametrize(
    ("points", "text"),
    [
        # Criterion prose is full of unrelated small numbers.
        (3.0, "입찰일 기준으로 최근 3년간 이행완료된 실적"),
        (5.0, "단일건으로 5천만원 이상의 실적증명서"),
        (9.0, "9건 이상"),
        (5.0, "실적 5건"),
        # A longer award must not be proven by its own leading digit.
        (1.0, "배점 10"),
        (2.0, "배점 20점"),
        (9.0, ""),
    ],
)
def test_a_bare_number_does_not_prove_an_award(points: float, text: str) -> None:
    assert _points_are_score_marked(points, text) is False


def test_trailing_zeros_do_not_change_the_award() -> None:
    """``9.0`` and ``9`` are the same award and must match the same source."""

    assert _points_are_score_marked(9.0, "배점 9") is True
    assert _points_are_score_marked(9.00, "9점") is True
    assert _points_are_score_marked(10.0, "10점") is True
