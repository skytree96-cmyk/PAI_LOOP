from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


APP_JS = Path(__file__).parents[1] / "src" / "pai_loop" / "static" / "app.js"
INDEX_HTML = APP_JS.with_name("index.html")
STYLES_CSS = APP_JS.with_name("styles.css")


def _function_body(source: str, name: str, next_name: str) -> str:
    pattern = rf"  (?:async )?function {re.escape(name)}\b(?P<body>.*?)\n  (?:async )?function {re.escape(next_name)}\b"
    match = re.search(pattern, source, flags=re.DOTALL)
    assert match, f"{name} frontend contract was not found"
    return match.group("body")


def test_public_document_evidence_is_flattened_with_an_explicit_allowlist() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    collect_body = _function_body(source, "collectNoticeEvidence", "flattenDocumentEvidence")
    body = _function_body(source, "flattenDocumentEvidence", "flattenRequirementEvidence")

    assert "documentEvidence.length ? documentEvidence : requirementEvidence" in collect_body
    for allowed in ("document_name", "requirements", "evidence", "page", "section", "quote", "confidence"):
        assert allowed in body
    for forbidden in (
        "source_payload",
        "response_id",
        "api_key",
        "access_token",
        "company_fact",
        "required_value",
        "attachment_id",
    ):
        assert forbidden not in body
    assert '["ACCEPTED", "COMPLETE"].includes(status)' in body
    assert 'status: "PROVISIONAL"' in body


def test_raw_requirement_evidence_uses_only_public_source_anchors() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    body = _function_body(source, "flattenRequirementEvidence", "normalizeHistory")

    assert "source.source_excerpt" in body
    assert "source.source_location" in body
    assert "source.parse_confidence" in body
    for forbidden in ("fact_key", "required_value", "evidence_key", "company_value"):
        assert forbidden not in body


def test_notice_search_contract_is_global_across_stored_notices() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    load_body = _function_body(source, "loadApplicationData", "applyRuntimeProfile")
    fetch_body = _function_body(source, "fetchNoticePages", "buildNoticeRequestPath")
    request_body = _function_body(source, "buildNoticeRequestPath", "noticeRequestTimeoutMs")
    timeout_body = _function_body(source, "noticeRequestTimeoutMs", "noticeStatusScopeForView")
    scope_body = _function_body(source, "noticeStatusScopeForView", "renderNoticeSearchScope")
    explanation_body = _function_body(source, "renderNoticeSearchScope", "scheduleNoticeSearch")
    filter_body = _function_body(source, "applyFilters", "compareNotices")
    reset_priority_body = _function_body(source, "resetPrioritySearch", "loadPerformance")
    bind_body = _function_body(source, "bindEvents", "refreshCurrentView")
    view_body = _function_body(source, "setView", "setLayout")

    assert 'params.set("q", query)' in request_body
    assert 'if (searchKeywords) params.set("search_keywords", searchKeywords)' in request_body
    assert "searchKeywords && !globalSearch" not in request_body
    assert 'params.set("status", statusScope)' in request_body
    assert '"ENDED"].includes(statusScope)' in request_body
    assert "ANALYZED_ENDED" not in request_body
    assert 'params.set("analysis_state", "EVALUATED")' not in request_body
    assert "noticeStatusScopeForView(state.currentView)" in load_body
    assert "requestedStatusScope !== noticeStatusScopeForView(state.currentView)" in load_body
    assert "noticeStatusScopeForView(state.currentView)" in request_body
    assert '["all", "new", "review", "undecided", "go", "urgent"].includes(state.currentView)' in filter_body
    assert "noticeStatusScopeForView(nextView)" in view_body
    assert 'noticeLifecycleStatus(notice) !== "OPEN"' in filter_body
    assert "!notice.isNew" not in filter_body
    assert "NOTICE_PAGE_SIZE = 200" in source
    assert "offset += NOTICE_PAGE_SIZE" in fetch_body
    assert 'params.set("offset", String(offset))' in request_body
    assert 'params.set("department_id", departmentId)' in request_body
    assert "explicitRanking" not in request_body
    assert "NOTICE_REQUEST_TIMEOUT_MS = 60000" in source
    assert "RANKING_REQUEST_TIMEOUT_MS = 60000" in source
    assert "RANKING_REQUEST_TIMEOUT_MS" in timeout_body
    assert "NOTICE_REQUEST_TIMEOUT_MS" in timeout_body
    assert "globalNoticeSearchActive()" in scope_body
    assert 'return "ALL"' in scope_body
    assert "if (!globalSearch)" in filter_body
    assert 'serverBackedSearch = globalSearch && state.source === "api"' in filter_body
    assert "query && !serverBackedSearch" in filter_body
    assert "notice.noticeKey" in filter_body
    assert 'els.departmentSelect.value = "organization"' in reset_priority_body
    assert 'els.priorityKeywordInput.value = ""' in reset_priority_body
    assert "loadApplicationData({ forceApi: true })" in reset_priority_body
    assert 'els.filterForm.addEventListener("reset"' in bind_body
    assert "저장된 전체 공고 검색" in explanation_body
    assert "공고를 숨기지 않고 표시 순서에만 반영합니다" in explanation_body
    assert "검색만으로 AI 비용은 발생하지 않습니다" in explanation_body
    assert "나라장터에서 아직 수집되지 않은 공고는 포함되지 않습니다" in explanation_body
    assert 'id="noticeSearchHelp"' in html
    assert 'id="noticeSearchScope"' in html
    assert "공고번호 검색" in html
    assert "관심 키워드와 가까운 공고에 정렬 가중치를 적용합니다" in html
    assert "모든 공고는 목록에 그대로 남습니다" in html
    assert "목록 제외 없음" in source


