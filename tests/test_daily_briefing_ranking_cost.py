"""Focused regressions for the stored daily-briefing ranking cost.

The briefing must keep ranking the whole window before ``limit`` is applied,
so the per-notice cost is what decides whether the endpoint finishes. These
cases pin the two reductions that were made and, more importantly, pin that
the visible ranking output did not change.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pai_loop import daily_operations
from pai_loop import department_ranking as ranking_module
from pai_loop.department_ranking import (
    _normalize,
    _normalize_text,
    load_department_keyword_profiles,
    rank_notice_across_departments,
    rank_notice_review_candidates,
    route_notice_across_regions,
)
from pai_loop.models import Notice


AS_OF = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
_SYNTHETIC_NOTICES = (
    ("SYN-BRIEF-RANK-1", "2026년도 SYN 공공기관 AI 교육 및 컨설팅 용역", "SYN 발주기관"),
    ("SYN-BRIEF-RANK-2", "SYN 부산광역시 스마트도시 데이터 분석 용역", "SYN 부산기관"),
    ("SYN-BRIEF-RANK-3", "SYN 리더십 역량강화 위탁운영", "SYN 교육지원청"),
)


@pytest.fixture()
def briefing_client(client: TestClient) -> TestClient:
    with client.app.state.session_factory() as session:
        for notice_key, title, agency in _SYNTHETIC_NOTICES:
            session.add(
                Notice(
                    notice_key=notice_key,
                    bid_notice_no=notice_key,
                    revision_no="00",
                    title=title,
                    agency=agency,
                    category="용역",
                    published_at=AS_OF - timedelta(days=2),
                    deadline=AS_OF + timedelta(days=20),
                    status="OPEN",
                    estimated_amount=120_000_000,
                )
            )
        session.commit()
    return client


def _briefing(client: TestClient) -> dict:
    response = client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "limit": 20, "as_of": AS_OF.isoformat()},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_briefing_scores_each_department_once_per_notice(
    briefing_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three separate helper calls scored business and region departments twice."""

    calls: list[str | None] = []
    original = ranking_module.rank_notice_for_department

    def counted(**kwargs: object):
        calls.append(kwargs.get("department_id"))  # type: ignore[arg-type]
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ranking_module, "rank_notice_for_department", counted)
    body = _briefing(briefing_client)

    department_count = len(load_department_keyword_profiles()["departments"])
    assert body["totals"]["observed"] == len(_SYNTHETIC_NOTICES)
    assert len(calls) == department_count * len(_SYNTHETIC_NOTICES)
    # Every department is scored exactly once per notice, none twice.
    assert len(set(calls)) == department_count


def test_briefing_department_views_match_the_separate_public_helpers(
    briefing_client: TestClient,
) -> None:
    """The single pass must return exactly what the three helpers returned."""

    body = _briefing(briefing_client)
    items = {item["notice_key"]: item for item in body["notices"]}
    assert set(items) == {key for key, _title, _agency in _SYNTHETIC_NOTICES}

    for notice_key, title, agency in _SYNTHETIC_NOTICES:
        item = items[notice_key]
        expected_top = rank_notice_across_departments(
            title=title, agency=agency, category="용역", limit=3,
        )
        expected_review = rank_notice_review_candidates(
            title=title, agency=agency, category="용역", limit=3,
        )
        expected_regions = route_notice_across_regions(
            title=title, agency=agency, category="용역", limit=2,
        )
        assert item["top_departments"] == expected_top, notice_key
        assert item["department_review_candidates"] == expected_review, notice_key
        assert item["region_routing"] == expected_regions, notice_key


def test_priority_score_still_follows_the_top_department_score(
    briefing_client: TestClient,
) -> None:
    """The ranking feeds priority_score, so a changed view would change order."""

    body = _briefing(briefing_client)
    for item in body["notices"]:
        department_score = (
            float(item["top_departments"][0]["score"])
            if item["top_departments"]
            else 0.0
        )
        eligibility_weight = {"PASS": 30, "REVIEW": 20, "PENDING": 10, "FAIL": 0}[
            item["fit"]["eligibility"]
        ]
        readiness = float(item["fit"]["readiness_score"] or 0.0)
        assert item["priority_score"] == round(
            min(100.0, eligibility_weight + 0.4 * department_score + 0.3 * readiness), 1
        )


def test_the_whole_window_is_ranked_before_limit_is_applied(
    briefing_client: TestClient,
) -> None:
    """``limit`` trims the response, it must not shrink the ranked population."""

    response = briefing_client.get(
        "/api/v1/operations/daily-briefing",
        params={"days": 7, "limit": 1, "as_of": AS_OF.isoformat()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["observed"] == len(_SYNTHETIC_NOTICES)
    assert body["totals"]["included"] == 1
    # The single returned notice is the top of the full population, not the
    # first row the database happened to return.
    full = _briefing(briefing_client)
    assert body["notices"][0]["notice_key"] == full["notices"][0]["notice_key"]


def test_the_briefing_still_makes_no_source_calls(briefing_client: TestClient) -> None:
    assert _briefing(briefing_client)["source_calls"] == {
        "pps": 0,
        "openai": 0,
        "teams": 0,
    }


@pytest.mark.parametrize("value", [
    "  2026년도  SYN   AI 교육 ",
    "SYN\tAI\n교육",
    "ＡＩ 교육",
    "AI 교육",
    "",
    "   ",
])
def test_memoised_normalisation_returns_the_uncached_result(value: str) -> None:
    """The cache is a speed change only; the folded value must be identical."""

    import re
    import unicodedata

    expected = re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()
    ).strip()
    assert _normalize(value) == expected
    # A second call comes from the cache and must agree with the first.
    assert _normalize(value) == expected


@pytest.mark.parametrize(("value", "expected"), [
    (None, ""),
    (0, ""),
    (False, ""),
    (12, "12"),
])
def test_non_string_inputs_keep_their_previous_normalisation(
    value: object, expected: str,
) -> None:
    """``_normalize`` accepted any object before; that contract is unchanged."""

    assert _normalize(value) == expected


def test_distinct_inputs_are_not_collapsed_by_the_cache() -> None:
    assert _normalize_text("AI 교육") != _normalize_text("AI 컨설팅")
    assert _normalize_text("교육") == _normalize("교육")
