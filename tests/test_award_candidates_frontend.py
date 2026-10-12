from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "pai_loop" / "static"


def _body(source: str, name: str) -> str:
    begin = source.index(f"function {name}(")
    following = re.search(r"\n  (?:async )?function ", source[begin + 1:])
    return source[begin:begin + 1 + following.start()] if following else source[begin:]


def test_award_card_offers_explicit_candidate_save_kept_apart_from_performance() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    action = _body(source, "renderAwardCandidateAction")
    save = _body(source, "saveAwardCandidate")

    assert 'id="awardCandidatesSection"' in html
    assert "회사 실적·정량점수와 분리" in html
    assert "회사 실적으로 자동 이동하지 않습니다" in html
    assert "${renderAwardCandidateAction(record, index)}" in source
    # Only the company's own awards get the button, and only on an explicit click.
    assert "isDefaultAwardCompany(record.winnerName)" in action
    assert "정량점수 미반영" in action
    assert 'apiRequest("/award-performance-candidates", {' in save
    assert "/performance-records" not in save
    assert "/performance-records" not in _body(source, "dismissAwardCandidate")
    assert "manualAnalysisAuthHeaders()" in save
    assert 'els.awardResultsList.addEventListener("click", handleAwardResultsClick)' in source
    # Private candidate state is cleared on logout like other department records.
    reset = _body(source, "clearAccountPrivateState")
    assert "state.awardCandidates = {" in reset


def test_award_card_save_key_matches_the_server_candidate_key() -> None:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    normalize = _body(source, "normalizeCompanyAward")
    assert "`award:${" in normalize
    assert "raw: source" in normalize