def test_pin_decision_reload_and_current_evaluation_frontend_contract() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    normalize_body = _function_body(source, "normalizeNotice", "mergeRequirementsAndAtomics")
    auth_body = _function_body(
        source, "manualAnalysisAuthHeaders", "clearManualAnalysisToken"
    )
    clear_body = _function_body(
        source, "clearManualAnalysisToken", "requestManualAnalysisToken"
    )
    hydrate_body = _function_body(
        source, "hydrateOperatorDecisions", "openDetail"
    )
    detail_body = _function_body(source, "openDetail", "renderDetail")
    save_body = _function_body(source, "saveDecision", "renderPipelineIntoExisting")

    assert 'evaluationId: stringValue(firstValue(evaluation.id' in normalize_body
    assert "source.evaluation," not in normalize_body
    assert "options.allowLegacyCurrentProjection === true" in normalize_body
    assert "{ allowLegacyCurrentProjection: true }" in source
    assert '["PENDING", "REVIEW", "COLLECTED", "VERSIONED"].includes(declaredAnalysisState)' in normalize_body
    assert "const allowCurrentProjection = hasCurrentEvaluation || allowLegacyCurrentProjection" in normalize_body
    assert "allowCurrentProjection ? source.evaluation_id : null" in normalize_body
    assert "allowCurrentProjection ? firstValue(source.quantitative" in normalize_body
    assert "allowCurrentProjection ? firstValue(source.risk_dimensions" in normalize_body
    assert "const useHistoricalEvaluation = !hasCurrentEvaluation" in normalize_body
    assert "const displayEvaluation = useHistoricalEvaluation ? historicalEvaluation : evaluation" in normalize_body
    assert 'window.sessionStorage.getItem("pai-loop-operator-pin")' in auth_body
    assert 'window.sessionStorage.setItem("pai-loop-operator-pin"' in auth_body
    assert 'window.sessionStorage.removeItem("pai-loop-operator-pin")' in clear_body
    assert 'state.manualAnalysisToken || ""' in hydrate_body
    assert '/operator-decisions/notices/${encodeURIComponent(notice.noticeKey)}' in hydrate_body
    assert "{ ...notice.raw, decisions }" in hydrate_body
    assert "state.notices[index] = merged" in hydrate_body
    assert "merged = await hydrateOperatorDecisions(merged)" in detail_body
    assert "evaluation_id: notice.evaluationId" in save_body
    assert 'error?.status === 409' in save_body
    assert 'includes("평가가 갱신")' in save_body
    assert "hydrateNoticeByKey(notice.noticeKey, { force: true })" in save_body
    assert "현재 브라우저 탭에서만 유지되며 탭을 닫으면 지워집니다" in html
    assert "새로고침하면 입력값은 지워집니다" not in html


def test_api_failure_is_explicit_and_demo_data_requires_demo_query() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    load_body = _function_body(source, "loadApplicationData", "applyRuntimeProfile")
    error_body = _function_body(source, "renderApplicationError", "applyRuntimeProfile")
    demo_body = source[source.index("  function createDemoData") : source.rindex("})();")]

    assert 'query.get("demo") === "1"' in load_body
    assert "activateDemo(`운영 서버 연결 실패" not in load_body
    assert "renderApplicationError(`운영 서버 연결 실패" in load_body
    assert 'state.source = "error"' in error_body
    assert "els.errorState.hidden = false" in error_body
    assert "다시 시도" in error_body
    assert "운영 서버 연결 오류 · 재시도 필요" in source
    assert demo_body.count('status: "OPEN"') == 5


def test_kpi_cards_are_keyboard_buttons_and_open_matching_views() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    bind_body = _function_body(source, "bindEvents", "refreshCurrentView")
    dashboard_body = _function_body(source, "normalizeDashboard", "deriveDashboard")
    derived_body = _function_body(source, "deriveDashboard", "renderAll")
    filter_body = _function_body(source, "applyFilters", "compareNotices")
    view_body = _function_body(source, "setView", "setLayout")

    for view in ("review", "urgent", "result-missing"):
        assert f'data-kpi-view="{view}"' in html
    assert html.count('class="kpi-card__action"') == 3
    assert html.count('aria-pressed="false"') >= 3
    assert "els.kpiViewButtons" in bind_body
    assert "setView(button.dataset.kpiView)" in bind_body
    assert "scrollIntoView" in bind_body
    assert 'state.currentView === "go"' in filter_body
    assert 'effectiveRecommendation(notice) !== "GO"' in filter_body
    assert 'state.currentView === "urgent"' in filter_body
    assert "URGENT_DEADLINE_DAYS" in filter_body
    assert 'timeZone: "Asia/Seoul"' in source
    assert 'state.currentView === "ended"' in filter_body
    assert "isVisibleEndedNotice(notice)" in filter_body
    assert "urgentCount: derived.urgentCount" in dashboard_body
    assert "goCount: derived.goCount" in dashboard_body
    assert "endedCount:" in dashboard_body
    assert "visible_ended_count" in dashboard_body
    assert "cancelled_count" in dashboard_body
    assert "analysis_review_backlog_count" in dashboard_body
    assert 'noticeLifecycleStatus(notice) !== "OPEN"' in derived_body
    assert "reviewCount: notices.filter(needsAnalysisOrReview).length" in derived_body
    assert 'noticeLifecycleStatus(notice) === "OPEN" && effectiveRecommendation(notice) === "GO"' in derived_body
    assert "notices.filter(isVisibleEndedNotice)" in derived_body
    assert "resultMissingCount:" in derived_body
    assert (
        "isVisibleEndedNotice(notice) && !isCancelledNotice(notice) && !notice.hasBidOutcome"
        in derived_body
    )
    assert "kpis.result_missing_count" in dashboard_body
    assert "els.kpiNew.textContent = displayNumber(data.totalNotices)" in source
    assert 'state.currentView === "result-missing"' in filter_body
    assert "!isVisibleEndedNotice(notice) || isCancelledNotice(notice) || notice.hasBidOutcome" in filter_body
    assert "source.has_bid_outcome" in source
    assert "effectiveRecommendation(notice) !== recommendation" in filter_body
    assert 'urgent: "/urgent"' in source
    assert '"result-missing": "/result-missing"' in source
    assert 'noticeLifecycleStatus(notice) === "OPEN" && !notice.decision' in derived_body
    assert 'collected: ["수집 공고", "수집된 전체 공고"]' in view_body
    assert 'go: ["GO 후보", "GO 추천 공고"]' in view_body
    assert 'ended: ["종료·취소 공고", "마감·종료·취소된 전체 공고와 당시 분석 이력"]' in view_body
    assert '"result-missing": ["결과 미기록", "입찰마감 후 결과를 기록해야 할 공고"]' in view_body
    assert "resetNoticeFiltersForView()" in view_body
    assert "state.source === \"api\" || state.loading" in view_body
    assert "requestNeedsReload" in view_body
    assert 'els.priorityKeywordInput.value = ""' in view_body
    assert 'els.departmentSelect.value = "organization"' in view_body
    assert 'els.eligibilityFilter.value = "all"' in view_body
    assert 'els.recommendationFilter.value = "all"' in view_body
    assert ".kpi-card__action:focus-visible" in styles
    assert "3일" in html
    assert "7일" not in html
    assert "72시간" not in html


def test_static_assets_have_a_deterministic_ui_cache_buster() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'href="./styles.css?v=20260904-contrast-paperlogy1"' in html
    assert 'src="./app.js?v=20260904-uiux-p0-v3"' in html


