(() => {
  "use strict";

  const API_BASE = (document.documentElement.dataset.apiBase || "/api/v1").replace(/\/$/, "");
  const REQUEST_TIMEOUT_MS = 12000;
  const NOTICE_REQUEST_TIMEOUT_MS = 30000;
  const RANKING_REQUEST_TIMEOUT_MS = 60000;
  const NOTICE_PAGE_SIZE = 200;
  const URGENT_DEADLINE_DAYS = 7;
  const DECIDER_NAME = "KMA 입찰팀";
  const RUNTIME_CONFIG = readRuntimeConfig();
  const PAI_BOT_TEAMS_URL = String(RUNTIME_CONFIG.paiBotTeamsUrl || "").trim();

  const state = {
    source: "loading",
    sourceReason: "",
    dashboard: {},
    notices: [],
    filteredNotices: [],
    selectedNotice: null,
    selectedTrigger: null,
    sourceDialogTrigger: null,
    currentView: "all",
    layout: window.matchMedia("(max-width: 680px)").matches ? "cards" : "table",
    loading: false,
    detailLoading: false,
    requestSequence: 0,
    noticeSearchTimer: null,
    noticeStatusScope: "ALL",
    teamsLogs: [],
    teamsLogMeta: {},
    privateMatchPreviews: {},
    awardHistoryMeta: {},
    quantitativeEstimates: {},
    departmentCatalog: null,
    accessMode: "UNKNOWN",
    writeControlsEnabled: true,
    manualAnalysisEnabled: false,
    manualAnalysisPolicy: null,
    manualAnalysisRequests: new Map(),
    performance: {
      summary: null,
      records: [],
      total: 0,
      offset: 0,
      limit: 24,
      loaded: false,
      loading: false,
      error: null,
      requestSequence: 0,
    },
  };

  const els = {};

  const STATUS_LABELS = {
    PASS: "PASS",
    REVIEW: "REVIEW",
    FAIL: "FAIL",
    UNKNOWN: "확인 필요",
  };

  const RECOMMENDATION_LABELS = {
    GO: "GO",
    CONDITIONAL_GO: "조건부 GO",
    HOLD: "보류",
    NO_GO: "NO-GO",
    UNKNOWN: "확인 필요",
  };

  const DECISION_LABELS = {
    GO: "GO",
    HOLD: "보류",
    CONDITIONAL_GO: "보류",
    NO_GO: "NO-GO",
  };

  const ANALYSIS_REASON_LABELS = {
    NOT_SELECTED: "자동 분석 우선순위에 아직 선정되지 않아 분석 대기 중입니다. 폐기된 공고가 아닙니다.",
    ATTACHMENT_MANIFEST_MISSING: "조달청 응답에 분석할 첨부파일 목록이 없어 문서 분석을 시작하지 못했습니다.",
    ATTACHMENT_MANIFEST_EMPTY: "조달청 공고에 분석 가능한 첨부파일이 확인되지 않았습니다.",
    ATTACHMENT_NONE: "조달청 공고에 분석 가능한 첨부파일이 확인되지 않아 자동 문서 분석을 시작하지 못했습니다.",
    HWP_ONLY_UNSUPPORTED: "첨부가 구형 HWP 형식뿐이라 현재 온라인 추출기가 읽지 못했습니다. HWP를 HWPX 또는 PDF로 변환하는 보완 경로가 필요합니다.",
    HWPX_EXTRACT_FAILED: "HWPX 첨부는 확인했지만 본문 추출에 실패해 재처리 또는 문서 변환이 필요합니다.",
    PDF_EXTRACT_FAILED: "PDF 첨부는 확인했지만 본문 추출에 실패해 OCR 또는 재처리가 필요합니다.",
    OPENAI_REVIEW: "첨부 본문은 읽었지만 AI 구조화 결과가 검토 기준을 통과하지 못해 담당자 확인을 기다리고 있습니다.",
    UNVERIFIED_QUOTE: "AI가 제시한 인용문을 추출 본문에서 검증하지 못해 확정 판정을 보류했습니다.",
    QUOTE_UNVERIFIED: "AI가 제시한 인용문을 추출 본문에서 검증하지 못해 확정 판정을 보류했습니다.",
    READY: "첨부 분석 준비가 완료되어 다음 자동 분석 배치를 기다리고 있습니다.",
    PARTIAL: "일부 첨부만 처리되어 나머지 문서 분석 또는 담당자 확인이 필요합니다.",
  };

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    configurePaiBotTeamsAccess();
    detectTeamsContext();
    bindEvents();
    setLayout(state.layout);
    loadApplicationData();
  }

  function cacheElements() {
    const ids = [
      "demoBanner", "demoBannerTitle", "demoBannerReason", "retryApiButton", "systemStatusDot", "systemStatusText", "lastSyncText",
      "pageTitle", "mobileMenuButton", "paiBotTeamsButton", "paiBotTeamsAccessNote", "refreshButton", "replayButton", "mainContent", "navNewCount", "navReviewCount",
      "navDecisionCount", "kpiNew", "kpiReview", "kpiGo", "kpiUrgent", "kpiNewTrend", "kpiReviewTrend", "kpiGoTrend",
      "noticeHeading", "noticeSummary", "departmentSelect", "priorityKeywordInput", "priorityApplyButton", "rankingProfileVersion", "filterForm", "searchInput", "eligibilityFilter", "recommendationFilter", "sortSelect",
      "resetFiltersButton", "noticePanel", "noticeTableWrap", "noticeTableBody", "noticeCardGrid", "loadingState", "errorState",
      "errorStateMessage", "errorRetryButton", "emptyState", "emptyResetButton", "dataSourceLabel", "sidebarScrim", "drawerScrim",
      "detailDrawer", "drawerLoading", "closeDetailButton", "manualAnalyzeButton", "openSourceDialogButton", "copyLinkButton", "detailSourceBadge", "detailNoticeId", "drawerScroll",
      "sourceLinkDialog", "closeSourceLinkDialogButton", "cancelSourceLinkDialogButton", "sourceLinkDialogTitle", "sourceLinkDialogNotice", "sourceLinkDialogMeta", "sourceLinkDialogMessage", "sourceLinkOpenAnchor",
      "detailTags", "detailTitle", "detailAgency", "detailFacts", "decisionSummary", "analysisPipeline", "evidenceCount",
      "detailSummary", "briefEvidenceLabel", "documentAnalysisCard", "documentAnalysisState", "documentAnalysisList", "privateMatchSection", "privateMatchBadge", "privateMatchRetryButton", "privateMatchBody", "privateMatchNote", "eligibilityOverall", "requirementList", "actionCard", "actionList", "evidenceList", "scoreOverview",
      "quantSeparationNote", "quantSourceStatus", "quantOpinion", "quantSourceAnchor", "quantAssumptionList", "quantTableBody", "quantObservationList", "riskTotalLabel", "riskBars", "historyList", "historyStatusLabel", "historyStatusText", "historyConcentration", "historyPrediction", "historyCoverage", "historyWarnings", "decisionForm", "decisionExisting", "toggleCommentButton",
      "commentField", "decisionComment", "commentCount", "saveDecisionButton", "toastRegion", "skeletonRowTemplate",
      "teamsMockSource", "teamsMockTitle", "teamsMockAgency", "teamsMockStatus", "teamsMockDeadline", "teamsMockReason",
      "teamsMockReadiness", "teamsMockRisk", "teamsMockRecommendation", "teamsPreviewOpenButton", "teamsPreviewDecisionButton",
      "teamsMockSendButton", "clearTeamsLogsButton", "teamsMockLogList", "teamsMockJson", "teamsLogStorageLabel",
      "opportunityHero", "opportunityKpis", "noticeSection", "performanceSection", "footerDisclaimer",
      "performanceTotal", "performancePeriod", "performanceYears", "performancePrivacy", "performanceResultSummary",
      "performanceFilterForm", "performanceSearchInput", "performanceYearFilter", "performanceDivisionFilter",
      "performancePanel", "performanceList", "performanceLoadingState", "performanceErrorState", "performanceErrorMessage",
      "performanceRetryButton", "performanceEmptyState", "performanceEmptyResetButton", "performancePagination",
      "performancePageRange", "performancePageLabel", "performancePreviousButton", "performanceNextButton",
    ];

    ids.forEach((id) => {
      els[id] = document.getElementById(id);
    });
    els.sidebar = document.querySelector(".sidebar");
    els.navItems = [...document.querySelectorAll(".nav-item[data-view]")];
    els.kpiViewButtons = [...document.querySelectorAll("[data-kpi-view]")];
    els.layoutButtons = [...document.querySelectorAll("[data-layout]")];
    els.tabButtons = [...document.querySelectorAll("[role='tab'][data-tab]")];
    els.tabPanels = [...document.querySelectorAll("[role='tabpanel'][data-panel]")];
    els.decisionInputs = [...document.querySelectorAll("input[name='decision']")];
  }

  function readRuntimeConfig() {
    const element = document.getElementById("paiLoopRuntimeConfig");
    if (!element) return {};
    try {
      const value = JSON.parse(element.textContent || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch (_error) {
      return {};
    }
  }

  function configurePaiBotTeamsAccess() {
    const teamsUrl = safePaiBotTeamsUrl(PAI_BOT_TEAMS_URL);
    const isReady = Boolean(teamsUrl);
    els.paiBotTeamsButton.disabled = !isReady;
    els.paiBotTeamsButton.setAttribute("aria-disabled", String(!isReady));
    els.paiBotTeamsButton.dataset.state = isReady ? "ready" : "pending";
    els.paiBotTeamsAccessNote.textContent = isReady
      ? "등록된 개발자 전용"
      : "등록된 개발자 전용 · 팀 생성 중";
  }

  function openPaiBotTeams() {
    const teamsUrl = safePaiBotTeamsUrl(PAI_BOT_TEAMS_URL);
    if (!teamsUrl) return;
    window.open(teamsUrl, "_blank", "noopener,noreferrer");
  }

  function safePaiBotTeamsUrl(value) {
    try {
      const url = new URL(String(value || ""));
      return url.protocol === "https:" && url.hostname.toLowerCase() === "teams.microsoft.com" ? url.href : "";
    } catch (_error) {
      return "";
    }
  }

  function detectTeamsContext() {
    let inFrame = false;
    try {
      inFrame = window.self !== window.top;
    } catch (_error) {
      inFrame = true;
    }
    const query = new URLSearchParams(window.location.search);
    if (inFrame || query.get("host") === "teams" || query.get("teams") === "1") {
      document.body.classList.add("teams-context");
      void initializeTeamsHost();
    }
  }

  async function initializeTeamsHost() {
    const teamsApp = window.microsoftTeams?.app;
    if (!teamsApp?.initialize) return;
    try {
      await teamsApp.initialize();
      const context = await teamsApp.getContext();
      const theme = stringValue(context?.app?.theme).toLowerCase();
      if (["dark", "contrast"].includes(theme)) document.body.dataset.teamsTheme = theme;
    } catch (_error) {
      // The same page remains a normal browser app when Teams context is not
      // available; no redirect or credential fallback is attempted.
    }
  }

  function bindEvents() {
    els.paiBotTeamsButton.addEventListener("click", openPaiBotTeams);
    els.refreshButton.addEventListener("click", refreshCurrentView);
    els.retryApiButton.addEventListener("click", () => loadApplicationData({ forceApi: true }));
    els.errorRetryButton.addEventListener("click", () => loadApplicationData({ forceApi: true }));
    els.replayButton.addEventListener("click", runReplay);

    els.departmentSelect.addEventListener("change", applyDepartmentRanking);
    els.priorityApplyButton.addEventListener("click", applyDepartmentRanking);
    els.priorityKeywordInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        applyDepartmentRanking();
      }
    });

    els.filterForm.addEventListener("input", applyFilters);
    els.filterForm.addEventListener("change", applyFilters);
    els.filterForm.addEventListener("submit", submitNoticeSearch);
    els.filterForm.addEventListener("reset", () => window.setTimeout(resetPrioritySearch, 0));
    els.searchInput.addEventListener("input", scheduleNoticeSearch);
    els.emptyResetButton.addEventListener("click", resetFilters);

    els.performanceFilterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      state.performance.offset = 0;
      void loadPerformance({ force: true });
    });
    els.performanceFilterForm.addEventListener("reset", () => {
      window.setTimeout(() => {
        state.performance.offset = 0;
        void loadPerformance({ force: true });
      }, 0);
    });
    els.performanceRetryButton.addEventListener("click", () => loadPerformance({ force: true }));
    els.performanceEmptyResetButton.addEventListener("click", resetPerformanceFilters);
    els.performancePreviousButton.addEventListener("click", () => changePerformancePage(-1));
    els.performanceNextButton.addEventListener("click", () => changePerformancePage(1));

    els.navItems.forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
    els.kpiViewButtons.forEach((button) => button.addEventListener("click", () => {
      setView(button.dataset.kpiView);
      window.requestAnimationFrame(() => els.noticeSection.scrollIntoView({ behavior: "smooth", block: "start" }));
    }));
    els.layoutButtons.forEach((button) => button.addEventListener("click", () => setLayout(button.dataset.layout)));

    els.noticeTableBody.addEventListener("click", handleNoticeActivation);
    els.noticeTableBody.addEventListener("click", handleManualAnalysisActivation);
    els.noticeTableBody.addEventListener("keydown", handleNoticeKeydown);
    els.noticeCardGrid.addEventListener("click", handleNoticeActivation);
    els.noticeCardGrid.addEventListener("click", handleManualAnalysisActivation);

    els.mobileMenuButton.addEventListener("click", toggleMobileMenu);
    els.sidebarScrim.addEventListener("click", closeMobileMenu);

    els.closeDetailButton.addEventListener("click", closeDetail);
    els.manualAnalyzeButton.addEventListener("click", () => {
      const noticeKey = state.selectedNotice?.noticeKey;
      if (noticeKey) void requestManualAnalysis(noticeKey);
    });
    els.drawerScrim.addEventListener("click", closeDetail);
    els.openSourceDialogButton.addEventListener("click", openCurrentNoticeSourceDialog);
    els.copyLinkButton.addEventListener("click", copyCurrentNoticeLink);
    els.detailDrawer.addEventListener("keydown", trapDrawerFocus);
    els.closeSourceLinkDialogButton.addEventListener("click", closeSourceLinkDialog);
    els.cancelSourceLinkDialogButton.addEventListener("click", closeSourceLinkDialog);
    els.sourceLinkDialog.addEventListener("close", restoreSourceDialogFocus);
    els.sourceLinkDialog.addEventListener("click", (event) => {
      if (event.target === els.sourceLinkDialog) closeSourceLinkDialog();
    });
    els.sourceLinkOpenAnchor.addEventListener("click", (event) => {
      if (event.currentTarget.getAttribute("aria-disabled") === "true") event.preventDefault();
    });

    els.tabButtons.forEach((button) => {
      button.addEventListener("click", () => selectTab(button.dataset.tab));
      button.addEventListener("keydown", handleTabKeydown);
    });
    els.requirementList.addEventListener("click", (event) => {
      if (event.target.closest("[data-evidence-jump]")) selectTab("evidence");
    });

    els.toggleCommentButton.addEventListener("click", toggleCommentField);
    els.decisionInputs.forEach((input) => input.addEventListener("change", updateDecisionButton));
    els.decisionComment.addEventListener("input", () => {
      els.commentCount.textContent = String(els.decisionComment.value.length);
    });
    els.decisionForm.addEventListener("submit", saveDecision);

    els.teamsMockSendButton.addEventListener("click", recordTeamsMockSend);
    els.clearTeamsLogsButton.addEventListener("click", refreshTeamsMockLogs);
    els.teamsPreviewOpenButton.addEventListener("click", () => selectTab("overview"));
    els.teamsPreviewDecisionButton.addEventListener("click", focusDecisionDockFromPreview);
    els.privateMatchRetryButton.addEventListener("click", () => {
      const noticeKey = state.selectedNotice?.noticeKey;
      if (noticeKey) void loadPrivateMatchPreview(noticeKey, { force: true });
    });

    document.addEventListener("keydown", handleGlobalKeydown);
    window.addEventListener("popstate", handleRouteChange);
    window.matchMedia("(max-width: 680px)").addEventListener("change", (event) => {
      if (event.matches) setLayout("cards");
    });
  }

  function refreshCurrentView() {
    if (state.currentView === "performance") {
      void loadPerformance({ force: true });
      return;
    }
    void loadApplicationData({ forceApi: true });
  }

  async function loadApplicationData({ forceApi = false } = {}) {
    const sequence = ++state.requestSequence;
    const requestedStatusScope = noticeStatusScopeForView(state.currentView);
    state.noticeStatusScope = requestedStatusScope;
    setLoading(true);
    setSystemStatus("loading");

    const query = new URLSearchParams(window.location.search);
    const explicitDemo = query.get("demo") === "1" && !forceApi;

    if (explicitDemo) {
      await delay(280);
      if (sequence !== state.requestSequence) return;
      activateDemo("URL의 demo=1 설정에 따라 예시 데이터를 표시합니다.");
      finishLoading();
      return;
    }

    const [dashboardResult, noticesResult, profilesResult, runtimeResult] = await Promise.allSettled([
      apiRequest("/dashboard"),
      fetchNoticePages({ statusScope: requestedStatusScope }),
      apiRequest("/departments/keyword-profiles"),
      apiRequest("/runtime-profile"),
    ]);

    if (sequence !== state.requestSequence) return;
    if (requestedStatusScope !== noticeStatusScopeForView(state.currentView)) {
      void loadApplicationData({ forceApi: true });
      return;
    }

    if (noticesResult.status === "fulfilled") {
      if (runtimeResult.status === "fulfilled") applyRuntimeProfile(runtimeResult.value);
      if (profilesResult.status === "fulfilled") {
        state.departmentCatalog = unwrapObject(profilesResult.value);
        populateDepartmentProfiles(state.departmentCatalog);
      }
      const list = extractList(noticesResult.value);
      state.notices = list.map(normalizeNotice).filter((notice) => notice.noticeKey);
      state.dashboard = dashboardResult.status === "fulfilled"
        ? normalizeDashboard(dashboardResult.value, state.notices)
        : deriveDashboard(state.notices);
      state.source = "api";
      state.sourceReason = dashboardResult.status === "rejected" ? "일부 운영 지표는 공고 데이터에서 계산했습니다." : "";
      setSystemStatus("online");
      if (state.dashboard.syntheticWarning && state.notices.some((notice) => notice.isSynthetic)) showDemoBanner(state.dashboard.syntheticWarning);
      else hideDemoBanner();
      renderAll();
      finishLoading();
      openNoticeFromRoute();
      return;
    }

    const reason = humanizeError(noticesResult.reason);
    renderApplicationError(`서버 API 연결 실패: ${reason}`);
  }

  function renderApplicationError(reason) {
    state.loading = false;
    state.source = "error";
    state.sourceReason = reason;
    state.dashboard = {};
    state.notices = [];
    state.filteredNotices = [];
    els.refreshButton.disabled = false;
    els.loadingState.hidden = true;
    els.noticeTableWrap.hidden = true;
    els.noticeCardGrid.hidden = true;
    els.emptyState.hidden = true;
    els.errorState.hidden = false;
    els.errorStateMessage.textContent = `${reason} 잠시 후 다시 시도해 주세요.`;
    els.noticeSummary.textContent = "실데이터를 불러오지 못했습니다.";
    hideDemoBanner();
    setSystemStatus("error");
    renderKpis();
    renderNavigationCounts();
    renderDataSource();
  }

  function applyRuntimeProfile(raw) {
    const profile = unwrapObject(raw);
    state.accessMode = stringValue(firstValue(profile.access_mode, profile.accessMode), "UNKNOWN");
    state.writeControlsEnabled = booleanValue(
      firstValue(profile.write_controls_enabled, profile.writeControlsEnabled),
    ) ?? state.accessMode !== "PUBLIC_READ_ONLY";
    state.manualAnalysisEnabled = booleanValue(
      firstValue(profile.manual_analysis_enabled, profile.manualAnalysisEnabled),
    ) ?? false;
    state.manualAnalysisPolicy = firstObject(
      profile.manual_analysis_policy,
      profile.manualAnalysisPolicy,
    );
    const readOnly = !state.writeControlsEnabled;
    els.replayButton.disabled = readOnly;
    els.replayButton.title = readOnly ? "공개 읽기 전용 화면에서는 서버 작업을 실행하지 않습니다." : "";
    els.teamsMockSendButton.disabled = readOnly;
    els.clearTeamsLogsButton.disabled = readOnly;
    els.decisionInputs.forEach((input) => { input.disabled = readOnly; });
    els.toggleCommentButton.disabled = readOnly;
    els.decisionComment.disabled = readOnly;
    if (readOnly) {
      els.saveDecisionButton.disabled = true;
      els.saveDecisionButton.textContent = "사내 로그인 후 저장 가능";
    }
  }

  function finishLoading() {
    setLoading(false);
    renderAll();
  }

  function activateDemo(reason) {
    const payload = createDemoData();
    state.source = "demo";
    state.sourceReason = reason;
    state.notices = payload.notices.map(normalizeNotice);
    state.dashboard = normalizeDashboard(payload.dashboard, state.notices);
    setSystemStatus("demo");
    showDemoBanner(reason);
    renderAll();
  }

  async function apiRequest(path, options = {}) {
    const { timeoutMs = REQUEST_TIMEOUT_MS, ...fetchOptions } = options;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    const headers = new Headers(fetchOptions.headers || {});
    headers.set("Accept", "application/json");
    if (fetchOptions.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

    try {
      const response = await fetch(`${API_BASE}${path}`, {
        credentials: "same-origin",
        ...fetchOptions,
        headers,
        signal: controller.signal,
      });

      const contentType = response.headers.get("content-type") || "";
      const payload = response.status === 204
        ? null
        : contentType.includes("application/json")
          ? await response.json()
          : await response.text();

      if (!response.ok) {
        const message = payload?.detail || payload?.message || (typeof payload === "string" ? payload : "") || `HTTP ${response.status}`;
        const requestError = new Error(message);
        requestError.status = response.status;
        requestError.payload = payload;
        throw requestError;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("요청 시간이 초과되었습니다");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function fetchNoticePages({ statusScope = noticeStatusScopeForView(state.currentView) } = {}) {
    const timeoutMs = noticeRequestTimeoutMs();
    const notices = [];
    let offset = 0;
    while (true) {
      const payload = await apiRequest(
        buildNoticeRequestPath({ statusScope, limit: NOTICE_PAGE_SIZE, offset }),
        { timeoutMs },
      );
      const page = extractList(payload);
      notices.push(...page);
      if (page.length < NOTICE_PAGE_SIZE) return notices;
      offset += NOTICE_PAGE_SIZE;
    }
  }

  function buildNoticeRequestPath({
    statusScope = noticeStatusScopeForView(state.currentView),
    limit = NOTICE_PAGE_SIZE,
    offset = 0,
  } = {}) {
    const params = new URLSearchParams();
    const departmentId = els.departmentSelect?.value || "organization";
    const searchKeywords = els.priorityKeywordInput?.value.trim() || "";
    const query = els.searchInput?.value.trim() || "";
    // Keep the organization ranking projection on the default board. The
    // backend now computes every department once per notice, so preserving
    // recommendation badges no longer forces the prior duplicate work.
    params.set("department_id", departmentId);
    if (searchKeywords) params.set("search_keywords", searchKeywords);
    if (query) params.set("q", query);
    if (statusScope === "OPEN") params.set("status", "OPEN");
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    return `/notices?${params.toString()}`;
  }

  function noticeRequestTimeoutMs() {
    const departmentId = els.departmentSelect?.value || "organization";
    const searchKeywords = els.priorityKeywordInput?.value.trim() || "";
    return departmentId !== "organization" || Boolean(searchKeywords)
      ? RANKING_REQUEST_TIMEOUT_MS
      : NOTICE_REQUEST_TIMEOUT_MS;
  }

  function noticeStatusScopeForView(view) {
    return ["collected", "closed"].includes(view) ? "ALL" : "OPEN";
  }

  function scheduleNoticeSearch(event) {
    if (event?.isComposing || state.source === "demo") return;
    window.clearTimeout(state.noticeSearchTimer);
    state.noticeSearchTimer = window.setTimeout(() => {
      state.noticeSearchTimer = null;
      void loadApplicationData({ forceApi: true });
    }, 350);
  }

  function submitNoticeSearch(event) {
    event.preventDefault();
    if (state.source === "demo") {
      applyFilters();
      return;
    }
    window.clearTimeout(state.noticeSearchTimer);
    state.noticeSearchTimer = null;
    void loadApplicationData({ forceApi: true });
  }

  function populateDepartmentProfiles(catalog) {
    const departments = arrayValue(catalog?.departments);
    if (!departments.length) return;
    const selected = els.departmentSelect.value || "organization";
    const groups = new Map();
    departments.forEach((profile) => {
      const group = stringValue(profile.group, "기타");
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(profile);
    });
    const fragment = document.createDocumentFragment();
    const common = document.createElement("option");
    common.value = "organization";
    common.textContent = "전사 공통 (교육·컨설팅)";
    fragment.append(common);
    groups.forEach((profiles, groupName) => {
      const optgroup = document.createElement("optgroup");
      optgroup.label = groupName;
      profiles.forEach((profile) => {
        const option = document.createElement("option");
        option.value = stringValue(profile.id);
        option.textContent = stringValue(profile.name, profile.id);
        optgroup.append(option);
      });
      fragment.append(optgroup);
    });
    els.departmentSelect.replaceChildren(fragment);
    els.departmentSelect.value = [...els.departmentSelect.options].some((option) => option.value === selected)
      ? selected
      : "organization";
    els.rankingProfileVersion.textContent = catalog.version ? `키워드 기준 ${catalog.version}` : "키워드 기준 확인됨";
    populatePerformanceDivisionOptions(state.performance.records);
  }

  function applyDepartmentRanking() {
    els.sortSelect.value = "department";
    void loadApplicationData({ forceApi: true });
  }

  function resetPrioritySearch() {
    els.departmentSelect.value = "organization";
    els.priorityKeywordInput.value = "";
    void loadApplicationData({ forceApi: true });
  }

  async function loadPerformance({ force = false, refreshSummary = force } = {}) {
    if (state.performance.loading && !force) return;
    const sequence = ++state.performance.requestSequence;
    state.performance.loading = true;
    state.performance.error = null;
    setPerformanceLoading(true);

    try {
      const useCachedSummary = Boolean(state.performance.summary && !refreshSummary);
      const summaryRequest = useCachedSummary
        ? Promise.resolve(null)
        : apiRequest("/performance/summary");
      const [summaryPayload, listPayload] = await Promise.all([
        summaryRequest,
        apiRequest(buildPerformanceRequestPath()),
      ]);
      if (sequence !== state.performance.requestSequence) return;

      const summary = useCachedSummary
        ? state.performance.summary
        : normalizePerformanceSummary(summaryPayload);
      const result = unwrapObject(listPayload);
      const records = arrayValue(firstValue(result.records, result.items, result.data))
        .map(normalizePerformanceRecord)
        .filter((record) => record.recordKey);

      state.performance.summary = summary;
      state.performance.records = records;
      state.performance.total = Math.max(numberOrNull(result.total) ?? records.length, 0);
      state.performance.loaded = true;
      state.performance.loading = false;
      state.performance.error = null;
      populatePerformanceYearOptions(summary);
      populatePerformanceDivisionOptions(records, summary);
      renderPerformanceView();
    } catch (error) {
      if (sequence !== state.performance.requestSequence) return;
      state.performance.loading = false;
      state.performance.error = error;
      renderPerformanceError(error);
    }
  }

  function buildPerformanceRequestPath() {
    const params = new URLSearchParams();
    const query = els.performanceSearchInput.value.trim();
    const year = els.performanceYearFilter.value;
    const division = els.performanceDivisionFilter.value;
    if (query) params.set("q", query);
    if (year) params.set("year", year);
    if (division) params.set("division", division);
    params.set("limit", String(state.performance.limit));
    params.set("offset", String(state.performance.offset));
    return `/performance?${params.toString()}`;
  }

  function normalizePerformanceSummary(payload) {
    const source = unwrapObject(payload);
    const aggregate = firstObject(source.aggregate);
    const classification = stringValue(source.classification);
    const directIdentifierFindings = numberOrNull(aggregate.direct_identifier_findings);
    if (classification !== "PUBLIC_DERIVED") {
      const error = new Error("공개 데이터 등급을 확인할 수 없습니다");
      error.code = "UNSAFE_PERFORMANCE_DATA";
      throw error;
    }
    if (directIdentifierFindings !== 0) {
      const error = new Error("직접식별자 검사 결과가 안전 기준을 충족하지 않습니다");
      error.code = "UNSAFE_PERFORMANCE_DATA";
      throw error;
    }
    return {
      schemaVersion: stringValue(source.schema_version),
      datasetVersion: stringValue(source.dataset_version),
      classification,
      policyVersion: stringValue(source.policy_version),
      recordCount: Math.max(numberOrNull(aggregate.record_count) ?? 0, 0),
      yearCounts: firstObject(aggregate.year_counts),
      divisionCounts: firstObject(aggregate.division_counts),
      dateMin: stringValue(aggregate.contract_date_min),
      dateMax: stringValue(aggregate.contract_date_max),
      directIdentifierFindings,
      redactions: firstObject(aggregate.redactions),
      recordsSha256: stringValue(firstObject(source.provenance).records_sha256),
    };
  }

  function normalizePerformanceRecord(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    return {
      recordKey: stringValue(source.record_key),
      projectName: stringValue(source.project_name, "사업명 미확인"),
      overview: sanitizePerformanceOverview(source.overview),
      agency: stringValue(source.agency, "발주기관 미확인"),
      contractDate: stringValue(source.contract_date),
      contractYear: numberOrNull(source.contract_year),
      contractAmountKrw: stringValue(source.contract_amount_krw),
      keywords: arrayValue(source.keywords).map((keyword) => stringValue(keyword)).filter(Boolean),
      division: stringValue(source.division, "수행부서 미확인"),
    };
  }

  function populatePerformanceYearOptions(summary) {
    const selected = els.performanceYearFilter.value;
    const years = Object.keys(summary.yearCounts)
      .filter((year) => /^\d{4}$/.test(year))
      .sort((a, b) => Number(b) - Number(a));
    const fragment = document.createDocumentFragment();
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "전체 연도";
    fragment.append(all);
    years.forEach((year) => {
      const option = document.createElement("option");
      option.value = year;
      option.textContent = `${year}년 (${formatNumber(summary.yearCounts[year])}건)`;
      fragment.append(option);
    });
    els.performanceYearFilter.replaceChildren(fragment);
    if ([...els.performanceYearFilter.options].some((option) => option.value === selected)) {
      els.performanceYearFilter.value = selected;
    }
  }

  function populatePerformanceDivisionOptions(records = [], summary = state.performance.summary) {
    if (!els.performanceDivisionFilter) return;
    const selected = els.performanceDivisionFilter.value;
    const divisions = new Set(
      [...els.performanceDivisionFilter.options]
        .map((option) => option.value)
        .filter(Boolean),
    );
    arrayValue(state.departmentCatalog?.departments).forEach((profile) => {
      const name = stringValue(profile.name);
      if (name) divisions.add(name);
    });
    Object.keys(firstObject(summary?.divisionCounts)).forEach((divisionValue) => {
      stringValue(divisionValue)
        .split(/[,/]/)
        .map((division) => division.trim())
        .filter(Boolean)
        .forEach((division) => divisions.add(division));
    });
    records.forEach((record) => {
      stringValue(record.division)
        .split(/[,/]/)
        .map((division) => division.trim())
        .filter((division) => division && division !== "수행부서 미확인")
        .forEach((division) => divisions.add(division));
    });
    const fragment = document.createDocumentFragment();
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "전체 수행부서";
    fragment.append(all);
    [...divisions]
      .sort((a, b) => a.localeCompare(b, "ko-KR"))
      .forEach((division) => {
        const option = document.createElement("option");
        option.value = division;
        option.textContent = division;
        fragment.append(option);
      });
    els.performanceDivisionFilter.replaceChildren(fragment);
    if ([...els.performanceDivisionFilter.options].some((option) => option.value === selected)) {
      els.performanceDivisionFilter.value = selected;
    }
  }

  function setPerformanceLoading(isLoading) {
    state.performance.loading = isLoading;
    els.performanceLoadingState.hidden = !isLoading;
    els.performanceErrorState.hidden = true;
    els.performanceEmptyState.hidden = true;
    els.performanceList.hidden = isLoading;
    els.performancePagination.hidden = true;
    els.performanceResultSummary.textContent = isLoading
      ? "실적 데이터를 불러오는 중입니다."
      : els.performanceResultSummary.textContent;
    [
      els.performanceSearchInput,
      els.performanceYearFilter,
      els.performanceDivisionFilter,
      els.performancePreviousButton,
      els.performanceNextButton,
    ].forEach((control) => { control.disabled = isLoading; });
    if (state.currentView === "performance") els.refreshButton.disabled = isLoading;
  }

  function renderPerformanceView() {
    const data = state.performance;
    const summary = data.summary;
    if (!summary) return;
    setPerformanceLoading(false);
    renderPerformanceSummary(summary);

    const count = data.records.length;
    const start = data.total ? data.offset + 1 : 0;
    const end = data.offset + count;
    const filtered = Boolean(
      els.performanceSearchInput.value.trim()
      || els.performanceYearFilter.value
      || els.performanceDivisionFilter.value,
    );
    els.performanceResultSummary.textContent = data.total
      ? `${filtered ? "검색 결과" : "전체"} ${formatNumber(data.total)}건 중 ${formatNumber(start)}–${formatNumber(end)}건을 표시합니다.`
      : filtered ? "검색 조건에 맞는 실적이 없습니다." : "표시할 공개 실적이 없습니다.";

    els.performanceErrorState.hidden = true;
    els.performanceLoadingState.hidden = true;
    els.performanceEmptyState.hidden = count !== 0;
    els.performanceList.hidden = count === 0;
    els.performanceList.innerHTML = count ? data.records.map(renderPerformanceCard).join("") : "";
    renderPerformancePagination();
    renderDataSource();
  }

  function renderPerformanceSummary(summary) {
    const minYear = validDate(summary.dateMin)?.getFullYear();
    const maxYear = validDate(summary.dateMax)?.getFullYear();
    const years = Object.keys(summary.yearCounts).filter((year) => /^\d{4}$/.test(year));
    els.performanceTotal.textContent = formatNumber(summary.recordCount);
    els.performancePeriod.textContent = minYear && maxYear ? `${minYear}–${maxYear}` : "—";
    els.performanceYears.textContent = years.length ? `${formatNumber(years.length)}개년` : "—";
    els.performancePrivacy.textContent = summary.directIdentifierFindings === 0 ? "0건" : "확인 필요";
  }

  function renderPerformanceCard(record) {
    const keywords = record.keywords.slice(0, 7);
    const remaining = Math.max(record.keywords.length - keywords.length, 0);
    const keywordMarkup = keywords.length
      ? `${keywords.map((keyword) => `<span>${escapeHtml(keyword)}</span>`).join("")}${remaining ? `<span class="performance-keyword--more">+${remaining}</span>` : ""}`
      : '<span class="performance-keyword--empty">키워드 미분류</span>';
    return `
      <article class="performance-card" role="listitem" data-record-key="${escapeAttribute(record.recordKey)}">
        <div class="performance-card__head">
          <span class="performance-division">${escapeHtml(record.division)}</span>
          <time datetime="${escapeAttribute(record.contractDate)}">${escapeHtml(formatPerformanceDate(record.contractDate))}</time>
        </div>
        <h4>${escapeHtml(record.projectName)}</h4>
        <p class="performance-overview ${record.overview ? "" : "is-empty"}">${escapeHtml(record.overview || "공개 요약이 제공되지 않은 실적입니다.")}</p>
        <dl class="performance-facts">
          <div><dt>발주기관</dt><dd>${escapeHtml(record.agency)}</dd></div>
          <div><dt>계약금액</dt><dd>${escapeHtml(formatPerformanceAmount(record.contractAmountKrw))}</dd></div>
        </dl>
        <div class="performance-keywords" aria-label="실적 키워드">${keywordMarkup}</div>
        <footer><span>PUBLIC_DERIVED</span><small>후보 조회용 · 인정실적/점수 미확정</small></footer>
      </article>`;
  }

  function renderPerformancePagination() {
    const data = state.performance;
    if (!data.records.length) {
      els.performancePagination.hidden = true;
      return;
    }
    const page = Math.floor(data.offset / data.limit) + 1;
    const totalPages = Math.max(Math.ceil(data.total / data.limit), 1);
    const start = data.offset + 1;
    const end = data.offset + data.records.length;
    els.performancePageRange.textContent = `${formatNumber(start)}–${formatNumber(end)} / ${formatNumber(data.total)}건`;
    els.performancePageLabel.textContent = `${formatNumber(page)} / ${formatNumber(totalPages)}`;
    els.performancePreviousButton.disabled = data.loading || data.offset <= 0;
    els.performanceNextButton.disabled = data.loading || data.offset + data.limit >= data.total;
    els.performancePagination.hidden = false;
  }

  function renderPerformanceError(error) {
    setPerformanceLoading(false);
    els.performanceList.replaceChildren();
    els.performanceList.hidden = true;
    els.performanceEmptyState.hidden = true;
    els.performancePagination.hidden = true;
    els.performanceErrorState.hidden = false;
    els.performanceErrorMessage.textContent = performanceErrorMessage(error);
    els.performanceResultSummary.textContent = "실적 데이터 연결 상태를 확인해 주세요.";
    if (state.currentView === "performance") els.refreshButton.disabled = false;
  }

  function performanceErrorMessage(error) {
    if (error?.status === 401 || error?.status === 403) {
      return "이 데이터는 인증된 사용자에게만 제공됩니다. 사내 인증 후 다시 시도해 주세요.";
    }
    if (error?.status === 404) {
      return "현재 서버에서 회사 실적 API를 제공하지 않습니다. 배포 버전을 확인해 주세요.";
    }
    if (error?.code === "UNSAFE_PERFORMANCE_DATA") {
      return "공개·비식별 데이터 안전 검증을 통과하지 못해 목록을 표시하지 않았습니다.";
    }
    return "공개 실적 API에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }

  function changePerformancePage(direction) {
    if (state.performance.loading) return;
    const nextOffset = state.performance.offset + direction * state.performance.limit;
    if (nextOffset < 0 || nextOffset >= state.performance.total) return;
    state.performance.offset = nextOffset;
    void loadPerformance();
    els.performanceSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function resetPerformanceFilters() {
    els.performanceSearchInput.value = "";
    els.performanceYearFilter.value = "";
    els.performanceDivisionFilter.value = "";
    state.performance.offset = 0;
    void loadPerformance();
  }

  function formatPerformanceDate(value) {
    const date = validDate(value);
    if (!date) return "계약일 미확인";
    return new Intl.DateTimeFormat("ko-KR", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(date);
  }

  function formatPerformanceAmount(value) {
    if (!/^\d+$/.test(String(value || ""))) return "금액 미확인";
    try {
      return `${BigInt(value).toLocaleString("ko-KR")}원`;
    } catch (_error) {
      return formatBudget(value);
    }
  }

  function sanitizePerformanceOverview(value) {
    return stringValue(value)
      .replace(/\[[^\]]*(?:PM|담당자|책임자|총괄|강사명?|명사\s*특강|강연자|연사|발표자|교수|작가|박사|감독|선수)[^\]]*\]/gi, "[비식별]")
      .replace(/(?:사업\s*총괄\s*P\.?\s*M\.?)\s*(?:[:：=]|\s|\()\s*[\(\[\{<「『]?\s*[가-힣]{2,5}\s*[\)\]\}>」』]?/gi, "[비식별]")
      .replace(/(?:P\.?\s*M\.?)\s*(?:[:：=]|\()\s*[\(\[\{<「『]?\s*[가-힣]{2,5}\s*[\)\]\}>」』]?/gi, "[비식별]")
      .replace(/(?:명사\s*특강|총괄책임자|연구책임자|프로젝트책임자|강연자|발표자|담당자|대표자|성명|책임자|강사명?|연사|교수|감독|선수)\s*(?:[:：=]|\s|\()\s*[\(\[\{<「『]?\s*[가-힣]{2,5}\s*[\)\]\}>」』]?(?:\s*[\(\[\{<「『][^\)\]\}>」』\r\n]{1,40}[\)\]\}>」』])?/gi, "[비식별]")
      .replace(/[가-힣]{2,5}(?:\s+|[\(\[\{<「『])\s*(?:작가|교수|박사|강사|연사|감독|선수)\s*[\)\]\}>」』]?/g, "[비식별]");
  }

  function extractList(payload) {
    if (Array.isArray(payload)) return payload;
    if (!payload || typeof payload !== "object") return [];
    if (Array.isArray(payload.notices)) return payload.notices;
    if (Array.isArray(payload.items)) return payload.items;
    if (Array.isArray(payload.results)) return payload.results;
    if (Array.isArray(payload.data)) return payload.data;
    if (payload.data && typeof payload.data === "object") return extractList(payload.data);
    return [];
  }

  function unwrapObject(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
    if (payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)) return payload.data;
    if (payload.notice && typeof payload.notice === "object") return payload.notice;
    return payload;
  }

  function normalizeDepartmentRanking(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return {
      departmentId: stringValue(firstValue(value.department_id, value.departmentId), "organization"),
      departmentName: stringValue(firstValue(value.department_name, value.departmentName), "전사 공통"),
      group: stringValue(value.group, "전사"),
      rankingScope: stringValue(firstValue(value.ranking_scope, value.rankingScope), "BUSINESS"),
      recommendationTier: stringValue(firstValue(value.recommendation_tier, value.recommendationTier), "NONE"),
      topRecommendationEligible: booleanValue(firstValue(value.top_recommendation_eligible, value.topRecommendationEligible)) ?? false,
      reviewCandidate: booleanValue(firstValue(value.review_candidate, value.reviewCandidate)) ?? false,
      score: numberOrNull(value.score) ?? 0,
      departmentScore: numberOrNull(firstValue(value.department_score, value.departmentScore)) ?? 0,
      businessScore: numberOrNull(firstValue(value.business_score, value.businessScore)) ?? 0,
      routingScore: numberOrNull(firstValue(value.routing_score, value.routingScore)) ?? 0,
      priority: stringValue(value.priority, "LOW"),
      priorityLabel: stringValue(firstValue(value.priority_label, value.priorityLabel), "낮음"),
      matchedUserKeywords: arrayValue(firstValue(value.matched_user_keywords, value.matchedUserKeywords)).map(String),
      matchedDepartmentKeywords: arrayValue(firstValue(value.matched_department_keywords, value.matchedDepartmentKeywords)).map(String),
      matchedRegions: arrayValue(firstValue(value.matched_regions, value.matchedRegions)).map(String),
      reasons: arrayValue(value.reasons).map(String),
    };
  }

  function rankingWithCollectionTier(ranking, tier) {
    if (!ranking || ranking.recommendationTier !== "NONE") return ranking;
    return {
      ...ranking,
      recommendationTier: tier,
      topRecommendationEligible: tier === "TOP",
      reviewCandidate: tier === "REVIEW",
    };
  }

  function normalizeNotice(raw = {}, index = 0) {
    const source = unwrapObject(raw);
    const evaluation = firstObject(source.latest_evaluation, source.latestEvaluation, source.evaluation);
    const explanation = firstObject(evaluation.explanation, source.explanation);
    const atomicResults = arrayValue(firstValue(evaluation.atomic_results, evaluation.atomicResults, source.atomic_results, []));
    const versions = arrayValue(firstValue(source.versions, source.notice_versions, [])).map(normalizeVersion);
    const latestVersion = versions.slice().sort((a, b) => b.versionNo - a.versionNo)[0] || null;
    const decisions = arrayValue(firstValue(source.decisions, source.decision_history, [])).map(normalizeDecisionRecord);
    const latestDecision = decisions.slice().sort((a, b) => nullableDateSort(b.createdAt, a.createdAt))[0] || {};
    const noticeKey = stringValue(
      firstValue(source.notice_key, source.noticeKey, source.id, source.bid_notice_no, source.bidNtceNo),
      `notice-${index + 1}`,
    );
    const deadline = firstValue(source.deadline, source.close_at, source.closeAt, source.bid_close_date, source.bidClseDt, null);
    const collectedAt = firstValue(source.collected_at, source.collectedAt, source.created_at, source.createdAt, null);
    const category = stringValue(firstValue(source.category, source.business_category, source.notice_type), "용역");
    const sourceKind = normalizeSourceKind(firstValue(source.source_kind, source.sourceKind, source.data_source), noticeKey, category);
    const hasEvaluation = Boolean(firstValue(
      evaluation.id,
      evaluation.evaluated_at,
      evaluation.evaluatedAt,
      evaluation.eligibility,
      source.eligibility_status,
      source.readiness_score,
      source.recommendation,
    ));
    const analysisState = normalizeAnalysisState(firstValue(source.analysis_state, source.ingestion_state, source.analysisState), hasEvaluation, source.status);
    const rawRequirements = arrayValue(firstValue(source.requirements, source.eligibility_requirements, source.conditions, []));
    const requirements = mergeRequirementsAndAtomics(rawRequirements, atomicResults);
    const rawEvidence = arrayValue(firstValue(source.evidence, source.evidences, source.source_evidence, []));
    const rawDocumentAnalyses = arrayValue(firstValue(
      source.document_analyses,
      source.documentAnalyses,
      evaluation.document_analyses,
      evaluation.documentAnalyses,
      [],
    ));
    const documentAnalyses = rawDocumentAnalyses.map(normalizeDocumentAnalysis);
    const evidence = collectNoticeEvidence({ rawEvidence, atomicResults, rawRequirements, rawDocumentAnalyses });
    const history = arrayValue(firstValue(source.award_history, source.awardHistory, source.history, []))
      .map(normalizeHistory);
    const safeRaw = sanitizeNoticeAwardHistory(source, history);
    const departmentRanking = normalizeDepartmentRanking(firstValue(source.department_ranking, source.departmentRanking));
    const topDepartmentRankings = arrayValue(firstValue(source.top_department_rankings, source.topDepartmentRankings))
      .map(normalizeDepartmentRanking)
      .map((item) => rankingWithCollectionTier(item, "TOP"))
      .filter(Boolean);
    const departmentReviewCandidates = arrayValue(firstValue(source.department_review_candidates, source.departmentReviewCandidates))
      .map(normalizeDepartmentRanking)
      .map((item) => rankingWithCollectionTier(item, "REVIEW"))
      .filter(Boolean);
    const regionRouting = arrayValue(firstValue(source.region_routing, source.regionRouting))
      .map(normalizeDepartmentRanking)
      .map((item) => rankingWithCollectionTier(item, "ROUTING"))
      .filter(Boolean);
    const analysisReason = normalizeAnalysisReason(source, analysisState, latestVersion);

    return {
      raw: safeRaw,
      noticeKey,
      noticeNumber: stringValue(firstValue(source.notice_number, source.notice_no, source.bid_notice_no, source.bidNtceNo, noticeKey)),
      title: stringValue(firstValue(source.title, source.notice_title, source.bidNtceNm, source.name), "공고명 미확인"),
      agency: stringValue(firstValue(source.agency, source.ordering_agency, source.ntceInsttNm, source.dminsttNm), "발주기관 미확인"),
      demandAgency: stringValue(firstValue(source.demand_agency, source.dmndInsttNm), ""),
      deadline,
      openAt: firstValue(source.open_at, source.published_at, source.bid_begin_at, source.bidBeginDt, null),
      collectedAt,
      budget: firstValue(source.budget, source.estimated_amount, source.presmptPrce, source.asignBdgtAmt, null),
      eligibilityStatus: normalizeEligibility(firstValue(evaluation.eligibility, source.eligibility_status, source.eligibilityStatus, source.eligibility)),
      readinessScore: numberOrNull(firstValue(evaluation.readiness_score, evaluation.readinessScore, source.readiness_score, source.readinessScore, source.fit_score, source.fitScore)),
      readinessStatus: normalizeReadiness(firstValue(evaluation.readiness_status, evaluation.status, source.readiness_status)),
      evidenceCoverage: numberOrNull(firstValue(evaluation.evidence_coverage, evaluation.evidenceCoverage, source.evidence_coverage, source.evidenceCoverage, source.coverage)),
      riskScore: numberOrNull(firstValue(evaluation.risk_score, evaluation.riskScore, source.risk_score, source.riskScore, source.risk)),
      riskBand: normalizeRecommendation(firstValue(evaluation.risk_band, evaluation.band, source.risk_band)),
      recommendation: normalizeRecommendation(firstValue(source.recommendation, source.ai_recommendation, source.recommended_decision, evaluation.risk_band, evaluation.band)),
      decision: normalizeDecision(firstValue(latestDecision.choice, source.decision, source.manager_decision, source.human_decision)),
      decisionComment: stringValue(firstValue(latestDecision.rationale, source.decision_comment, source.comment, source.manager_comment), ""),
      decidedBy: stringValue(firstValue(latestDecision.actorLabel, source.decided_by, source.decider), ""),
      decidedAt: firstValue(latestDecision.createdAt, source.decided_at, source.decision_at, null),
      resultStatus: stringValue(firstValue(source.result_status, source.award_result, source.outcome), ""),
      noticeStatus: stringValue(firstValue(source.status, source.notice_status), ""),
      isNew: booleanValue(source.is_new) ?? (isRecent(collectedAt, 48) || String(source.status || "").toUpperCase() === "OPEN"),
      summary: stringValue(firstValue(source.summary, source.ai_summary, source.brief), buildEvaluationSummary(evaluation, explanation)),
      category,
      method: stringValue(firstValue(source.method, source.contract_method, source.cntrctCnclsMthdNm), "확인 필요"),
      region: stringValue(firstValue(source.region, source.location_restriction), "전국"),
      requirements,
      evidence,
      awardHistory: history,
      quantitative: arrayValue(firstValue(source.quantitative, source.quantitative_items, source.score_items, []))
        .map(normalizeQuantItem),
      riskAxes: normalizeRiskAxes(firstValue(source.risk_dimensions, source.risk_axes, source.riskAxes, source.risks, [])),
      actions: arrayValue(firstValue(source.actions, source.next_actions, source.review_actions, [])).map((value) => stringValue(value)).filter(Boolean),
      pipeline: firstValue(source.pipeline, source.analysis_pipeline, null),
      evaluationId: stringValue(firstValue(evaluation.id, source.evaluation_id), ""),
      reasonCode: stringValue(firstValue(evaluation.reason_code, evaluation.reasonCode), ""),
      evaluatedAt: firstValue(evaluation.evaluated_at, evaluation.evaluatedAt, null),
      analysisState,
      analysisReasonCode: analysisReason.code,
      analysisReason: analysisReason.message,
      analysisAttempted: booleanValue(firstValue(source.analysis_attempted, source.analysisAttempted)) ?? false,
      analysisUpdatedAt: firstValue(source.analysis_updated_at, source.analysisUpdatedAt, null),
      sourceKind,
      isSynthetic: sourceKind === "SYNTHETIC",
      explanation,
      atomicResults,
      versions,
      latestVersion,
      decisions,
      documentAnalyses,
      departmentRanking,
      topDepartmentRankings,
      departmentReviewCandidates,
      regionRouting,
      sourceUrl: safeHttpUrl(firstValue(source.source_url, source.notice_url, source.url, "")),
    };
  }

  function mergeRequirementsAndAtomics(requirements, atomics) {
    if (!requirements.length) return atomics.map((item, index) => normalizeRequirement(item, index));
    const atomicsByKey = new Map(atomics.map((item) => [stringValue(firstValue(item?.requirement_key, item?.id)), item]));
    return requirements.map((item, index) => {
      const key = stringValue(firstValue(item?.requirement_key, item?.id));
      const atomic = atomicsByKey.get(key) || atomics[index] || {};
      return normalizeRequirement({ ...item, ...atomic }, index);
    });
  }

  function normalizeVersion(item) {
    const source = item && typeof item === "object" ? item : {};
    return {
      id: stringValue(source.id),
      versionNo: numberOrNull(firstValue(source.version_no, source.versionNo)) ?? 0,
      documentComplete: booleanValue(firstValue(source.document_complete, source.documentComplete)),
      extractionStatus: stringValue(firstValue(source.extraction_status, source.extractionStatus), "UNKNOWN"),
      extractionConfidence: normalizeConfidence(firstValue(source.extraction_confidence, source.extractionConfidence)),
      createdAt: firstValue(source.created_at, source.createdAt, null),
    };
  }

  function normalizeDocumentAnalysis(item, index) {
    const source = item && typeof item === "object" ? item : {};
    const result = firstObject(source.result, source.data, source.analysis_result, source.analysisResult);
    const extractedRequirements = arrayValue(firstValue(
      source.requirements,
      source.extracted_requirements,
      source.requirement_items,
      result.requirements,
      [],
    ));
    const status = stringValue(firstValue(source.analysis_status, source.status), "COMPLETE").toUpperCase();
    const explicitReview = booleanValue(firstValue(source.needs_review, source.needsReview, source.review_required, source.reviewRequired, result.needs_review));
    return {
      id: stringValue(firstValue(source.id, source.analysis_id), `document-analysis-${index + 1}`),
      documentName: stringValue(firstValue(source.document_name, source.documentName, source.filename, source.file_name, source.source_label, source.name), `첨부문서 ${index + 1}`),
      summary: stringValue(firstValue(source.summary, source.analysis_summary, source.brief, result.summary), "구조화 분석 요약이 아직 제공되지 않았습니다."),
      requirementCount: numberOrNull(firstValue(
        source.requirement_count,
        source.requirements_count,
        source.extracted_requirement_count,
        extractedRequirements.length || null,
      )),
      needsReview: explicitReview ?? (Boolean(source.review_code) || ["REVIEW", "NEEDS_REVIEW", "PARTIAL", "FAILED"].includes(status)),
      status,
      confidence: normalizeConfidence(firstValue(source.confidence, source.extraction_confidence, source.analysis_confidence)),
      analyzedAt: firstValue(source.analyzed_at, source.analysis_updated_at, source.updated_at, source.created_at, null),
    };
  }

  function normalizeDecisionRecord(item) {
    const source = item && typeof item === "object" ? item : {};
    return {
      id: stringValue(source.id),
      evaluationId: stringValue(firstValue(source.evaluation_id, source.evaluationId)),
      choice: normalizeDecision(firstValue(source.choice, source.decision)),
      actorLabel: stringValue(firstValue(source.actor_label, source.actorLabel, source.decided_by)),
      rationale: stringValue(firstValue(source.rationale, source.comment)),
      conditions: arrayValue(source.conditions).map((value) => stringValue(value)).filter(Boolean),
      createdAt: firstValue(source.created_at, source.createdAt, null),
    };
  }

  function buildEvaluationSummary(evaluation, explanation) {
    const eligibility = normalizeEligibility(evaluation.eligibility);
    const reasonCode = stringValue(firstValue(evaluation.reason_code, evaluation.reasonCode), "");
    const reviewCodes = arrayValue(firstValue(explanation.review_codes, explanation.reviewCodes, [])).map((value) => stringValue(value)).filter(Boolean);
    const defaultFails = arrayValue(firstValue(explanation.default_fail_details, explanation.defaultFailDetails, []));
    if (eligibility === "PASS") {
      return "활성화된 필수 조건이 PASS 경로와 일치합니다. 참가 자격과 별개로 준비도·증빙·사업 리스크를 함께 확인한 뒤 최종 입찰 여부를 결정하세요.";
    }
    if (eligibility === "REVIEW") {
      const codes = reviewCodes.length ? ` (${reviewCodes.join(", ")})` : reasonCode ? ` (${reasonCode})` : "";
      return `PASS 조건과 바로 일치하지 않지만 연결된 검토 예외${codes}가 확인되었습니다. 담당자가 원문과 최신 증빙을 확인해야 최종 판단할 수 있습니다.`;
    }
    if (eligibility === "FAIL") {
      const condition = stringValue(firstValue(defaultFails[0]?.failed_condition, defaultFails[0]?.failedCondition), "필수 참가 조건");
      return `${condition}이 PASS 경로와 일치하지 않았고 적용 가능한 REVIEW 예외가 없어 DEFAULT FAIL로 판정되었습니다. 원문 조건과 마감일 당시 회사 사실을 확인하세요.`;
    }
    return "평가 결과가 아직 생성되지 않았습니다. 공고 버전과 자격 조건을 등록한 뒤 결정론적 평가를 실행하세요.";
  }

  function normalizeRequirement(item, index) {
    if (typeof item === "string") {
      return { id: `req-${index + 1}`, title: item, description: "근거 확인 필요", status: "UNKNOWN", evidenceId: "" };
    }
    const source = item && typeof item === "object" ? item : {};
    return {
      id: stringValue(firstValue(source.id, source.requirement_key, source.rule_id), `req-${index + 1}`),
      title: stringValue(firstValue(source.title, source.label, source.name, source.condition, source.requirement), `자격 조건 ${index + 1}`),
      description: stringValue(firstValue(source.message, source.description, source.detail, source.reason, source.explanation, source.source_excerpt), "근거 확인 필요"),
      status: normalizeEligibility(firstValue(source.result, source.status, source.judgement)),
      evidenceId: stringValue(firstValue(source.evidence_id, source.evidenceId, source.evidence_key, source.source_location), ""),
      reasonCode: stringValue(firstValue(source.reason_code, source.pass_rule_id, source.linked_review_code), ""),
    };
  }

  function normalizeEvidence(item, index) {
    if (typeof item === "string") {
      return { id: `ev-${index + 1}`, file: "첨부문서", page: "위치 미확인", quote: item, status: "PROVISIONAL", confidence: null };
    }
    const source = item && typeof item === "object" ? item : {};
    const evidenceValid = booleanValue(source.evidence_valid);
    return {
      id: stringValue(firstValue(source.id, source.evidence_id, source.requirement_key), `ev-${index + 1}`),
      file: stringValue(firstValue(source.file, source.filename, source.document_name, source.source, source.evidence_key), "공고 판정 원문"),
      page: stringValue(firstValue(source.page, source.page_number, source.location, source.anchor, source.source_location), "위치 미확인"),
      quote: stringValue(firstValue(source.quote, source.text, source.excerpt, source.content, source.source_excerpt), "추출된 원문이 없습니다."),
      status: evidenceValid === null
        ? normalizeEvidenceStatus(firstValue(source.status, source.verification_status, source.verified))
        : evidenceValid ? "VERIFIED" : "PROVISIONAL",
      confidence: normalizeConfidence(firstValue(source.confidence, source.score, source.confidence_score, source.parse_confidence)),
    };
  }

  function collectNoticeEvidence({ rawEvidence, atomicResults, rawRequirements, rawDocumentAnalyses }) {
    const documentEvidence = flattenDocumentEvidence(rawDocumentAnalyses);
    const requirementEvidence = flattenRequirementEvidence(rawRequirements);
    const publicSourceEvidence = documentEvidence.length ? documentEvidence : requirementEvidence;
    const candidates = [
      ...rawEvidence.map((item, index) => normalizeEvidence(item, index)),
      ...atomicResults
        .filter((item) => item?.source_excerpt || item?.source_location)
        .map((item, index) => normalizeEvidence(item, rawEvidence.length + index)),
      ...publicSourceEvidence,
    ];
    const seen = new Set();
    return candidates.filter((item) => {
      const quoteKey = stringValue(item.quote).replace(/\s+/g, " ").trim().toLocaleLowerCase("ko-KR");
      const fallbackKey = [item.file, item.page, item.id].map((value) => stringValue(value)).join("|");
      const key = quoteKey || fallbackKey;
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function flattenDocumentEvidence(analyses) {
    return analyses.flatMap((item, analysisIndex) => {
      const source = item && typeof item === "object" ? item : {};
      const status = stringValue(firstValue(source.status, source.analysis_status), "").toUpperCase();
      if (!["ACCEPTED", "COMPLETE"].includes(status)) return [];
      const result = firstObject(source.result, source.data, source.analysis_result, source.analysisResult);
      const requirements = arrayValue(firstValue(
        source.requirements,
        source.extracted_requirements,
        source.requirement_items,
        result.requirements,
        [],
      ));
      const documentName = stringValue(
        firstValue(source.document_name, source.documentName, source.filename, source.file_name),
        `첨부문서 ${analysisIndex + 1}`,
      );
      return requirements.flatMap((requirement, requirementIndex) => {
        const requirementSource = requirement && typeof requirement === "object" ? requirement : {};
        return arrayValue(requirementSource.evidence).flatMap((anchor, anchorIndex) => {
          const sourceAnchor = anchor && typeof anchor === "object" ? anchor : {};
          const quote = stringValue(sourceAnchor.quote).trim();
          if (!quote) return [];
          const page = numberOrNull(sourceAnchor.page);
          const section = stringValue(sourceAnchor.section).trim();
          const location = [page === null ? "" : `${formatNumber(page)}쪽`, section].filter(Boolean).join(" · ") || "위치 미확인";
          return [normalizeEvidence({
            id: `document-${analysisIndex + 1}-requirement-${requirementIndex + 1}-evidence-${anchorIndex + 1}`,
            file: documentName,
            location,
            quote,
            status: "PROVISIONAL",
            confidence: normalizeConfidence(sourceAnchor.confidence),
          }, anchorIndex)];
        });
      });
    });
  }

  function flattenRequirementEvidence(requirements) {
    return requirements.flatMap((item, index) => {
      const source = item && typeof item === "object" ? item : {};
      const quote = stringValue(source.source_excerpt).trim();
      if (!quote) return [];
      return [normalizeEvidence({
        id: `requirement-evidence-${index + 1}`,
        file: "공고 판정 원문",
        location: stringValue(source.source_location, "위치 미확인"),
        quote,
        status: "PROVISIONAL",
        confidence: normalizeConfidence(source.parse_confidence),
      }, index)];
    });
  }

  function normalizeHistory(item) {
    const source = item && typeof item === "object" ? item : {};
    const awardedAt = firstValue(source.awarded_at, source.awardedAt, source.award_date, null);
    const openedAt = firstValue(source.opened_at, source.openedAt, source.open_date, null);
    const eventDate = awardedAt || openedAt;
    const similarity = numberOrNull(firstValue(source.similarity_score, source.similarityScore, source.title_similarity));
    return {
      id: stringValue(source.id),
      bidNoticeNo: stringValue(firstValue(source.bid_notice_no, source.bidNoticeNo), ""),
      revisionNo: stringValue(firstValue(source.revision_no, source.revisionNo), ""),
      year: stringValue(firstValue(source.year, source.award_year, eventDate ? String(eventDate).slice(0, 4) : null), "연도 미확인"),
      title: stringValue(firstValue(source.title, source.project_name, source.notice_title), "유사 사업"),
      winner: stringValue(firstValue(source.winner_name, source.winner, source.awardee), "낙찰자 미확인"),
      amount: firstValue(source.amount, source.award_amount, source.contract_amount, null),
      rate: numberOrNull(firstValue(source.rate, source.award_rate, source.bid_rate)),
      agency: stringValue(firstValue(source.agency, source.ordering_agency), ""),
      participantCount: numberOrNull(firstValue(source.participant_count, source.participantCount)),
      openedAt,
      awardedAt,
      similarityScore: similarity === null ? null : clamp(similarity, 0, 100),
      source: stringValue(source.source, ""),
      estimatedPrice: firstValue(source.estimated_price, source.estimatedPrice, null),
      submittedBidPrice: firstValue(source.submitted_bid_price, source.submittedBidPrice, null),
      submittedBidRate: numberOrNull(firstValue(source.submitted_bid_rate, source.submittedBidRate)),
      awardRateBasis: stringValue(firstValue(source.award_rate_basis, source.awardRateBasis), ""),
      technicalScore: numberOrNull(firstValue(source.technical_score, source.technicalScore)),
      priceScore: numberOrNull(firstValue(source.price_score, source.priceScore)),
    };
  }

  function sanitizeNoticeAwardHistory(source, history) {
    const safeSource = { ...source };
    delete safeSource.award_history;
    delete safeSource.awardHistory;
    delete safeSource.history;
    safeSource.award_history = history.map(toSafeAwardHistoryRaw);
    return safeSource;
  }

  function toSafeAwardHistoryRaw(item) {
    return {
      id: item.id,
      bid_notice_no: item.bidNoticeNo,
      revision_no: item.revisionNo,
      year: item.year,
      title: item.title,
      agency: item.agency,
      winner_name: item.winner,
      participant_count: item.participantCount,
      award_amount: item.amount,
      award_rate: item.rate,
      opened_at: item.openedAt,
      awarded_at: item.awardedAt,
      similarity_score: item.similarityScore,
      source: item.source,
    };
  }

  function normalizeQuantItem(item) {
    const source = item && typeof item === "object" ? item : {};
    return {
      label: stringValue(firstValue(source.label, source.name, source.criterion), "평가 항목"),
      maxScore: numberOrNull(firstValue(source.max_score, source.maxScore, source.weight)),
      expectedScore: firstValue(source.expected_score, source.expectedScore, source.score, null),
      status: normalizeEvidenceStatus(firstValue(source.status, source.verification_status)),
    };
  }

  function normalizeRiskAxes(value) {
    if (Array.isArray(value)) {
      return value.map((item, index) => {
        if (typeof item === "number") return { label: `리스크 ${index + 1}`, score: clamp(item, 0, 100) };
        return {
          label: stringValue(firstValue(item?.label, item?.name, item?.axis), `리스크 ${index + 1}`),
          score: clamp(numberOrNull(firstValue(item?.score, item?.value)) ?? 0, 0, 100),
        };
      });
    }
    if (value && typeof value === "object") {
      const labels = {
        qualification: "자격 조건",
        execution: "수행 역량",
        competition: "경쟁 강도",
        profitability: "수익성",
        operation: "운영 부담",
        document: "문서 품질",
      };
      return Object.entries(value).map(([label, score]) => ({ label: labels[label] || label, score: clamp(numberOrNull(score) ?? 0, 0, 100) }));
    }
    return [];
  }

  function normalizeDashboard(payload, notices) {
    const source = unwrapObject(payload);
    const kpis = source.kpis && typeof source.kpis === "object" ? source.kpis : source;
    const totals = firstObject(source.totals, kpis.totals);
    const eligibilityCounts = firstObject(source.eligibility_counts, source.eligibilityCounts);
    const readinessCounts = firstObject(source.readiness_counts, source.readinessCounts);
    const derived = deriveDashboard(notices);
    return {
      newCount: numberOrNull(firstValue(kpis.new_count, kpis.newCount, kpis.new_notices, kpis.new, totals.active, totals.notices)) ?? derived.newCount,
      // The backend aggregate counts every fail-closed REVIEW. The board KPI
      // is intentionally narrower: substantive eligibility review only.
      // Document-quality/R07 work is shown separately as "근거 보완".
      reviewCount: derived.reviewCount,
      qualityReviewCount: derived.qualityReviewCount,
      // These clickable KPIs must match their OPEN-only board filters. The
      // backend aggregate can include already-closed notices with a future
      // deadline, so use the loaded notice projection for both counts.
      goCount: derived.goCount,
      urgentCount: derived.urgentCount,
      undecidedCount: numberOrNull(firstValue(kpis.undecided_count, kpis.undecidedCount)) ?? Math.max((numberOrNull(totals.notices) ?? notices.length) - (numberOrNull(totals.decisions) ?? 0), 0),
      totalNotices: numberOrNull(totals.notices) ?? notices.length,
      totalEvaluations: numberOrNull(totals.evaluations) ?? notices.filter((notice) => notice.evaluationId).length,
      totalDecisions: numberOrNull(totals.decisions) ?? notices.filter((notice) => notice.decision).length,
      eligibilityCounts,
      readinessCounts,
      lastSync: firstValue(source.generated_at, source.generatedAt, source.last_sync, source.lastSync, source.updated_at, source.updatedAt, derived.lastSync),
      systemStatus: stringValue(firstValue(source.system_status, source.status), "online"),
      syntheticWarning: stringValue(firstValue(source.synthetic_data_warning, source.syntheticWarning), ""),
    };
  }

  function deriveDashboard(notices) {
    return {
      newCount: notices.filter((notice) => notice.noticeStatus.toUpperCase() === "OPEN").length,
      reviewCount: notices.filter((notice) => notice.noticeStatus.toUpperCase() === "OPEN" && isActionableEligibilityReview(notice)).length,
      qualityReviewCount: notices.filter((notice) => notice.noticeStatus.toUpperCase() === "OPEN" && isDocumentQualityReview(notice)).length,
      goCount: notices.filter((notice) => notice.noticeStatus.toUpperCase() === "OPEN" && notice.recommendation === "GO").length,
      urgentCount: notices.filter((notice) => {
        if (notice.noticeStatus.toUpperCase() !== "OPEN") return false;
        const days = daysUntil(notice.deadline);
        return days !== null && days >= 0 && days <= URGENT_DEADLINE_DAYS;
      }).length,
      undecidedCount: notices.filter((notice) => !notice.decision).length,
      lastSync: new Date().toISOString(),
      systemStatus: "online",
    };
  }

  function renderAll() {
    renderKpis();
    renderNavigationCounts();
    applyFilters();
    renderDataSource();
  }

  function renderKpis() {
    const data = state.dashboard;
    // "수집 공고" is the database total; the sidebar's "진행 공고" keeps using active/newCount.
    els.kpiNew.textContent = displayNumber(data.totalNotices);
    els.kpiReview.textContent = displayNumber(data.reviewCount);
    els.kpiGo.textContent = displayNumber(data.goCount);
    els.kpiUrgent.textContent = displayNumber(data.urgentCount);
    els.kpiNewTrend.textContent = state.source === "demo" ? "데모" : "실시간";
    els.kpiReviewTrend.textContent = "자격";
    els.kpiGoTrend.textContent = "추천";
  }

  function renderNavigationCounts() {
    els.navNewCount.textContent = displayNumber(state.dashboard.newCount);
    els.navReviewCount.textContent = displayNumber(state.dashboard.reviewCount);
    els.navDecisionCount.textContent = displayNumber(state.dashboard.undecidedCount);
  }

  function renderDataSource() {
    if (state.currentView === "performance") {
      const summary = state.performance.summary;
      els.dataSourceLabel.textContent = summary
        ? `PUBLIC_DERIVED · ${stringValue(summary.datasetVersion, "버전 미확인")} · 비식별 공개 snapshot ${formatNumber(summary.recordCount)}건`
        : "PUBLIC_DERIVED · 공개 실적 데이터 확인 중";
      return;
    }
    if (state.source === "api") {
      const syntheticCount = state.notices.filter((notice) => notice.isSynthetic).length;
      const ppsCount = state.notices.filter((notice) => notice.sourceKind === "PPS").length;
      const manualCount = state.notices.filter((notice) => notice.sourceKind === "MANUAL").length;
      const composition = `조달청 ${formatNumber(ppsCount)}건 · 수동 ${formatNumber(manualCount)}건 · 합성 ${formatNumber(syntheticCount)}건`;
      const accessLabel = state.accessMode === "PUBLIC_READ_ONLY" ? " · 공개 읽기 전용" : "";
      els.dataSourceLabel.textContent = state.sourceReason ? `실시간 API${accessLabel} · ${composition} · ${state.sourceReason}` : `실시간 API${accessLabel} · ${composition}`;
    } else if (state.source === "demo") {
      els.dataSourceLabel.textContent = "데모 예시 데이터 · 실제 판정 결과가 아닙니다";
    } else if (state.source === "error") {
      els.dataSourceLabel.textContent = "운영 API 연결 오류 · 재시도 필요";
    } else {
      els.dataSourceLabel.textContent = "데이터 출처 확인 중";
    }
  }

  function applyFilters() {
    if (state.loading) return;
    const query = els.searchInput.value.trim().toLocaleLowerCase("ko-KR");
    const eligibility = els.eligibilityFilter.value;
    const recommendation = els.recommendationFilter.value;
    const sort = els.sortSelect.value;

    let notices = state.notices.filter((notice) => {
      if (["all", "new", "review", "undecided", "go", "urgent"].includes(state.currentView) && notice.noticeStatus.toUpperCase() !== "OPEN") return false;
      if (state.currentView === "review" && (notice.noticeStatus.toUpperCase() !== "OPEN" || !isActionableEligibilityReview(notice))) return false;
      if (state.currentView === "go" && notice.recommendation !== "GO") return false;
      if (state.currentView === "urgent") {
        const days = daysUntil(notice.deadline);
        if (days === null || days < 0 || days > URGENT_DEADLINE_DAYS) return false;
      }
      if (state.currentView === "undecided" && notice.decision) return false;
      if (state.currentView === "closed" && !notice.resultStatus) return false;
      if (eligibility !== "all" && notice.eligibilityStatus !== eligibility) return false;
      if (recommendation !== "all" && notice.recommendation !== recommendation) return false;
      if (query && !`${notice.title} ${notice.agency} ${notice.noticeNumber}`.toLocaleLowerCase("ko-KR").includes(query)) return false;
      return true;
    });

    notices = notices.slice().sort((a, b) => compareNotices(a, b, sort));
    state.filteredNotices = notices;
    renderNoticeList();
  }

  function compareNotices(a, b, sort) {
    const priority = analysisPriorityRank(a) - analysisPriorityRank(b);
    if (priority !== 0) return priority;

    let selectedOrder = 0;
    if (sort === "department") {
      const left = departmentSortSignal(a);
      const right = departmentSortSignal(b);
      selectedOrder = nullableNumberSort(right.tier, left.tier)
        || nullableNumberSort(right.score, left.score);
    }
    else if (sort === "readiness") selectedOrder = nullableNumberSort(b.readinessScore, a.readinessScore);
    else if (sort === "risk") selectedOrder = nullableNumberSort(a.riskScore, b.riskScore);
    else if (sort === "newest") selectedOrder = nullableDateSort(b.collectedAt, a.collectedAt);
    else selectedOrder = nullableDateSort(a.deadline, b.deadline);
    if (selectedOrder !== 0) return selectedOrder;

    const deadlineOrder = nullableDateSort(a.deadline, b.deadline);
    if (deadlineOrder !== 0) return deadlineOrder;
    const leftDepartment = departmentSortSignal(a);
    const rightDepartment = departmentSortSignal(b);
    const departmentOrder = nullableNumberSort(rightDepartment.tier, leftDepartment.tier)
      || nullableNumberSort(rightDepartment.score, leftDepartment.score);
    if (departmentOrder !== 0) return departmentOrder;
    return nullableDateSort(b.collectedAt, a.collectedAt);
  }

  function departmentSortSignal(notice) {
    const selected = notice.departmentRanking;
    if (selected && selected.departmentId !== "organization") {
      const tiers = { TOP: 3, ROUTING: 3, REVIEW: 2, NONE: 0 };
      return {
        tier: tiers[selected.recommendationTier] ?? 0,
        score: selected.rankingScope === "REGION" ? selected.routingScore : selected.businessScore,
      };
    }
    const top = notice.topDepartmentRankings[0];
    if (top) return { tier: 3, score: top.businessScore };
    const review = notice.departmentReviewCandidates[0];
    if (review) return { tier: 2, score: review.businessScore };
    return { tier: 0, score: selected?.score ?? 0 };
  }

  function analysisPriorityRank(notice) {
    if (notice.analysisState === "EVALUATED" && notice.eligibilityStatus === "PASS") return 0;
    if (isActionableEligibilityReview(notice)) return 1;
    if (notice.analysisState === "EVALUATED" && notice.eligibilityStatus === "FAIL") return 3;
    return 2;
  }

  function isDocumentQualityReview(notice) {
    if (notice.analysisState !== "EVALUATED" || notice.eligibilityStatus !== "REVIEW") return false;
    const code = String(notice.analysisReasonCode || notice.reasonCode || "").toUpperCase();
    return notice.reasonCode === "R07" || [
      "ATTACHMENT_MANIFEST_MISSING",
      "ATTACHMENT_NONE",
      "HWP_ONLY_UNSUPPORTED",
      "HWPX_EXTRACT_FAILED",
      "PDF_EXTRACT_FAILED",
      "OPENAI_REVIEW",
      "UNVERIFIED_QUOTE",
      "QUOTE_UNVERIFIED",
      "PARTIAL",
    ].includes(code);
  }

  function isActionableEligibilityReview(notice) {
    return notice.analysisState === "EVALUATED"
      && notice.eligibilityStatus === "REVIEW"
      && !isDocumentQualityReview(notice);
  }

  function nullableNumberSort(a, b) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    return a - b;
  }

  function nullableDateSort(a, b) {
    const first = validDate(a)?.getTime();
    const second = validDate(b)?.getTime();
    if (first == null && second == null) return 0;
    if (first == null) return 1;
    if (second == null) return -1;
    return first - second;
  }

  function renderNoticeList() {
    const count = state.filteredNotices.length;
    const total = state.notices.length;
    const ownerLabel = els.departmentSelect.selectedOptions[0]?.textContent || "전사 공통";
    const keywordLabel = els.priorityKeywordInput.value.trim();
    const context = keywordLabel ? `${ownerLabel} · 검색어 ${keywordLabel}` : ownerLabel;
    els.noticeSummary.textContent = count === total
      ? `총 ${formatNumber(total)}건 · ${context} 기준 우선순위입니다.`
      : `전체 ${formatNumber(total)}건 중 ${formatNumber(count)}건이 표시됩니다.`;

    els.loadingState.hidden = true;
    els.errorState.hidden = true;
    els.emptyState.hidden = count !== 0;
    els.noticeTableWrap.hidden = count === 0 || state.layout !== "table";
    els.noticeCardGrid.hidden = count === 0 || state.layout !== "cards";

    if (count === 0) {
      els.noticeTableBody.replaceChildren();
      els.noticeCardGrid.replaceChildren();
      return;
    }

    els.noticeTableBody.innerHTML = state.filteredNotices.map(renderNoticeRow).join("");
    els.noticeCardGrid.innerHTML = state.filteredNotices.map(renderNoticeCard).join("");
  }

  function renderNoticeRow(notice) {
    const deadline = deadlineInfo(notice.deadline);
    const analyzed = notice.analysisState === "EVALUATED";
    const readiness = analyzed ? formatScore(notice.readinessScore) : "미산정";
    const readinessClass = analyzed ? scoreClass(notice.readinessScore) : "is-unknown";
    return `
      <tr class="notice-row" data-notice-key="${escapeAttribute(notice.noticeKey)}">
        <td>
          <button class="notice-title-button" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 상세보기">
            <span class="notice-title">${escapeHtml(notice.title)}</span>
            <span class="notice-meta">${sourceKindBadge(notice)}<span>${escapeHtml(notice.agency)}</span><span class="dot-divider">${escapeHtml(formatBudget(notice.budget))}</span></span>
            ${analyzed ? "" : `<span class="notice-analysis-reason" title="${escapeAttribute(notice.analysisReason)}">미분석 사유 · ${escapeHtml(truncateText(notice.analysisReason, 120))}</span>`}
            ${departmentPriorityBadge(notice)}
          </button>
          ${manualAnalysisAction(notice, "table")}
        </td>
        <td><span class="deadline ${deadline.urgent ? "is-urgent" : ""}">${escapeHtml(deadline.date)}<small>${escapeHtml(deadline.relative)}</small></span></td>
        <td>${analysisStatusPill(notice)}</td>
        <td><div class="score-cell ${readinessClass}"><strong class="${analyzed ? "" : "metric-pending"}">${readiness}</strong><span class="mini-bar" aria-hidden="true"><span style="width:${analyzed ? clamp(notice.readinessScore ?? 0, 0, 100) : 0}%"></span></span></div></td>
        <td><span class="risk-score ${analyzed ? riskClass(notice.riskScore) : "is-unknown"}">${analyzed ? riskDisplayValue(notice) : "미산정"}</span></td>
        <td>${analysisRecommendationPill(notice)}</td>
        <td>
          <button class="row-arrow" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 상세 패널 열기">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
          </button>
        </td>
      </tr>`;
  }

  function renderNoticeCard(notice) {
    const deadline = deadlineInfo(notice.deadline);
    const analyzed = notice.analysisState === "EVALUATED";
    return `
      <article class="notice-card" data-notice-key="${escapeAttribute(notice.noticeKey)}">
        <button class="notice-card__body" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 상세보기">
          <span class="notice-card__head">
            <span>${sourceKindBadge(notice)} ${analysisStatusPill(notice)}</span>
            <span class="notice-card__deadline ${deadline.urgent ? "is-urgent" : ""}">${escapeHtml(deadline.relative)}</span>
          </span>
          <h3>${escapeHtml(notice.title)}</h3>
          <p>${escapeHtml(notice.agency)} · ${escapeHtml(formatBudget(notice.budget))}</p>
          ${analyzed ? "" : `<span class="notice-card__analysis-reason">미분석 사유 · ${escapeHtml(truncateText(notice.analysisReason, 140))}</span>`}
          ${departmentPriorityBadge(notice)}
          <span class="notice-card__metrics">
            <span class="notice-card__metric"><small>준비도</small><strong class="${analyzed ? "" : "metric-pending"}">${analyzed ? formatScore(notice.readinessScore) : "미산정"}</strong></span>
            <span class="notice-card__metric"><small>리스크</small><strong class="${analyzed && notice.riskScore !== null ? "" : "metric-pending"}">${analyzed ? riskDisplayValue(notice) : "미산정"}</strong></span>
          </span>
        </button>
        <footer class="notice-card__foot">
          ${analysisRecommendationPill(notice)}
          <span class="notice-card__actions">
            ${manualAnalysisAction(notice, "card")}
            <button class="recommendation-arrow" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 상세 패널 열기">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
            </button>
          </span>
        </footer>
      </article>`;
  }

  function canRequestManualAnalysis(notice) {
    return Boolean(
      state.manualAnalysisEnabled
      && state.source === "api"
      && notice?.sourceKind === "PPS"
      && String(notice.noticeStatus || "").toUpperCase() === "OPEN"
      && notice.analysisState !== "EVALUATED",
    );
  }

  function manualAnalysisAction(notice, context) {
    if (!canRequestManualAnalysis(notice)) return "";
    const running = state.manualAnalysisRequests.get(notice.noticeKey) === "running";
    const retryLabel = notice.analysisAttempted ? "재분석 요청" : "지금 분석";
    return `<button class="manual-analysis-action manual-analysis-action--${escapeAttribute(context)}" type="button" data-manual-analysis data-notice-key="${escapeAttribute(notice.noticeKey)}" ${running ? "disabled" : ""} aria-label="${escapeAttribute(notice.title)} ${retryLabel}">${running ? '<span class="button-spinner" aria-hidden="true"></span>분석 중…' : retryLabel}</button>`;
  }

  function departmentPriorityBadge(notice) {
    const ranking = notice.departmentRanking;
    if (!ranking) return "";
    const businessOwner = notice.topDepartmentRankings[0] || null;
    const reviewOwner = notice.departmentReviewCandidates[0] || null;
    const regionOwner = notice.regionRouting[0] || null;
    const selectedOwner = ranking.departmentId === "organization"
      ? (businessOwner || reviewOwner || ranking)
      : ranking;
    const label = selectedOwner.recommendationTier === "TOP"
      ? "부서 추천"
      : selectedOwner.recommendationTier === "REVIEW"
        ? "추가 검토"
        : selectedOwner.recommendationTier === "ROUTING"
          ? "지역 라우팅"
          : "사업부 미분류";
    const fitScore = selectedOwner.rankingScope === "REGION"
      ? selectedOwner.routingScore
      : selectedOwner.businessScore;
    const region = regionOwner && selectedOwner.departmentId !== regionOwner.departmentId
      ? ` · 지역 라우팅 ${regionOwner.departmentName}`
      : "";
    const reasons = [
      ...selectedOwner.reasons,
      ...(regionOwner?.reasons || []),
    ].join(" · ");
    return `<span class="department-priority department-priority--${escapeAttribute(selectedOwner.priority.toLowerCase())}" title="${escapeAttribute(reasons)}"><strong>${escapeHtml(label)}${fitScore > 0 ? ` ${formatScore(fitScore)}` : ""}</strong><span>${escapeHtml(selectedOwner.departmentName + region)}</span></span>`;
  }

  function setLoading(isLoading) {
    state.loading = isLoading;
    els.refreshButton.disabled = isLoading;
    if (isLoading) {
      els.loadingState.hidden = false;
      els.errorState.hidden = true;
      els.emptyState.hidden = true;
      els.noticeTableWrap.hidden = true;
      els.noticeCardGrid.hidden = true;
      els.noticeSummary.textContent = "공고를 불러오는 중입니다.";
    }
  }

  function setSystemStatus(mode) {
    els.systemStatusDot.className = "status-dot";
    if (mode === "online") {
      els.systemStatusDot.classList.add("is-online");
      els.systemStatusText.textContent = state.accessMode === "PUBLIC_READ_ONLY" ? "온라인 · 읽기 전용" : "운영 API 연결됨";
    } else if (mode === "demo") {
      els.systemStatusDot.classList.add("is-demo");
      els.systemStatusText.textContent = "데모 모드";
    } else if (mode === "error") {
      els.systemStatusDot.classList.add("is-error");
      els.systemStatusText.textContent = "연결 오류";
    } else {
      els.systemStatusText.textContent = "연결 확인 중";
    }
    const sync = state.dashboard.lastSync;
    els.lastSyncText.textContent = sync ? `마지막 동기화 ${formatRelativeDateTime(sync)}` : "마지막 동기화 —";
  }

  function showDemoBanner(reason) {
    els.demoBanner.hidden = state.currentView === "performance";
    els.demoBannerTitle.textContent = state.source === "api" ? "합성 데이터가 포함되어 있습니다." : "데모 데이터로 보고 있습니다.";
    els.demoBannerReason.textContent = reason;
    els.retryApiButton.textContent = state.source === "api" ? "데이터 새로고침" : "실데이터 다시 연결";
  }

  function hideDemoBanner() {
    els.demoBanner.hidden = true;
  }

  function setView(view) {
    const clearedServerFilters = resetNoticeFiltersForView();
    state.currentView = view;
    const titles = {
      all: ["오늘의 입찰 기회", "검토할 공고"],
      collected: ["수집 공고", "수집된 전체 공고"],
      new: ["진행 공고", "현재 진행 중인 공고"],
      review: ["자격 검토", "원문 품질 보완과 구분된 자격 검토 공고"],
      go: ["GO 후보", "GO 추천 공고"],
      urgent: ["마감 임박", `${URGENT_DEADLINE_DAYS}일 이내 마감 공고`],
      undecided: ["결정 관리", "아직 결정되지 않은 공고"],
      closed: ["결과 학습", "결과가 확인된 공고"],
      performance: ["회사 실적", "회사 수행 실적"],
    };
    els.pageTitle.textContent = titles[view]?.[0] || titles.all[0];
    els.noticeHeading.textContent = titles[view]?.[1] || titles.all[1];
    const navigationView = ["collected", "go", "urgent"].includes(view) ? "all" : view;
    els.navItems.forEach((item) => {
      const active = item.dataset.view === navigationView;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    els.kpiViewButtons.forEach((button) => {
      const active = button.dataset.kpiView === view;
      button.setAttribute("aria-pressed", String(active));
      button.closest(".kpi-card")?.classList.toggle("is-active", active);
    });
    const performanceView = view === "performance";
    els.opportunityHero.hidden = performanceView;
    els.opportunityKpis.hidden = performanceView;
    els.noticeSection.hidden = performanceView;
    els.performanceSection.hidden = !performanceView;
    els.replayButton.hidden = performanceView;
    els.footerDisclaimer.textContent = performanceView
      ? "공개 실적은 유사 후보 탐색용이며, 공고별 인정실적·인정금액·정량점수를 확정하지 않습니다."
      : "PAI LOOP는 담당자의 판단을 돕는 도구이며 자동 입찰을 수행하지 않습니다.";
    if (performanceView) {
      els.demoBanner.hidden = true;
      if (!state.performance.loaded && !state.performance.loading) void loadPerformance();
      else if (state.performance.loaded) renderPerformanceView();
    } else if (state.source === "demo") {
      showDemoBanner(state.sourceReason);
    } else if (state.dashboard.syntheticWarning && state.notices.some((notice) => notice.isSynthetic)) {
      showDemoBanner(state.dashboard.syntheticWarning);
    } else {
      hideDemoBanner();
    }
    closeMobileMenu();
    if (!performanceView) {
      const desiredStatusScope = noticeStatusScopeForView(view);
      const requestNeedsReload = state.noticeStatusScope !== desiredStatusScope || clearedServerFilters;
      if ((state.source === "api" || state.loading) && requestNeedsReload) {
        void loadApplicationData({ forceApi: true });
      } else if (state.source === "error") {
        renderApplicationError(state.sourceReason);
      } else {
        applyFilters();
      }
    }
    renderDataSource();
    els.mainContent.focus({ preventScroll: true });
  }

  function resetNoticeFiltersForView() {
    const clearedServerFilters = Boolean(
      els.searchInput.value.trim()
      || els.priorityKeywordInput.value.trim()
      || els.departmentSelect.value !== "organization"
    );
    window.clearTimeout(state.noticeSearchTimer);
    state.noticeSearchTimer = null;
    els.searchInput.value = "";
    els.priorityKeywordInput.value = "";
    els.departmentSelect.value = "organization";
    els.eligibilityFilter.value = "all";
    els.recommendationFilter.value = "all";
    return clearedServerFilters;
  }

  function setLayout(layout) {
    state.layout = layout === "cards" ? "cards" : "table";
    els.layoutButtons.forEach((button) => {
      const active = button.dataset.layout === state.layout;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (!state.loading) renderNoticeList();
  }

  function resetFilters() {
    els.filterForm.reset();
  }

  function handleNoticeActivation(event) {
    const target = event.target.closest("[data-open-notice]");
    if (!target) return;
    const row = target.closest("[data-notice-key]");
    if (!row) return;
    openDetail(row.dataset.noticeKey, target);
  }

  function handleManualAnalysisActivation(event) {
    const button = event.target.closest("[data-manual-analysis]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const noticeKey = button.dataset.noticeKey;
    if (noticeKey) void requestManualAnalysis(noticeKey);
  }

  async function requestManualAnalysis(noticeKey) {
    if (state.manualAnalysisRequests.get(noticeKey) === "running") return;
    const notice = state.notices.find((item) => item.noticeKey === noticeKey);
    if (!canRequestManualAnalysis(notice)) {
      showToast("분석 요청 불가", "현재 열려 있는 미분석 조달청 공고만 요청할 수 있습니다.", "warning");
      return;
    }

    state.manualAnalysisRequests.set(noticeKey, "running");
    if (!state.loading) renderNoticeList();
    if (state.selectedNotice?.noticeKey === noticeKey) renderManualAnalysisDetailAction(state.selectedNotice);
    try {
      const payload = unwrapObject(await apiRequest(
        `/notices/${encodeURIComponent(noticeKey)}/analysis/request`,
        { method: "POST", timeoutMs: 90000 },
      ));
      const outcome = stringValue(payload.outcome).toUpperCase();
      const message = stringValue(payload.message, "분석 상태를 갱신했습니다.");
      await loadApplicationData({ forceApi: true });
      if (outcome === "COOLDOWN") {
        showToast("최근 분석 결과 사용", message, "warning");
      } else if (outcome === "REVIEW") {
        showToast("분석 요청 처리 완료", message, "warning");
      } else {
        showToast(outcome === "ALREADY_ANALYZED" ? "기존 분석 결과 사용" : "공고 분석 완료", message, "success");
      }
    } catch (error) {
      showToast("공고 분석 요청 실패", humanizeError(error), "error");
    } finally {
      state.manualAnalysisRequests.delete(noticeKey);
      if (!state.loading) renderNoticeList();
      if (state.selectedNotice?.noticeKey === noticeKey) renderManualAnalysisDetailAction(state.selectedNotice);
    }
  }

  function handleNoticeKeydown(event) {
    if ((event.key === "Enter" || event.key === " ") && event.target.closest(".notice-row") && !event.target.closest("button")) {
      event.preventDefault();
      const row = event.target.closest(".notice-row");
      openDetail(row.dataset.noticeKey, row);
    }
  }

  async function openDetail(noticeKey, trigger = null, { updateRoute = true } = {}) {
    const baseNotice = state.notices.find((notice) => notice.noticeKey === noticeKey);
    if (!baseNotice) return;
    state.selectedNotice = baseNotice;
    state.selectedTrigger = trigger || document.activeElement;
    renderDetail(baseNotice);
    els.detailDrawer.classList.add("is-open");
    els.detailDrawer.setAttribute("aria-hidden", "false");
    els.drawerScrim.hidden = false;
    document.body.classList.add("is-locked");
    selectTab("overview");
    requestAnimationFrame(() => els.closeDetailButton.focus());
    void loadTeamsMockLogs(noticeKey);

    if (updateRoute) updateNoticeRoute(noticeKey);
    if (baseNotice.documentAnalyses.length) void loadPrivateMatchPreview(noticeKey);
    if (state.source !== "api") return;

    state.detailLoading = true;
    els.drawerLoading.hidden = false;
    try {
      const payload = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}`);
      const detail = normalizeNotice(unwrapObject(payload));
      const mergedSource = { ...baseNotice.raw, ...detail.raw, notice_key: noticeKey };
      if (!detail.departmentRanking && baseNotice.raw.department_ranking) {
        mergedSource.department_ranking = baseNotice.raw.department_ranking;
      }
      if (!detail.topDepartmentRankings.length && baseNotice.raw.top_department_rankings) {
        mergedSource.top_department_rankings = baseNotice.raw.top_department_rankings;
      }
      if (!detail.departmentReviewCandidates.length && baseNotice.raw.department_review_candidates) {
        mergedSource.department_review_candidates = baseNotice.raw.department_review_candidates;
      }
      if (!detail.regionRouting.length && baseNotice.raw.region_routing) {
        mergedSource.region_routing = baseNotice.raw.region_routing;
      }
      const merged = normalizeNotice(mergedSource);
      const index = state.notices.findIndex((notice) => notice.noticeKey === noticeKey);
      if (index >= 0) state.notices[index] = merged;
      state.selectedNotice = merged;
      renderDetail(merged);
      if (merged.documentAnalyses.length) void loadPrivateMatchPreview(noticeKey);
      applyFilters();
    } catch (error) {
      showToast("상세 API 연결 오류", `${humanizeError(error)} · 목록에 포함된 정보로 표시합니다.`, "warning");
    } finally {
      state.detailLoading = false;
      els.drawerLoading.hidden = true;
    }
  }

  function renderDetail(notice) {
    const deadline = deadlineInfo(notice.deadline);
    const requirements = notice.requirements;
    const evidence = notice.evidence;
    const analyzed = notice.analysisState === "EVALUATED";
    const qualityReview = isDocumentQualityReview(notice);
    els.detailSourceBadge.textContent = sourceKindLabel(notice, true);
    els.detailSourceBadge.classList.toggle("is-demo", notice.isSynthetic);
    els.detailNoticeId.textContent = `공고번호 ${notice.noticeNumber}`;
    els.openSourceDialogButton.title = notice.sourceUrl ? "조달청 원문 링크 확인" : "공개 가능한 원문 링크 상태 확인";
    renderManualAnalysisDetailAction(notice);
    els.detailTitle.textContent = notice.title;
    els.detailAgency.textContent = [notice.agency, notice.demandAgency].filter(Boolean).join(" · ");
    els.detailTags.innerHTML = [
      `<span class="detail-tag">${escapeHtml(notice.category)}</span>`,
      `<span class="detail-tag">${escapeHtml(notice.region)}</span>`,
      deadline.urgent ? `<span class="detail-tag detail-tag--urgent">${escapeHtml(deadline.relative)}</span>` : "",
    ].join("");
    els.detailFacts.innerHTML = [
      detailFact("마감일", `${deadline.date} ${deadline.time}`.trim()),
      detailFact("사업예산", formatBudget(notice.budget)),
      detailFact("계약방식", notice.method),
    ].join("");
    els.decisionSummary.innerHTML = [
      summaryMetric("참가 자격", analyzed ? analysisStatusLabel(notice) : "미분석", analyzed && !isDocumentQualityReview(notice) ? "" : "summary-metric--pending"),
      summaryMetric("준비도 / 증빙", qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.readinessScore)} / ${formatScore(notice.evidenceCoverage)}` : "미산정", analyzed && !qualityReview ? "" : "summary-metric--pending"),
      summaryMetric("AI 추천", analysisRecommendationLabel(notice), analyzed && !isDocumentQualityReview(notice) ? "summary-metric--recommendation" : "summary-metric--pending"),
    ].join("");
    els.analysisPipeline.innerHTML = renderPipeline(notice);
    els.detailSummary.textContent = qualityReview
      ? notice.analysisReason || "원문 근거 검증을 보완한 뒤 자격과 추천을 확정합니다."
      : analyzed ? notice.summary : notice.analysisReason;
    els.briefEvidenceLabel.innerHTML = qualityReview
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M12 8v4M12 16h.01" /></svg>근거 보완'
      : analyzed && evidence.length
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>근거 연결'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M12 8v4M12 16h.01" /></svg>분석 대기';
    els.briefEvidenceLabel.classList.toggle("is-pending", qualityReview || !analyzed || !evidence.length);
    renderDocumentAnalyses(notice);
    els.eligibilityOverall.innerHTML = analysisStatusPill(notice);
    els.evidenceCount.textContent = String(evidence.length);
    els.requirementList.innerHTML = requirements.length
      ? requirements.map(renderRequirement).join("")
      : emptyPanel("구조화된 자격 조건이 없습니다", "첨부파일 분석이 완료되면 조건별 판정이 표시됩니다.");
    renderActions(notice);
    els.evidenceList.innerHTML = evidence.length
      ? evidence.map(renderEvidence).join("")
      : emptyPanel("연결된 원문 근거가 없습니다", "근거가 없는 결과는 확정 판정으로 사용하지 마세요.");
    renderQuantAndRisk(notice);
    renderAwardHistoryPanel(notice);
    renderTeamsPreview(notice);
    renderExistingDecision(notice);
    els.drawerScroll.scrollTop = 0;
  }

  function renderManualAnalysisDetailAction(notice) {
    const visible = canRequestManualAnalysis(notice);
    els.manualAnalyzeButton.hidden = !visible;
    if (!visible) return;
    const running = state.manualAnalysisRequests.get(notice.noticeKey) === "running";
    const label = running ? "분석 중…" : notice.analysisAttempted ? "재분석 요청" : "이 공고 분석";
    els.manualAnalyzeButton.disabled = running;
    els.manualAnalyzeButton.dataset.noticeKey = notice.noticeKey;
    els.manualAnalyzeButton.querySelector("span").textContent = label;
    els.manualAnalyzeButton.setAttribute("aria-label", `${notice.title} ${label}`);
  }

  function detailFact(label, value) {
    return `<div class="detail-fact"><small>${escapeHtml(label)}</small><strong title="${escapeAttribute(value)}">${escapeHtml(value)}</strong></div>`;
  }

  function summaryMetric(label, value, className) {
    return `<div class="summary-metric ${className}"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`;
  }

  function renderDocumentAnalyses(notice) {
    const analyses = notice.documentAnalyses;
    const reviewCount = analyses.filter((item) => item.needsReview).length;
    els.documentAnalysisState.className = "document-analysis-state";
    renderPrivateMatchPreview(notice);

    if (analyses.length) {
      els.documentAnalysisState.textContent = reviewCount ? `${reviewCount}건 검토 필요` : "구조화 완료";
      els.documentAnalysisState.classList.add(reviewCount ? "is-review" : "is-ready");
      els.documentAnalysisList.innerHTML = analyses.map((item) => {
        const statusLabel = item.needsReview
          ? "검토 필요"
          : ["FAILED", "ERROR"].includes(item.status) ? "분석 오류" : "분석 완료";
        const requirementLabel = item.requirementCount === null ? "미확인" : `${formatNumber(item.requirementCount)}건`;
        return `
          <article class="document-analysis-item">
            <div class="document-analysis-item__head">
              <strong title="${escapeAttribute(item.documentName)}">${escapeHtml(item.documentName)}</strong>
              <span class="${item.needsReview ? "is-review" : ""}">${escapeHtml(statusLabel)}</span>
            </div>
            <p>${escapeHtml(truncateText(item.summary, 260))}</p>
            <dl>
              <div><dt>추출 요구조건</dt><dd>${escapeHtml(requirementLabel)}</dd></div>
              <div><dt>분석 신뢰도</dt><dd>${item.confidence === null ? "미제공" : `${Math.round(item.confidence)}%`}</dd></div>
              ${item.analyzedAt ? `<div><dt>분석 시각</dt><dd>${escapeHtml(formatShortDateTime(item.analyzedAt))}</dd></div>` : ""}
            </dl>
          </article>`;
      }).join("");
      return;
    }

    const versionCount = notice.versions.length;
    let stateLabel = "수집 완료";
    let title = "공고 원문 수집 완료";
    let description = notice.analysisReason;

    if (notice.analysisState === "VERSIONED") {
      stateLabel = "문서 버전 수집됨";
      title = `첨부문서 버전 ${versionCount || 1}건 수집 완료`;
      description = notice.analysisReason;
    } else if (notice.analysisState === "FAILED") {
      stateLabel = "분석 확인 필요";
      title = "첨부문서 분석이 완료되지 않았습니다";
      description = notice.analysisReason;
      els.documentAnalysisState.classList.add("is-review");
    } else if (notice.analysisState === "EVALUATED") {
      stateLabel = "평가 완료";
      title = "문서별 구조화 요약이 제공되지 않았습니다";
      description = "종합 평가는 완료됐지만 현재 API 응답에는 안전하게 표시할 문서별 요약이 없습니다.";
      els.documentAnalysisState.classList.add("is-ready");
    }

    els.documentAnalysisState.textContent = stateLabel;
    els.documentAnalysisList.innerHTML = `<div class="document-analysis-waiting"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(description)}</span></div>`;
  }

  async function loadPrivateMatchPreview(noticeKey, { force = false } = {}) {
    const notice = state.notices.find((item) => item.noticeKey === noticeKey) || state.selectedNotice;
    if (!notice || !notice.documentAnalyses.length) return;
    const current = state.privateMatchPreviews[noticeKey];
    if (!force && current?.status === "loading") return;

    if (state.source !== "api") {
      state.privateMatchPreviews[noticeKey] = {
        status: "waiting",
        message: "데모 데이터에서는 온라인 공개 프로필 판정을 실행하지 않습니다.",
        data: null,
      };
      if (state.selectedNotice?.noticeKey === noticeKey) renderPrivateMatchPreview(notice);
      return;
    }

    state.privateMatchPreviews[noticeKey] = { status: "loading", message: "", data: null };
    if (state.selectedNotice?.noticeKey === noticeKey) renderPrivateMatchPreview(notice);

    try {
      const payload = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}/analysis/requirement-policy`);
      state.privateMatchPreviews[noticeKey] = {
        status: "ready",
        message: "",
        data: normalizePrivateMatchPreview(payload),
      };
    } catch (error) {
      const waiting = error?.status === 422;
      state.privateMatchPreviews[noticeKey] = {
        status: waiting ? "waiting" : "error",
        message: humanizeError(error),
        data: null,
      };
      if (!waiting && force) showToast("회사 데이터 매칭 조회 오류", humanizeError(error), "warning");
    } finally {
      if (state.selectedNotice?.noticeKey === noticeKey) renderPrivateMatchPreview(notice);
    }
  }

  function normalizePrivateMatchPreview(raw) {
    const source = unwrapObject(raw);
    const counts = firstObject(source.counts);
    return {
      eligibilityCount: numberOrNull(firstValue(counts.ELIGIBILITY, counts.eligibility)) ?? 0,
      actionCount: numberOrNull(firstValue(counts.ACTION_REQUIRED, counts.action_required)) ?? 0,
      checklistCount: numberOrNull(firstValue(counts.CHECKLIST, counts.checklist)) ?? 0,
      informationCount: numberOrNull(firstValue(counts.INFORMATION, counts.information)) ?? 0,
      blockingActions: numberOrNull(firstValue(source.blocking_items, source.blockingItems, source.blocking_actions, source.blockingActions)) ?? 0,
      profileVersion: stringValue(firstValue(source.profile_version, source.profileVersion), "미확인"),
      policyVersion: stringValue(firstValue(source.policy_version, source.policyVersion), "미확인"),
      note: stringValue(firstValue(source.decision_boundary, source.decisionBoundary), "적격성, 행동필요, 체크리스트, 정보를 서로 분리합니다."),
      matches: arrayValue(source.items).map(normalizePrivateMatchItem),
    };
  }

  function normalizePrivateMatchItem(item, index) {
    const source = item && typeof item === "object" ? item : {};
    const evidence = firstObject(source.evidence);
    const condition = stringValue(firstValue(
      source.condition,
      source.normalized_condition,
      source.normalizedCondition,
      source.description,
      source.source_excerpt,
      source.sourceExcerpt,
    ), `구조화 요구조건 ${index + 1}`);
    return {
      requirementId: stringValue(firstValue(source.requirement_id, source.requirementId), `requirement-${index + 1}`),
      category: stringValue(firstValue(source.policy_class, source.policyClass), "INFORMATION").toUpperCase(),
      sourceCategory: stringValue(firstValue(source.source_category, source.sourceCategory), "OTHER").toUpperCase(),
      condition,
      mandatory: booleanValue(source.mandatory) ?? true,
      outcome: stringValue(source.outcome, "INFORMATION").toUpperCase(),
      blocking: booleanValue(source.blocking) ?? false,
      companyFactKey: normalizeCompanyFactKey(firstValue(source.company_fact_key, source.companyFactKey)),
      evidenceState: stringValue(firstValue(source.evidence_state, source.evidenceState), "NOT_REQUIRED"),
      deadlineCheckRequired: booleanValue(firstValue(source.deadline_check_required, source.deadlineCheckRequired)) ?? false,
      message: stringValue(source.message),
      detailLines: collectPrivateMatchDetails(source, condition),
      evidence: Object.keys(evidence).length ? {
        name: stringValue(firstValue(evidence.display_name, evidence.displayName), "공개 증빙"),
        fileName: stringValue(firstValue(evidence.source_file_name, evidence.sourceFileName)),
        sha256: stringValue(evidence.sha256),
        validFrom: firstValue(evidence.valid_from, evidence.validFrom, null),
        lastObservedAt: firstValue(evidence.last_observed_at, evidence.lastObservedAt, null),
        validUntil: firstValue(evidence.valid_until, evidence.validUntil, null),
      } : null,
    };
  }

  function normalizeCompanyFactKey(value) {
    const key = stringValue(value);
    if (key.toUpperCase().endsWith(":__NONE__")) return "";
    const sentinel = key.replace(/[\s_\/-]+/g, "").toUpperCase();
    if (["", "NONE", "NULL", "NA", "NOTREQUIRED", "별도회사증빙불필요", "회사증빙불필요"].includes(sentinel)) return "";
    return key;
  }

  function collectPrivateMatchDetails(source, condition) {
    const fields = [
      ["정규화 조건", firstValue(source.normalized_condition, source.normalizedCondition)],
      ["공고 원문", firstValue(source.source_excerpt, source.sourceExcerpt)],
      ["설명", source.description],
      ["확인할 일", source.action],
      ["판단 이유", source.why],
      ["판정 안내", source.message],
    ];
    const seen = new Set([stringValue(condition).replace(/\s+/g, " ").trim().toLocaleLowerCase("ko-KR")]);
    const details = [];
    fields.forEach(([label, rawValue]) => {
      const values = Array.isArray(rawValue) ? rawValue : [rawValue];
      values.forEach((value) => {
        if (typeof value !== "string" && typeof value !== "number") return;
        const text = stringValue(value).replace(/\s+/g, " ").trim();
        const key = text.toLocaleLowerCase("ko-KR");
        if (!text || seen.has(key)) return;
        seen.add(key);
        details.push({ label, text: truncateText(text, 600) });
      });
    });
    return details.length ? details : [{
      label: "판단 안내",
      text: "공개 가능한 상세 판단 근거가 아직 연결되지 않았습니다. 공고 원문과 담당자 확인이 필요합니다.",
    }];
  }

  function renderPrivateMatchPreview(notice) {
    const hasAnalyses = notice.documentAnalyses.length > 0;
    els.privateMatchSection.hidden = !hasAnalyses;
    if (!hasAnalyses) return;

    const preview = state.privateMatchPreviews[notice.noticeKey];
    const status = preview?.status || (state.source === "api" ? "idle" : "waiting");
    els.privateMatchBadge.className = "private-match-badge";
    els.privateMatchRetryButton.disabled = status === "loading";

    if (status !== "ready") {
      const content = {
        idle: ["확인 대기", "온라인 공개 프로필 판정 준비", "상세 데이터가 준비되면 4분류 판단 기준을 확인합니다."],
        loading: ["조회 중", "온라인 공개 프로필로 판단 기준을 적용하고 있습니다", "적격성·행동필요·체크리스트·정보를 분리합니다."],
        waiting: ["준비 대기", "판단 기준 적용 준비가 필요합니다", preview?.message || "공고문 구조화 분석을 먼저 완료하세요."],
        error: ["연결 오류", "온라인 공개 프로필 판정을 불러오지 못했습니다", preview?.message || "잠시 후 다시 확인해 주세요."],
      }[status] || ["확인 대기", "온라인 공개 프로필 판정 준비", "잠시 후 다시 확인해 주세요."];
      els.privateMatchBadge.textContent = content[0];
      els.privateMatchBadge.classList.add(status === "loading" ? "is-loading" : status === "error" ? "is-error" : "is-review");
      els.privateMatchBody.innerHTML = `<div class="private-match-waiting"><strong>${escapeHtml(content[1])}</strong><span>${escapeHtml(content[2])}</span></div>`;
      els.privateMatchNote.textContent = "GitHub에 저장된 공개 안전 프로필만 사용하며 원문 증명서·등록번호·주소·사람 이름은 포함하지 않습니다.";
      return;
    }

    const data = preview.data;
    els.privateMatchBadge.textContent = data.blockingActions ? `확인 전 BLOCK ${data.blockingActions}건` : "온라인 프로필 적용";
    els.privateMatchBadge.classList.add(data.blockingActions ? "is-review" : "is-ready");
    els.privateMatchBody.innerHTML = `
      <div class="private-match-summary" aria-label="판단 기준 4분류 요약">
        ${privateMatchMetric("적격성", data.eligibilityCount, "건")}
        ${privateMatchMetric("행동 필요", data.actionCount, "건")}
        ${privateMatchMetric("체크리스트", data.checklistCount, "건")}
        ${privateMatchMetric("정보", data.informationCount, "건")}
      </div>
      <p class="private-match-score-boundary">${escapeHtml(data.note)} 프로필 ${escapeHtml(data.profileVersion)}</p>
      <div class="private-match-list">
        ${data.matches.length ? data.matches.map(renderPrivateMatchItem).join("") : '<div class="private-match-waiting"><strong>표시할 요구조건이 없습니다</strong><span>구조화 요구조건이 추가되면 4분류 판단 기준을 표시합니다.</span></div>'}
      </div>`;
    els.privateMatchNote.textContent = "PASS 근거도 공고 마감일 기준으로 다시 확인합니다. 체크리스트와 정보는 그 자체로 참가자격 REVIEW를 만들지 않습니다.";
  }

  function privateMatchMetric(label, value, unit) {
    return `<div class="private-match-metric"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)} <span>${escapeHtml(unit)}</span></strong></div>`;
  }

  function renderPrivateMatchItem(item) {
    const outcomeLabels = {
      PASS_CURRENT: "현재 PASS · 마감일 재확인",
      PASS_EXCEPTION: "예외 PASS · 마감일 재확인",
      BLOCK_UNTIL_CONFIRMED: "참석 확인 전 BLOCK",
      READY: "체크 준비",
      CHECK_REQUIRED: "체크 필요",
      ACKNOWLEDGED: "정보 확인",
      INFORMATION: "정보",
      REVIEW: "REVIEW",
    };
    const stateClass = item.blocking
      ? "is-blocking"
      : item.outcome.startsWith("PASS")
        ? "is-pass"
        : item.category === "CHECKLIST"
          ? "is-action"
          : "is-unmapped";
    const evidenceMarkup = item.evidence
      ? `<div class="private-match-candidates"><span>공개 근거</span><span>${escapeHtml(item.evidence.name)}</span><code class="private-fact-key" title="${escapeAttribute(item.evidence.sha256)}">SHA-256 ${escapeHtml(item.evidence.sha256.slice(0, 12))}…</code><span>${escapeHtml(item.evidence.fileName)}</span></div>`
      : item.companyFactKey
        ? `<div class="private-match-candidates"><span>판단 기준</span><code class="private-fact-key">${escapeHtml(item.companyFactKey)}</code></div>`
        : "";
    const detailLines = item.detailLines.slice();
    if (!item.evidence && !item.companyFactKey && item.evidenceState === "NOT_REQUIRED") {
      detailLines.push({ label: "증빙 적용", text: "회사 증빙 대조 대상이 아닌 공고 정보·체크 항목입니다." });
    }
    return `
      <article class="private-match-item">
        <div class="private-match-item__head">
          <span class="private-match-category">${escapeHtml(privateMatchCategoryLabel(item.category))}${item.mandatory ? " · 공고상 필수" : ""}</span>
          <span class="private-match-state ${stateClass}">${escapeHtml(outcomeLabels[item.outcome] || item.outcome)}</span>
        </div>
        <p class="private-match-condition">${escapeHtml(item.condition)}</p>
        <ul class="private-match-details">${detailLines.map((detail) => `<li><strong>${escapeHtml(detail.label)}</strong><span>${escapeHtml(detail.text)}</span></li>`).join("")}</ul>
        ${evidenceMarkup}
      </article>`;
  }

  function privateMatchCategoryLabel(category) {
    return ({ ELIGIBILITY: "적격성", ACTION_REQUIRED: "행동 필요", CHECKLIST: "체크리스트", INFORMATION: "정보" })[category] || category;
  }

  function renderPipeline(notice) {
    const hasDocuments = notice.evidence.length > 0;
    const hasRules = notice.requirements.length > 0;
    const analyzed = notice.analysisState === "EVALUATED";
    const version = notice.latestVersion;
    const extractionComplete = version
      ? version.documentComplete !== false && version.extractionStatus === "COMPLETE"
      : hasDocuments;
    const extractionDetail = version
      ? `v${version.versionNo} · ${version.extractionConfidence === null ? version.extractionStatus : `${Math.round(version.extractionConfidence)}%`}`
      : hasDocuments ? `${notice.evidence.length}개 근거` : "확인 필요";
    const steps = [
      { name: "공고 수집", detail: "원문 보존", status: "done" },
      { name: "첨부 추출", detail: extractionDetail, status: extractionComplete ? "done" : "review" },
      { name: "규칙 판정", detail: analyzed ? analysisStatusLabel(notice) : hasRules ? "분석 대기" : "조건 대기", status: analyzed ? (notice.eligibilityStatus === "REVIEW" ? "review" : "done") : "pending" },
      { name: "담당자 결정", detail: notice.decision ? DECISION_LABELS[notice.decision] : "미결정", status: notice.decision ? "done" : "pending" },
    ];
    return steps.map((step) => `
      <div class="pipeline-step ${step.status === "review" ? "is-review" : step.status === "pending" ? "is-pending" : ""}">
        <span class="pipeline-step__icon" aria-hidden="true"><svg viewBox="0 0 24 24">${step.status === "done" ? '<path d="m5 12 4 4L19 6" />' : step.status === "review" ? '<path d="M12 7v6M12 17h.01" />' : '<circle cx="12" cy="12" r="7" />'}</svg></span>
        <span><strong>${escapeHtml(step.name)}</strong><small>${escapeHtml(step.detail)}</small></span>
      </div>`).join("");
  }

  function renderRequirement(requirement) {
    const mode = requirement.status.toLowerCase();
    const icon = requirement.status === "PASS"
      ? '<path d="m5 12 4 4L19 6" />'
      : requirement.status === "FAIL"
        ? '<path d="m7 7 10 10M17 7 7 17" />'
        : '<path d="M12 7v6M12 17h.01" />';
    return `
      <div class="requirement-item is-${escapeAttribute(mode)}">
        <span class="requirement-icon" aria-hidden="true"><svg viewBox="0 0 24 24">${icon}</svg></span>
        <span class="requirement-copy"><strong>${escapeHtml(requirement.title)}</strong><small>${escapeHtml(requirement.description)}</small></span>
        ${requirement.evidenceId ? '<button type="button" class="evidence-jump" data-evidence-jump>근거 보기</button>' : ''}
      </div>`;
  }

  function renderActions(notice) {
    let actions = notice.actions.slice();
    if (!actions.length) {
      actions = notice.requirements
        .filter((requirement) => ["REVIEW", "UNKNOWN", "FAIL"].includes(requirement.status))
        .map((requirement) => requirement.status === "FAIL"
          ? `${requirement.title}의 불일치 사유와 적용 가능한 예외 경로가 있는지 확인하세요.`
          : `${requirement.title}의 충족 여부와 최신 증빙을 확인하세요.`);
    }
    els.actionCard.hidden = actions.length === 0;
    els.actionList.innerHTML = actions.map((action) => `<li>${escapeHtml(action)}</li>`).join("");
  }

  function renderEvidence(item) {
    const confidence = item.confidence;
    const statusClass = item.status === "PROVISIONAL" ? "is-provisional" : item.status === "MISSING" ? "is-missing" : "";
    const statusLabel = item.status === "VERIFIED" ? "검증됨" : item.status === "PROVISIONAL" ? "잠정" : "누락";
    return `
      <article class="evidence-card" id="evidence-${escapeAttribute(item.id)}">
        <div class="evidence-card__head">
          <span class="evidence-file"><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6V3Z" /><path d="M14 3v5h5" /></svg><span>${escapeHtml(item.file)}</span></span>
          <span class="evidence-status ${statusClass}">${escapeHtml(statusLabel)}</span>
        </div>
        <blockquote class="evidence-quote">“${escapeHtml(item.quote)}”</blockquote>
        <div class="evidence-card__foot">
          <span>${escapeHtml(item.page)}</span>
          <span class="confidence"><span>추출 신뢰도 ${confidence === null ? "—" : `${Math.round(confidence)}%`}</span><span class="mini-bar" aria-hidden="true"><span style="width:${confidence ?? 0}%"></span></span></span>
        </div>
      </article>`;
  }

  async function loadQuantitativeEstimate(noticeKey, { force = false } = {}) {
    if (state.source !== "api") return;
    const current = state.quantitativeEstimates[noticeKey];
    if (!force && ["loading", "ready"].includes(current?.status)) return;
    state.quantitativeEstimates[noticeKey] = { status: "loading", data: null, message: "" };
    if (state.selectedNotice?.noticeKey === noticeKey) renderQuantAndRisk(state.selectedNotice);
    try {
      const data = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}/quantitative-estimate`);
      state.quantitativeEstimates[noticeKey] = { status: "ready", data, message: "" };
    } catch (error) {
      state.quantitativeEstimates[noticeKey] = { status: "error", data: null, message: humanizeError(error) };
    } finally {
      if (state.selectedNotice?.noticeKey === noticeKey) renderQuantAndRisk(state.selectedNotice);
    }
  }

  function renderQuantAndRisk(notice) {
    renderRiskPanel(notice);
    if (state.source !== "api") {
      renderLegacyQuantitative(notice);
      return;
    }
    const meta = state.quantitativeEstimates[notice.noticeKey];
    if (meta?.status === "ready" && meta.data) {
      renderQuantitativeEstimate(meta.data);
      return;
    }
    renderQuantitativePending(meta?.status || "idle", meta?.message || "");
  }

  function renderRiskPanel(notice) {
    const analyzed = notice.analysisState === "EVALUATED";
    const risk = notice.riskScore;
    els.riskTotalLabel.textContent = !analyzed ? "분석 전" : risk === null ? "근거 부족" : `총점 ${Math.round(risk)}`;
    const axes = analyzed ? notice.riskAxes : [];
    els.riskBars.innerHTML = axes.length
      ? axes.map((axis) => `
        <div class="risk-row ${riskClass(axis.score)}">
          <strong>${escapeHtml(axis.label)}</strong>
          <span class="risk-bar" aria-label="${escapeAttribute(axis.label)} ${Math.round(axis.score)}점"><span style="width:${clamp(axis.score, 0, 100)}%"></span></span>
          <span>${Math.round(axis.score)}</span>
        </div>`).join("")
      : emptyPanel(analyzed ? "리스크 근거가 아직 부족합니다" : "아직 리스크 분석 전입니다", analyzed ? "임의의 0점 대신 근거가 확보된 위험 축만 계산합니다. 자격·실행·경쟁·수익성·운영·문서 근거 연결이 필요합니다." : "수집된 공고의 첨부·조건 분석이 완료된 뒤 실제 산정값을 표시합니다.");
  }

  function renderQuantitativePending(status, message) {
    const loading = status === "loading";
    els.scoreOverview.innerHTML = [
      quantSummaryCard("예상 점수 범위", "미산정", loading ? "배점표 확인 중" : "공고별 산식 필요", "score-card--readiness"),
      quantSummaryCard("검증 커버리지", "0%", "확정 증빙 기준", "score-card--coverage"),
      quantSummaryCard("정량 준비도", "GRAY", "참가자격과 별도", "score-card--risk"),
    ].join("");
    els.quantSourceStatus.className = `quant-source-status ${status === "error" ? "is-error" : "is-loading"}`;
    els.quantSourceStatus.textContent = loading ? "배점표 확인 중" : status === "error" ? "조회 오류" : "조회 대기";
    els.quantOpinion.textContent = status === "error"
      ? `정량 추정치를 불러오지 못했습니다. ${message}`
      : "공고별 배점표와 공개 증빙 연결 상태를 확인합니다.";
    els.quantSourceAnchor.textContent = "";
    els.quantAssumptionList.innerHTML = "";
    els.quantTableBody.innerHTML = `<tr><td colspan="4">${emptyPanel(loading ? "정량 배점표를 확인하고 있습니다" : "정량 조회를 시작하지 않았습니다", loading ? "누락값은 임의 점수로 채우지 않습니다." : "정량·리스크 탭을 열면 저장된 공개 데이터를 조회합니다.")}</td></tr>`;
    els.quantObservationList.innerHTML = emptyPanel("적용 전 공개 근거 확인 중", "공개 실적 후보와 회사 프로필의 적용 경계를 함께 표시합니다.");
    els.quantSeparationNote.textContent = "정량 준비도는 참가자격과 GO/NO-GO 판단을 바꾸지 않는 별도 보조지표입니다.";
  }

  function renderLegacyQuantitative(notice) {
    const analyzed = notice.analysisState === "EVALUATED";
    els.scoreOverview.innerHTML = [
      scoreCard("준비도", notice.readinessScore, "score-card--readiness", analyzed),
      scoreCard("증빙 커버리지", notice.evidenceCoverage, "score-card--coverage", analyzed),
      quantSummaryCard("정량 데이터", "DEMO", "명시적 예시", "score-card--risk"),
    ].join("");
    els.quantSourceStatus.className = "quant-source-status is-demo";
    els.quantSourceStatus.textContent = "명시적 데모";
    els.quantOpinion.textContent = "현재 화면은 합성 예시입니다. 실제 공고의 정량점수로 사용하지 마세요.";
    els.quantSourceAnchor.textContent = "실제 제안요청서 원문 앵커 없음";
    els.quantAssumptionList.innerHTML = "<li>데모 데이터는 화면 동작 확인 전용입니다.</li>";
    els.quantTableBody.innerHTML = notice.quantitative.length
      ? notice.quantitative.map(renderQuantRow).join("")
      : `<tr><td colspan="4">${emptyPanel("정량 산식이 연결되지 않았습니다", "실제 평가표 구조화 후 확정점수 또는 예상 범위를 제공합니다.")}</td></tr>`;
    els.quantObservationList.innerHTML = emptyPanel("실제 공개 근거 없음", "명시적 데모에서는 공개 실적을 점수로 적용하지 않습니다.");
    els.quantSeparationNote.textContent = "DEMO · 정량 준비도는 참가자격과 GO/NO-GO 판단을 바꾸지 않는 별도 보조지표입니다.";
  }

  function renderQuantitativeEstimate(data) {
    const total = numberOrNull(data.total_max_points);
    const lower = numberOrNull(data.lower_points);
    const upper = numberOrNull(data.upper_points);
    const coverage = numberOrNull(data.evidence_coverage_pct) ?? 0;
    const readiness = numberOrNull(data.readiness_pct);
    const range = lower === null || upper === null || total === null
      ? "미산정"
      : `${formatNumber(lower, 1)}${lower === upper ? "" : `–${formatNumber(upper, 1)}`} / ${formatNumber(total, 1)}`;
    els.scoreOverview.innerHTML = [
      quantSummaryCard("예상 점수 범위", range, total === null ? "배점표 미확보" : "원문상 조건부 하한~상한", "score-card--readiness"),
      quantSummaryCard("검증 커버리지", `${formatNumber(coverage, 1)}%`, "CONFIRMED 항목 배점 기준", "score-card--coverage"),
      quantSummaryCard("정량 준비도", data.readiness_band || "GRAY", readiness === null ? "산정 불가" : `하한 기준 ${formatNumber(readiness, 1)}%`, "score-card--risk"),
    ].join("");

    const sourceLabels = { AVAILABLE: "배점표 연결", INCOMPLETE: "배점표 일부", MISSING: "배점표 미확보" };
    els.quantSourceStatus.className = `quant-source-status is-${String(data.rule_source_status || "missing").toLowerCase()}`;
    els.quantSourceStatus.textContent = `${sourceLabels[data.rule_source_status] || "배점표 검토"} · ${quantStatusLabel(data.overall_status)}`;
    els.quantOpinion.textContent = data.opinion || "정량 의견이 없습니다.";
    const anchor = data.source_anchor;
    els.quantSourceAnchor.textContent = anchor
      ? `${anchor.document_label} · ${anchor.page ? `PDF ${anchor.page}쪽 · ` : ""}${anchor.section} · SHA-256 ${anchor.document_sha256 ? `${anchor.document_sha256.slice(0, 12)}…` : "미확인"}`
      : "연결된 정량평가표 원문 앵커 없음";
    els.quantAssumptionList.innerHTML = Array.isArray(data.assumptions) && data.assumptions.length
      ? data.assumptions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")
      : "<li>추가 가정 없음</li>";
    els.quantTableBody.innerHTML = Array.isArray(data.criteria) && data.criteria.length
      ? data.criteria.map(renderQuantitativeEstimateRow).join("")
      : `<tr><td colspan="4">${emptyPanel("정량점수를 표시하지 않습니다", "배점표와 인정 산식이 확보될 때까지 REVIEW로 유지합니다.")}</td></tr>`;
    els.quantObservationList.innerHTML = Array.isArray(data.evidence_observations) && data.evidence_observations.length
      ? data.evidence_observations.map(renderQuantObservation).join("")
      : emptyPanel("적용 전 공개 근거 없음", "공고별 배점 산식과 연결된 공개 근거가 없습니다.");
    els.quantSeparationNote.textContent = data.separation_notice || "정량 준비도는 참가자격과 GO/NO-GO 판단을 바꾸지 않습니다.";
  }

  function quantSummaryCard(label, value, detail, className) {
    return `<div class="score-card quant-summary-card ${className}"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong><span class="quant-card-detail">${escapeHtml(detail)}</span></div>`;
  }

  function quantStatusLabel(status) {
    return ({ CONFIRMED: "확정", ESTIMATED: "잠정 범위", UNSCORABLE: "산정 불가", REVIEW: "검토 필요" })[status] || "검토 필요";
  }

  function renderQuantitativeEstimateRow(item) {
    const lower = numberOrNull(item.lower_points);
    const upper = numberOrNull(item.upper_points);
    const range = lower === null || upper === null
      ? "—"
      : `${formatNumber(lower, 1)}${lower === upper ? "" : `–${formatNumber(upper, 1)}`}점`;
    const anchor = item.source_anchor;
    const source = anchor
      ? `${anchor.page ? `PDF ${anchor.page}쪽 · ` : ""}SHA-256 ${anchor.document_sha256 ? `${anchor.document_sha256.slice(0, 10)}…` : "미확인"}`
      : "원문 앵커 없음";
    const floor = numberOrNull(item.rule_floor_points);
    const base = numberOrNull(item.rule_base_points);
    return `<tr>
      <td><strong>${escapeHtml(item.label)}</strong><small class="quant-row-formula">${escapeHtml(item.formula)}</small><small class="quant-row-source">${escapeHtml(source)}</small></td>
      <td>${formatNumber(item.max_points, 1)}</td>
      <td><strong class="quant-score-range">${escapeHtml(range)}</strong><small class="quant-row-rationale">${escapeHtml(item.rationale || "근거 확인 필요")}</small>${floor ? `<small class="quant-row-floor">원문상 조건부 하한 ${formatNumber(floor, 1)}점</small>` : ""}${base !== null ? `<small class="quant-row-base">원문상 가감 전 기본 ${formatNumber(base, 1)}점</small>` : ""}</td>
      <td><span class="quant-status is-${escapeAttribute(String(item.status || "review").toLowerCase())}">${escapeHtml(quantStatusLabel(item.status))}</span></td>
    </tr>`;
  }

  function renderQuantObservation(item) {
    const value = item.unit === "원"
      ? `${formatNumber(item.value)}원`
      : `${formatNumber(item.value)}${item.unit ? ` ${item.unit}` : ""}`;
    return `<article class="quant-observation">
      <span><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.status === "CANDIDATE_ONLY" ? "후보 전용 · 점수 확정값 아님" : "정량점수 미적용")}</small></span>
      <strong class="quant-observation-value">${escapeHtml(value)}</strong>
      <p>${escapeHtml(item.rationale)}</p>
      <code title="${escapeAttribute(item.evidence_key)}">${escapeHtml(String(item.evidence_key).slice(0, 48))}${String(item.evidence_key).length > 48 ? "…" : ""}</code>
    </article>`;
  }

  function scoreCard(label, value, className, analyzed = true) {
    const display = analyzed ? (value === null ? "미산정" : Math.round(value)) : "미산정";
    return `<div class="score-card ${className}"><small>${escapeHtml(label)}</small><strong class="${analyzed && value !== null ? "" : "metric-pending"}">${display}<span>${analyzed && value !== null ? " / 100" : ""}</span></strong><span class="progress-bar" aria-hidden="true"><span style="width:${analyzed ? value ?? 0 : 0}%"></span></span></div>`;
  }

  function renderQuantRow(item) {
    const statusClass = item.status === "PROVISIONAL" ? "is-provisional" : item.status === "MISSING" ? "is-missing" : "";
    const statusLabel = item.status === "VERIFIED" ? "확정" : item.status === "PROVISIONAL" ? "잠정" : "미확인";
    const expected = item.expectedScore === null || item.expectedScore === undefined ? "—" : String(item.expectedScore);
    return `<tr><td>${escapeHtml(item.label)}</td><td>${item.maxScore === null ? "—" : formatNumber(item.maxScore)}</td><td>${escapeHtml(expected)}</td><td><span class="quant-status ${statusClass}">${statusLabel}</span></td></tr>`;
  }

  async function loadStoredAwardHistory(noticeKey, { force = false } = {}) {
    if (state.source !== "api") return;
    const current = state.awardHistoryMeta[noticeKey];
    if (!force && ["loading", "ready", "empty"].includes(current?.status)) return;
    const notice = state.notices.find((item) => item.noticeKey === noticeKey) || state.selectedNotice;
    if (!notice) return;

    state.awardHistoryMeta[noticeKey] = { status: "loading", message: "" };
    if (state.selectedNotice?.noticeKey === noticeKey) renderAwardHistoryPanel(notice);

    try {
      const payload = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}/award-intelligence`);
      const rows = Array.isArray(payload?.records) ? payload.records.map(normalizeHistory) : [];
      const updated = {
        ...notice,
        awardHistory: rows,
        raw: sanitizeNoticeAwardHistory(notice.raw, rows),
      };
      const index = state.notices.findIndex((item) => item.noticeKey === noticeKey);
      if (index >= 0) state.notices[index] = updated;
      if (state.selectedNotice?.noticeKey === noticeKey) state.selectedNotice = updated;
      state.awardHistoryMeta[noticeKey] = { status: rows.length ? "ready" : "empty", message: "", intelligence: payload };
    } catch (error) {
      state.awardHistoryMeta[noticeKey] = { status: "error", message: humanizeError(error) };
    } finally {
      if (state.selectedNotice?.noticeKey === noticeKey) renderAwardHistoryPanel(state.selectedNotice);
    }
  }

  function renderAwardHistoryPanel(notice) {
    const items = notice.awardHistory;
    const meta = state.awardHistoryMeta[notice.noticeKey] || {};
    const status = state.source === "demo" ? "demo" : meta.status || (items.length ? "stored" : "empty");
    els.historyStatusLabel.className = "history-status-badge";

    if (status === "loading") {
      els.historyStatusLabel.textContent = "저장본 확인 중";
      els.historyStatusLabel.classList.add("is-loading");
      els.historyStatusText.textContent = "PAI_LOOP 서버에 저장된 낙찰 후보를 읽고 있습니다.";
    } else if (status === "ready" || status === "stored") {
      els.historyStatusLabel.textContent = `저장본 ${items.length}건`;
      els.historyStatusLabel.classList.add("is-ready");
      els.historyStatusText.textContent = "저장된 제목 유사 후보이며 동일 사업 확정 이력이 아닙니다.";
    } else if (status === "error") {
      els.historyStatusLabel.textContent = items.length ? `저장본 ${items.length}건` : "미수집";
      els.historyStatusLabel.classList.add("is-error");
      els.historyStatusText.textContent = items.length
        ? `저장 이력 재조회 실패 · 상세 응답의 저장본을 표시합니다. ${meta.message}`
        : `저장 이력을 확인하지 못했습니다. 외부 API는 호출하지 않았습니다. ${meta.message}`;
    } else if (status === "demo") {
      els.historyStatusLabel.textContent = items.length ? `예시 ${items.length}건` : "예시 미수집";
      els.historyStatusLabel.classList.add("is-demo");
      els.historyStatusText.textContent = "명시적 데모 데이터이며 실제 조달청 낙찰 기록이 아닙니다.";
    } else {
      els.historyStatusLabel.textContent = "미수집";
      els.historyStatusLabel.classList.add("is-empty");
      els.historyStatusText.textContent = "현재 서버에 저장된 낙찰 후보가 없습니다.";
    }

    renderAwardIntelligence(meta.intelligence, status);

    els.historyList.innerHTML = items.length
      ? items.map(renderHistory).join("")
      : emptyPanel("저장된 낙찰 이력이 없습니다", "아직 수집되지 않은 상태입니다. 이 화면에서는 외부 조달청 API를 자동 호출하지 않습니다.");
  }

  function renderAwardIntelligence(intelligence, status) {
    const loading = status === "loading";
    const concentration = intelligence?.concentration;
    const prediction = intelligence?.prediction;
    const coverage = intelligence?.field_coverage;
    if (loading) {
      els.historyConcentration.innerHTML = `<p class="section-kicker">WINNER CONCENTRATION</p><h4>집중도 계산 중</h4><p>저장 이력을 읽고 있습니다.</p>`;
      els.historyPrediction.innerHTML = `<p class="section-kicker">MODEL ESTIMATE</p><h4>가격 전략 계산 중</h4><p>외부 API 호출 없이 저장값만 사용합니다.</p>`;
      els.historyCoverage.innerHTML = `<p class="section-kicker">FIELD COVERAGE</p><h4>필드 확인 중</h4><p>누락값은 사실로 보간하지 않습니다.</p>`;
      els.historyWarnings.innerHTML = "";
      return;
    }
    if (!intelligence) {
      const note = status === "demo" ? "데모 이력에는 서버 계산 인텔리전스를 적용하지 않습니다." : "저장된 분석 결과가 없습니다.";
      els.historyConcentration.innerHTML = `<p class="section-kicker">WINNER CONCENTRATION</p><h4>산정 불가</h4><p>${escapeHtml(note)}</p>`;
      els.historyPrediction.innerHTML = `<p class="section-kicker">MODEL ESTIMATE</p><h4>예측 미제공</h4><p>유효 낙찰률 3건 이상과 명시적 기준금액이 필요합니다.</p>`;
      els.historyCoverage.innerHTML = `<p class="section-kicker">FACT BOUNDARY</p><h4>확인 가능한 값만 표시</h4><p>낙찰금액과 투찰금액, 기술점수와 가격점수는 서로 대체하지 않습니다.</p>`;
      els.historyWarnings.innerHTML = "";
      return;
    }

    const top = concentration?.top_winner;
    const hhi = numberOrNull(concentration?.hhi);
    const competition = intelligence?.competition_risk;
    const competitionAvailable = competition?.status === "MODEL_ESTIMATE" && numberOrNull(competition?.score) !== null;
    const competitionBand = ({ LOW: "낮음", MODERATE: "보통", HIGH: "높음", VERY_HIGH: "매우 높음", UNKNOWN: "미산정" })[competition?.band] || "미산정";
    const participantMedian = numberOrNull(competition?.components?.participant_count?.value);
    els.historyConcentration.innerHTML = `
      <p class="section-kicker">COMPETITION RISK · ELIGIBILITY와 별도</p><h4>경쟁·집중 리스크 ${escapeHtml(competitionBand)}</h4>
      <strong class="history-intel-value">${competitionAvailable ? formatNumber(competition.score, 1) : "—"} <small>${competitionAvailable ? "/ 100" : "UNKNOWN"}</small></strong>
      <p>HHI ${hhi === null ? "미산정" : formatNumber(hhi, 0)} · 상위 수주 비중 ${top ? `${formatNumber(top.share * 100, 1)}%` : "미확인"} · 참여 중앙값 ${participantMedian === null ? "미확인" : `${formatNumber(participantMedian, 1)}곳`}</p>
      <small>${escapeHtml(competition?.rationale || "필수 사실 커버리지가 부족해 점수를 보류했습니다.")}</small>
      <small>신뢰도 ${escapeHtml(competition?.confidence || "INSUFFICIENT")} · ${top ? `표본 상위 ${escapeHtml(top.winner_name)} ${formatNumber(top.count)}건` : "낙찰자 표본 없음"} · 법적 독점 판정 아님</small>`;

    const award = prediction?.award_rate;
    const submitted = prediction?.submitted_bid_rate;
    const amount = prediction?.award_amount_range;
    const pricingMethod = intelligence?.pricing_method;
    const awardAvailable = award?.status === "MODEL_ESTIMATE";
    els.historyPrediction.innerHTML = `
      <p class="section-kicker">MODEL ESTIMATE · 의사결정 참고</p><h4>예측 낙찰률 ${awardAvailable ? `${formatNumber(award.center, 2)}%` : "미제공"}</h4>
      <strong class="history-intel-value">${awardAvailable ? `${formatNumber(award.range_low, 2)}–${formatNumber(award.range_high, 2)}%` : "표본 부족"}</strong>
      <p>${amount ? `예상 낙찰금액 ${escapeHtml(formatBudget(amount.low))}–${escapeHtml(formatBudget(amount.high))}` : "기준/추정금액이 없거나 표본이 부족해 금액 범위를 산정하지 않았습니다."}</p>
      <p class="history-pricing-method">${pricingMethod ? `문서 근거 가격평가: ${escapeHtml(pricingMethod.method?.at_or_above_80_percent || "산식 확인")}` : "현재 공고 문서와 정확히 일치하는 가격평가 산식 근거 없음"}</p>
      <small>신뢰도 ${escapeHtml(award?.confidence || "INSUFFICIENT")} · ${formatNumber(award?.sample_count || 0)}건 · 투찰률 ${submitted?.status === "MODEL_ESTIMATE" ? `${formatNumber(submitted.center, 2)}%` : "별도 표본 부족"}</small>
      <small>${escapeHtml(award?.rationale || "유효 표본이 부족해 예측 근거를 제시하지 않습니다.")} · ${escapeHtml(award?.method || "산정 안 함")}</small>`;

    const coverageCell = (label, key) => {
      const field = coverage?.[key];
      return `<span><strong>${escapeHtml(label)}</strong><small>${formatNumber(field?.available || 0)} / ${formatNumber(field?.total || intelligence.record_count || 0)}건</small></span>`;
    };
    els.historyCoverage.innerHTML = `
      <p class="section-kicker">FIELD COVERAGE</p><h4>사실 필드 충족도</h4>
      <div class="history-coverage-grid">${coverageCell("낙찰자", "winner")}${coverageCell("참여기관 수", "participant_count")}${coverageCell("낙찰금액", "award_amount")}${coverageCell("예정가격", "estimated_price")}${coverageCell("투찰금액", "submitted_bid_price")}${coverageCell("기술점수", "technical_score")}${coverageCell("가격점수", "price_score")}</div>`;
    const warnings = [...new Set([
      ...(Array.isArray(intelligence.warnings) ? intelligence.warnings : []),
      ...(Array.isArray(competition?.warnings) ? competition.warnings : []),
    ])];
    els.historyWarnings.innerHTML = warnings.length ? `<strong>해석 주의</strong><ul>${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : "";
  }

  function renderHistory(item) {
    const eventDate = item.awardedAt || item.openedAt;
    const eventLabel = item.awardedAt ? "낙찰일" : item.openedAt ? "개찰일" : "일자";
    const dateText = formatCalendarDate(eventDate);
    const participants = item.participantCount === null ? "참여자 수 미확인" : `참여 ${formatNumber(item.participantCount)}곳`;
    const similarity = item.similarityScore === null ? "제목 유사도 미제공" : `제목 유사도 ${formatNumber(item.similarityScore, 1)}%`;
    return `
      <article class="history-card">
        <span class="history-year"><strong>${escapeHtml(item.year)}</strong><small>${item.awardedAt ? "낙찰" : item.openedAt ? "개찰" : "저장본"}</small></span>
        <span class="history-copy">
          <strong>${escapeHtml(item.title)}</strong>
          <span>${escapeHtml(item.winner)}${item.agency ? ` · ${escapeHtml(item.agency)}` : ""}</span>
          <small>${escapeHtml(`${eventLabel} ${dateText} · ${participants}`)}</small>
          <span class="history-fact-line">예정가격 ${item.estimatedPrice === null ? "미확인" : escapeHtml(formatBudget(item.estimatedPrice))} · 투찰금액 ${item.submittedBidPrice === null ? "미확인" : escapeHtml(formatBudget(item.submittedBidPrice))} · 기술점수 ${item.technicalScore === null ? "미확인" : formatNumber(item.technicalScore, 2)} · 가격점수 ${item.priceScore === null ? "미확인" : formatNumber(item.priceScore, 2)}</span>
        </span>
        <span class="history-price">
          <strong>${escapeHtml(formatBudget(item.amount))}</strong>
          <span>${item.rate === null ? "낙찰률 미확인" : `낙찰률 ${formatNumber(item.rate, 2)}%`}</span>
          <span>${item.submittedBidRate === null ? "투찰률 미확인" : `투찰률 ${formatNumber(item.submittedBidRate, 2)}%`}</span>
          <em>${escapeHtml(similarity)} · 후보</em>
        </span>
      </article>`;
  }

  function renderTeamsPreview(notice) {
    const analyzed = notice.analysisState === "EVALUATED";
    const qualityReview = isDocumentQualityReview(notice);
    const deadline = deadlineInfo(notice.deadline);
    els.teamsMockSource.textContent = sourceKindLabel(notice, true);
    els.teamsMockTitle.textContent = notice.title;
    els.teamsMockAgency.textContent = notice.agency;
    els.teamsMockStatus.textContent = analyzed
      ? qualityReview ? "근거 보완 · 판단 보류" : `자격 ${STATUS_LABELS[notice.eligibilityStatus]}`
      : notice.analysisState === "FAILED" ? "분석 오류 · 재처리 필요" : "수집 완료 · 분석 대기";
    els.teamsMockDeadline.textContent = `${deadline.relative} · ${deadline.date}`;
    els.teamsMockReason.textContent = analyzed
      ? qualityReview
        ? truncateText(notice.analysisReason || "원문 근거 검증을 보완한 뒤 자격과 추천을 확정합니다.", 180)
        : truncateText(notice.summary, 180)
      : "아직 결정론적 평가가 실행되지 않았습니다. 준비도·리스크·추천값을 임의로 생성하지 않고 분석 대기 상태만 알립니다.";
    els.teamsMockReadiness.textContent = qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.readinessScore)} / 100` : "미산정";
    els.teamsMockRisk.textContent = qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.riskScore)} / 100` : "미산정";
    els.teamsMockRecommendation.textContent = analysisRecommendationLabel(notice);
    els.teamsMockJson.textContent = JSON.stringify(buildAdaptiveCardPayload(notice), null, 2);
    els.teamsMockSendButton.disabled = !state.writeControlsEnabled || Boolean(state.teamsLogMeta[notice.noticeKey]?.sending);
    renderTeamsMockLogs(notice.noticeKey);
  }

  function buildAdaptiveCardPayload(notice) {
    const analyzed = notice.analysisState === "EVALUATED";
    const qualityReview = isDocumentQualityReview(notice);
    const deadline = deadlineInfo(notice.deadline);
    return {
      type: "AdaptiveCard",
      $schema: "http://adaptivecards.io/schemas/adaptive-card.json",
      version: "1.5",
      msteams: { width: "Full" },
      body: [
        {
          type: "TextBlock",
          text: "PAI LOOP · 새 입찰 검토 알림",
          weight: "Bolder",
          color: "Accent",
          size: "Medium",
        },
        {
          type: "TextBlock",
          text: notice.title,
          weight: "Bolder",
          wrap: true,
        },
        {
          type: "TextBlock",
          text: `${notice.agency} · ${sourceKindLabel(notice, true)}`,
          isSubtle: true,
          spacing: "Small",
          wrap: true,
        },
        {
          type: "FactSet",
          facts: [
            { title: "분석 상태", value: analyzed ? qualityReview ? "근거 보완 · 판단 보류" : `자격 ${STATUS_LABELS[notice.eligibilityStatus]}` : "수집 완료 · 분석 대기" },
            { title: "마감", value: `${deadline.relative} · ${deadline.date}` },
            { title: "준비도", value: qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.readinessScore)} / 100` : "미산정" },
            { title: "리스크", value: qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.riskScore)} / 100` : "미산정" },
            { title: "추천", value: analysisRecommendationLabel(notice) },
          ],
        },
        {
          type: "TextBlock",
          text: analyzed
            ? qualityReview
              ? truncateText(notice.analysisReason || "원문 근거 검증을 보완한 뒤 자격과 추천을 확정합니다.", 240)
              : truncateText(notice.summary, 240)
            : "평가 완료 전에는 점수와 추천을 제공하지 않습니다.",
          wrap: true,
          spacing: "Medium",
        },
      ],
      actions: [
        { type: "Action.Submit", title: "근거 상세보기", data: { action: "OPEN_NOTICE", notice_key: notice.noticeKey } },
        { type: "Action.Submit", title: "담당자 판단", data: { action: "OPEN_DECISION", notice_key: notice.noticeKey } },
      ],
    };
  }

  async function recordTeamsMockSend() {
    const notice = state.selectedNotice;
    if (!notice) return;
    if (!state.writeControlsEnabled) {
      showToast("읽기 전용 화면입니다", "Teams mock 기록은 사내 로그인 환경에서만 사용할 수 있습니다.", "warning");
      return;
    }
    const noticeKey = notice.noticeKey;
    const card = buildAdaptiveCardPayload(notice);
    const correlationId = createTeamsCorrelationId(noticeKey);
    const previousButtonHtml = els.teamsMockSendButton.innerHTML;
    state.teamsLogMeta[noticeKey] = {
      ...(state.teamsLogMeta[noticeKey] || {}),
      sending: true,
    };
    els.teamsMockSendButton.disabled = true;
    els.teamsMockSendButton.textContent = "서버 mock 로그에 기록 중…";

    try {
      if (state.source !== "api") throw new Error("데모 모드에서는 서버 mock API에 기록하지 않습니다");
      const payload = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}/notifications/teams/mock`, {
        method: "POST",
        body: JSON.stringify({
          card,
          channel: "teams",
          delivery_mode: "mock",
          correlation_id: correlationId,
        }),
      });
      const log = normalizeTeamsMockLog(payload, notice);
      upsertTeamsLog(log);
      state.teamsLogMeta[noticeKey] = { status: "server", error: "", sending: true };
      showToast("서버 mock 기록 완료", "PAI_LOOP 서버 로그에 저장했습니다. Teams 외부 전송은 발생하지 않았습니다.", "success");
    } catch (error) {
      const reason = humanizeError(error);
      upsertTeamsLog({
        id: `LOCAL-${Date.now()}`,
        noticeKey,
        title: notice.title,
        timestamp: new Date().toISOString(),
        status: "LOCAL_FALLBACK",
        correlationId,
        origin: "LOCAL_FALLBACK",
        errorReason: reason,
      });
      state.teamsLogMeta[noticeKey] = { status: "fallback", error: reason, sending: true };
      showToast("브라우저 fallback으로 기록", `${reason} · 서버에는 저장되지 않았고 Teams 외부 전송도 없습니다.`, "warning");
    } finally {
      state.teamsLogMeta[noticeKey] = {
        ...(state.teamsLogMeta[noticeKey] || {}),
        sending: false,
      };
      if (state.selectedNotice?.noticeKey === noticeKey) {
        els.teamsMockSendButton.innerHTML = previousButtonHtml;
        els.teamsMockSendButton.disabled = !state.writeControlsEnabled;
        renderTeamsMockLogs(noticeKey);
      }
    }
  }

  async function refreshTeamsMockLogs() {
    const noticeKey = state.selectedNotice?.noticeKey;
    if (!noticeKey) return;
    await loadTeamsMockLogs(noticeKey, { announce: true });
  }

  async function loadTeamsMockLogs(noticeKey, { announce = false } = {}) {
    if (!noticeKey) return;
    if (!state.writeControlsEnabled) {
      state.teamsLogMeta[noticeKey] = {
        status: "readonly",
        error: "공개 읽기 전용 화면에서는 내부 mock 로그를 조회하지 않습니다",
        sending: false,
      };
      if (state.selectedNotice?.noticeKey === noticeKey) renderTeamsMockLogs(noticeKey);
      return;
    }
    if (state.source !== "api") {
      state.teamsLogMeta[noticeKey] = {
        status: "fallback",
        error: "데모 모드에서는 서버 로그를 불러오지 않습니다",
        sending: false,
      };
      if (state.selectedNotice?.noticeKey === noticeKey) renderTeamsMockLogs(noticeKey);
      if (announce) showToast("데모 모드", "브라우저 fallback 기록만 표시합니다.", "warning");
      return;
    }

    state.teamsLogMeta[noticeKey] = {
      ...(state.teamsLogMeta[noticeKey] || {}),
      status: "loading",
      error: "",
    };
    if (state.selectedNotice?.noticeKey === noticeKey) renderTeamsMockLogs(noticeKey);

    try {
      const payload = await apiRequest(`/notifications/mock?notice_key=${encodeURIComponent(noticeKey)}&limit=20`);
      const notice = state.notices.find((item) => item.noticeKey === noticeKey) || state.selectedNotice;
      const serverLogs = extractList(payload).map((item) => normalizeTeamsMockLog(item, notice));
      const retainedLogs = state.teamsLogs.filter((item) => item.noticeKey !== noticeKey || item.origin === "LOCAL_FALLBACK");
      state.teamsLogs = [...serverLogs, ...retainedLogs].slice(0, 100);
      state.teamsLogMeta[noticeKey] = {
        status: "server",
        error: "",
        sending: Boolean(state.teamsLogMeta[noticeKey]?.sending),
      };
      if (announce) showToast("서버 mock 로그를 갱신했습니다", `${serverLogs.length}건을 불러왔습니다.`, "success");
    } catch (error) {
      const reason = humanizeError(error);
      state.teamsLogMeta[noticeKey] = {
        status: "fallback",
        error: reason,
        sending: Boolean(state.teamsLogMeta[noticeKey]?.sending),
      };
      if (announce) showToast("서버 로그를 불러오지 못했습니다", `${reason} · 브라우저 fallback만 표시합니다.`, "warning");
    } finally {
      if (state.selectedNotice?.noticeKey === noticeKey) renderTeamsMockLogs(noticeKey);
    }
  }

  function normalizeTeamsMockLog(raw, notice) {
    const source = unwrapObject(raw);
    return {
      id: stringValue(firstValue(source.id, source.correlation_id), `SERVER-${Date.now()}`),
      noticeKey: stringValue(firstValue(source.notice_key, source.noticeKey), notice?.noticeKey || ""),
      title: notice?.title || "입찰 공고",
      timestamp: firstValue(source.created_at, source.createdAt, new Date().toISOString()),
      status: stringValue(source.status, "MOCK_RECORDED"),
      correlationId: stringValue(firstValue(source.correlation_id, source.correlationId), ""),
      origin: "SERVER",
      errorReason: "",
    };
  }

  function upsertTeamsLog(log) {
    const duplicateIndex = state.teamsLogs.findIndex((item) =>
      item.id === log.id || (log.correlationId && item.correlationId === log.correlationId));
    if (duplicateIndex >= 0) state.teamsLogs.splice(duplicateIndex, 1);
    state.teamsLogs.unshift(log);
    state.teamsLogs = state.teamsLogs.slice(0, 100);
  }

  function createTeamsCorrelationId(noticeKey) {
    const safeKey = String(noticeKey || "notice").replace(/[^A-Za-z0-9._:-]/g, "-").slice(0, 44);
    const random = typeof window.crypto?.randomUUID === "function"
      ? window.crypto.randomUUID().replaceAll("-", "").slice(0, 10)
      : Math.random().toString(36).slice(2, 12);
    return `pai-ui-${safeKey}-${Date.now()}-${random}`.slice(0, 120);
  }

  function renderTeamsMockLogs(noticeKey) {
    const logs = state.teamsLogs
      .filter((item) => item.noticeKey === noticeKey)
      .sort((a, b) => nullableDateSort(b.timestamp, a.timestamp));
    const meta = state.teamsLogMeta[noticeKey] || { status: "idle", error: "" };
    const serverLogCount = logs.filter((item) => item.origin === "SERVER").length;
    const fallbackLogCount = logs.filter((item) => item.origin === "LOCAL_FALLBACK").length;
    const storageLabels = {
      idle: "서버 mock 기록 준비 중",
      loading: "PAI_LOOP 서버 mock 기록 불러오는 중",
      server: `서버 mock ${serverLogCount}건${fallbackLogCount ? ` · 브라우저 fallback ${fallbackLogCount}건` : ""} · Teams 외부 전송 없음`,
      readonly: "공개 읽기 전용 · 내부 mock 로그 비공개",
      fallback: serverLogCount
        ? `서버 연결 실패 · 이전 서버 기록 ${serverLogCount}건 · 브라우저 fallback ${fallbackLogCount}건`
        : `서버 미기록 · 브라우저 fallback ${fallbackLogCount}건만 표시`,
    };
    els.teamsLogStorageLabel.textContent = storageLabels[meta.status] || storageLabels.idle;
    els.teamsLogStorageLabel.title = meta.error || "";

    if (meta.status === "loading" && !logs.length) {
      els.teamsMockLogList.innerHTML = '<li class="teams-log-empty"><strong><span class="teams-log-status is-loading">불러오는 중</span></strong><span>PAI_LOOP 서버의 mock 기록을 확인하고 있습니다.</span></li>';
      return;
    }

    els.teamsMockLogList.innerHTML = logs.length
      ? logs.map((item) => {
        const fallback = item.origin === "LOCAL_FALLBACK";
        const status = fallback ? "LOCAL_FALLBACK" : item.status || "MOCK_RECORDED";
        const boundary = fallback
          ? `브라우저 fallback · 서버 미기록${item.errorReason ? ` · ${item.errorReason}` : ""}`
          : "PAI_LOOP 서버 mock 기록 · Teams 외부 전송 없음";
        return `
        <li class="teams-log-item">
          <div class="teams-log-item__head"><span class="teams-log-status ${fallback ? "is-fallback" : ""}">${escapeHtml(status)}</span><time datetime="${escapeAttribute(item.timestamp)}">${escapeHtml(formatShortDateTime(item.timestamp))}</time></div>
          <strong title="${escapeAttribute(item.title)}">${escapeHtml(item.title)}</strong>
          <p>${escapeHtml(boundary)}${item.correlationId ? ` · ID ${escapeHtml(truncateText(item.correlationId, 34))}` : ""}</p>
        </li>`;
      }).join("")
      : `<li class="teams-log-empty"><strong>${meta.status === "readonly" ? "공개 화면에서는 내부 mock 로그를 표시하지 않습니다" : "아직 mock 기록이 없습니다"}</strong><span>${meta.status === "fallback" ? "서버 연결 실패 시 기록한 브라우저 fallback도 없습니다." : meta.status === "readonly" ? "Teams 승인이 끝난 뒤 사내 로그인 환경에서 사용할 수 있습니다." : "버튼을 누르면 Teams 전송 없이 PAI_LOOP 서버 mock 로그에만 기록됩니다."}</span></li>`;
  }

  function focusDecisionDockFromPreview() {
    const notice = state.selectedNotice;
    if (!notice) return;
    if (!state.writeControlsEnabled) {
      showToast("읽기 전용 화면입니다", "담당자 판단은 사내 로그인 환경에서만 저장할 수 있습니다.", "warning");
      return;
    }
    if (notice.analysisState !== "EVALUATED") {
      showToast("담당자 판단은 분석 후 가능합니다", "현재 공고는 수집 완료·분석 대기 상태입니다.", "warning");
      return;
    }
    selectTab("overview");
    els.decisionForm.scrollIntoView({ behavior: "smooth", block: "end" });
    els.decisionInputs[0]?.focus();
  }

  function renderExistingDecision(notice) {
    const analyzed = notice.analysisState === "EVALUATED";
    els.decisionInputs.forEach((input) => {
      input.checked = notice.decision === input.value || (notice.decision === "CONDITIONAL_GO" && input.value === "HOLD");
      input.disabled = !analyzed || !state.writeControlsEnabled;
    });
    els.decisionComment.value = notice.decisionComment;
    els.commentCount.textContent = String(notice.decisionComment.length);
    els.commentField.hidden = !notice.decisionComment;
    els.toggleCommentButton.setAttribute("aria-expanded", String(Boolean(notice.decisionComment)));
    if (!analyzed) {
      els.decisionExisting.textContent = "분석 완료 후 담당자 판단을 기록할 수 있습니다.";
    } else if (notice.decision) {
      const meta = [DECISION_LABELS[notice.decision] || notice.decision, notice.decidedBy, notice.decidedAt ? formatShortDateTime(notice.decidedAt) : ""].filter(Boolean);
      els.decisionExisting.textContent = meta.join(" · ");
    } else {
      els.decisionExisting.textContent = "아직 결정되지 않았습니다.";
    }
    els.toggleCommentButton.disabled = !analyzed || !state.writeControlsEnabled;
    els.decisionComment.disabled = !state.writeControlsEnabled;
    updateDecisionButton();
  }

  function selectTab(tabName) {
    els.tabButtons.forEach((button) => {
      const selected = button.dataset.tab === tabName;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    els.tabPanels.forEach((panel) => {
      panel.hidden = panel.dataset.panel !== tabName;
    });
    if (tabName === "history" && state.selectedNotice?.noticeKey && state.source === "api") {
      void loadStoredAwardHistory(state.selectedNotice.noticeKey);
    }
    if (tabName === "quant" && state.selectedNotice?.noticeKey && state.source === "api") {
      void loadQuantitativeEstimate(state.selectedNotice.noticeKey);
    }
  }

  function handleTabKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = els.tabButtons.indexOf(event.currentTarget);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % els.tabButtons.length;
    if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + els.tabButtons.length) % els.tabButtons.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = els.tabButtons.length - 1;
    const next = els.tabButtons[nextIndex];
    selectTab(next.dataset.tab);
    next.focus();
  }

  function closeDetail({ updateRoute = true } = {}) {
    if (!els.detailDrawer.classList.contains("is-open")) return;
    if (els.sourceLinkDialog.open) {
      state.sourceDialogTrigger = null;
      els.sourceLinkDialog.close();
    }
    els.detailDrawer.classList.remove("is-open");
    els.detailDrawer.setAttribute("aria-hidden", "true");
    els.drawerScrim.hidden = true;
    document.body.classList.remove("is-locked");
    if (updateRoute) clearNoticeRoute();
    const trigger = state.selectedTrigger;
    state.selectedNotice = null;
    if (trigger && typeof trigger.focus === "function" && document.contains(trigger)) trigger.focus();
  }

  function trapDrawerFocus(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDetail();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...els.detailDrawer.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href]')]
      .filter((node) => !node.closest("[hidden]") && node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function toggleCommentField() {
    const willOpen = els.commentField.hidden;
    els.commentField.hidden = !willOpen;
    els.toggleCommentButton.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) els.decisionComment.focus();
  }

  function updateDecisionButton() {
    const selected = els.decisionInputs.some((input) => input.checked);
    const analyzed = state.selectedNotice?.analysisState === "EVALUATED";
    els.saveDecisionButton.disabled = !state.writeControlsEnabled || !selected || !state.selectedNotice || !analyzed;
    els.saveDecisionButton.textContent = !state.writeControlsEnabled
      ? "사내 로그인 후 저장 가능"
      : analyzed ? "판단 저장" : "분석 완료 후 저장 가능";
  }

  async function saveDecision(event) {
    event.preventDefault();
    if (!state.writeControlsEnabled) {
      showToast("읽기 전용 화면입니다", "담당자 판단은 사내 로그인 환경에서만 저장할 수 있습니다.", "warning");
      return;
    }
    const notice = state.selectedNotice;
    const decision = els.decisionInputs.find((input) => input.checked)?.value;
    if (!notice || !decision) return;
    if (notice.analysisState !== "EVALUATED") {
      showToast("아직 분석 전입니다", "결정론적 자격 평가가 완료된 뒤 담당자 판단을 저장할 수 있습니다.", "warning");
      return;
    }
    const comment = els.decisionComment.value.trim();
    const rationale = comment || `${DECISION_LABELS[decision]} 판단을 기록했습니다.`;
    const payload = {
      choice: decision,
      actor_label: DECIDER_NAME,
      rationale,
      conditions: decision === "HOLD" && comment ? [comment] : null,
    };
    const originalText = els.saveDecisionButton.textContent;
    els.saveDecisionButton.disabled = true;
    els.saveDecisionButton.textContent = "저장 중…";

    try {
      let result = null;
      if (state.source === "api") {
        result = await apiRequest(`/notices/${encodeURIComponent(notice.noticeKey)}/decisions`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
      }
      const response = unwrapObject(result);
      const updated = {
        ...notice,
        decision: normalizeDecision(firstValue(response.choice, response.decision, response.manager_decision, decision)) || decision,
        decisionComment: stringValue(firstValue(response.rationale, response.comment, response.decision_comment, rationale), rationale),
        decidedBy: stringValue(firstValue(response.actor_label, response.actorLabel, response.decided_by, response.decider, DECIDER_NAME), DECIDER_NAME),
        decidedAt: firstValue(response.created_at, response.createdAt, response.decided_at, response.updated_at, new Date().toISOString()),
      };
      const index = state.notices.findIndex((item) => item.noticeKey === notice.noticeKey);
      if (index >= 0) state.notices[index] = updated;
      state.selectedNotice = updated;
      state.dashboard = deriveDashboard(state.notices);
      renderExistingDecision(updated);
      renderPipelineIntoExisting(updated);
      renderAll();
      showToast(
        state.source === "demo" ? "데모 판단 반영" : "판단을 저장했습니다",
        state.source === "demo" ? "현재 브라우저에서만 반영되며 서버에는 저장되지 않습니다." : `${DECISION_LABELS[updated.decision]} 결정과 의견이 기록되었습니다.`,
        state.source === "demo" ? "warning" : "success",
      );
    } catch (error) {
      showToast("판단 저장 실패", humanizeError(error), "error");
    } finally {
      els.saveDecisionButton.textContent = originalText;
      updateDecisionButton();
    }
  }

  function renderPipelineIntoExisting(notice) {
    els.analysisPipeline.innerHTML = renderPipeline(notice);
  }

  async function runReplay() {
    if (!state.writeControlsEnabled) {
      showToast("읽기 전용 화면입니다", "수집·분석 작업은 서버 인증이 있는 운영 환경에서 실행합니다.", "warning");
      return;
    }
    if (state.source !== "api") {
      showToast("실행할 수 없습니다", "데모 모드에서는 수집·분석 워크플로를 실행하지 않습니다. 실데이터 연결을 확인해 주세요.", "warning");
      return;
    }
    const original = els.replayButton.innerHTML;
    els.replayButton.disabled = true;
    els.replayButton.textContent = "분석 요청 중…";
    try {
      const payload = await apiRequest("/ingestion/replay", { method: "POST", body: JSON.stringify({}) });
      const job = unwrapObject(payload);
      const jobId = stringValue(firstValue(job.job_id, job.id, job.execution_id), "");
      showToast("샘플 분석을 시작했습니다", jobId ? `작업 ID ${jobId} · 완료 후 공고 목록을 새로고침하세요.` : "완료 후 공고 목록을 새로고침하세요.", "success");
    } catch (error) {
      showToast("분석 시작 실패", humanizeError(error), "error");
    } finally {
      els.replayButton.disabled = !state.writeControlsEnabled;
      els.replayButton.innerHTML = original;
    }
  }

  function toggleMobileMenu() {
    const open = !els.sidebar.classList.contains("is-open");
    els.sidebar.classList.toggle("is-open", open);
    els.sidebarScrim.hidden = !open;
    els.mobileMenuButton.setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("is-locked", open);
  }

  function closeMobileMenu() {
    els.sidebar.classList.remove("is-open");
    els.sidebarScrim.hidden = true;
    els.mobileMenuButton.setAttribute("aria-expanded", "false");
    if (!els.detailDrawer.classList.contains("is-open")) document.body.classList.remove("is-locked");
  }

  function handleGlobalKeydown(event) {
    if (els.sourceLinkDialog.open) return;
    if (event.key === "/" && !isEditableTarget(event.target)) {
      event.preventDefault();
      if (state.currentView === "performance") els.performanceSearchInput.focus();
      else els.searchInput.focus();
    }
  }

  function updateNoticeRoute(noticeKey) {
    const url = new URL(window.location.href);
    url.searchParams.set("notice", noticeKey);
    history.pushState({ noticeKey }, "", url);
  }

  function clearNoticeRoute() {
    const url = new URL(window.location.href);
    url.searchParams.delete("notice");
    history.pushState({}, "", url);
  }

  function openNoticeFromRoute() {
    const key = new URLSearchParams(window.location.search).get("notice");
    if (key && state.notices.some((notice) => notice.noticeKey === key)) {
      openDetail(key, null, { updateRoute: false });
    }
  }

  function handleRouteChange() {
    const key = new URLSearchParams(window.location.search).get("notice");
    if (key && state.notices.some((notice) => notice.noticeKey === key)) {
      openDetail(key, null, { updateRoute: false });
    } else {
      closeDetail({ updateRoute: false });
    }
  }

  async function copyCurrentNoticeLink() {
    const notice = state.selectedNotice;
    if (!notice) return;
    const url = notice.sourceUrl || window.location.href;
    try {
      await navigator.clipboard.writeText(url);
      showToast("링크를 복사했습니다", notice.sourceUrl ? "조달청 원문 링크가 클립보드에 복사되었습니다." : "현재 상세 화면 링크가 복사되었습니다.", "success");
    } catch (_error) {
      showToast("링크 복사 실패", "브라우저의 클립보드 권한을 확인해 주세요.", "error");
    }
  }

  function openCurrentNoticeSourceDialog() {
    const notice = state.selectedNotice;
    if (!notice) return;
    const sourceUrl = safeHttpUrl(notice.sourceUrl);
    const deadline = deadlineInfo(notice.deadline);
    const sourceHost = sourceUrl ? new URL(sourceUrl).hostname : "";
    state.sourceDialogTrigger = document.activeElement;
    els.sourceLinkDialogTitle.textContent = "공고 원문 확인";
    els.sourceLinkDialogNotice.textContent = notice.title;
    els.sourceLinkDialogMeta.textContent = [notice.agency, `공고번호 ${notice.noticeNumber}`, deadline.date]
      .filter(Boolean)
      .join(" · ");
    els.sourceLinkDialogMessage.textContent = sourceUrl
      ? `${sourceHost}의 공식 공고를 새 탭에서 엽니다. PAI LOOP 분석 화면은 그대로 유지됩니다.`
      : "공개 가능한 조달청 원문 링크가 아직 연결되지 않았습니다. 공고번호로 나라장터에서 다시 확인해 주세요.";
    if (sourceUrl) {
      els.sourceLinkOpenAnchor.href = sourceUrl;
      els.sourceLinkOpenAnchor.removeAttribute("aria-disabled");
      els.sourceLinkOpenAnchor.tabIndex = 0;
      els.sourceLinkOpenAnchor.textContent = "조달청 원문 새 탭에서 열기";
    } else {
      els.sourceLinkOpenAnchor.removeAttribute("href");
      els.sourceLinkOpenAnchor.setAttribute("aria-disabled", "true");
      els.sourceLinkOpenAnchor.tabIndex = -1;
      els.sourceLinkOpenAnchor.textContent = "공개 원문 링크 없음";
    }
    if (typeof els.sourceLinkDialog.showModal === "function") els.sourceLinkDialog.showModal();
    else els.sourceLinkDialog.setAttribute("open", "");
    requestAnimationFrame(() => els.closeSourceLinkDialogButton.focus());
  }

  function closeSourceLinkDialog() {
    if (!els.sourceLinkDialog.open) return;
    if (typeof els.sourceLinkDialog.close === "function") els.sourceLinkDialog.close();
    else {
      els.sourceLinkDialog.removeAttribute("open");
      restoreSourceDialogFocus();
    }
  }

  function restoreSourceDialogFocus() {
    const trigger = state.sourceDialogTrigger;
    state.sourceDialogTrigger = null;
    if (trigger && typeof trigger.focus === "function" && document.contains(trigger)) trigger.focus();
  }

  function showToast(title, message, type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast ${type === "error" ? "is-error" : type === "warning" ? "is-warning" : ""}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    const icon = type === "error" ? "!" : type === "warning" ? "i" : "✓";
    toast.innerHTML = `
      <span class="toast__icon" aria-hidden="true">${icon}</span>
      <span class="toast__copy"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></span>
      <button class="toast__close" type="button" aria-label="알림 닫기">×</button>`;
    const remove = () => {
      toast.classList.add("is-leaving");
      window.setTimeout(() => toast.remove(), 190);
    };
    toast.querySelector("button").addEventListener("click", remove);
    els.toastRegion.appendChild(toast);
    window.setTimeout(remove, type === "error" ? 7000 : 5000);
  }

  function statusPill(status) {
    const value = STATUS_LABELS[status] ? status : "UNKNOWN";
    return `<span class="status-pill status-pill--${value.toLowerCase()}">${STATUS_LABELS[value]}</span>`;
  }

  function analysisStatusPill(notice) {
    if (isDocumentQualityReview(notice)) return '<span class="status-pill status-pill--pending" title="자격 REVIEW가 아니라 원문 근거 검증 보완 상태입니다">근거 보완</span>';
    if (notice.analysisState === "EVALUATED") return statusPill(notice.eligibilityStatus);
    if (notice.analysisState === "FAILED") return '<span class="status-pill status-pill--fail">분석 오류</span>';
    return '<span class="status-pill status-pill--pending">미분석</span>';
  }

  function analysisRecommendationPill(notice) {
    if (isDocumentQualityReview(notice)) return '<span class="recommendation-pill recommendation-pill--pending">판단 보류</span>';
    if (notice.analysisState === "EVALUATED") return recommendationPill(notice.recommendation);
    return '<span class="recommendation-pill recommendation-pill--pending">분석 전</span>';
  }

  function sourceKindBadge(notice) {
    const className = notice.sourceKind === "SYNTHETIC" ? "source-kind-badge--synthetic" : notice.sourceKind === "MANUAL" ? "source-kind-badge--manual" : "source-kind-badge--real";
    return `<span class="source-kind-badge ${className}">${escapeHtml(sourceKindLabel(notice))}</span>`;
  }

  function sourceKindLabel(notice, detailed = false) {
    if (notice.sourceKind === "SYNTHETIC") return detailed ? "합성 회귀 데이터" : "합성";
    if (notice.sourceKind === "MANUAL") return detailed ? "수동 등록 공고" : "수동";
    return detailed ? "조달청 실공고 · API" : "실제";
  }

  function recommendationPill(recommendation) {
    const value = RECOMMENDATION_LABELS[recommendation] ? recommendation : "UNKNOWN";
    const className = value === "GO" ? "go" : value === "CONDITIONAL_GO" || value === "HOLD" ? "conditional" : value === "NO_GO" ? "no" : "unknown";
    return `<span class="recommendation-pill recommendation-pill--${className}">${RECOMMENDATION_LABELS[value]}</span>`;
  }

  function emptyPanel(title, copy) {
    return `<div class="empty-panel"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(copy)}</p></div>`;
  }

  function deadlineInfo(value) {
    const date = validDate(value);
    if (!date) return { date: "마감 미확인", time: "", relative: "일정 확인 필요", urgent: false };
    const days = daysUntil(value);
    let relative = "마감됨";
    if (days === 0) relative = "오늘 마감";
    else if (days === 1) relative = "내일 마감";
    else if (days > 1) relative = `D-${days}`;
    else if (days < 0) relative = `D+${Math.abs(days)}`;
    return {
      date: new Intl.DateTimeFormat("ko-KR", { month: "2-digit", day: "2-digit", weekday: "short" }).format(date),
      time: new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false }).format(date),
      relative,
      urgent: days !== null && days >= 0 && days <= URGENT_DEADLINE_DAYS,
    };
  }

  function daysUntil(value) {
    const date = validDate(value);
    if (!date) return null;
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const end = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    return Math.round((end - start) / 86400000);
  }

  function formatBudget(value) {
    if (value === null || value === undefined || value === "") return "예산 미확인";
    if (typeof value === "string") {
      const numeric = Number(value.replace(/[^0-9.-]/g, ""));
      if (!Number.isFinite(numeric) || /억|만|원/.test(value)) return value;
      value = numeric;
    }
    const number = Number(value);
    if (!Number.isFinite(number)) return "예산 미확인";
    if (number >= 100000000) {
      const units = number / 100000000;
      return `${formatNumber(units, units < 10 && units % 1 ? 1 : 0)}억원`;
    }
    if (number >= 10000) return `${formatNumber(number / 10000, 0)}만원`;
    return `${formatNumber(number)}원`;
  }

  function formatNumber(value, maximumFractionDigits = 0) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("ko-KR", { maximumFractionDigits }).format(number);
  }

  function displayNumber(value) {
    return value === null || value === undefined ? "—" : formatNumber(value);
  }

  function formatScore(value) {
    return value === null || value === undefined ? "—" : String(Math.round(value));
  }

  function riskDisplayValue(notice) {
    return notice.riskScore === null ? "근거 부족" : formatScore(notice.riskScore);
  }

  function analysisStatusLabel(notice) {
    return isDocumentQualityReview(notice) ? "근거 보완" : STATUS_LABELS[notice.eligibilityStatus];
  }

  function analysisRecommendationLabel(notice) {
    if (isDocumentQualityReview(notice)) return "판단 보류";
    if (notice.analysisState === "EVALUATED") return RECOMMENDATION_LABELS[notice.recommendation];
    return "분석 전";
  }

  function formatRelativeDateTime(value) {
    const date = validDate(value);
    if (!date) return "—";
    const diffMinutes = Math.round((Date.now() - date.getTime()) / 60000);
    if (Math.abs(diffMinutes) < 1) return "방금 전";
    if (diffMinutes >= 1 && diffMinutes < 60) return `${diffMinutes}분 전`;
    if (diffMinutes >= 60 && diffMinutes < 1440) return `${Math.floor(diffMinutes / 60)}시간 전`;
    return formatShortDateTime(value);
  }

  function formatShortDateTime(value) {
    const date = validDate(value);
    if (!date) return "—";
    return new Intl.DateTimeFormat("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function formatCalendarDate(value) {
    const date = validDate(value);
    if (!date) return "미확인";
    return new Intl.DateTimeFormat("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
  }

  function truncateText(value, maxLength) {
    const text = stringValue(value);
    if (text.length <= maxLength) return text;
    return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…`;
  }

  function scoreClass(value) {
    if (value === null) return "is-unknown";
    if (value < 60) return "is-low";
    if (value < 80) return "is-medium";
    return "is-high";
  }

  function riskClass(value) {
    if (value === null) return "is-unknown";
    if (value >= 60) return "is-high";
    if (value >= 30) return "is-medium";
    return "is-low";
  }

  function normalizeEligibility(value) {
    const normalized = String(value ?? "").trim().toUpperCase().replace(/[\s-]+/g, "_");
    if (["PASS", "ELIGIBLE", "OK", "GREEN"].includes(normalized)) return "PASS";
    if (["REVIEW", "CONDITIONAL", "CHECK", "YELLOW", "PENDING"].includes(normalized)) return "REVIEW";
    if (["FAIL", "FAILED", "INELIGIBLE", "DEFAULT_FAIL", "RED"].includes(normalized)) return "FAIL";
    return "UNKNOWN";
  }

  function normalizeReadiness(value) {
    const normalized = String(value ?? "").trim().toUpperCase();
    return ["GREEN", "YELLOW", "RED", "GRAY"].includes(normalized) ? normalized : "UNKNOWN";
  }

  function normalizeSourceKind(value, noticeKey, category) {
    const normalized = String(value ?? "").trim().toUpperCase().replace(/[\s-]+/g, "_");
    if (["SYNTHETIC", "DEMO", "FIXTURE", "TEST"].includes(normalized)) return "SYNTHETIC";
    if (["MANUAL", "UPLOAD", "USER"].includes(normalized)) return "MANUAL";
    if (["PPS", "G2B", "API", "REAL", "PUBLIC_DATA"].includes(normalized)) return "PPS";
    const identity = `${noticeKey} ${category}`.toUpperCase();
    if (identity.includes("SYN-") || identity.includes("SYNTHETIC") || String(noticeKey).toLowerCase().startsWith("demo-")) return "SYNTHETIC";
    return "PPS";
  }

  function normalizeAnalysisState(value, hasEvaluation, noticeStatus) {
    if (hasEvaluation) return "EVALUATED";
    const normalized = String(value ?? "").trim().toUpperCase().replace(/[\s-]+/g, "_");
    if (["EVALUATED", "ANALYZED", "COMPLETE"].includes(normalized)) return "EVALUATED";
    if (["FAILED", "ERROR", "ANALYSIS_FAILED"].includes(normalized) || ["FAILED", "ERROR"].includes(String(noticeStatus ?? "").toUpperCase())) return "FAILED";
    if (normalized === "VERSIONED") return "VERSIONED";
    return "COLLECTED";
  }

  function normalizeAnalysisReason(source, analysisState, latestVersion) {
    const reasonObject = firstObject(source.analysis_reason, source.analysisReason);
    let code = stringValue(firstValue(
      source.analysis_reason_code,
      source.analysisReasonCode,
      reasonObject.code,
      reasonObject.reason_code,
      reasonObject.reasonCode,
      source.pending_reason_code,
      source.pendingReasonCode,
    )).toUpperCase().replace(/[\s-]+/g, "_");
    const explicitDetail = firstValue(
      reasonObject.message,
      reasonObject.description,
      typeof source.analysis_reason === "string" ? source.analysis_reason : null,
      typeof source.analysisReason === "string" ? source.analysisReason : null,
      source.analysis_reason_description,
      source.analysisReasonDescription,
      source.analysis_reason_detail,
      source.analysisReasonDetail,
      source.pending_reason,
      source.pendingReason,
    );
    const detail = typeof explicitDetail === "string" || typeof explicitDetail === "number" ? stringValue(explicitDetail) : "";
    if (!code && detail && /^[A-Z][A-Z0-9_-]+$/.test(detail)) code = detail.replace(/[\s-]+/g, "_");

    const mapped = ANALYSIS_REASON_LABELS[code];
    if (mapped) return { code, message: mapped };
    if (detail && detail.toUpperCase() !== code) return { code: code || "PUBLIC_DESCRIPTION", message: detail };
    if (analysisState === "EVALUATED") return { code: code || "EVALUATED", message: "분석과 판정이 완료되었습니다." };

    const noticeStatus = stringValue(firstValue(source.status, source.notice_status)).toUpperCase();
    if (["CLOSED", "CANCELLED", "CANCELED", "EXPIRED"].includes(noticeStatus)) {
      return { code: noticeStatus, message: "공고가 마감·취소 또는 종료 상태여서 자동 분석 대상에서 제외되었습니다." };
    }
    const extractionStatus = stringValue(latestVersion?.extractionStatus).toUpperCase();
    if (["REVIEW", "FAILED", "ERROR", "INCOMPLETE"].includes(extractionStatus) || analysisState === "FAILED") {
      return { code: extractionStatus || "ANALYSIS_FAILED", message: "첨부문서 추출 또는 구조화 분석이 완료되지 않아 재처리와 담당자 확인이 필요합니다." };
    }
    if (analysisState === "VERSIONED") {
      return { code: "READY", message: ANALYSIS_REASON_LABELS.READY };
    }
    return { code: "NOT_SELECTED", message: ANALYSIS_REASON_LABELS.NOT_SELECTED };
  }

  function normalizeRecommendation(value) {
    const normalized = String(value ?? "").trim().toUpperCase().replace(/[\s-]+/g, "_");
    if (["GO", "RECOMMEND", "YES"].includes(normalized)) return "GO";
    if (["CONDITIONAL_GO", "CONDITIONAL", "HOLD", "REVIEW"].includes(normalized)) return "CONDITIONAL_GO";
    if (["NO_GO", "NOGO", "NO", "STOP"].includes(normalized)) return "NO_GO";
    return "UNKNOWN";
  }

  function normalizeDecision(value) {
    const normalized = String(value ?? "").trim().toUpperCase().replace(/[\s-]+/g, "_");
    if (normalized === "GO") return "GO";
    if (["HOLD", "CONDITIONAL_GO", "REVIEW"].includes(normalized)) return normalized === "CONDITIONAL_GO" ? "CONDITIONAL_GO" : "HOLD";
    if (["NO_GO", "NOGO", "NO"].includes(normalized)) return "NO_GO";
    return "";
  }

  function normalizeEvidenceStatus(value) {
    if (value === true) return "VERIFIED";
    if (value === false) return "MISSING";
    const normalized = String(value ?? "").trim().toUpperCase().replace(/[\s-]+/g, "_");
    if (["VERIFIED", "CONFIRMED", "PASS", "COMPLETE"].includes(normalized)) return "VERIFIED";
    if (["MISSING", "FAIL", "NOT_FOUND", "INCOMPLETE"].includes(normalized)) return "MISSING";
    return "PROVISIONAL";
  }

  function normalizeConfidence(value) {
    const number = numberOrNull(value);
    if (number === null) return null;
    return clamp(number <= 1 ? number * 100 : number, 0, 100);
  }

  function firstValue(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  function firstObject(...values) {
    return values.find((value) => value && typeof value === "object" && !Array.isArray(value)) || {};
  }

  function stringValue(value, fallback = "") {
    if (value === undefined || value === null) return fallback;
    const text = String(value).trim();
    return text || fallback;
  }

  function arrayValue(value) {
    return Array.isArray(value) ? value : [];
  }

  function numberOrNull(value) {
    if (value === undefined || value === null || value === "") return null;
    const number = typeof value === "string" ? Number(value.replace(/[,\s%]/g, "")) : Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function booleanValue(value) {
    if (typeof value === "boolean") return value;
    if (value === 1 || value === "1" || String(value).toLowerCase() === "true") return true;
    if (value === 0 || value === "0" || String(value).toLowerCase() === "false") return false;
    return null;
  }

  function validDate(value) {
    if (!value) return null;
    const date = value instanceof Date ? value : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function isRecent(value, hours) {
    const date = validDate(value);
    if (!date) return false;
    const diff = Date.now() - date.getTime();
    return diff >= 0 && diff <= hours * 3600000;
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value || ""));
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch (_error) {
      return "";
    }
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, Number(value) || 0));
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replaceAll("`", "&#096;");
  }

  function humanizeError(error) {
    const message = error?.message || String(error || "알 수 없는 오류");
    if (/failed to fetch|networkerror|load failed/i.test(message)) return "네트워크 또는 API 서버를 확인해 주세요";
    return message.length > 120 ? `${message.slice(0, 117)}…` : message;
  }

  function isEditableTarget(target) {
    return target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable);
  }

  function delay(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function futureIso(days, hour = 14) {
    const date = new Date();
    date.setDate(date.getDate() + days);
    date.setHours(hour, 0, 0, 0);
    return date.toISOString();
  }

  function createDemoData() {
    const now = new Date().toISOString();
    const commonEvidence = [
      {
        id: "ev-eligibility",
        file: "입찰공고서.pdf",
        page: "4페이지 · 참가자격 3항",
        quote: "입찰참가자는 국가종합전자조달시스템 입찰참가자격등록규정에 따라 학술·연구용역 업종으로 등록한 업체이어야 한다.",
        status: "VERIFIED",
        confidence: 0.97,
      },
      {
        id: "ev-performance",
        file: "제안요청서.hwpx",
        page: "27페이지 · 정량평가표",
        quote: "최근 3년 이내 국가 또는 공공기관 대상 유사 용역 수행실적을 기준으로 차등 배점한다.",
        status: "VERIFIED",
        confidence: 0.93,
      },
      {
        id: "ev-certificate",
        file: "입찰공고서.pdf",
        page: "5페이지 · 참가자격 7항",
        quote: "직접생산확인증명서는 입찰서 제출 마감일 전일까지 발급된 것으로 유효기간 내에 있어야 한다.",
        status: "PROVISIONAL",
        confidence: 0.89,
      },
    ];

    const notices = [
      {
        notice_key: "demo-2026-001",
        status: "OPEN",
        notice_number: "20260816-001",
        title: "2026년 지역관광 경쟁력 강화 및 글로벌 마케팅 전략 수립 용역",
        agency: "한국관광공사",
        demand_agency: "관광콘텐츠전략팀",
        deadline: futureIso(2, 17),
        collected_at: now,
        budget: 485000000,
        eligibility_status: "REVIEW",
        readiness_score: 82,
        evidence_coverage: 78,
        risk_score: 36,
        recommendation: "CONDITIONAL_GO",
        category: "연구·컨설팅",
        contract_method: "제한경쟁 · 협상계약",
        region: "전국",
        summary: "지역관광 사업 분석과 글로벌 마케팅 전략을 결합한 컨설팅 용역입니다. KMA의 공공기관 전략 컨설팅 실적과 과업 유사성이 높습니다. 다만 직접생산확인증명서 요구 문구의 적용 대상과 공동수급 허용 범위를 발주처에 확인한 뒤 입찰 여부를 확정해야 합니다.",
        document_analyses: [
          {
            id: "doc-analysis-rfp",
            document_name: "제안요청서.hwpx",
            summary: "과업 범위와 정량평가 기준을 구조화했습니다. 직접생산확인증명서 적용 범위는 담당자 확인이 필요합니다.",
            requirement_count: 4,
            needs_review: true,
            status: "COMPLETE",
            confidence: 0.93,
            analyzed_at: now,
          },
        ],
        requirements: [
          { id: "r1", title: "학술·연구용역 업종 등록", description: "입찰 마감일 기준 유효한 업종 등록을 확인했습니다.", status: "PASS", evidence_id: "ev-eligibility" },
          { id: "r2", title: "최근 3년 유사 용역 실적", description: "회사 실적 DB에서 조건을 충족하는 후보 실적 7건을 확인했습니다.", status: "PASS", evidence_id: "ev-performance" },
          { id: "r3", title: "직접생산확인증명서", description: "현재 회사 마스터에 보유 증빙이 없습니다. 적용 품목과 대체 가능 여부 확인이 필요합니다.", status: "REVIEW", evidence_id: "ev-certificate" },
          { id: "r4", title: "공동수급 허용 범위", description: "공동이행 방식은 허용되나 분담 비율 제한을 확인해야 합니다.", status: "REVIEW" },
        ],
        evidence: commonEvidence,
        quantitative: [
          { label: "유사 용역 수행실적", max_score: 8, expected_score: "6~8", status: "PROVISIONAL" },
          { label: "경영상태", max_score: 5, expected_score: 5, status: "VERIFIED" },
          { label: "신인도", max_score: 2, expected_score: "1~2", status: "PROVISIONAL" },
          { label: "참여인력 경력", max_score: 10, expected_score: "7~9", status: "PROVISIONAL" },
        ],
        risk_axes: [
          { label: "자격 조건", score: 42 }, { label: "증빙 완전성", score: 38 }, { label: "경쟁 강도", score: 46 },
          { label: "제안 일정", score: 31 }, { label: "수행 운영", score: 27 }, { label: "데이터 품질", score: 22 },
        ],
        actions: [
          "직접생산확인증명서가 학술·연구용역사에 적용되는지 발주처에 문의하세요.",
          "공동수급 구성 시 최소 지분율과 실적 합산 기준을 확인하세요.",
          "정량평가용 참여인력 경력증명서의 최신본을 확보하세요.",
        ],
        award_history: [
          { year: 2025, title: "지역관광 글로벌 경쟁력 강화 연구", winner: "한국관광개발연구원", amount: 421000000, rate: 88.2, agency: "한국관광공사" },
          { year: 2024, title: "지역관광 통합마케팅 전략 수립", winner: "에이치컨설팅", amount: 368000000, rate: 87.6, agency: "한국관광공사" },
          { year: 2023, title: "방한관광 시장 다변화 컨설팅", winner: "글로벌리서치", amount: 312000000, rate: 89.1, agency: "문화체육관광부" },
        ],
      },
      {
        notice_key: "demo-2026-002",
        status: "OPEN",
        notice_number: "R26BK01093812",
        title: "공공기관 조직문화 진단 및 중장기 변화관리 체계 구축",
        agency: "한국산업인력공단",
        deadline: futureIso(6, 11),
        collected_at: futureIso(-1, 9),
        budget: 320000000,
        eligibility_status: "PASS",
        readiness_score: 91,
        evidence_coverage: 94,
        risk_score: 21,
        recommendation: "GO",
        category: "조직·인사 컨설팅",
        contract_method: "일반경쟁 · 협상계약",
        region: "전국",
        summary: "조직문화 진단, 임직원 조사, 변화관리 로드맵 수립이 핵심인 사업으로 회사의 유사 실적과 인력 구성이 모두 확인됩니다. 필수 참가자격과 정량평가 주요 증빙이 확보되어 우선 검토 가치가 높습니다.",
        requirements: [
          { title: "학술·연구용역 등록", description: "회사 마스터와 조달청 등록정보가 일치합니다.", status: "PASS", evidence_id: "e1" },
          { title: "조직진단 유사 실적", description: "기준금액 이상 실적 11건이 확인됩니다.", status: "PASS", evidence_id: "e2" },
          { title: "중소기업 확인서", description: "마감일 기준 유효기간을 충족합니다.", status: "PASS", evidence_id: "e3" },
        ],
        evidence: [
          { id: "e1", file: "입찰공고서.pdf", page: "3페이지", quote: "학술·연구용역 업종으로 경쟁입찰 참가자격을 등록한 자", status: "VERIFIED", confidence: 98 },
          { id: "e2", file: "제안요청서.pdf", page: "18페이지 · 실적평가", quote: "최근 5년 이내 조직진단 또는 조직문화 개선 컨설팅 수행실적", status: "VERIFIED", confidence: 96 },
          { id: "e3", file: "중소기업확인서.pdf", page: "문서 전체", quote: "유효기간 2026.04.01.~2027.03.31.", status: "VERIFIED", confidence: 100 },
        ],
        quantitative: [
          { label: "유사 용역 수행실적", max_score: 10, expected_score: 10, status: "VERIFIED" },
          { label: "경영상태", max_score: 5, expected_score: 5, status: "VERIFIED" },
          { label: "참여인력 구성", max_score: 10, expected_score: 9, status: "VERIFIED" },
        ],
        risk_axes: [
          { label: "자격 조건", score: 8 }, { label: "증빙 완전성", score: 12 }, { label: "경쟁 강도", score: 39 },
          { label: "제안 일정", score: 24 }, { label: "수행 운영", score: 18 }, { label: "데이터 품질", score: 7 },
        ],
        award_history: [
          { year: 2025, title: "조직문화 혁신체계 고도화", winner: "피플앤체인지", amount: 285000000, rate: 87.9 },
          { year: 2024, title: "조직진단 및 인사제도 개선", winner: "한국능률협회컨설팅", amount: 301000000, rate: 88.5 },
          { year: 2023, title: "일하는 방식 혁신 컨설팅", winner: "조직혁신연구소", amount: 247000000, rate: 86.8 },
        ],
      },
      {
        notice_key: "demo-2026-003",
        status: "OPEN",
        notice_number: "20260816-099",
        title: "AI 기반 지역산업 디지털 전환 교육 콘텐츠 개발 및 운영",
        agency: "부산테크노파크",
        deadline: futureIso(1, 16),
        collected_at: now,
        budget: 612000000,
        eligibility_status: "FAIL",
        readiness_score: 48,
        evidence_coverage: 86,
        risk_score: 72,
        recommendation: "NO_GO",
        category: "교육 운영",
        contract_method: "제한경쟁 · 협상계약",
        region: "부산광역시",
        summary: "AI 교육 콘텐츠 개발 및 운영 경험은 유사하나, 공고에서 지정한 직접생산확인증명서가 필수이고 공동수급 및 예외 적용이 허용되지 않습니다. 회사 현재 증빙으로는 참가자격을 충족하지 못해 DEFAULT FAIL로 판정됩니다.",
        requirements: [
          { title: "지역 제한", description: "부산광역시 소재 조건은 충족합니다.", status: "PASS", evidence_id: "f1" },
          { title: "직접생산확인증명서", description: "필수 품목 증명서를 보유하지 않았으며 대체·예외 조항이 없습니다.", status: "FAIL", evidence_id: "f2" },
          { title: "공동수급", description: "공동수급이 허용되지 않습니다.", status: "FAIL", evidence_id: "f3" },
        ],
        evidence: [
          { id: "f1", file: "사업자등록증.pdf", page: "사업장 소재지", quote: "부산광역시 해운대구 소재", status: "VERIFIED", confidence: 100 },
          { id: "f2", file: "입찰공고서.pdf", page: "4페이지 · 참가자격 라항", quote: "세부품명번호에 해당하는 직접생산확인증명서를 소지한 업체", status: "VERIFIED", confidence: 97 },
          { id: "f3", file: "입찰공고서.pdf", page: "6페이지", quote: "본 입찰은 공동수급을 허용하지 아니한다.", status: "VERIFIED", confidence: 99 },
        ],
        actions: [],
        award_history: [],
      },
      {
        notice_key: "demo-2026-004",
        status: "OPEN",
        notice_number: "R26BK01094275",
        title: "국가 연구개발사업 성과분석 및 정책환류 모델 고도화",
        agency: "한국연구재단",
        deadline: futureIso(9, 15),
        collected_at: futureIso(-3, 10),
        budget: 275000000,
        eligibility_status: "REVIEW",
        readiness_score: 74,
        evidence_coverage: 61,
        risk_score: 47,
        recommendation: "CONDITIONAL_GO",
        category: "정책 연구",
        contract_method: "일반경쟁 · 협상계약",
        region: "전국",
        summary: "정책 성과분석 역량은 보유하고 있으나 연구책임자 학술실적과 계량분석 전문인력 요건에 대한 사내 증빙 연결이 부족합니다. 참여인력 구성과 학술실적을 확인하면 입찰 가능성을 재평가할 수 있습니다.",
        requirements: [
          { title: "정책연구 수행실적", description: "유사 실적 후보 4건이 확인됩니다.", status: "PASS", evidence_id: "g1" },
          { title: "연구책임자 자격", description: "박사학위 및 연구경력 조건의 최신 증빙 연결이 필요합니다.", status: "REVIEW", evidence_id: "g2" },
          { title: "계량분석 전문인력", description: "투입 예정 인력의 수행 이력을 확인해야 합니다.", status: "REVIEW" },
        ],
        evidence: [
          { id: "g1", file: "제안요청서.pdf", page: "22페이지", quote: "국가연구개발사업 또는 정책사업 성과분석 실적을 인정한다.", status: "VERIFIED", confidence: 95 },
          { id: "g2", file: "제안요청서.pdf", page: "14페이지", quote: "연구책임자는 관련 분야 박사학위 취득 후 5년 이상의 연구경력을 보유하여야 한다.", status: "PROVISIONAL", confidence: 92 },
        ],
        quantitative: [
          { label: "기관 수행실적", max_score: 8, expected_score: "5~7", status: "PROVISIONAL" },
          { label: "연구책임자 경력", max_score: 7, expected_score: "미확인", status: "MISSING" },
        ],
        risk_axes: [
          { label: "자격 조건", score: 38 }, { label: "증빙 완전성", score: 68 }, { label: "경쟁 강도", score: 54 },
          { label: "제안 일정", score: 22 }, { label: "수행 운영", score: 47 }, { label: "데이터 품질", score: 51 },
        ],
        actions: ["연구책임자 후보의 학위·경력증명서를 회사 마스터에 연결하세요.", "계량분석 전문인력 2인 이상의 투입 가능 일정을 확인하세요."],
        award_history: [
          { year: 2025, title: "국가 R&D 성과분석 연구", winner: "과학기술정책연구원", amount: 258000000, rate: 90.1 },
          { year: 2024, title: "연구성과 정책활용 체계 구축", winner: "정책평가연구원", amount: 230000000, rate: 88.4 },
        ],
      },
      {
        notice_key: "demo-2026-005",
        status: "OPEN",
        notice_number: "R26BK01094701",
        title: "2026년 공공서비스 고객경험 조사 및 서비스디자인 컨설팅",
        agency: "국민연금공단",
        deadline: futureIso(13, 10),
        collected_at: futureIso(-5, 13),
        budget: 198000000,
        eligibility_status: "PASS",
        readiness_score: 87,
        evidence_coverage: 90,
        risk_score: 28,
        recommendation: "GO",
        decision: "GO",
        decision_comment: "서비스디자인 실적과 전담인력 가용성을 확인함. 제안 준비 착수.",
        decided_by: "전략사업팀 김담당",
        decided_at: now,
        result_status: "PREPARING",
        category: "서비스디자인",
        contract_method: "제한경쟁 · 협상계약",
        region: "전국",
        summary: "고객경험 조사와 서비스디자인 방법론을 적용하는 사업으로, 최근 수행실적과 전담인력 증빙이 확보되었습니다. 경쟁 리스크는 보통 수준이나 제안 차별화 여지가 있어 GO 후보입니다.",
        requirements: [
          { title: "조사·컨설팅 업종", description: "등록정보가 확인되었습니다.", status: "PASS", evidence_id: "h1" },
          { title: "고객경험 조사 실적", description: "기준금액 이상 실적 6건이 확인됩니다.", status: "PASS", evidence_id: "h2" },
        ],
        evidence: [
          { id: "h1", file: "입찰공고서.pdf", page: "3페이지", quote: "입찰참가자격 등록을 완료한 조사·컨설팅 사업자", status: "VERIFIED", confidence: 98 },
          { id: "h2", file: "제안요청서.hwpx", page: "21페이지", quote: "최근 3년간 공공서비스 고객경험 또는 만족도 조사 실적", status: "VERIFIED", confidence: 94 },
        ],
        quantitative: [
          { label: "유사 실적", max_score: 10, expected_score: 10, status: "VERIFIED" },
          { label: "전담인력", max_score: 8, expected_score: 7, status: "VERIFIED" },
          { label: "신인도", max_score: 2, expected_score: 2, status: "VERIFIED" },
        ],
        risk_axes: [
          { label: "자격 조건", score: 10 }, { label: "증빙 완전성", score: 16 }, { label: "경쟁 강도", score: 53 },
          { label: "제안 일정", score: 21 }, { label: "수행 운영", score: 24 }, { label: "데이터 품질", score: 8 },
        ],
        award_history: [
          { year: 2025, title: "공공서비스 만족도 및 경험조사", winner: "한국능률협회컨설팅", amount: 181000000, rate: 89.7 },
          { year: 2024, title: "고객경험 기반 서비스 개선", winner: "리서치앤리서치", amount: 174000000, rate: 88.9 },
          { year: 2023, title: "국민체감 서비스디자인", winner: "디자인정책연구소", amount: 165000000, rate: 87.4 },
        ],
      },
    ];

    return {
      dashboard: {
        new_count: notices.filter((notice) => notice.is_new !== false && isRecent(notice.collected_at, 48)).length,
        review_count: notices.filter((notice) => notice.eligibility_status === "REVIEW").length,
        go_count: notices.filter((notice) => notice.recommendation === "GO").length,
        urgent_count: notices.filter((notice) => {
          const days = daysUntil(notice.deadline);
          return days !== null && days >= 0 && days <= URGENT_DEADLINE_DAYS;
        }).length,
        undecided_count: notices.filter((notice) => !notice.decision).length,
        last_sync: now,
      },
      notices,
    };
  }
})();
