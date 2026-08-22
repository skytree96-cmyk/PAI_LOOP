from __future__ import annotations

import json
import re
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


def test_notice_search_contract_uses_server_query_and_open_status() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    load_body = _function_body(source, "loadApplicationData", "applyRuntimeProfile")
    fetch_body = _function_body(source, "fetchNoticePages", "buildNoticeRequestPath")
    request_body = _function_body(source, "buildNoticeRequestPath", "noticeRequestTimeoutMs")
    timeout_body = _function_body(source, "noticeRequestTimeoutMs", "noticeStatusScopeForView")
    filter_body = _function_body(source, "applyFilters", "compareNotices")
    view_body = _function_body(source, "setView", "setLayout")

    assert 'params.set("q", query)' in request_body
    assert 'params.set("status", statusScope)' in request_body
    assert '"ENDED"].includes(statusScope)' in request_body
    assert "ANALYZED_ENDED" not in request_body
    assert 'params.set("analysis_state", "EVALUATED")' not in request_body
    assert "noticeStatusScopeForView(state.currentView)" in load_body
    assert "requestedStatusScope !== noticeStatusScopeForView(state.currentView)" in load_body
    assert "noticeStatusScopeForView(state.currentView)" in request_body
    assert '["all", "new", "review", "undecided", "go", "urgent"].includes(state.currentView)' in filter_body
    assert "noticeStatusScopeForView(view)" in view_body
    assert 'noticeLifecycleStatus(notice) !== "OPEN"' in filter_body
    assert "!notice.isNew" not in filter_body
    assert "NOTICE_PAGE_SIZE = 200" in source
    assert "offset += NOTICE_PAGE_SIZE" in fetch_body
    assert 'params.set("offset", String(offset))' in request_body
    assert 'params.set("department_id", departmentId)' in request_body
    assert "explicitRanking" not in request_body
    assert "NOTICE_REQUEST_TIMEOUT_MS = 30000" in source
    assert "RANKING_REQUEST_TIMEOUT_MS = 60000" in source
    assert "RANKING_REQUEST_TIMEOUT_MS" in timeout_body
    assert "NOTICE_REQUEST_TIMEOUT_MS" in timeout_body
    assert 'id="noticeSearchHelp"' in INDEX_HTML.read_text(encoding="utf-8")


def test_api_failure_is_explicit_and_demo_data_requires_demo_query() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    load_body = _function_body(source, "loadApplicationData", "applyRuntimeProfile")
    error_body = _function_body(source, "renderApplicationError", "applyRuntimeProfile")
    demo_body = source[source.index("  function createDemoData") : source.rindex("})();")]

    assert 'query.get("demo") === "1"' in load_body
    assert "activateDemo(`서버 API 연결 실패" not in load_body
    assert "renderApplicationError(`서버 API 연결 실패" in load_body
    assert 'state.source = "error"' in error_body
    assert "els.errorState.hidden = false" in error_body
    assert "다시 시도" in error_body
    assert "운영 API 연결 오류 · 재시도 필요" in source
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

    for view in ("collected", "review", "go", "urgent", "ended"):
        assert f'data-kpi-view="{view}"' in html
    assert html.count('class="kpi-card__action"') == 5
    assert html.count('aria-pressed="false"') >= 5
    assert "els.kpiViewButtons" in bind_body
    assert "setView(button.dataset.kpiView)" in bind_body
    assert "scrollIntoView" in bind_body
    assert 'state.currentView === "go"' in filter_body
    assert 'notice.recommendation !== "GO"' in filter_body
    assert 'state.currentView === "urgent"' in filter_body
    assert "URGENT_DEADLINE_DAYS" in filter_body
    assert 'state.currentView === "ended"' in filter_body
    assert "isVisibleEndedNotice(notice)" in filter_body
    assert "urgentCount: derived.urgentCount" in dashboard_body
    assert "goCount: derived.goCount" in dashboard_body
    assert "endedCount:" in dashboard_body
    assert "visible_ended_count" in dashboard_body
    assert "cancelled_count" in dashboard_body
    assert 'noticeLifecycleStatus(notice) !== "OPEN"' in derived_body
    assert 'noticeLifecycleStatus(notice) === "OPEN" && notice.recommendation === "GO"' in derived_body
    assert "notices.filter(isVisibleEndedNotice)" in derived_body
    assert 'noticeLifecycleStatus(notice) === "OPEN" && !notice.decision' in derived_body
    assert 'collected: ["수집 공고", "수집된 전체 공고"]' in view_body
    assert 'go: ["GO 후보", "GO 추천 공고"]' in view_body
    assert 'ended: ["종료·취소 공고", "분석된 마감·종료 및 전체 취소 공고"]' in view_body
    assert "resetNoticeFiltersForView()" in view_body
    assert "state.source === \"api\" || state.loading" in view_body
    assert "requestNeedsReload" in view_body
    assert 'els.priorityKeywordInput.value = ""' in view_body
    assert 'els.departmentSelect.value = "organization"' in view_body
    assert 'els.eligibilityFilter.value = "all"' in view_body
    assert 'els.recommendationFilter.value = "all"' in view_body
    assert ".kpi-card__action:focus-visible" in styles
    assert "7일" in html
    assert "72시간" not in html


def test_static_assets_have_a_deterministic_ui_cache_buster() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'href="./styles.css?v=20260822-cancel1"' in html
    assert 'src="./app.js?v=20260822-cancel1"' in html