def test_uiux_handoff_contract_separates_states_and_uses_full_screen_detail() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    normalize_body = _function_body(source, "normalizeNotice", "mergeRequirementsAndAtomics")
    row_body = _function_body(source, "renderNoticeRow", "renderNoticeCard")
    detail_body = _function_body(source, "renderDetail", "renderRecommendationCondition")
    condition_body = _function_body(
        source, "renderRecommendationCondition", "renderManualAnalysisDetailAction"
    )
    open_body = _function_body(source, "openDetail", "moveDetailSelection")
    keyboard_body = _function_body(source, "handleGlobalKeydown", "updateNoticeRoute")

    assert "DECIDE WITH EVIDENCE" not in html
    assert "확인이 필요한 공고부터 처리하세요" in html
    assert "검토 대기" in html
    assert "결과 미기록" in html
    assert "저장된 전체 공고" in html
    assert "취소공고" in html

    for status, label in (
        ("PASS", "충족"),
        ("PASS_EXCEPTION", "조건부 충족"),
        ("PASS_CURRENT", "현재 충족"),
        ("REVIEW", "확인 필요"),
        ("FAIL", "미충족"),
    ):
        assert f'{status}: "{label}"' in source
        assert f'value="{status}">{label}' in html

    for color in ("#0f7a3e", "#f5a524", "#b26a00", "#c42b2b"):
        assert color in styles.lower()

    assert "recommendationConditions:" in normalize_body
    assert "recommendationEvidenceCount:" in normalize_body
    assert "ai-judgment" in source
    assert "operator-decision" in source
    assert "recommendation-pill recommendation-pill" not in row_body
    assert 'tabindex="0" role="link"' not in row_body
    assert 'class="notice-title-button" type="button" data-open-notice' in row_body
    assert 'class="detail-link-button" type="button" data-open-notice' in row_body
    assert "전체 상세 보기" in row_body
    assert "<th scope=\"col\">AI 판단</th>" in html
    assert "<th scope=\"col\">담당자 판단</th>" in html
    assert ">참여</span>" in html
    assert ">보류</span>" in html
    assert ">불참</span>" in html

    assert 'summaryMetric("참가자격"' in detail_body
    assert 'summaryMetric("AI 판단"' in detail_body
    assert 'summaryMetric("담당자 판단"' in detail_body
    assert "renderRecommendationCondition(notice)" in detail_body
    assert "조건부 GO · 확인할 조건" in condition_body
    assert "권고 보류 · 조건 근거 없음" in condition_body
    assert "hasPublishedCondition" in condition_body
    assert "conditions.map" in condition_body
    assert "conditions.join" in source
    assert "recommendation_conditions" in source
    assert "recommendation_evidence_count" in source

    assert "width: 100vw" in styles
    assert "transform: translateY(100%)" in styles
    assert "drawerScrim.hidden = true" in open_body
    assert "els.closeDetailButton.focus(" in open_body
    assert 'key === "j" || key === "k"' in keyboard_body
    assert "moveDetailSelection" in keyboard_body
    assert '["Tab", "Escape"].includes(event.key)' in keyboard_body
    assert "trapDrawerFocus(event)" in keyboard_body

    close_body = _function_body(source, "closeDetail", "trapDrawerFocus")
    trap_body = _function_body(source, "trapDrawerFocus", "handleGlobalKeydown")
    assert 'document.querySelectorAll("[data-notice-key]")' in close_body
    assert 'replacement?.querySelector("[data-open-notice]") || replacement' in close_body
    assert "document.contains(trigger)" in close_body
    assert '[tabindex]:not([tabindex="-1"])' in trap_body


def test_external_pps_discovery_and_company_awards_require_explicit_actions() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    search_body = _function_body(source, "searchPpsNotices", "renderPpsDiscovery")
    date_invalidation_body = _function_body(source, "invalidatePpsDiscoveryDates", "normalizePpsCandidate")
    render_body = _function_body(source, "renderPpsDiscovery", "renderPpsCandidate")
    save_body = _function_body(source, "savePpsCandidate", "populateDepartmentProfiles")
    awards_body = _function_body(source, "searchCompanyAwards", "setCompanyAwardsLoading")
    awards_render_body = _function_body(
        source,
        "renderCompanyAwardsView",
        "renderCompanyAwardCard",
    )
    view_body = _function_body(source, "setView", "setLayout")

    assert 'id="ppsDiscoverySection"' in html
    assert 'data-notice-search-mode="stored"' in html
    assert 'data-notice-search-mode="pps"' in html
    assert 'data-notice-search-mode="prespec"' in html
    assert "PAI 저장 공고" in html
    assert "나라장터 용역 공고 실시간 조회" in html
    assert "검색·저장: 문서 분석 0회" in html
    assert 'apiRequest("/pps-discovery/search"' in search_body
    assert "span > 30" in search_body
    assert 'state.noticeSearchMode !== "pps"' in search_body
    assert 'state.source !== "api"' in search_body
    assert "state.ppsDiscovery.submitting" in search_body
    assert 'els.ppsDiscoveryFromDate.addEventListener("change", invalidatePpsDiscoveryDates)' in source
    assert 'els.ppsDiscoveryToDate.addEventListener("change", invalidatePpsDiscoveryDates)' in source
    assert "resetPpsDiscovery" in date_invalidation_body
    assert 'state.noticeSearchMode === "pps"' in render_body
    assert "state.notices.length === 0" in render_body
    assert "const suggestPps" in render_body
    assert "state.filteredNotices.length === 0" not in source
    assert 'apiRequest("/pps-discovery/save"' in save_body
    assert "저장만으로 문서 분석은 시작되지 않습니다" in save_body
    assert "/analysis/request" not in search_body
    assert "/analysis/request" not in save_body
    assert "allow_openai" not in search_body
    assert "allow_openai" not in save_body
    assert "현재 수집 공고 아님" in source
    assert ".pps-discovery" in styles

    assert 'data-view="awards"' in html
    assert 'id="awardResultsSection"' in html
    assert 'value="1058201810"' in html
    assert "formatAwardBusinessNumberInput()" in source
    assert "회사 실적으로 자동 이동하지 않습니다" in html
    assert 'apiRequest("/company-awards/search"' in awards_body
    assert "manualAnalysisAuthHeaders()" in awards_body
    assert "Math.ceil((span + 1) / 28)" in awards_body
    assert 'showToast("낙찰 결과 검색 실패"' in awards_body
    assert "awardResultsErrorMessage.textContent = humanizeError(data.error)" in awards_render_body
    assert "/analysis/request" not in awards_body
    assert "awards: [\"낙찰 분석\", \"회사별 낙찰 결과\"]" in view_body
    assert "els.awardResultsSection.hidden = !awardsView" in view_body


