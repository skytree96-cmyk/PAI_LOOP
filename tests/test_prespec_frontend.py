from __future__ import annotations

from pathlib import Path


STATIC_DIR = Path(__file__).parents[1] / "src" / "pai_loop" / "static"
APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"


def test_pre_specification_is_the_third_notice_discovery_tab_with_two_search_tracks() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'data-view="prespec"' not in html
    assert 'data-view="new"' in html
    assert 'data-notice-search-mode="stored"' in html
    assert 'data-notice-search-mode="pps"' in html
    assert 'data-notice-search-mode="prespec"' in html
    assert 'data-notice-search-mode="stored" aria-pressed="true" aria-controls="noticePanel"' in html
    assert 'data-notice-search-mode="pps" aria-pressed="false" aria-controls="ppsDiscoverySection"' in html
    assert 'data-notice-search-mode="prespec" aria-pressed="false" aria-controls="prespecSection"' in html
    assert "PAI 저장 공고" in html
    assert 'id="prespecSection"' in html
    assert 'id="prespecStoredForm"' in html
    assert 'id="prespecStoredStatusFilter"' in html
    assert 'id="prespecLiveForm"' in html
    assert 'id="prespecLiveFromDate"' in html
    assert 'id="prespecLiveToDate"' in html
    assert 'id="prespecLiveLimit"' in html
    assert 'apiRequest(`/pre-specifications?${params}`)' in source
    assert 'apiRequest("/prespec-discovery/search"' in source
    assert 'apiRequest("/prespec-discovery/save"' in source
    assert 'span > 30' in source
    assert 'const nextMode = ["pps", "prespec"].includes(mode) ? mode : "stored"' in source
    assert 'const targetView = prespecMode ? "prespec" : "new"' in source
    assert 'setView(targetView, { noticeSearchMode: nextMode, focusMain: false })' in source
    assert '["stored", "pps", "prespec"].includes(noticeSearchMode)' in source
    assert 'els.prespecSection.hidden = !prespecMode' in source
    assert 'prespec: ["공고 탐색", "사전규격 탐색"]' in source
    assert 'nextView === "prespec"\n      ? "new"' in source
    assert 'if (initialView !== "prespec") loadApplicationData()' not in source
    assert 'setLayout(state.layout);\n    loadApplicationData();' in source
    assert html.index('id="noticeSection"') < html.index('id="prespecSection"') < html.index('id="resultLearningSection"')


def test_pre_specification_help_explains_boundaries_and_zero_openai_search() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="prespecHelpButton"' in html
    assert 'id="prespecHelpDialog"' in html
    for phrase in (
        "입찰공고 전 단계",
        "검색·저장 문서 분석 0회",
        "선택 저장",
        "분석은 별도 실행",
        "GO 판정 아님",
    ):
        assert phrase in html
    assert "저장 자료 검색은 외부 조회 0회" in html
    assert "나라장터 검색·선택 저장도 문서 분석 0회" in html
    assert "검색 결과가 자동 저장되지 않습니다" in html
    assert "공고는 분석·판단 · 사전규격은 문서 분석" in html


def test_pre_specification_cards_and_saved_detail_expose_required_facts_safely() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    for phrase in ("의견마감", "예산", "첨부", "저장상태", "연결입찰"):
        assert phrase in source
    assert "preSpecificationAgency(record)" in source
    assert "formatBudget(record.budgetAmount)" in source
    assert "record.documentCount" in source
    assert "record.alreadyStored" in source
    assert "detail.linkedBidNoticeNos" in source
    assert "escapeHtml(record.title)" in source
    assert "escapeAttribute(record.registryNo)" in source
    assert "safeHttpUrl(item?.safe_url)" in source
    assert 'rel="noopener noreferrer"' in source


def test_pre_specification_analysis_requires_explicit_cost_approval_and_bounded_polling() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    for phrase in (
        "문서당 최대 2회",
        "사전규격 1건당 총 최대 10회",
        "중복 실행 잠금과 공고별 재시도 대기 적용",
        "GO 판정이 아님",
    ):
        assert phrase in source
    assert "window.confirm" in source
    assert "manualAnalysisAuthHeaders()" in source
    assert "body: JSON.stringify({ run_extraction: true })" in source
    assert "PRESPEC_ANALYSIS_POLL_INTERVAL_MS = 3000" in source
    assert "PRESPEC_ANALYSIS_MAX_POLLS = 40" in source
    assert "PRESPEC_ANALYSIS_POLL_MAX_MS = 120000" in source
    assert "Date.now() < deadline" in source
    assert "for (let attempt = 1; attempt <= PRESPEC_ANALYSIS_MAX_POLLS" in source
    assert "/analysis/${encodeURIComponent(analysisId)}" in source
    assert "void pollPreSpecificationAnalysis" in source
    assert "await pollPreSpecificationAnalysis" not in source
    assert 'credentials: "same-origin"' in source
    assert "X-PAI-LOOP-API-KEY" not in source


def test_pre_specification_styles_distinguish_sources_and_cover_teams_mobile() -> None:
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert ".prespec-track--stored" in styles
    assert ".prespec-track--live" in styles
    assert ".prespec-card--live" in styles
    assert ".prespec-help-dialog" in styles
    assert ".prespec-detail-dialog" in styles
    assert "body.teams-context .prespec-hero" in styles
    assert "@media (max-width: 680px)" in styles
    assert ".prespec-search-form--live { grid-template-columns: 1fr" in styles


def test_pre_specification_assets_use_the_current_release_cache_key() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'href="./styles.css?v=20260908-accounts-v1"' in html
    assert 'src="./app.js?v=20260908-accounts-v1"' in html


def test_pre_specification_completion_uses_one_fail_closed_coverage_helper() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    result_start = source.index("  function renderPreSpecificationAnalysisResult")
    helper_start = source.index("  function derivePreSpecificationCompletion")
    status_start = source.index("  function renderPreSpecificationAnalysisStatus")
    confirm_start = source.index("  function confirmPreSpecificationAnalysis")
    result_body = source[result_start:helper_start]
    helper_body = source[helper_start:status_start]
    status_body = source[status_start:confirm_start]

    assert "derivePreSpecificationCompletion({" in result_body
    assert "derivePreSpecificationCompletion({" in status_body
    assert "total > 0" in helper_body
    assert "processed >= total" in helper_body
    assert "accepted !== null" in helper_body
    assert "accepted >= total" in helper_body
    assert "completed: Boolean(declaredComplete && coverageComplete)" in helper_body
    assert 'declaredComplete && !completion.coverageComplete ? "REVIEW"' in status_body
    assert 'completion.completed ? "완료된 분석 결과" : "분석 상태 확인 필요"' in result_body


def test_pre_specification_search_forms_use_card_safe_responsive_columns() -> None:
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert ".prespec-search-form:not(.prespec-search-form--live) .prespec-field--query" in styles
    assert ".prespec-search-form--live .prespec-field--query { grid-column: 1 / -1; }" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