def test_quantitative_ui_separates_source_validation_from_activation() -> None:
    app = APP_JS.read_text(encoding="utf-8")

    assert "source_validation_status" in app
    assert "activation_status" in app
    assert "activation_reasons" in app
    assert 'SOURCE_VALIDATED: "원문 기계검증"' in app
    assert 'AUTO_ACTIVE: "규칙 자동 활성"' in app
    assert 'REVIEW_REQUIRED: "자동 산정 보류"' in app
    assert "FACT_DIMENSIONS_UNMODELED" in app
    assert "점수 산출조건이 아직 구조화되지 않았습니다" in app
    assert 'AVAILABLE: "배점표 연결"' not in app

    mentor_brief = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "PAI_LOOP_TEAM_MENTOR_BRIEF_v0.8.0.md"
    ).read_text(encoding="utf-8")
    assert "사람 승인 후에만 규칙 버전으로 승격" not in mentor_brief
    assert "반복 사람 승인 없이 `AUTO_ACTIVE`" in mentor_brief


def test_ended_notice_scope_is_db_only_visible_and_status_aware() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    scope_body = _function_body(source, "noticeStatusScopeForView", "scheduleNoticeSearch")
    normalize_body = _function_body(source, "normalizeNotice", "mergeRequirementsAndAtomics")
    lifecycle_body = _function_body(source, "noticeLifecycleStatus", "isEndedNotice")
    ended_body = _function_body(source, "isVisibleEndedNotice", "noticeLifecycleLabel")
    label_body = _function_body(source, "noticeLifecycleLabel", "noticeLifecycleBadge")
    badge_body = _function_body(source, "noticeLifecycleBadge", "recommendationPill")
    detail_body = _function_body(source, "renderDetail", "detailFact")

    assert 'view === "ended"' in scope_body
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
    assert 'return "취소"' in label_body
    assert "notice-lifecycle-badge--cancelled" in badge_body
    assert ".notice-lifecycle-badge--cancelled" in styles
    assert ".detail-tag--cancelled" in styles
    assert "notice-lifecycle-badge" in badge_body
    assert "noticeLifecycleLabel(notice)" in detail_body
    assert "isCancelledNotice(notice)" in detail_body
    assert 'id="kpiEnded"' in html
    assert "분석된 종료 · 전체 취소 공고" in html
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in styles


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
        "if (!state.writeControlsEnabled)"
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
        "if (!state.writeControlsEnabled)"
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
    assert "취소 · 추천 비활성" in recommendation_body
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
    assert 'eligibilityStatus === "PASS") return 0' in rank_body
    assert "isActionableEligibilityReview(notice)) return 1" in rank_body
    assert 'eligibilityStatus === "FAIL") return 3' in rank_body
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
    normalize_body = _function_body(source, "normalizePrivateMatchItem", "collectPrivateMatchDetails")
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
    assert '<span>공고 원문</span>' in html


def test_document_quality_review_is_not_presented_as_eligibility_review() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    dashboard_body = _function_body(source, "deriveDashboard", "renderAll")
    filter_body = _function_body(source, "applyFilters", "compareNotices")
    status_body = _function_body(source, "analysisStatusPill", "analysisRecommendationPill")
    reason_body = _function_body(source, "normalizeAnalysisReason", "normalizeRecommendation")
    pipeline_body = _function_body(source, "renderPipeline", "renderRequirement")
    detail_body = _function_body(source, "renderDetail", "detailFact")
    teams_body = _function_body(source, "renderTeamsPreview", "buildAdaptiveCardPayload")
    teams_body += _function_body(source, "buildAdaptiveCardPayload", "recordTeamsMockSend")
    recommendation_body = _function_body(source, "analysisRecommendationLabel", "formatRelativeDateTime")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "isActionableEligibilityReview" in dashboard_body
    assert "isDocumentQualityReview" in dashboard_body
    assert "isActionableEligibilityReview(notice)" in filter_body
    assert "근거 보완" in status_body
    assert "자격 REVIEW가 아니라 원문 근거 검증 보완 상태" in status_body
    assert 'analysisState === "EVALUATED"' in reason_body
    assert reason_body.index("ANALYSIS_REASON_LABELS[code]") < reason_body.index('analysisState === "EVALUATED"')
    assert "analysisStatusLabel(notice)" in pipeline_body
    assert 'qualityReview ? "근거 보완 후 산정"' in detail_body
    assert 'notice.analysisReason || "원문 근거 검증을 보완한 뒤 자격과 추천을 확정합니다."' in detail_body
    assert "근거 보완'" in detail_body
    assert "근거 보완 · 판단 보류" in teams_body
    assert "근거 보완 후 산정" in teams_body
    assert 'if (isDocumentQualityReview(notice)) return "판단 보류"' in recommendation_body
    assert "자격 검토" in html


def test_missing_risk_is_labeled_as_insufficient_evidence_not_zero() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    render_body = _function_body(source, "renderRiskPanel", "renderQuantitativePending")
    display_body = _function_body(source, "riskDisplayValue", "analysisStatusLabel")

    assert 'risk === null ? "근거 부족"' in render_body
    assert "임의의 0점 대신 근거가 확보된 위험 축만 계산합니다" in render_body
    assert 'notice.riskScore === null ? "근거 부족"' in display_body


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