def test_three_track_search_help_cards_and_deep_links_are_explicit() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    request_path_body = _function_body(source, "buildNoticeRequestPath", "noticeRequestTimeoutMs")
    mode_render_body = _function_body(source, "renderNoticeSearchMode", "setNoticeSearchMode")
    mode_body = _function_body(source, "setNoticeSearchMode", "openNoticeSearchHelpDialog")
    schedule_body = _function_body(source, "scheduleNoticeSearch", "submitNoticeSearch")
    card_body = _function_body(source, "renderPpsCandidate", "handlePpsDiscoveryAction")
    hydrate_body = _function_body(source, "hydrateNoticeByKey", "openDetail")
    route_body = _function_body(source, "noticeDetailHref", "clearNoticeRoute")
    copy_body = _function_body(source, "copyCurrentNoticeLink", "openCurrentNoticeSourceDialog")

    assert 'id="noticeSearchHelpButton"' in html
    assert 'aria-haspopup="dialog"' in html
    assert 'aria-controls="noticeSearchHelpDialog"' in html
    assert 'id="noticeSearchHelpDialog"' in html
    assert "세 가지 탐색은 목적과 비용이 다릅니다" in html
    assert "공고는 분석·판단 · 사전규격은 문서 분석" in html
    assert 'data-notice-search-mode="prespec"' in html
    assert 'state.noticeSearchMode = nextMode' in mode_body
    assert 'const targetView = prespecMode ? "prespec" : "new"' in mode_body
    assert 'setView(targetView, { noticeSearchMode: nextMode, focusMain: false })' in mode_body
    assert 'els.prespecSection.hidden = !prespecMode' in mode_render_body
    assert 'const storedMode = state.noticeSearchMode === "stored"' in request_path_body
    assert 'const query = storedMode ? els.searchInput?.value.trim() || "" : ""' in request_path_body
    assert 'if (state.noticeSearchMode === "pps")' in schedule_body
    assert "renderPpsDiscovery();\n      return;" in schedule_body

    for label in ("나라장터 실시간", "PAI LOOP 저장됨", "판단 필요", "판단 완료"):
        assert label in card_body
    assert "data-stored-notice-link" in card_body
    assert "저장된 공고로 이동" in card_body
    assert "저장된 판단 결과 보기" in card_body
    assert "data-pps-analysis-key" in card_body
    assert "manualAnalysisAvailability(availabilityNotice" in card_body
    assert "canonicalStateKnown: Boolean(storedNotice)" in card_body

    assert 'url.searchParams.set("notice", noticeKey)' in route_body
    assert "new URL(window.location.href)" in route_body
    assert 'apiRequest(`/notices/${encodeURIComponent(noticeKey)}`)' in hydrate_body
    assert "state.notices.push(hydrated)" in hydrate_body
    assert "noticeDetailHref(notice.noticeKey)" in copy_body
    assert "notice.sourceUrl ||" not in copy_body
    assert ".notice-search-modes" in styles
    assert ".notice-search-help-dialog" in styles
    assert ".pps-candidate__detail-link" in styles


def test_sidebar_work_groups_are_clickable_persistent_disclosures() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    for group, controls_id in (
        ("search", "navGroupSearchItems"),
        ("decision", "navGroupDecisionItems"),
        ("learning", "navGroupLearningItems"),
    ):
        assert f'data-nav-group="{group}"' in html
        assert f'aria-controls="{controls_id}"' in html
        assert f'id="{controls_id}"' in html
    assert html.count('class="nav-group-toggle"') == 3
    assert html.count('aria-expanded="true"') >= 3
    assert 'document.querySelectorAll(".nav-group-toggle[aria-controls]")' in source
    assert 'button.addEventListener("click", () => toggleNavigationGroup(button))' in source
    assert "items.hidden = !expanded" in source
    assert "window.localStorage.getItem(NAV_GROUP_STORAGE_KEY)" in source
    assert "window.localStorage.setItem(NAV_GROUP_STORAGE_KEY" in source
    assert 'parsed && typeof parsed === "object" && !Array.isArray(parsed)' in source
    assert source.index("setView(initialView") < source.index("restoreNavigationGroups();")
    assert "revealActiveNavigationGroup(navigationView)" in source
    assert ".nav-group-toggle" in styles
    assert '.nav-group-toggle[aria-expanded="false"] svg' in styles
    assert ".nav-group-items[hidden]" in styles
    assert '.nav-item[data-view="all"] .nav-icon svg' in styles
    assert ".nav-item:first-child .nav-icon svg" not in styles


def test_quantitative_ui_separates_source_validation_from_activation() -> None:
    app = APP_JS.read_text(encoding="utf-8")

    assert "source_validation_status" in app
    assert "activation_status" in app
    assert "activation_reasons" in app
    assert 'SOURCE_VALIDATED: "원문 검증 완료"' in app
    assert 'AUTO_ACTIVE: "규칙 자동 활성"' in app
    assert 'REVIEW_REQUIRED: "자동 산정 보류"' in app
    assert "FACT_DIMENSIONS_UNMODELED" in app
    assert "점수 산출조건이 아직 구조화되지 않았습니다" in app
    assert '"배점표 후보 확인 · 원문 검증 보류"' in app
    assert '"원문 위치 검증 완료 · 공개 화면 비공개"' in app
    assert '"자동 산정 가능한 항목 없음"' in app
    assert "수기 기술평가 또는 검증 보류 항목에 임의 점수를 넣지 않습니다" in app
    assert 'UNKNOWN_METRIC: "제안서·제품·수기평가 항목' in app
    assert 'AVAILABLE: "배점표 연결"' not in app

    mentor_brief = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "PAI_LOOP_TEAM_MENTOR_BRIEF_v0.8.0.md"
    ).read_text(encoding="utf-8")
    assert "사람 승인 후에만 규칙 버전으로 승격" not in mentor_brief
    assert "반복 사람 승인 없이 `AUTO_ACTIVE`" in mentor_brief


def test_quantitative_cache_refreshes_without_allowing_stale_responses_to_win() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    load_body = _function_body(source, "loadApplicationData", "hydrateApplicationMetadata")
    save_body = _function_body(source, "savePpsCandidate", "populateDepartmentProfiles")
    manual_body = _function_body(source, "requestManualAnalysis", "handleNoticeKeydown")
    invalidation_body = _function_body(
        source,
        "invalidateQuantitativeEstimate",
        "loadQuantitativeEstimate",
    )
    loader_body = _function_body(source, "loadQuantitativeEstimate", "renderQuantAndRisk")

    assert "state.quantitativeEstimates = {};" in load_body
    assert "const reloadQuantitative = quantitativeEstimateIsVisible(savedNoticeKey)" in save_body
    assert "invalidateQuantitativeEstimate(savedNoticeKey, { forceReload: reloadQuantitative })" in save_body
    assert "const reloadQuantitative = quantitativeEstimateIsVisible(noticeKey)" in manual_body
    assert "invalidateQuantitativeEstimate(noticeKey, { forceReload: reloadQuantitative })" in manual_body
    assert "delete state.quantitativeEstimates[noticeKey]" in invalidation_body
    assert "loadQuantitativeEstimate(noticeKey, { force: true })" in invalidation_body
    assert "const requestToken = Symbol(noticeKey)" in loader_body
    assert loader_body.count("?.requestToken !== requestToken") == 3
    assert loader_body.count("requestToken }") >= 2


