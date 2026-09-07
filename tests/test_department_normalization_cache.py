"""Cache retention, failure boundaries, and ranking equivalence regressions."""
from __future__ import annotations

import re
import unicodedata

import pytest

from pai_loop import department_ranking as ranking


def _reference_normalize(value: object) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold(),
    ).strip()


class _UnhashableText(str):
    __hash__ = None


class _FalseyText(str):
    def __bool__(self) -> bool:
        return False


class _DisplayText(str):
    def __str__(self) -> str:
        return "SYN 표시 ＡＩ"


@pytest.fixture(autouse=True)
def clean_normalization_cache():
    ranking._normalize_text.cache_clear()
    yield
    ranking._normalize_text.cache_clear()


@pytest.mark.parametrize("value", [
    _UnhashableText("SYN ＡＩ 교육"),
    _FalseyText("SYN ignored"),
    _DisplayText("SYN original"),
    ["SYN", "ＡＩ"],
    {"SYN": "ＡＩ"},
])
def test_object_coercion_remains_identical_before_cache_lookup(value: object) -> None:
    assert ranking._normalize(value) == _reference_normalize(value)
    assert ranking._normalize(value) == _reference_normalize(value)


def test_large_text_is_normalized_fully_without_retaining_it() -> None:
    # Unicode compatibility expansion and whitespace folding must not truncate.
    value = "SYN " + "\ufdfa\tＡＩ " * 1024
    before = ranking._normalize_text.cache_info()
    assert ranking._normalize(value) == _reference_normalize(value)
    assert ranking._normalize(value) == _reference_normalize(value)
    assert ranking._normalize_text.cache_info() == before


def test_short_text_cache_reuses_results_and_evicts_old_entries() -> None:
    first = "SYN-cache-first"
    assert ranking._normalize(first) == _reference_normalize(first)
    assert ranking._normalize(first) == _reference_normalize(first)
    info = ranking._normalize_text.cache_info()
    assert (info.misses, info.hits, info.currsize) == (1, 1, 1)
    assert info.maxsize == 8192
    for index in range(info.maxsize):
        ranking._normalize(f"SYN-cache-{index}")
    filled = ranking._normalize_text.cache_info()
    assert filled.currsize == info.maxsize
    assert ranking._normalize(first) == _reference_normalize(first)
    after = ranking._normalize_text.cache_info()
    assert after.misses == filled.misses + 1  # The oldest key was evicted.
    assert after.currsize == info.maxsize


def test_failed_coercion_does_not_poison_the_cache() -> None:
    class SyntheticValue:
        fails = True

        def __str__(self) -> str:
            if self.fails:
                raise ValueError("SYN coercion failure")
            return "SYN ＡＩ"

    value = SyntheticValue()
    with pytest.raises(ValueError, match="SYN coercion failure"):
        ranking._normalize(value)
    assert ranking._normalize_text.cache_info().currsize == 0
    value.fails = False
    assert ranking._normalize(value) == "syn ai"
    assert ranking._normalize_text.cache_info().currsize == 1


def test_rejected_oversized_keyword_does_not_remain_in_cache() -> None:
    with pytest.raises(ValueError, match="60자"):
        ranking.parse_search_keywords("SYN" + "x" * 400)
    assert ranking._normalize_text.cache_info().currsize == 0


def test_all_catalog_views_equal_uncached_separate_helpers_when_cold_and_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ranking.load_department_keyword_profiles()
    cases = [
        {"title": "SYN 교육", "agency": "SYN 교육지원청", "category": "용역"},
        {"title": "SYN 인공", "agency": "지능 SYN 기관", "category": "용역"},
        {"title": "SYN ＡＩ\t교육", "agency": "SYN 기관", "category": "용역"},
        {"title": "SYN " + "분석 " * 200, "agency": "SYN 기관", "category": "용역"},
    ]
    for profile in catalog["departments"]:
        # Exercise weak matches, exclusions and geographical routing for every
        # catalog profile, without making the cache its own expected-value oracle.
        cases.extend([
            {
                "title": "SYN " + " ".join(profile["supporting_keywords"][:1]),
                "agency": "SYN 기관", "category": "용역",
            },
            {
                "title": "SYN " + " ".join([
                    *profile["strong_keywords"][:1],
                    *profile["excluded_keywords"][:1],
                    *profile["regions"][:1],
                ]),
                "agency": "SYN 기관", "category": "용역",
            },
        ])
    with monkeypatch.context() as patch:
        patch.setattr(ranking, "_normalize", _reference_normalize)
        expected = [{
            "top_department_rankings": ranking.rank_notice_across_departments(**case),
            "department_review_candidates": ranking.rank_notice_review_candidates(**case),
            "region_routing": ranking.route_notice_across_regions(**case),
        } for case in cases]
    for case, reference in zip(cases, expected, strict=True):
        ranking._normalize_text.cache_clear()
        for _ in range(2):
            actual = ranking.rank_notice_department_views(**case)
            assert {key: actual[key] for key in reference} == reference, case