def test_ended_notice_scope_is_db_only_visible_and_status_aware() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    scope_body = _function_body(source, "noticeStatusScopeForView", "renderNoticeSearchScope")
    normalize_body = _function_body(source, "normalizeNotice", "mergeRequirementsAndAtomics")
    lifecycle_body = _function_body(source, "noticeLifecycleStatus", "isEndedNotice")
    ended_body = _function_body(source, "isVisibleEndedNotice", "noticeLifecycleLabel")
    label_body = _function_body(source, "noticeLifecycleLabel", "noticeLifecycleBadge")
    badge_body = _function_body(source, "noticeLifecycleBadge", "recommendationPill")
    detail_body = _function_body(source, "renderDetail", "detailFact")

    assert '["ended", "result-missing"].includes(view)' in scope_body
    assert 'return "ENDED"' in scope_body
    assert "provider_disposition" in normalize_body
    assert "provider_event_kind" in normalize_body
    assert "provider_changed_at" in normalize_body
    assert "조달청 취소 공고로 확인되어 현재 입찰 검토 대상에서 제외되었습니다" in source
    assert 'providerDisposition === "CANCELLED"' in lifecycle_body
    assert 'return "CANCELLED"' in lifecycle_body
    assert 'status === "CLOSED"' in lifecycle_body
    assert 'status === "EXPIRED"' in lifecycle_body
    assert 'deadline.getTime() < Date.now()' in lifecycle_body
    assert "isCancelledNotice(notice)" in ended_body
    assert 'notice.analysisState === "EVALUATED"' in ended_body
    assert 'lifecycle === "CANCELLED"' in label_body
    assert 'return "취소공고"' in label_body
    assert 'return lifecycle === "CLOSED" ? "공고 종료" : "입찰마감 경과"' in label_body
    assert "notice-lifecycle-badge--cancelled" in badge_body
    assert ".notice-lifecycle-badge--cancelled" in styles
    assert ".detail-tag--cancelled" in styles
    assert "notice-lifecycle-badge" in badge_body
    assert "noticeLifecycleLabel(notice)" in detail_body
    assert "isCancelledNotice(notice)" in detail_body
    assert 'id="kpiEnded"' in html
    assert "운영 대상에서 제외된 공고" in html
    assert "grid-template-columns: repeat(6, minmax(0, 1fr))" in styles


def test_cancelled_notice_decision_entry_points_are_strictly_read_only() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    preview_body = _function_body(
        source, "focusDecisionDockFromPreview", "renderExistingDecision"
    )
    existing_body = _function_body(source, "renderExistingDecision", "selectTab")
    toggle_body = _function_body(source, "toggleCommentField", "updateDecisionButton")
    button_body = _function_body(source, "updateDecisionButton", "saveDecision")
    save_body = _function_body(source, "saveDecision", "renderPipelineIntoExisting")
    teams_body = _function_body(source, "renderTeamsPreview", "buildAdaptiveCardPayload")
    card_body = _function_body(source, "buildAdaptiveCardPayload", "recordTeamsMockSend")

    assert "if (isCancelledNotice(notice))" in preview_body
    assert preview_body.index("if (isCancelledNotice(notice))") < preview_body.index(
        "if (!canWriteDecision())"
    )
    assert "const cancelled = isCancelledNotice(notice)" in existing_body
    assert "input.disabled = cancelled ||" in existing_body
    assert "els.toggleCommentButton.disabled = cancelled ||" in existing_body
    assert "els.decisionComment.disabled = cancelled ||" in existing_body
    assert "과거 판단 기록(참고용)" in existing_body
    assert "담당자 판단을 새로 저장할 수 없습니다" in existing_body
    assert "if (isCancelledNotice(state.selectedNotice)) return" in toggle_body
    assert "const cancelled = isCancelledNotice(state.selectedNotice)" in button_body
    assert "els.saveDecisionButton.disabled = cancelled ||" in button_body
    assert '"취소 공고 · 저장 불가"' in button_body
    assert "if (isCancelledNotice(notice))" in save_body
    assert save_body.index("if (isCancelledNotice(notice))") < save_body.index(
        "if (!canWriteDecision())"
    )
    assert "취소된 공고에는 담당자 판단을 새로 저장할 수 없습니다" in save_body
    assert "els.teamsPreviewDecisionButton.disabled = cancelled ||" in teams_body
    assert "...(cancelled ? [] : [" in card_body
    assert 'action: "OPEN_DECISION"' in card_body


def test_cancelled_notice_presentation_never_promotes_historical_go_as_current() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    status_body = _function_body(source, "analysisStatusPill", "analysisRecommendationPill")
    recommendation_body = _function_body(
        source, "analysisRecommendationPill", "sourceKindBadge"
    )
    status_label_body = _function_body(
        source, "analysisStatusLabel", "analysisRecommendationLabel"
    )
    recommendation_label_body = _function_body(
        source, "analysisRecommendationLabel", "formatRelativeDateTime"
    )
    detail_body = _function_body(source, "renderDetail", "detailFact")
    teams_body = _function_body(source, "renderTeamsPreview", "buildAdaptiveCardPayload")
    card_body = _function_body(source, "buildAdaptiveCardPayload", "recordTeamsMockSend")

    assert status_body.index("isCancelledNotice(notice)") < status_body.index(
        "isDocumentQualityReview(notice)"
    )
    assert "취소 공고" in status_body
    assert recommendation_body.index(
        "isCancelledNotice(notice)"
    ) < recommendation_body.index("isDocumentQualityReview(notice)")
    assert 'aiJudgmentMarkup("추천 비활성", "취소 공고"' in recommendation_body
    assert "if (isCancelledNotice(notice)) return \"취소 공고\"" in status_label_body
    assert (
        "if (isCancelledNotice(notice)) return \"취소 · 추천 비활성\""
        in recommendation_label_body
    )
    assert 'cancelled ? "취소 공고"' in detail_body
    assert 'cancelled ? "과거 분석 참고"' in detail_body
    assert "취소 공고 · 현재 검토 제외" in teams_body
    assert "analysisRecommendationLabel(notice)" in teams_body
    assert "취소 공고 · 현재 검토 제외" in card_body
    assert "analysisRecommendationLabel(notice)" in card_body
    assert 'cancelled ? "PAI LOOP · 취소 공고 알림"' in card_body
    assert 'cancelled ? "과거 분석 참고"' in card_body


def test_manual_analysis_polling_covers_ten_attachment_bounded_continuations() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    request_body = _function_body(source, "requestManualAnalysis", "handleNoticeKeydown")

    assert "MANUAL_ANALYSIS_POLL_INTERVAL_MS = 3000" in source
    assert "MANUAL_ANALYSIS_MAX_POLLS = 900" in source
    assert "poll < MANUAL_ANALYSIS_MAX_POLLS" in request_body
    assert "window.setTimeout(resolve, MANUAL_ANALYSIS_POLL_INTERVAL_MS)" in request_body


def test_manual_analysis_action_covers_incomplete_attachment_audits_and_confirms_cost() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    normalize_state_body = _function_body(source, "normalizeAnalysisState", "normalizeAnalysisReason")
    normalize_reason_body = _function_body(source, "normalizeAnalysisReason", "normalizeRecommendation")
    eligibility_body = _function_body(source, "isActionableEligibilityReview", "needsAnalysisOrReview")
    backlog_body = _function_body(source, "needsAnalysisOrReview", "nullableNumberSort")
    action_body = _function_body(source, "manualAnalysisAvailability", "manualAnalysisLabel")
    label_body = _function_body(source, "manualAnalysisLabel", "confirmManualAnalysis")
    confirm_body = _function_body(source, "confirmManualAnalysis", "manualAnalysisAction")
    request_body = _function_body(source, "requestManualAnalysis", "handleNoticeKeydown")

    assert 'if (normalized === "ANALYZED") return "ANALYZED"' in normalize_state_body
    assert '["EVALUATED", "COMPLETE"].includes(normalized)' in normalize_state_body
    assert 'analysisState === "ANALYZED" && code === "ANALYZED"' in normalize_reason_body
    assert "EVALUATION_MISSING" in normalize_reason_body
    assert "isDocumentQualityReview(notice)" in eligibility_body
    assert "!notice.analysisAttachmentCoverageComplete" in backlog_body
    assert 'notice.analysisState !== "EVALUATED"' in backlog_body
    assert 'notice.analysisState === "EVALUATED"' in action_body
    assert "notice.analysisAttachmentCoverageComplete" in action_body
    assert 'code: "RECOMPUTE_CURRENT"' in action_body
    assert 'notice.analysisState === "ANALYZED"' in label_body
    assert "판단 실행" in label_body
    assert "첨부 전체 재분석" in label_body
    assert "window.confirm" in confirm_body
    assert "state.manualAnalysisPolicy?.max_attachments" in confirm_body
    assert "policyMax * 2" in confirm_body
    assert "문서 재분석 없이" in confirm_body
    assert "문서 분석 요청 상한" in confirm_body
    assert "Claude" not in confirm_body
    assert "검색" not in confirm_body
    assert "if (!confirmManualAnalysis(notice, availability)) return" in request_body
    assert request_body.index("if (!confirmManualAnalysis(notice, availability)) return") < request_body.index(
        'state.manualAnalysisRequests.set(noticeKey, "running")'
    )


def test_notice_sort_groups_pass_review_pending_and_fail_before_secondary_order() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    compare_body = _function_body(source, "compareNotices", "analysisPriorityRank")
    rank_body = _function_body(source, "analysisPriorityRank", "nullableNumberSort")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "analysisPriorityRank(a) - analysisPriorityRank(b)" in compare_body
    assert 'sort === "department"' in compare_body
    assert 'sort === "readiness"' in compare_body
    assert 'sort === "risk"' in compare_body
    assert "nullableDateSort(a.deadline, b.deadline)" in compare_body
    assert 'eligibility.startsWith("PASS")) return 0' in rank_body
    assert "isActionableEligibilityReview(notice)) return 1" in rank_body
    assert 'eligibility === "FAIL") return 3' in rank_body
    assert "return 2" in rank_body
    assert '<option value="judgement">판정 우선 · 마감 임박순</option>' in html
    assert "판정 우선 · 부서 적합도순" in html


def test_department_recommendation_and_region_routing_are_rendered_separately() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    normalize_body = _function_body(source, "normalizeDepartmentRanking", "normalizeNotice")
    notice_body = _function_body(source, "normalizeNotice", "mergeRequirementsAndAtomics")
    compare_body = _function_body(source, "compareNotices", "analysisPriorityRank")
    badge_body = _function_body(source, "departmentPriorityBadge", "setLoading")

    for field in (
        "recommendation_tier",
        "top_recommendation_eligible",
        "review_candidate",
        "business_score",
        "routing_score",
    ):
        assert field in normalize_body
    assert "department_review_candidates" in notice_body
    assert "region_routing" in notice_body
    assert "departmentSortSignal" in compare_body
    assert "topDepartmentRankings[0]" in badge_body
    assert "departmentReviewCandidates[0]" in badge_body
    assert "regionRouting[0]" in badge_body
    assert '"부서 추천"' in badge_body
    assert '"추가 검토"' in badge_body
    assert '"지역 라우팅"' in badge_body


def test_private_match_uses_public_text_lines_instead_of_a_dangling_label() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    normalize_body = _function_body(source, "normalizePrivateMatchItem", "eligibilityRequirementsForDisplay")
    fact_key_body = _function_body(source, "normalizeCompanyFactKey", "collectPrivateMatchDetails")
    details_body = _function_body(source, "collectPrivateMatchDetails", "renderPrivateMatchPreview")
    render_body = _function_body(source, "renderPrivateMatchItem", "privateMatchCategoryLabel")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    for allowed in ("normalized_condition", "source_excerpt", "description", "action", "why"):
        assert allowed in normalize_body + details_body
    assert "공개 가능한 상세 판단 근거가 아직 연결되지 않았습니다" in details_body
    assert "private-match-details" in render_body
    assert "회사 증빙 대조 대상이 아닌 공고 정보·체크 항목입니다" in render_body
    assert "별도 회사 증빙 불필요" not in render_body
    assert "별도회사증빙불필요" in fact_key_body
    assert "NOTREQUIRED" in fact_key_body
    assert 'endsWith(":__NONE__")' in fact_key_body
    assert ".private-match-details" in styles


def test_public_eligibility_policy_is_supplemental_and_422_is_not_an_error() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    load_body = _function_body(
        source,
        "loadPrivateMatchPreview",
        "normalizePrivateMatchPreview",
    )
    adapter_body = _function_body(
        source,
        "eligibilityRequirementsForDisplay",
        "publicEligibilityPolicyPending",
    )
    panel_body = _function_body(
        source,
        "renderEligibilityPanel",
        "normalizeCompanyFactKey",
    )
    preview_body = _function_body(
        source,
        "renderPrivateMatchPreview",
        "privateMatchMetric",
    )
    actions_body = _function_body(source, "renderActions", "renderEvidence")
    detail_body = _function_body(source, "renderDetail", "renderManualAnalysisDetailAction")

    assert "error?.status === 422" in load_body
    assert 'status: noVerifiedPolicy ? "unavailable" : "error"' in load_body
    assert "종합 판단을 임의로 보완하지 않습니다" in load_body
    assert "renderEligibilityPanel(notice)" in load_body
    assert "renderActions(notice)" in load_body

    assert '.filter((item) => item?.category === "ELIGIBILITY")' in adapter_body
    assert 'source: "PUBLIC_POLICY_SUPPLEMENT"' in adapter_body
    assert 'notice.analysisState === "EVALUATED"' in adapter_body
    assert 'notice.eligibilityStatus === "PASS"' not in adapter_body
    assert 'currentEvidence ? outcome : "REVIEW"' in adapter_body
    assert "현재 첨부 검증이 완료되지 않았습니다" in adapter_body
    assert "공고 마감일 기준" in adapter_body

    assert "analysisStatusPill(notice)" in panel_body
    assert "검증된 공개 자격정책이 없습니다" in panel_body
    assert "종합 판단을 PASS로 보완하지 않습니다" in panel_body
    assert "eligibilityRequirementsForDisplay(notice)" in actions_body
    assert 'requirement.status))' in actions_body
    assert "eligibilityRequirementsForDisplay(notice)" in detail_body
    assert "renderEligibilityPanel(notice, requirements)" in detail_body

    # The complete four-class preview stays independent from the supplemental
    # eligibility cards and keeps all server-provided policy items.
    for label in ("적격성", "행동 필요", "체크리스트", "정보"):
        assert label in preview_body
    assert "data.matches.map(renderPrivateMatchItem)" in preview_body
    assert "unavailable:" in preview_body


def test_mixed_eligibility_results_preserve_individual_verdicts() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    adapter = "function eligibilityRequirementsForDisplay" + _function_body(
        source, "eligibilityRequirementsForDisplay", "publicEligibilityPolicyPending"
    )
    script = r'''
const assert = require("node:assert/strict");
const arrayValue = value => Array.isArray(value) ? value : [];
const stringValue = (value, fallback = "") => value == null ? fallback : String(value);
const isDocumentQualityReview = notice => Boolean(notice.documentQualityReview);
const state = { privateMatchPreviews: { "SYN-MIXED": { status: "ready", data: {
  matches: [
    { category: "ELIGIBILITY", outcome: "PASS_CURRENT" },
    { category: "ELIGIBILITY", outcome: "PASS_EXCEPTION" },
    { category: "ELIGIBILITY", outcome: "FAIL_CONFIRMED", blocking: true },
    { category: "ELIGIBILITY", outcome: "REVIEW", blocking: true },
    { category: "CHECKLIST", outcome: "CHECK_REQUIRED" },
    { category: "INFORMATION", outcome: "INFORMATION" },
  ]
} } } };
const notice = { noticeKey: "SYN-MIXED", analysisState: "EVALUATED", eligibilityStatus: "FAIL" };
const before = JSON.stringify(notice);
assert.deepEqual(eligibilityRequirementsForDisplay(notice).map(x => x.status),
  ["PASS_CURRENT", "PASS_EXCEPTION", "FAIL", "REVIEW"]);
assert.equal(JSON.stringify(notice), before);
for (const incomplete of [
  { ...notice, analysisState: "PENDING" },
  { ...notice, documentQualityReview: true },
]) {
  assert.deepEqual(eligibilityRequirementsForDisplay(incomplete).map(x => x.status),
    ["REVIEW", "REVIEW", "REVIEW", "REVIEW"]);
}
const stored = [{ status: "FAIL", source: "STORED" }];
assert.equal(eligibilityRequirementsForDisplay({ ...notice, requirements: stored }), stored);
state.privateMatchPreviews[notice.noticeKey].status = "loading";
assert.deepEqual(eligibilityRequirementsForDisplay(notice), []);
'''
    subprocess.run(["node", "-e", adapter + "\n" + script], check=True)


def test_unanalysed_reason_adapter_maps_document_failure_codes_to_korean() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    normalize_body = _function_body(source, "normalizeAnalysisReason", "normalizeRecommendation")

    for code in (
        "NOT_SELECTED",
        "ATTACHMENT_MANIFEST_MISSING",
        "ATTACHMENT_NONE",
        "HWP_ONLY_UNSUPPORTED",
        "HWPX_EXTRACT_FAILED",
        "PDF_EXTRACT_FAILED",
        "OPENAI_REVIEW",
        "UNVERIFIED_QUOTE",
        "QUOTE_UNVERIFIED",
        "PARTIAL",
    ):
        assert code in source
    assert "analysis_reason_code" in normalize_body
    assert "analysisReasonCode" in normalize_body
    assert "폐기된 공고가 아닙니다" in source
    assert "notice.analysisReason" in source


def test_official_notice_link_opens_an_accessible_confirmation_dialog() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    open_body = _function_body(source, "openCurrentNoticeSourceDialog", "closeSourceLinkDialog")

    for element_id in (
        "openSourceDialogButton",
        "sourceLinkDialog",
        "sourceLinkDialogTitle",
        "sourceLinkDialogMessage",
        "sourceLinkOpenAnchor",
        "closeSourceLinkDialogButton",
    ):
        assert f'id="{element_id}"' in html
        assert f'"{element_id}"' in source
    assert 'aria-labelledby="sourceLinkDialogTitle"' in html
    assert 'aria-describedby="sourceLinkDialogMessage"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "safeHttpUrl(notice.sourceUrl)" in open_body
    assert "showModal" in open_body
    assert 'removeAttribute("href")' in open_body
    assert "window.location" not in open_body
    assert ".source-link-dialog::backdrop" in styles
    assert '<span>나라장터 원문 열기</span>' in html


def test_document_quality_review_is_not_presented_as_eligibility_review() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    dashboard_body = _function_body(source, "deriveDashboard", "renderAll")
    filter_body = _function_body(source, "applyFilters", "compareNotices")
    quality_body = _function_body(
        source, "isDocumentQualityReview", "isActionableEligibilityReview"
    )
    status_body = _function_body(source, "analysisStatusPill", "analysisRecommendationPill")
    reason_body = _function_body(source, "normalizeAnalysisReason", "normalizeRecommendation")
    pipeline_body = _function_body(source, "renderPipeline", "renderRequirement")
    detail_body = _function_body(source, "renderDetail", "detailFact")
    teams_body = _function_body(source, "renderTeamsPreview", "buildAdaptiveCardPayload")
    teams_body += _function_body(source, "buildAdaptiveCardPayload", "recordTeamsMockSend")
    recommendation_body = _function_body(source, "analysisRecommendationLabel", "formatRelativeDateTime")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "needsAnalysisOrReview" in dashboard_body
    assert "isDocumentQualityReview" in dashboard_body
    assert "needsAnalysisOrReview(notice)" in filter_body
    assert 'String(notice.analysisReasonCode || "")' in quality_body
    assert "notice.reasonCode" not in quality_body
    assert "ATTACHMENT_COVERAGE_INCOMPLETE" in quality_body
    assert "DOCUMENT_EXTRACT_FAILED" in quality_body
    assert "분석 보완" in status_body
    assert "원문 근거 검증을 보완해야 참가자격을 판단할 수 있습니다" in status_body
    assert 'analysisState === "EVALUATED"' in reason_body
    assert reason_body.index("ANALYSIS_REASON_LABELS[code]") < reason_body.index('analysisState === "EVALUATED"')
    assert "analysisStatusLabel(notice)" in pipeline_body
    assert 'qualityReview ? "근거 보완 후 산정"' in detail_body
    assert 'notice.analysisReason || "원문 근거 검증을 보완한 뒤 자격과 추천을 확정합니다."' in detail_body
    assert "근거 보완'" in detail_body
    assert "근거 보완 · 판단 보류" in teams_body
    assert "근거 보완 후 산정" in teams_body
    assert 'if (isDocumentQualityReview(notice)) return "판단 보류"' in recommendation_body
    assert "체크리스트와 정보는 그 자체로 참가자격 ‘확인 필요’를 만들지 않습니다." in source
    assert "자격 검토" in html


def test_missing_risk_is_labeled_as_insufficient_evidence_not_zero() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    render_body = _function_body(source, "renderRiskPanel", "renderQuantitativePending")
    display_body = _function_body(source, "riskDisplayValue", "analysisStatusLabel")

    assert "riskReady" in render_body
    assert "산정 보류 · 확인된 축" in render_body
    assert "score === null ? null" in source
    assert 'score === null ? "미확인"' in render_body
    assert "임의의 0점 대신 근거가 확보된 위험 축만 계산합니다" in render_body
    assert "verifiedAxes < 4" in display_body
    assert "산정 보류" in display_body


def test_recommendation_filter_and_errors_fail_closed_without_raw_internal_text() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    eligibility_body = _function_body(source, "effectiveEligibilityStatus", "effectiveRecommendation")
    effective_body = _function_body(source, "effectiveRecommendation", "aiJudgmentMarkup")
    filter_body = _function_body(source, "applyFilters", "compareNotices")
    error_body = _function_body(source, "humanizeError", "isEditableTarget")
    editor_error_body = _function_body(source, "editorErrorMessage", "formatAwardBusinessNumberInput")

    assert "PASS_CURRENT: 1" in eligibility_body
    assert "PASS_EXCEPTION: 2" in eligibility_body
    assert "FAIL: 4" in eligibility_body
    assert "arrayValue(notice?.requirements).forEach" in eligibility_body
    assert "requirement?.mandatory !== false" in eligibility_body
    assert "severity[candidate] > severity[current]" in eligibility_body
    assert 'return worst === "UNKNOWN" ? "REVIEW" : worst' in eligibility_body
    assert 'value === "GO"' in effective_body
    assert '["FAIL", "REVIEW", "UNKNOWN"].includes(effectiveEligibilityStatus(notice))' in effective_body
    assert '["CONDITIONAL_GO", "HOLD"].includes(value)' in effective_body
    assert '!arrayValue(notice?.recommendationConditions).length' in effective_body
    assert 'return "DEFERRED"' in effective_body
    assert "effectiveRecommendation(notice) !== recommendation" in filter_body

    for status in (401, 403, 404, 409, 422, 429):
        assert f"status === {status}" in error_body
    assert "status >= 500" in error_body
    assert "네트워크 연결을 확인한 뒤 다시 시도해 주세요" in error_body
    assert "요청을 완료하지 못했습니다" in error_body
    assert "return message" not in error_body
    assert "return humanizeError(error)" in editor_error_body


def test_connection_status_exposes_delay_and_exact_kst_last_success_time() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    load_body = _function_body(source, "loadApplicationData", "fetchNoticePages")
    status_body = _function_body(source, "setSystemStatus", "showDemoBanner")
    kst_body = _function_body(source, "formatKstDateTime", "formatCalendarDate")

    assert "window.clearTimeout(state.connectionDelayTimer)" in load_body
    assert "state.connectionDelayTimer = window.setTimeout" in load_body
    assert "sequence === state.requestSequence && state.loading" in load_body
    assert 'setSystemStatus("delayed")' in load_body
    assert "}, 10000)" in load_body
    assert 'mode === "delayed"' in status_body
    assert 'els.systemStatusText.textContent = "연결 지연 · 10초 초과"' in status_body
    assert "현재 화면: 서버 저장본 · 조회" in status_body
    assert "데이터 동기화" in status_body
    assert '"서버 저장본 · 조회 시각 확인 중"' in status_body
    assert 'timeZone: "Asia/Seoul"' in kst_body
    assert "hour12: false" in kst_body
    assert "KST" in kst_body


def test_pai_bot_teams_access_is_member_only_and_fails_closed_until_configured() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    configure_body = _function_body(source, "configurePaiBotTeamsAccess", "openPaiBotTeams")
    open_body = _function_body(source, "openPaiBotTeams", "safePaiBotTeamsUrl")

    assert 'id="paiBotTeamsButton"' in html
    assert "PAI 봇 Teams 열기" in html
    assert "등록된 개발자 전용" in html
    assert 'aria-disabled="true"' in html
    assert "disabled" in html
    config_match = re.search(
        r'<script id="paiLoopRuntimeConfig" type="application/json">(?P<config>.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert config_match
    runtime_config = json.loads(config_match.group("config"))
    assert runtime_config["paiBotTeamsUrl"].startswith("https://teams.microsoft.com/")
    assert 'document.getElementById("paiLoopRuntimeConfig")' in source
    assert "safePaiBotTeamsUrl(PAI_BOT_TEAMS_URL)" in configure_body
    assert "disabled = !isReady" in configure_body
    assert 'dataset.state = isReady ? "ready" : "pending"' in configure_body
    assert 'window.open(teamsUrl, "_blank", "noopener,noreferrer")' in open_body
    assert 'url.protocol === "https:"' in source
    assert 'url.hostname.toLowerCase() === "teams.microsoft.com"' in source
    assert ".pai-bot-access__note" in styles
    assert ".button--teams:disabled" in styles
