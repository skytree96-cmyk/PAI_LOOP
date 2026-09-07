(() => {
  "use strict";

  const API_BASE = (document.documentElement.dataset.apiBase || "/api/v1").replace(/\/$/, "");
  const REQUEST_TIMEOUT_MS = 12000;
  const DASHBOARD_REQUEST_TIMEOUT_MS = 60000;
  const NOTICE_REQUEST_TIMEOUT_MS = 60000;
  const RANKING_REQUEST_TIMEOUT_MS = 60000;
  const EXTERNAL_PPS_REQUEST_TIMEOUT_MS = 90000;
  const NOTICE_PAGE_SIZE = 200;
  const URGENT_DEADLINE_DAYS = 5;
  const MANUAL_ANALYSIS_POLL_INTERVAL_MS = 3000;
  const MANUAL_ANALYSIS_MAX_POLLS = 1800;
  // Compatibility note for older embedded contracts: MANUAL_ANALYSIS_MAX_POLLS = 900.
  const PRESPEC_ANALYSIS_POLL_INTERVAL_MS = 3000;
  const PRESPEC_ANALYSIS_MAX_POLLS = 40;
  const PRESPEC_ANALYSIS_POLL_MAX_MS = 120000;
  const NAV_GROUP_STORAGE_KEY = "pai-loop-nav-groups-v1";
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
    decisionDockNoticeKey: null,
    selectedTrigger: null,
    sourceDialogTrigger: null,
    currentView: "all",
    noticeSearchMode: "stored",
    noticeSearchGuideOpened: false,
    noticeSearchHelpTrigger: null,
    layout: window.matchMedia("(max-width: 680px)").matches ? "cards" : "table",
    loading: false,
    detailLoading: false,
    requestSequence: 0,
    connectionDelayTimer: null,
    lastSuccessfulQueryAt: null,
    lastSuccessfulSyncAt: null,
    runtimeProfileAvailable: false,
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
    operatorDecisionEnabled: false,
    manualAnalysisEnabled: false,
    manualAnalysisUnavailableReason: "분석 기능 상태를 확인하고 있습니다.",
    manualAnalysisAuthRequired: false,
    manualAnalysisToken: "",
    manualAnalysisPolicy: null,
    manualAnalysisRequests: new Map(),
    ppsDiscovery: {
      query: "",
      fromDate: "",
      toDate: "",
      candidates: [],
      resultCount: 0,
      apiCalls: 0,
      truncated: false,
      searched: false,
      loading: false,
      submitting: false,
      error: null,
      saving: new Set(),
      requestSequence: 0,
    },
    prespec: {
      stored: { records: [], loaded: false, loading: false, error: null, truncated: false, requestSequence: 0 },
      live: { records: [], searched: false, loading: false, error: null, apiCalls: 0, fetched: 0, truncated: false, warnings: [], query: "", fromDate: "", toDate: "", limit: 25, saving: new Set(), requestSequence: 0 },
      details: new Map(),
      selectedDetail: null,
      detailLoading: false,
      helpTrigger: null,
      analysis: { registryNo: "", analysisId: "", polling: false, polls: 0, response: null },
    },
    companyAwards: {
      company: null,
      records: [],
      count: 0,
      apiCalls: 0,
      truncated: false,
      partial: false,
      warnings: [],
      searched: false,
      loading: false,
      error: null,
    },
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
    performanceEditor: {
      records: [], total: 0, loaded: false, loading: false, editingRecord: null,
    },
    resultLearning: {
      records: [], total: 0, offset: 0, limit: 40, loaded: false, loading: false,
      editingNotice: null, editingOutcome: null,
    },
  };

  const els = {};

  const STATUS_LABELS = {
    PASS: "충족",
    PASS_EXCEPTION: "조건부 충족",
    PASS_CURRENT: "현재 충족",
    REVIEW: "확인 필요",
    FAIL: "미충족",
    UNKNOWN: "확인 필요",
  };

  const RECOMMENDATION_LABELS = {
    GO: "GO",
    CONDITIONAL_GO: "조건부 GO",
    HOLD: "조건부 GO",
    DEFERRED: "권고 보류",
    NO_GO: "NO-GO",
    UNKNOWN: "확인 필요",
  };

  const DECISION_LABELS = {
    GO: "참여",
    HOLD: "보류",
    CONDITIONAL_GO: "보류",
    NO_GO: "불참",
  };

  const ANALYSIS_REASON_LABELS = {
    NOT_SELECTED: "자동 분석 우선순위에 아직 선정되지 않아 분석 대기 중입니다. 폐기된 공고가 아닙니다.",
    ATTACHMENT_MANIFEST_MISSING: "조달청 응답에 분석할 첨부파일 목록이 없어 문서 분석을 시작하지 못했습니다.",
    ATTACHMENT_MANIFEST_EMPTY: "조달청 공고에 분석 가능한 첨부파일이 확인되지 않았습니다.",
    ATTACHMENT_NONE: "조달청 공고에 분석 가능한 첨부파일이 확인되지 않아 자동 문서 분석을 시작하지 못했습니다.",
    ATTACHMENT_COVERAGE_INCOMPLETE: "현재 공고의 모든 공개 첨부에 대한 분석 감사가 아직 완료되지 않았습니다.",
    HWP_ONLY_UNSUPPORTED: "첨부가 구형 HWP 형식뿐이라 현재 온라인 추출기가 읽지 못했습니다. HWP를 HWPX 또는 PDF로 변환하는 보완 경로가 필요합니다.",
    HWPX_EXTRACT_FAILED: "HWPX 첨부는 확인했지만 본문 추출에 실패해 재처리 또는 문서 변환이 필요합니다.",
    PDF_EXTRACT_FAILED: "PDF 첨부는 확인했지만 본문 추출에 실패해 OCR 또는 재처리가 필요합니다.",
    OPENAI_REVIEW: "첨부 본문은 읽었지만 AI 구조화 결과가 검토 기준을 통과하지 못해 담당자 확인을 기다리고 있습니다.",
    UNVERIFIED_QUOTE: "AI가 제시한 인용문을 추출 본문에서 검증하지 못해 확정 판정을 보류했습니다.",
    QUOTE_UNVERIFIED: "AI가 제시한 인용문을 추출 본문에서 검증하지 못해 확정 판정을 보류했습니다.",
    READY: "첨부 분석 준비가 완료되어 다음 자동 분석 배치를 기다리고 있습니다.",
    PARTIAL: "일부 첨부만 처리되어 나머지 문서 분석 또는 담당자 확인이 필요합니다.",
    EVALUATION_MISSING: "첨부 분석은 완료됐지만 현재 공고 버전의 자격·정량 판단이 아직 저장되지 않았습니다.",
  };

  const VIEW_ROUTE_MAP = Object.freeze({
    all: "/",
    new: "/notices",
    review: "/reviews",
    undecided: "/decisions",
    closed: "/results",
    awards: "/awards",
    prespec: "/prespec",
    performance: "/performance",
    collected: "/",
    go: "/",
    urgent: "/urgent",
    fail: "/fail",
    cancelled: "/cancelled",
    ended: "/",
    "result-missing": "/result-missing",
  });

  const ROUTE_VIEW_MAP = Object.freeze({
    "/": "all",
    "/notices": "new",
    "/reviews": "review",
    "/urgent": "urgent",
    "/fail": "fail",
    "/cancelled": "cancelled",
    "/result-missing": "result-missing",
    "/decisions": "undecided",
    "/results": "closed",
    "/awards": "awards",
    "/prespec": "prespec",
    "/performance": "performance",
  });

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    formatAwardBusinessNumberInput();
    initializeExternalSearchDates();
    configurePaiBotTeamsAccess();
    detectTeamsContext();
    bindEvents();
    const initialView = routeViewFromLocation();
    setView(initialView, { syncRoute: true, replaceRoute: true });
    restoreNavigationGroups();
    setNoticeSearchMode(initialView === "prespec" ? "prespec" : "stored", {
      announce: false,
      syncView: false,
    });
    setLayout(state.layout);
    loadApplicationData();
  }

  function cacheElements() {
    const ids = [
      "demoBanner", "demoBannerTitle", "demoBannerReason", "retryApiButton", "systemStatusDot", "systemStatusText", "lastSyncText",
      "pageTitle", "mobileMenuButton", "paiBotTeamsButton", "paiBotTeamsAccessNote", "refreshButton", "replayButton", "mainContent", "navNewCount", "navReviewCount",
      "navDecisionCount", "kpiNew", "kpiReview", "kpiGo", "kpiUrgent", "kpiResultMissing", "kpiEnded", "kpiNewTrend", "kpiReviewTrend", "kpiGoTrend",
      "analysisProgress", "analysisProgressScope", "analysisAttachmentValue", "analysisAttachmentDetail", "analysisEligibilityValue", "analysisEligibilityDetail", "analysisScoreValue", "analysisScoreDetail",
      "noticeHeading", "noticeSummary", "noticeViewToggle", "noticeSearchScope", "noticeSearchHelp", "noticeSearchInputLabel", "noticeSearchHelpButton", "noticeSearchHelpDialog", "prioritySearch", "departmentSelect", "priorityKeywordInput", "priorityApplyButton", "rankingProfileVersion", "filterForm", "searchInput", "eligibilityFilter", "recommendationFilter", "operatorDecisionFilter", "operatorDecisionFilterHelp", "sortSelect",
      "ppsSearchSuggestion", "ppsSearchSuggestionButton", "ppsDiscoverySection", "ppsDiscoveryStatus", "ppsDiscoveryQuery", "ppsDiscoveryForm", "ppsDiscoveryFromDate", "ppsDiscoveryToDate", "ppsDiscoverySearchButton", "ppsDiscoveryResults",
      "resetFiltersButton", "noticePanel", "noticeTableWrap", "noticeTableBody", "noticeCardGrid", "loadingState", "errorState",
      "errorStateMessage", "errorRetryButton", "emptyState", "emptyResetButton", "dataSourceLabel", "sidebarScrim", "drawerScrim",
      "detailDrawer", "drawerLoading", "closeDetailButton", "previousNoticeButton", "nextNoticeButton", "detailPosition", "manualAnalyzeButton", "openSourceDialogButton", "copyLinkButton", "detailSourceBadge", "detailNoticeId", "drawerScroll",
      "sourceLinkDialog", "closeSourceLinkDialogButton", "cancelSourceLinkDialogButton", "sourceLinkDialogTitle", "sourceLinkDialogNotice", "sourceLinkDialogMeta", "sourceLinkDialogMessage", "sourceLinkOpenAnchor",
      "manualAnalysisTokenDialog", "manualAnalysisTokenInput",
      "detailTags", "detailTitle", "detailAgency", "detailFacts", "decisionSummary", "recommendationCondition", "analysisPipeline", "evidenceCount",
      "detailSummary", "briefEvidenceLabel", "documentAnalysisCard", "documentAnalysisState", "documentAnalysisList", "privateMatchSection", "privateMatchBadge", "privateMatchRetryButton", "privateMatchBody", "privateMatchNote", "eligibilityOverall", "requirementList", "actionCard", "actionList", "evidenceList", "scoreOverview",
      "quantSeparationNote", "quantSourceStatus", "quantOpinion", "quantSourceAnchor", "quantAssumptionList", "quantTableBody", "quantObservationList", "riskTotalLabel", "riskBars", "historyList", "historyStatusLabel", "historyStatusText", "historyConcentration", "historyPrediction", "historyCoverage", "historyWarnings", "decisionForm", "decisionExisting", "toggleCommentButton", "decisionDockToggle", "decisionDockBody",
      "commentField", "decisionComment", "commentCount", "saveDecisionButton", "toastRegion", "skeletonRowTemplate",
      "teamsMockSource", "teamsMockTitle", "teamsMockAgency", "teamsMockStatus", "teamsMockDeadline", "teamsMockReason",
      "teamsMockReadiness", "teamsMockRisk", "teamsMockRecommendation", "teamsPreviewOpenButton", "teamsPreviewDecisionButton",
      "teamsMockSendButton", "clearTeamsLogsButton", "teamsMockLogList", "teamsMockJson", "teamsLogStorageLabel",
      "opportunityHero", "opportunityKpis", "noticeSection", "prespecSection", "resultLearningSection", "awardResultsSection", "performanceSection", "footerDisclaimer",
      "prespecHelpButton", "prespecHelpDialog", "prespecStoredForm", "prespecStoredSearchInput", "prespecStoredStatusFilter", "prespecStoredSubmitButton", "prespecStoredSummary", "prespecStoredList", "prespecStoredState",
      "prespecLiveForm", "prespecLiveQuery", "prespecLiveFromDate", "prespecLiveToDate", "prespecLiveLimit", "prespecLiveSearchButton", "prespecLiveSummary", "prespecLiveList", "prespecLiveState",
      "prespecDetailDialog", "prespecDetailTitle", "prespecDetailMeta", "prespecDetailCloseButton", "prespecDetailCancelButton", "prespecDetailBody", "prespecAnalysisStatus", "prespecAnalysisButton",
      "awardResultsSummary", "awardSearchForm", "awardCompanyName", "awardBusinessNumber", "awardStartDate", "awardEndDate", "awardSearchButton",
      "awardResultsPanel", "awardResultsList", "awardResultsLoadingState", "awardResultsErrorState", "awardResultsErrorMessage", "awardResultsEmptyState",
      "performanceTotal", "performancePeriod", "performanceYears", "performancePrivacy", "performanceResultSummary",
      "performanceFilterForm", "performanceSearchInput", "performanceYearFilter", "performanceDivisionFilter",
      "performanceDateFrom", "performanceDateTo", "performanceMinAmount", "performanceMaxAmount",
      "performancePanel", "performanceList", "performanceLoadingState", "performanceErrorState", "performanceErrorMessage",
      "performanceRetryButton", "performanceEmptyState", "performanceEmptyResetButton", "performancePagination",
      "performancePageRange", "performancePageLabel", "performancePreviousButton", "performanceNextButton",
      "performanceEditorSummary", "performanceEditorUnlockButton", "performanceEditorCreateButton", "performanceEditorList", "performanceEditorState",
      "performanceRecordDialog", "performanceRecordForm", "performanceRecordDialogTitle", "performanceRecordCloseButton", "performanceRecordCancelButton", "performanceRecordSaveButton",
      "performanceRecordProject", "performanceRecordAgency", "performanceRecordDivision", "performanceRecordStatus", "performanceRecordContractDate", "performanceRecordStartDate", "performanceRecordEndDate", "performanceRecordAmount", "performanceRecordVat", "performanceRecordShare", "performanceRecordCertificate", "performanceRecordCompleted", "performanceRecordEvidence", "performanceRecordKeywords", "performanceRecordOverview",
      "resultLearningSummary", "resultLearningUnlockButton", "resultLearningFilterForm", "resultLearningSearchInput", "resultLearningOutcomeFilter", "resultLearningRecordFilter", "resultLearningList", "resultLearningState", "resultLearningPagination", "resultLearningPageRange", "resultLearningPageLabel", "resultLearningPreviousButton", "resultLearningNextButton",
      "resultLearningDialog", "resultLearningForm", "resultLearningDialogTitle", "resultLearningDialogNotice", "resultLearningCloseButton", "resultLearningCancelButton", "resultLearningSaveButton", "resultLearningStatus", "resultLearningRecordStatus", "resultLearningSubmittedAmount", "resultLearningSubmittedRate", "resultLearningWinningAmount", "resultLearningWinningRate", "resultLearningTechnicalScore", "resultLearningPriceScore", "resultLearningTotalScore", "resultLearningRank", "resultLearningWinner", "resultLearningOccurredAt", "resultLearningLossReason", "resultLearningSourceReference", "resultLearningOperatorNote",
    ];

    ids.forEach((id) => {
      els[id] = document.getElementById(id);
    });
    els.sidebar = document.querySelector(".sidebar");
    els.navItems = [...document.querySelectorAll(".nav-item[data-view]")];
    els.navGroupToggles = [...document.querySelectorAll(".nav-group-toggle[aria-controls]")];
    els.kpiViewButtons = [...document.querySelectorAll("[data-kpi-view]")];
    els.layoutButtons = [...document.querySelectorAll("[data-layout]")];
    els.noticeSearchModeButtons = [...document.querySelectorAll("[data-notice-search-mode]")];
    els.storedSearchControls = [...document.querySelectorAll(".stored-search-only")];
    els.tabButtons = [...document.querySelectorAll("[role='tab'][data-tab]")];
    els.tabPanels = [...document.querySelectorAll("[role='tabpanel'][data-panel]")];
    els.decisionInputs = [...document.querySelectorAll("input[name='decision']")];
    els.awardScopeInputs = [...document.querySelectorAll("input[name='awardScope']")];
  }

  function restoreNavigationGroups() {
    let preferences = {};
    try {
      const parsed = JSON.parse(window.localStorage.getItem(NAV_GROUP_STORAGE_KEY) || "{}");
      preferences = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (_error) {
      preferences = {};
    }
    els.navGroupToggles.forEach((button) => {
      const group = button.closest("[data-nav-group]");
      const groupKey = group?.dataset.navGroup;
      const expanded = groupKey && typeof preferences[groupKey] === "boolean"
        ? preferences[groupKey]
        : true;
      setNavigationGroupExpanded(button, expanded);
    });
  }

  function setNavigationGroupExpanded(button, expanded) {
    const controlsId = button?.getAttribute("aria-controls");
    const items = controlsId ? document.getElementById(controlsId) : null;
    if (!items) return;
    button.setAttribute("aria-expanded", String(Boolean(expanded)));
    items.hidden = !expanded;
    button.closest("[data-nav-group]")?.classList.toggle("is-collapsed", !expanded);
  }

  function persistNavigationGroups() {
    const preferences = {};
    els.navGroupToggles.forEach((button) => {
      const groupKey = button.closest("[data-nav-group]")?.dataset.navGroup;
      if (groupKey) preferences[groupKey] = button.getAttribute("aria-expanded") === "true";
    });
    try {
      window.localStorage.setItem(NAV_GROUP_STORAGE_KEY, JSON.stringify(preferences));
    } catch (_error) {
      // Menu disclosure still works when storage is unavailable.
    }
  }

  function toggleNavigationGroup(button) {
    const expanded = button.getAttribute("aria-expanded") === "true";
    setNavigationGroupExpanded(button, !expanded);
    persistNavigationGroups();
  }

  function revealActiveNavigationGroup(view) {
    const activeItem = els.navItems.find((item) => item.dataset.view === view);
    const group = activeItem?.closest("[data-nav-group]");
    const button = group?.querySelector(".nav-group-toggle[aria-controls]");
    if (!button || button.getAttribute("aria-expanded") === "true") return;
    setNavigationGroupExpanded(button, true);
    persistNavigationGroups();
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
      ? "Teams 채널 열기"
      : "채널 연결 준비 중";
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

    els.filterForm.addEventListener("input", () => {
      if (state.noticeSearchMode === "stored") applyFilters();
    });
    els.filterForm.addEventListener("change", () => {
      if (state.noticeSearchMode === "stored") applyFilters();
    });
    els.filterForm.addEventListener("submit", submitNoticeSearch);
    els.filterForm.addEventListener("reset", () => window.setTimeout(resetPrioritySearch, 0));
    els.searchInput.addEventListener("input", scheduleNoticeSearch);
    els.emptyResetButton.addEventListener("click", resetFilters);
    els.ppsDiscoveryForm.addEventListener("submit", searchPpsNotices);
    els.ppsDiscoveryResults.addEventListener("click", handlePpsDiscoveryAction);
    els.ppsDiscoveryFromDate.addEventListener("change", invalidatePpsDiscoveryDates);
    els.ppsDiscoveryToDate.addEventListener("change", invalidatePpsDiscoveryDates);
    els.ppsSearchSuggestionButton.addEventListener("click", () => setNoticeSearchMode("pps", { showGuide: true }));
    els.noticeSearchModeButtons.forEach((button) => {
      button.addEventListener("click", () => setNoticeSearchMode(button.dataset.noticeSearchMode, { showGuide: true }));
    });
    els.noticeSearchHelpButton.addEventListener("click", openNoticeSearchHelpDialog);
    els.noticeSearchHelpDialog.addEventListener("click", (event) => {
      if (event.target === els.noticeSearchHelpDialog) els.noticeSearchHelpDialog.close("close");
    });
    els.noticeSearchHelpDialog.addEventListener("close", () => {
      els.noticeSearchHelpButton.setAttribute("aria-expanded", "false");
      const trigger = state.noticeSearchHelpTrigger;
      state.noticeSearchHelpTrigger = null;
      if (trigger?.isConnected) trigger.focus();
    });

    els.prespecStoredForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void loadStoredPreSpecifications({ force: true });
    });
    els.prespecStoredList.addEventListener("click", handlePreSpecificationAction);
    els.prespecLiveForm.addEventListener("submit", searchLivePreSpecifications);
    els.prespecLiveList.addEventListener("click", handlePreSpecificationAction);
    els.prespecHelpButton.addEventListener("click", openPreSpecificationHelp);
    els.prespecHelpDialog.addEventListener("click", (event) => {
      if (event.target === els.prespecHelpDialog) els.prespecHelpDialog.close("close");
    });
    els.prespecHelpDialog.addEventListener("close", restorePreSpecificationHelpFocus);
    els.prespecDetailCloseButton.addEventListener("click", closePreSpecificationDetail);
    els.prespecDetailCancelButton.addEventListener("click", closePreSpecificationDetail);
    els.prespecDetailDialog.addEventListener("click", (event) => {
      if (event.target === els.prespecDetailDialog) closePreSpecificationDetail();
    });
    els.prespecAnalysisButton.addEventListener("click", requestPreSpecificationAnalysis);

    els.awardSearchForm.addEventListener("submit", searchCompanyAwards);
    els.awardBusinessNumber.addEventListener("input", formatAwardBusinessNumberInput);

    els.performanceFilterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!validatePerformanceRanges()) return;
      state.performance.offset = 0;
      void loadPerformance({ force: true });
    });
    els.performanceFilterForm.addEventListener("reset", () => {
      window.setTimeout(() => {
        validatePerformanceRanges(false);
        state.performance.offset = 0;
        void loadPerformance({ force: true });
      }, 0);
    });
    els.performanceFilterForm.addEventListener("input", () => validatePerformanceRanges(false));
    els.performanceRetryButton.addEventListener("click", () => loadPerformance({ force: true }));
    els.performanceEmptyResetButton.addEventListener("click", resetPerformanceFilters);
    els.performancePreviousButton.addEventListener("click", () => changePerformancePage(-1));
    els.performanceNextButton.addEventListener("click", () => changePerformancePage(1));
    els.performanceEditorUnlockButton.addEventListener("click", () => loadPerformanceEditor({ force: true }));
    els.performanceEditorCreateButton.addEventListener("click", () => openPerformanceRecordDialog());
    els.performanceEditorList.addEventListener("click", handlePerformanceEditorAction);
    els.performanceRecordForm.addEventListener("submit", savePerformanceRecord);
    els.performanceRecordCloseButton.addEventListener("click", closePerformanceRecordDialog);
    els.performanceRecordCancelButton.addEventListener("click", closePerformanceRecordDialog);

    els.resultLearningUnlockButton.addEventListener("click", () => loadResultLearning({ force: true }));
    els.resultLearningFilterForm.addEventListener("submit", (event) => {
      event.preventDefault();
      state.resultLearning.offset = 0;
      void loadResultLearning({ force: true });
    });
    els.resultLearningFilterForm.addEventListener("reset", () => {
      window.setTimeout(() => {
        state.resultLearning.offset = 0;
        void loadResultLearning({ force: true });
      }, 0);
    });
    els.resultLearningList.addEventListener("click", handleResultLearningAction);
    els.resultLearningPreviousButton.addEventListener("click", () => changeResultLearningPage(-1));
    els.resultLearningNextButton.addEventListener("click", () => changeResultLearningPage(1));
    els.resultLearningForm.addEventListener("submit", saveResultLearning);
    els.resultLearningStatus.addEventListener("change", () => {
      if (els.resultLearningStatus.value !== "NO_BID") return;
      [els.resultLearningSubmittedAmount, els.resultLearningSubmittedRate, els.resultLearningWinningAmount, els.resultLearningWinningRate, els.resultLearningTechnicalScore, els.resultLearningPriceScore, els.resultLearningTotalScore, els.resultLearningRank].forEach((input) => { input.value = ""; });
      els.resultLearningWinner.value = "";
    });
    els.resultLearningCloseButton.addEventListener("click", closeResultLearningDialog);
    els.resultLearningCancelButton.addEventListener("click", closeResultLearningDialog);

    els.navItems.forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
    els.navGroupToggles.forEach((button) => {
      button.addEventListener("click", () => toggleNavigationGroup(button));
    });
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
    els.previousNoticeButton.addEventListener("click", () => moveDetailSelection(-1));
    els.nextNoticeButton.addEventListener("click", () => moveDetailSelection(1));
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
    [els.requirementList, els.actionList].forEach((list) => {
      list.addEventListener("click", (event) => {
        const button = event.target.closest("[data-evidence-jump]");
        if (!button) return;
        selectTab("evidence");
        requestAnimationFrame(() => {
          const target = document.getElementById(`evidence-${button.dataset.evidenceJump}`) || els.evidenceList;
          els.evidenceList.focus({ preventScroll: true });
          target.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      });
    });

    els.decisionDockToggle.addEventListener("click", () => setDecisionDockExpanded(els.decisionDockBody.hidden));
    els.toggleCommentButton.addEventListener("click", toggleCommentField);
    els.decisionInputs.forEach((input) => input.addEventListener("change", updateDecisionButton));
    els.decisionComment.addEventListener("input", () => {
      els.commentCount.textContent = String(els.decisionComment.value.length);
      updateDecisionButton();
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
    if (state.noticeSearchMode === "prespec") {
      void loadStoredPreSpecifications({ force: true });
      return;
    }
    if (state.currentView === "closed") {
      void loadResultLearning({ force: true });
      return;
    }
    if (state.currentView === "awards") {
      if (state.companyAwards.searched) void searchCompanyAwards();
      else renderCompanyAwardsView();
      return;
    }
    if (state.currentView === "performance") {
      void loadPerformance({ force: true });
      if (state.performanceEditor.loaded) void loadPerformanceEditor({ force: true });
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
    window.clearTimeout(state.connectionDelayTimer);
    state.connectionDelayTimer = window.setTimeout(() => {
      if (sequence === state.requestSequence && state.loading) setSystemStatus("delayed");
    }, 10000);

    const query = new URLSearchParams(window.location.search);
    const explicitDemo = query.get("demo") === "1" && !forceApi;

    if (explicitDemo) {
      await delay(280);
      if (sequence !== state.requestSequence) return;
      activateDemo("URL의 demo=1 설정에 따라 예시 데이터를 표시합니다.");
      finishLoading();
      return;
    }

    // Render the usable board before scanning all-history aggregates. Running
    // both relationship-heavy reads together can exceed a small worker's
    // memory limit as the stored extraction history grows.
    const [noticesResult, runtimeResult] = await Promise.allSettled([
      fetchNoticePages({ statusScope: requestedStatusScope }),
      apiRequest("/runtime-profile"),
    ]);

    if (sequence !== state.requestSequence) return;
    if (requestedStatusScope !== noticeStatusScopeForView(state.currentView)) {
      void loadApplicationData({ forceApi: true });
      return;
    }

    if (noticesResult.status === "fulfilled") {
      state.runtimeProfileAvailable = runtimeResult.status === "fulfilled";
      if (runtimeResult.status === "fulfilled") {
        applyRuntimeProfile(runtimeResult.value);
      } else {
        state.manualAnalysisEnabled = false;
        state.manualAnalysisAuthRequired = false;
        state.manualAnalysisPolicy = null;
        state.manualAnalysisUnavailableReason = "분석 설정을 불러오지 못했습니다. 새로고침 후 다시 확인해 주세요.";
      }
      state.quantitativeEstimates = {};
      const list = extractList(noticesResult.value);
      const previousNotices = new Map(state.notices.map((notice) => [notice.noticeKey, notice]));
      state.source = "api";
      state.notices = list.map((raw) => {
        const notice = normalizeNotice(raw);
        return preserveOperatorDecision(notice, previousNotices.get(notice.noticeKey));
      }).filter((notice) => notice.noticeKey);
      state.dashboard = dashboardWithoutGlobalTotals(state.notices);
      state.sourceReason = "";
      state.lastSuccessfulQueryAt = new Date().toISOString();
      setSystemStatus("loading");
      if (state.dashboard.syntheticWarning && state.notices.some((notice) => notice.isSynthetic)) showDemoBanner(state.dashboard.syntheticWarning);
      else hideDemoBanner();
      finishLoading();
      openNoticeFromRoute();
      void hydrateApplicationMetadata({ sequence, requestedStatusScope });
      return;
    }

    const reason = humanizeError(noticesResult.reason);
    renderApplicationError(`운영 서버 연결 실패: ${reason}`);
  }

  async function hydrateApplicationMetadata({ sequence, requestedStatusScope }) {
    const [dashboardResult, profilesResult] = await Promise.allSettled([
      apiRequest("/dashboard", { timeoutMs: DASHBOARD_REQUEST_TIMEOUT_MS }),
      apiRequest("/departments/keyword-profiles"),
    ]);
    if (sequence !== state.requestSequence) return;
    if (requestedStatusScope !== noticeStatusScopeForView(state.currentView)) return;
    if (state.source !== "api") return;

    if (profilesResult.status === "fulfilled") {
      state.departmentCatalog = unwrapObject(profilesResult.value);
      populateDepartmentProfiles(state.departmentCatalog);
    }
    if (dashboardResult.status === "fulfilled") {
      state.dashboard = normalizeDashboard(dashboardResult.value, state.notices);
      state.sourceReason = "";
    } else {
      state.sourceReason = "전체 집계 조회가 지연되었습니다. 공고 목록은 조회됐으며 전체 통계는 새로고침이 필요합니다.";
    }
    setSystemStatus(dashboardResult.status === "fulfilled" && profilesResult.status === "fulfilled" && state.runtimeProfileAvailable ? "online" : "partial");
    renderAll();
  }

  async function refreshDashboardAfterMutation() {
    if (state.source === "api") {
      try {
        const payload = await apiRequest("/dashboard", { timeoutMs: DASHBOARD_REQUEST_TIMEOUT_MS });
        state.dashboard = normalizeDashboard(payload, state.notices);
        return;
      } catch (_) {
        // The mutation has already succeeded. Preserve server totals instead
        // of replacing the whole dashboard with an incomplete local shape.
      }
    }
    state.dashboard = dashboardWithoutGlobalTotals(state.notices, state.dashboard);
  }

  function renderApplicationError(reason) {
    window.clearTimeout(state.connectionDelayTimer);
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
    state.manualAnalysisUnavailableReason = state.manualAnalysisEnabled
      ? ""
      : "현재 서버에서 수동 분석 기능이 비활성화되어 있습니다.";
    state.manualAnalysisAuthRequired = booleanValue(
      firstValue(profile.manual_analysis_auth_required, profile.manualAnalysisAuthRequired),
    ) ?? false;
    state.manualAnalysisPolicy = firstObject(
      profile.manual_analysis_policy,
      profile.manualAnalysisPolicy,
    );
    state.operatorDecisionEnabled = booleanValue(
      firstValue(profile.operator_decisions_enabled, profile.operatorDecisionsEnabled),
    ) ?? state.manualAnalysisEnabled;
    const readOnly = !state.writeControlsEnabled;
    const decisionWritable = canWriteDecision();
    els.replayButton.disabled = readOnly;
    els.replayButton.title = readOnly ? "공개 읽기 전용 화면에서는 서버 작업을 실행하지 않습니다." : "";
    els.teamsMockSendButton.disabled = readOnly;
    els.clearTeamsLogsButton.disabled = readOnly;
    els.decisionInputs.forEach((input) => { input.disabled = !decisionWritable; });
    els.toggleCommentButton.disabled = !decisionWritable;
    els.decisionComment.disabled = !decisionWritable;
    if (!decisionWritable) {
      els.saveDecisionButton.disabled = true;
      els.saveDecisionButton.textContent = "현재 판단 저장 미제공";
    }
  }

  function canWriteDecision() {
    return Boolean(state.writeControlsEnabled || state.operatorDecisionEnabled);
  }

  function finishLoading() {
    window.clearTimeout(state.connectionDelayTimer);
    setLoading(false);
    renderAll();
  }

  function activateDemo(reason) {
    const payload = createDemoData();
    state.source = "demo";
    state.sourceReason = reason;
    state.notices = payload.notices.map((item, index) => normalizeNotice(
      item,
      index,
      { allowLegacyCurrentProjection: true },
    ));
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
        if (response.status === 401 && headers.has("X-PAI-Manual-Token")) {
          clearManualAnalysisToken();
        }
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

  function globalNoticeSearchActive() {
    return state.noticeSearchMode === "stored" && Boolean(els.searchInput?.value.trim());
  }

  function buildNoticeRequestPath({
    statusScope = noticeStatusScopeForView(state.currentView),
    limit = NOTICE_PAGE_SIZE,
    offset = 0,
  } = {}) {
    const params = new URLSearchParams();
    const storedMode = state.noticeSearchMode === "stored";
    const departmentId = storedMode ? els.departmentSelect?.value || "organization" : "organization";
    const searchKeywords = storedMode ? els.priorityKeywordInput?.value.trim() || "" : "";
    const query = storedMode ? els.searchInput?.value.trim() || "" : "";
    // Keep the organization ranking projection on the default board. The
    // backend now computes every department once per notice, so preserving
    // recommendation badges no longer forces the prior duplicate work.
    params.set("department_id", departmentId);
    // Department and interest keywords only influence ranking. They must not
    // narrow the stored-notice result set, including during a global search.
    if (searchKeywords) params.set("search_keywords", searchKeywords);
    if (query) params.set("q", query);
    if (["OPEN", "CLOSED", "EXPIRED", "ENDED"].includes(statusScope)) {
      params.set("status", statusScope);
    }
    params.set("limit", String(limit));
    params.set("offset", String(offset));
    return `/notices?${params.toString()}`;
  }

  function noticeRequestTimeoutMs() {
    if (state.noticeSearchMode !== "stored") return NOTICE_REQUEST_TIMEOUT_MS;
    const departmentId = els.departmentSelect?.value || "organization";
    const searchKeywords = els.priorityKeywordInput?.value.trim() || "";
    return departmentId !== "organization" || Boolean(searchKeywords)
      ? RANKING_REQUEST_TIMEOUT_MS
      : NOTICE_REQUEST_TIMEOUT_MS;
  }

  function noticeStatusScopeForView(view) {
    if (globalNoticeSearchActive()) return "ALL";
    if (["ended", "cancelled", "result-missing"].includes(view)) return "ENDED";
    return ["collected", "closed", "fail"].includes(view) ? "ALL" : "OPEN";
  }

  function renderNoticeSearchScope() {
    if (!els.noticeSearchScope) return;
    if (state.noticeSearchMode === "prespec") {
      els.noticeSearchScope.classList.remove("is-global", "is-pps");
      els.noticeSearchScope.textContent = "사전규격은 입찰공고 전 단계의 요구조건 검토용이며, 입찰 참여 GO/NO-GO를 결정하지 않습니다.";
      return;
    }
    if (state.noticeSearchMode === "pps") {
      els.noticeSearchScope.classList.remove("is-global");
      els.noticeSearchScope.classList.add("is-pps");
      els.noticeSearchScope.textContent = "나라장터 실시간 조회 · 게시일 기준 최대 31일의 용역 공고명을 검색합니다. 저장 전에는 자격 판단과 정량 점수가 없습니다.";
      return;
    }
    const globalSearch = globalNoticeSearchActive();
    els.noticeSearchScope.classList.remove("is-pps");
    els.noticeSearchScope.classList.toggle("is-global", globalSearch);
    const priorityNote = "부서·관심 키워드는 공고를 숨기지 않고 표시 순서에만 반영합니다.";
    els.noticeSearchScope.textContent = globalSearch
      ? `저장된 전체 공고 검색 · 현재 탭과 진행/종료 상태를 넘어서 찾습니다. ${priorityNote} 검색만으로 AI 비용은 발생하지 않습니다.`
      : `현재 화면 범위에서 공고를 표시합니다. ${priorityNote} 나라장터에서 아직 수집되지 않은 공고는 포함되지 않습니다.`;
  }

  function renderNoticeSearchMode() {
    const ppsMode = state.noticeSearchMode === "pps";
    const prespecMode = state.noticeSearchMode === "prespec";
    const bidNoticeMode = !prespecMode;

    els.noticeSearchModeButtons.forEach((button) => {
      const active = button.dataset.noticeSearchMode === state.noticeSearchMode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    els.filterForm.hidden = prespecMode;
    els.filterForm.classList.toggle("is-pps-mode", ppsMode);
    els.filterForm.setAttribute("aria-label", ppsMode ? "나라장터 용역 공고 검색" : "저장 공고 검색");
    els.prioritySearch.hidden = ppsMode || prespecMode;
    els.noticeSearchScope.hidden = prespecMode;
    els.noticeViewToggle.hidden = prespecMode;
    els.storedSearchControls.forEach((control) => {
      const field = control.matches("select, button, input") ? control : control.querySelector("select, button, input");
      if (field) field.disabled = !bidNoticeMode || ppsMode;
    });
    els.searchInput.placeholder = ppsMode
      ? "나라장터 용역 공고명 검색 (2자 이상)"
      : "공고명 · 발주기관 · 공고번호 검색";
    els.noticeSearchInputLabel.textContent = ppsMode
      ? "나라장터 용역 공고명 검색"
      : "공고명, 발주기관 또는 공고번호 검색";
    els.noticeSearchHelp.textContent = ppsMode
      ? "검색어와 게시일을 입력한 뒤 조회 버튼을 눌러야 나라장터를 조회합니다."
      : "검색어를 입력하면 PAI에 저장된 전체 공고에서 찾습니다.";
    els.noticePanel.hidden = ppsMode || prespecMode;
    els.prespecSection.hidden = !prespecMode;
    if (ppsMode) {
      els.noticeSummary.textContent = "PAI 저장 공고와 분리된 나라장터 용역 공고 조회입니다. 저장 전에는 판단 결과가 없습니다.";
    } else if (prespecMode) {
      els.noticeSummary.textContent = "입찰공고 전 공개되는 사전규격을 저장 자료와 나라장터에서 함께 찾습니다.";
      renderPreSpecificationView();
    }
    renderNoticeSearchScope();
    renderPpsDiscovery();
  }

  function setNoticeSearchMode(mode, { announce = true, showGuide = false, syncView = true } = {}) {
    const nextMode = ["pps", "prespec"].includes(mode) ? mode : "stored";
    const changed = state.noticeSearchMode !== nextMode;
    state.noticeSearchMode = nextMode;
    window.clearTimeout(state.noticeSearchTimer);
    state.noticeSearchTimer = null;

    const ppsMode = nextMode === "pps";
    const prespecMode = nextMode === "prespec";
    const targetView = prespecMode ? "prespec" : "new";
    const viewChanged = syncView && state.currentView !== targetView;
    if (viewChanged) {
      setView(targetView, { noticeSearchMode: nextMode, focusMain: false });
    }
    renderNoticeSearchMode();
    if (prespecMode && !state.prespec.stored.loaded && !state.prespec.stored.loading) {
      void loadStoredPreSpecifications();
    }
    if (nextMode === "stored" && changed) {
      if (state.source === "api" && !state.loading) void loadApplicationData({ forceApi: true });
      else if (!state.loading) applyFilters();
    }

    if ((ppsMode || prespecMode) && showGuide && !state.noticeSearchGuideOpened) {
      state.noticeSearchGuideOpened = true;
      openNoticeSearchHelpDialog();
    }
    if (announce && changed) {
      const title = ppsMode ? "나라장터 용역 공고 조회" : prespecMode ? "사전규격 탐색" : "저장 공고 검색";
      const message = ppsMode
        ? "외부 조회는 버튼을 눌렀을 때만 실행되며, 저장 전에는 판단과 점수가 없습니다."
        : prespecMode
          ? "저장된 사전규격과 나라장터 사전규격을 한 화면에서 확인합니다."
          : "PAI에 저장된 공고와 기존 판단 결과를 검색합니다.";
      showToast(title, message, "success");
    }
  }

  function openNoticeSearchHelpDialog() {
    const dialog = els.noticeSearchHelpDialog;
    if (!dialog || typeof dialog.showModal !== "function" || dialog.open) return;
    state.noticeSearchHelpTrigger = document.activeElement;
    els.noticeSearchHelpButton.setAttribute("aria-expanded", "true");
    dialog.showModal();
  }

  function scheduleNoticeSearch(event) {
    if (event?.isComposing || state.source === "demo") return;
    const query = els.searchInput.value.trim().replace(/\s+/g, " ");
    if (state.ppsDiscovery.query !== query && !state.ppsDiscovery.loading) resetPpsDiscovery(query);
    renderNoticeSearchScope();
    if (state.noticeSearchMode === "pps") {
      renderPpsDiscovery();
      return;
    }
    window.clearTimeout(state.noticeSearchTimer);
    state.noticeSearchTimer = window.setTimeout(() => {
      state.noticeSearchTimer = null;
      void loadApplicationData({ forceApi: true });
    }, 350);
  }

  function submitNoticeSearch(event) {
    event.preventDefault();
    if (state.noticeSearchMode === "pps") {
      void searchPpsNotices();
      return;
    }
    if (state.source === "demo") {
      applyFilters();
      return;
    }
    window.clearTimeout(state.noticeSearchTimer);
    state.noticeSearchTimer = null;
    const query = els.searchInput.value.trim().replace(/\s+/g, " ");
    if (state.ppsDiscovery.query !== query && !state.ppsDiscovery.loading) resetPpsDiscovery(query);
    void loadApplicationData({ forceApi: true });
  }

  function initializeExternalSearchDates() {
    const today = new Date();
    const ppsFrom = new Date(today);
    ppsFrom.setDate(ppsFrom.getDate() - 30);
    const awardFrom = new Date(today);
    awardFrom.setFullYear(awardFrom.getFullYear() - 3);
    els.ppsDiscoveryFromDate.value = formatDateInputValue(ppsFrom);
    els.ppsDiscoveryToDate.value = formatDateInputValue(today);
    els.prespecLiveFromDate.value = formatDateInputValue(ppsFrom);
    els.prespecLiveToDate.value = formatDateInputValue(today);
    els.awardStartDate.value = formatDateInputValue(awardFrom);
    els.awardEndDate.value = formatDateInputValue(today);
  }

  function resetPpsDiscovery(query = "") {
    state.ppsDiscovery.requestSequence += 1;
    state.ppsDiscovery.query = query;
    state.ppsDiscovery.fromDate = "";
    state.ppsDiscovery.toDate = "";
    state.ppsDiscovery.candidates = [];
    state.ppsDiscovery.resultCount = 0;
    state.ppsDiscovery.apiCalls = 0;
    state.ppsDiscovery.truncated = false;
    state.ppsDiscovery.searched = false;
    state.ppsDiscovery.loading = false;
    state.ppsDiscovery.submitting = false;
    state.ppsDiscovery.error = null;
  }

  function invalidatePpsDiscoveryDates() {
    const hadExecutedScope = Boolean(
      state.ppsDiscovery.fromDate
      || state.ppsDiscovery.toDate
      || state.ppsDiscovery.searched
      || state.ppsDiscovery.error,
    );
    if (!hadExecutedScope || state.ppsDiscovery.loading || state.ppsDiscovery.submitting) return;
    resetPpsDiscovery(els.searchInput.value.trim().replace(/\s+/g, " "));
    renderPpsDiscovery();
  }

  function normalizePpsCandidate(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    return {
      selectionKey: stringValue(firstValue(source.selection_key, source.selectionKey)),
      bidNoticeNo: stringValue(firstValue(source.bid_notice_no, source.bidNoticeNo), "공고번호 미확인"),
      revisionNo: stringValue(firstValue(source.revision_no, source.revisionNo), "00"),
      title: stringValue(source.title, "공고명 미확인"),
      agency: stringValue(source.agency, "발주기관 미확인"),
      publishedAt: stringValue(firstValue(source.published_at, source.publishedAt)),
      deadline: stringValue(source.deadline),
      estimatedAmount: numberOrNull(firstValue(source.estimated_amount, source.estimatedAmount)),
      noticeKind: stringValue(firstValue(source.notice_kind, source.noticeKind), "업무 구분 미확인"),
      directContractSignal: booleanValue(firstValue(source.direct_contract_signal, source.directContractSignal)) ?? false,
      sourceUrl: safeHttpUrl(firstValue(source.source_url, source.sourceUrl)),
      alreadyStored: booleanValue(firstValue(source.already_stored, source.alreadyStored)) ?? false,
      storedNoticeKey: stringValue(firstValue(source.stored_notice_key, source.storedNoticeKey)),
      relatedRevisionStored: booleanValue(firstValue(source.related_revision_stored, source.relatedRevisionStored)) ?? false,
      saveable: booleanValue(source.saveable) ?? false,
      saveBlockReason: stringValue(firstValue(source.save_block_reason, source.saveBlockReason)),
    };
  }

  async function searchPpsNotices(event) {
    event?.preventDefault?.();
    if (
      state.noticeSearchMode !== "pps"
      || state.source !== "api"
      || state.ppsDiscovery.loading
      || state.ppsDiscovery.submitting
    ) return;
    const query = els.searchInput.value.trim().replace(/\s+/g, " ");
    const fromDate = els.ppsDiscoveryFromDate.value;
    const toDate = els.ppsDiscoveryToDate.value;
    const span = dateSpanDays(fromDate, toDate);
    if (query.length < 2 || query.length > 60) {
      showToast("검색어 확인 필요", "나라장터 검색어는 2–60자로 입력해 주세요.", "warning");
      return;
    }
    if (span === null || span < 0 || span > 30) {
      showToast("조회 기간 확인 필요", "나라장터 공고 검색은 한 번에 최대 31일까지 조회할 수 있습니다.", "warning");
      return;
    }
    state.ppsDiscovery.submitting = true;
    renderPpsDiscovery();
    let authHeaders = null;
    let authError = null;
    try {
      authHeaders = await manualAnalysisAuthHeaders();
    } catch (error) {
      authError = error;
      showToast("나라장터 조회 준비 실패", humanizeError(error), "error");
    } finally {
      state.ppsDiscovery.submitting = false;
    }
    if (!authHeaders) {
      if (!authError) showToast("나라장터 조회 취소", "4자리 운영 PIN이 입력되지 않았습니다.", "warning");
      renderPpsDiscovery();
      return;
    }

    const requestSequence = ++state.ppsDiscovery.requestSequence;
    state.ppsDiscovery.query = query;
    state.ppsDiscovery.fromDate = fromDate;
    state.ppsDiscovery.toDate = toDate;
    state.ppsDiscovery.loading = true;
    state.ppsDiscovery.searched = false;
    state.ppsDiscovery.error = null;
    state.ppsDiscovery.candidates = [];
    renderPpsDiscovery();
    try {
      const payload = unwrapObject(await apiRequest("/pps-discovery/search", {
        method: "POST",
        headers: authHeaders,
        timeoutMs: EXTERNAL_PPS_REQUEST_TIMEOUT_MS,
        body: JSON.stringify({ query, from_date: fromDate, to_date: toDate, limit: 30 }),
      }));
      if (requestSequence !== state.ppsDiscovery.requestSequence) return;
      state.ppsDiscovery.candidates = arrayValue(payload.candidates).map(normalizePpsCandidate);
      state.ppsDiscovery.resultCount = Math.max(numberOrNull(payload.result_count) ?? state.ppsDiscovery.candidates.length, 0);
      state.ppsDiscovery.apiCalls = Math.max(numberOrNull(payload.api_calls) ?? 0, 0);
      state.ppsDiscovery.truncated = booleanValue(payload.truncated) ?? false;
      state.ppsDiscovery.searched = true;
      state.ppsDiscovery.error = null;
    } catch (error) {
      if (requestSequence !== state.ppsDiscovery.requestSequence) return;
      if (error?.status === 401) state.manualAnalysisToken = "";
      state.ppsDiscovery.error = error;
      showToast("나라장터 공고 검색 실패", humanizeError(error), "error");
    } finally {
      if (requestSequence !== state.ppsDiscovery.requestSequence) return;
      state.ppsDiscovery.loading = false;
      renderPpsDiscovery();
    }
  }

  function renderPpsDiscovery() {
    if (!els.ppsDiscoverySection) return;
    const query = els.searchInput.value.trim().replace(/\s+/g, " ");
    const noticeView = state.currentView !== "awards" && state.currentView !== "performance";
    const visible = state.noticeSearchMode === "pps" && noticeView;
    const suggestPps = Boolean(
      state.noticeSearchMode === "stored"
      && noticeView
      && state.source === "api"
      && !state.loading
      && query.length >= 2
      && state.notices.length === 0
    );
    els.ppsSearchSuggestion.hidden = !suggestPps;
    els.ppsDiscoverySection.hidden = !visible;
    if (!visible) return;
    if (state.ppsDiscovery.query && state.ppsDiscovery.query !== query && !state.ppsDiscovery.loading) {
      resetPpsDiscovery(query);
    }
    els.ppsDiscoveryQuery.textContent = query ? `검색어 · ${query}` : "검색어를 입력하세요";
    els.ppsDiscoverySearchButton.disabled = state.source !== "api" || state.ppsDiscovery.loading || state.ppsDiscovery.submitting || query.length < 2;
    els.ppsDiscoveryFromDate.disabled = state.ppsDiscovery.loading || state.ppsDiscovery.submitting;
    els.ppsDiscoveryToDate.disabled = state.ppsDiscovery.loading || state.ppsDiscovery.submitting;

    if (state.ppsDiscovery.submitting) {
      els.ppsDiscoverySearchButton.textContent = "실행 권한 확인 중…";
      els.ppsDiscoveryStatus.textContent = "나라장터 조회를 실행하기 전에 운영 기능 권한을 확인하고 있습니다.";
      return;
    }

    if (state.ppsDiscovery.loading) {
      els.ppsDiscoverySearchButton.textContent = "용역 공고 조회 중…";
      els.ppsDiscoveryStatus.textContent = "나라장터 용역 공고 API를 조회하고 있습니다. 아직 PAI에 저장하거나 분석·판단을 실행하지 않았습니다.";
      els.ppsDiscoveryResults.innerHTML = '<div class="pps-discovery__loading"><span class="spinner" aria-hidden="true"></span><span>외부 공고 목록을 확인하는 중입니다.</span></div>';
      return;
    }
    els.ppsDiscoverySearchButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m20 20-4.8-4.8" /></svg>나라장터 용역 공고 조회';
    if (state.ppsDiscovery.error) {
      els.ppsDiscoveryStatus.textContent = `나라장터 조회 실패 · ${humanizeError(state.ppsDiscovery.error)}`;
      els.ppsDiscoveryResults.replaceChildren();
      return;
    }
    if (!state.ppsDiscovery.searched) {
      els.ppsDiscoveryStatus.textContent = state.source !== "api"
        ? "PAI 서버 연결을 확인한 뒤 나라장터 조회를 실행할 수 있습니다."
        : query.length < 2
          ? "나라장터 용역 공고명 조회를 위해 검색어를 2자 이상 입력해 주세요."
          : "아래 버튼을 누르면 선택한 게시일 범위의 용역 공고명을 조회합니다. 버튼을 누르기 전에는 외부 조회를 시작하지 않습니다.";
      els.ppsDiscoveryResults.replaceChildren();
      return;
    }
    const count = state.ppsDiscovery.candidates.length;
    const suffix = state.ppsDiscovery.truncated ? " · 조회 상한까지 표시" : "";
    els.ppsDiscoveryStatus.textContent = count
      ? `나라장터 용역 공고 조회 결과 ${formatNumber(state.ppsDiscovery.resultCount)}건 · API ${formatNumber(state.ppsDiscovery.apiCalls)}회${suffix}`
      : `선택한 기간의 나라장터에도 일치 공고가 없습니다 · API ${formatNumber(state.ppsDiscovery.apiCalls)}회`;
    els.ppsDiscoveryResults.innerHTML = count
      ? state.ppsDiscovery.candidates.map(renderPpsCandidate).join("")
      : emptyPanel("나라장터 검색 결과가 없습니다", "검색어 또는 게시 날짜 범위를 바꿔 다시 확인해 보세요.");
  }

  function renderPpsCandidate(candidate, index) {
    const saving = state.ppsDiscovery.saving.has(candidate.selectionKey || `${candidate.bidNoticeNo}:${index}`);
    const stored = candidate.alreadyStored || Boolean(candidate.storedNoticeKey);
    const storedNotice = stored
      ? state.notices.find((notice) => notice.noticeKey === candidate.storedNoticeKey) || null
      : null;
    const completed = storedNotice?.analysisState === "EVALUATED"
      && storedNotice.analysisAttachmentCoverageComplete;
    const candidateDeadline = validDate(candidate.deadline);
    const ended = storedNotice
      ? noticeLifecycleStatus(storedNotice) !== "OPEN"
      : Boolean(candidateDeadline && candidateDeadline.getTime() < Date.now());
    const sourceLink = candidate.sourceUrl
      ? `<a class="pps-candidate__source-link" href="${escapeAttribute(candidate.sourceUrl)}" target="_blank" rel="noopener noreferrer">나라장터 원문 ↗</a>`
      : "";
    const detailHref = stored && candidate.storedNoticeKey
      ? noticeDetailHref(candidate.storedNoticeKey)
      : "";
    const stateLabel = !stored
      ? "현재 수집 공고 아님"
      : !storedNotice
        ? "저장 상태 확인"
        : completed
          ? "판단 완료"
          : ended
            ? "종료 · 판단 비활성"
            : "판단 필요";
    const stateClass = completed ? "is-complete" : ended ? "is-ended" : storedNotice ? "is-pending" : stored ? "is-stored-unknown" : "is-external";
    const title = detailHref
      ? `<a class="pps-candidate__title-link" href="${escapeAttribute(detailHref)}" data-stored-notice-link data-notice-key="${escapeAttribute(candidate.storedNoticeKey)}" aria-label="${escapeAttribute(candidate.title)} 저장된 공고 상세 보기">${escapeHtml(candidate.title)}</a>`
      : escapeHtml(candidate.title);
    const guidance = !stored
      ? "PAI에 저장하기 전에는 자격 판단과 정량 점수가 없습니다."
      : !storedNotice
        ? "정확히 저장된 공고입니다. 저장된 공고로 이동해 현재 판단 상태를 확인할 수 있습니다."
        : completed
          ? "저장된 공고의 자격·정량 판단 결과를 확인할 수 있습니다."
          : ended
            ? "종료 또는 취소된 공고입니다. 저장된 공고 이력은 상세 화면에서 확인할 수 있습니다."
            : "이미 저장된 공고입니다. 중복 저장하지 않고 기존 공고에서 판단을 계속할 수 있습니다.";
    const running = storedNotice && state.manualAnalysisRequests.get(storedNotice.noticeKey) === "running";
    // Stored search results can sit outside the board's currently loaded
    // lifecycle scope. Keep an explicit state-check action; the request path
    // hydrates the canonical notice before allowing an analysis.
    const availabilityNotice = storedNotice || {
      noticeKey: candidate.storedNoticeKey,
      sourceKind: "PPS",
      noticeStatus: ended ? "EXPIRED" : "OPEN",
      providerDisposition: candidate.noticeKind.includes("취소") ? "CANCELLED" : "",
      deadline: candidate.deadline,
      analysisState: "COLLECTED",
      analysisAttachmentCoverageComplete: false,
      analysisAttempted: false,
      analysisReasonCode: "",
    };
    const analysisAvailability = manualAnalysisAvailability(availabilityNotice, {
      stored,
      canonicalStateKnown: Boolean(storedNotice),
    });
    const detailLink = detailHref
      ? `<a class="button button--ghost pps-candidate__detail-link" href="${escapeAttribute(detailHref)}" data-stored-notice-link data-notice-key="${escapeAttribute(candidate.storedNoticeKey)}" aria-label="${escapeAttribute(candidate.title)} ${completed ? "저장된 판단 결과 보기" : "저장된 공고로 이동"}">${completed ? "저장된 판단 결과 보기" : "저장된 공고로 이동"} →</a>`
      : "";
    const analysisLabel = running ? "분석 중…" : analysisAvailability.label;
    const analysisDisabled = running || !analysisAvailability.enabled;
    const analysisButton = `<button class="button button--primary" type="button" data-pps-analysis-key="${escapeAttribute(storedNotice?.noticeKey || candidate.storedNoticeKey)}" ${analysisDisabled ? "disabled" : ""} title="${escapeAttribute(analysisAvailability.reason)}" aria-label="${escapeAttribute(candidate.title)} ${escapeAttribute(analysisLabel)}${analysisAvailability.enabled ? "" : ` · ${escapeAttribute(analysisAvailability.reason)}`}">${running ? '<span class="button-spinner" aria-hidden="true"></span>' : ""}${escapeHtml(analysisLabel)}</button>`;
    const saveButton = !stored
      ? `<button class="button button--pps" type="button" data-pps-save-index="${index}" ${saving || !candidate.saveable ? "disabled" : ""} aria-label="${escapeAttribute(candidate.title)} ${candidate.saveable ? "PAI에 저장" : "저장 불가"}">${escapeHtml(saving ? "저장 중…" : candidate.saveable ? "PAI에 저장" : "저장 불가")}</button>`
      : "";
    return `<article class="pps-candidate ${stored ? "is-stored" : ""} ${stateClass}" role="listitem" ${saving ? 'aria-busy="true"' : ""}>
      <div class="pps-candidate__head">
        <span class="pps-candidate__badges"><span class="pps-candidate__source-state">${stored ? "PAI 저장됨" : "나라장터 실시간"}</span><span class="pps-candidate__state">${escapeHtml(stateLabel)}</span></span>
        <span>${escapeHtml(candidate.noticeKind)}${candidate.relatedRevisionStored ? " · 다른 차수 저장됨" : ""}</span>
      </div>
      <h4>${title}</h4>
      <p>${escapeHtml(candidate.agency)} · 공고 ${escapeHtml(candidate.bidNoticeNo)}-${escapeHtml(candidate.revisionNo)}</p>
      <dl>
        <div><dt>게시</dt><dd>${escapeHtml(formatShortDateTime(candidate.publishedAt))}</dd></div>
        <div><dt>마감</dt><dd>${escapeHtml(formatShortDateTime(candidate.deadline))}</dd></div>
        <div><dt>추정금액</dt><dd>${escapeHtml(formatBudget(candidate.estimatedAmount))}</dd></div>
      </dl>
      <p class="pps-candidate__guidance">${escapeHtml(guidance)}</p>
      ${candidate.directContractSignal ? '<p class="pps-candidate__warning">수의계약 신호가 있어 저장 전 원문 확인이 필요합니다.</p>' : ""}
      ${candidate.saveBlockReason ? `<p class="pps-candidate__warning">${escapeHtml(candidate.saveBlockReason)}</p>` : ""}
      <footer>${sourceLink}<span class="pps-candidate__actions">${detailLink}${analysisButton}${saveButton}</span></footer>
      <small class="pps-candidate__cost">${analysisAvailability.enabled ? (analysisAvailability.recomputeCurrent ? "저장 근거 재판단 · 문서 분석 0회" : "분석 실행 전 문서 분석 범위를 확인") : escapeHtml(analysisAvailability.reason)}</small>
    </article>`;
  }

  function handlePpsDiscoveryAction(event) {
    const storedLink = event.target.closest("[data-stored-notice-link]");
    if (storedLink) {
      const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
      if (!modified) {
        event.preventDefault();
        const noticeKey = storedLink.dataset.noticeKey;
        if (noticeKey) void openDetail(noticeKey, storedLink);
      }
      return;
    }
    const analysisButton = event.target.closest("[data-pps-analysis-key]");
    if (analysisButton) {
      event.preventDefault();
      const noticeKey = analysisButton.dataset.ppsAnalysisKey;
      if (noticeKey) void requestManualAnalysis(noticeKey);
      return;
    }
    const button = event.target.closest("[data-pps-save-index]");
    if (!button) return;
    const index = Number(button.dataset.ppsSaveIndex);
    const candidate = state.ppsDiscovery.candidates[index];
    if (candidate) void savePpsCandidate(candidate, index);
  }

  async function savePpsCandidate(candidate, index) {
    if (!candidate.saveable || candidate.alreadyStored || !candidate.selectionKey) return;
    const savingKey = candidate.selectionKey || `${candidate.bidNoticeNo}:${index}`;
    if (state.ppsDiscovery.saving.has(savingKey)) return;
    const confirmed = window.confirm(`${candidate.title}\n\n이 공고 한 건을 PAI에 저장합니다. 저장만으로 문서 분석은 시작되지 않습니다.\n\n저장할까요?`);
    if (!confirmed) return;
    state.ppsDiscovery.saving.add(savingKey);
    renderPpsDiscovery();
    const authHeaders = await manualAnalysisAuthHeaders();
    if (!authHeaders) {
      state.ppsDiscovery.saving.delete(savingKey);
      renderPpsDiscovery();
      return;
    }
    let savedNoticeKey = "";
    try {
      const result = unwrapObject(await apiRequest("/pps-discovery/save", {
        method: "POST",
        headers: authHeaders,
        timeoutMs: EXTERNAL_PPS_REQUEST_TIMEOUT_MS,
        body: JSON.stringify({
          query: state.ppsDiscovery.query,
          from_date: state.ppsDiscovery.fromDate,
          to_date: state.ppsDiscovery.toDate,
          bid_notice_no: candidate.bidNoticeNo,
          selection_key: candidate.selectionKey,
        }),
      }));
      savedNoticeKey = stringValue(result.notice_key);
      if (!savedNoticeKey) throw new Error("저장된 공고 식별자가 응답에 없습니다.");
      candidate.alreadyStored = true;
      candidate.storedNoticeKey = savedNoticeKey;
      showToast("공고 저장 완료", stringValue(result.message, "저장만 완료했습니다. 저장된 공고 링크에서 분석·판단을 별도로 실행할 수 있습니다."), "success");
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      showToast("공고 저장 실패", humanizeError(error), "error");
    } finally {
      state.ppsDiscovery.saving.delete(savingKey);
      renderPpsDiscovery();
    }
    if (!savedNoticeKey) return;
    const reloadQuantitative = quantitativeEstimateIsVisible(savedNoticeKey);
    invalidateQuantitativeEstimate(savedNoticeKey, { forceReload: reloadQuantitative });
    try {
      await hydrateNoticeByKey(savedNoticeKey, { force: true });
      await refreshDashboardAfterMutation();
      renderKpis();
      renderNavigationCounts();
      renderPpsDiscovery();
    } catch (error) {
      showToast("저장은 완료되었습니다", `저장된 공고 상세 상태 새로고침 실패 · ${humanizeError(error)}`, "warning");
    }
    window.requestAnimationFrame(() => {
      const link = [...els.ppsDiscoveryResults.querySelectorAll("[data-stored-notice-link]")]
        .find((item) => item.dataset.noticeKey === savedNoticeKey);
      link?.focus();
    });
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
    els.rankingProfileVersion.textContent = catalog.version
      ? `키워드 기준 ${catalog.version} · 목록 제외 없음`
      : "키워드 기준 확인됨 · 목록 제외 없음";
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
    for (const [name, control] of performanceRangeControls()) {
      if (control.value !== "") params.set(name, control.value);
    }
    params.set("limit", String(state.performance.limit));
    params.set("offset", String(state.performance.offset));
    return `/performance?${params.toString()}`;
  }

  function performanceRangeControls() {
    return [
      ["date_from", els.performanceDateFrom], ["date_to", els.performanceDateTo],
      ["min_amount", els.performanceMinAmount], ["max_amount", els.performanceMaxAmount],
    ];
  }

  function validatePerformanceRanges(report = true) {
    const from = els.performanceDateFrom;
    const to = els.performanceDateTo;
    const min = els.performanceMinAmount;
    const max = els.performanceMaxAmount;
    to.setCustomValidity(from.value && to.value && from.value > to.value
      ? "종료일은 시작일 이후로 선택해 주세요." : "");
    max.setCustomValidity(min.value !== "" && max.value !== "" && Number(min.value) > Number(max.value)
      ? "최대 금액은 최소 금액 이상으로 입력해 주세요." : "");
    return report ? els.performanceFilterForm.reportValidity() : true;
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
      ...performanceRangeControls().map(([, control]) => control),
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
      || els.performanceDivisionFilter.value
      || performanceRangeControls().some(([, control]) => control.value !== ""),
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
        <footer><span>공개·비식별 자료</span><small>후보 조회용 · 인정실적/점수 미확정</small></footer>
        <div class="performance-certificate-preview">
          <button class="button button--ghost" type="button" disabled>실적증명서 · 연결 예정</button>
          <small>증명서 연결 후 내려받을 수 있습니다.</small>
        </div>
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
    if (error?.status === 422) {
      return "계약 기간과 금액 범위를 확인해 주세요. 시작값은 끝값보다 클 수 없습니다.";
    }
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
    performanceRangeControls().forEach(([, control]) => { control.value = ""; });
    validatePerformanceRanges(false);
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

  function renderPreSpecificationView() {
    renderStoredPreSpecifications();
    renderLivePreSpecifications();
    renderDataSource();
  }

  function normalizePreSpecification(raw = {}) {
    const analysisRaw = raw.analysis && typeof raw.analysis === "object" ? raw.analysis : null;
    const documents = arrayValue(raw.documents).map((item) => ({
      slot: numberOrNull(item?.slot),
      safeUrl: safeHttpUrl(item?.safe_url),
      sourceDigest: stringValue(item?.source_digest),
    }));
    return {
      preSpecificationKey: stringValue(raw.pre_specification_key),
      registryNo: stringValue(raw.registry_no),
      selectionToken: stringValue(raw.selection_token),
      title: stringValue(raw.title, "사전규격명 미확인"),
      orderingAgency: stringValue(raw.ordering_agency),
      demandAgency: stringValue(raw.demand_agency),
      businessDivision: stringValue(raw.business_division),
      budgetAmount: numberOrNull(raw.budget_amount),
      registeredAt: stringValue(raw.registered_at),
      changedAt: stringValue(raw.changed_at),
      opinionDeadline: stringValue(raw.opinion_deadline),
      deliveryDue: stringValue(raw.delivery_due),
      softwareBusiness: Boolean(raw.software_business),
      status: stringValue(raw.status, "OPINION_CLOSED").toUpperCase(),
      linkedBidNoticeNos: arrayValue(raw.linked_bid_notice_nos).map(String).filter(Boolean),
      matchedKeywords: arrayValue(raw.matched_keywords).map(String).filter(Boolean),
      documentCount: numberOrNull(raw.document_count) ?? (Array.isArray(raw.documents) ? documents.length : null),
      alreadyStored: Boolean(raw.already_stored),
      sourceDigest: stringValue(raw.source_digest),
      versionCount: numberOrNull(raw.version_count),
      currentVersion: numberOrNull(raw.current_version),
      documents,
      analysis: analysisRaw ? {
        analysisId: stringValue(analysisRaw.analysis_id),
        status: stringValue(analysisRaw.status).toUpperCase(),
        sourceDigest: stringValue(analysisRaw.source_digest),
        warnings: arrayValue(analysisRaw.warnings).map(String),
        completedAt: stringValue(analysisRaw.completed_at),
        result: analysisRaw.result && typeof analysisRaw.result === "object" ? analysisRaw.result : null,
        documents: arrayValue(analysisRaw.documents),
      } : null,
    };
  }

  function preSpecificationStatusLabel(value) {
    return ({ OPEN_FOR_OPINION: "의견 접수 중", OPINION_CLOSED: "의견 마감", LINKED_TO_BID: "입찰공고 연결" })[value] || "상태 미확인";
  }

  function preSpecificationAnalysisLabel(value) {
    return ({ RUNNING: "분석 중", QUEUED: "분석 대기", COMPLETED: "분석 완료", PARTIAL: "일부 분석", REVIEW: "상태 확인 필요", FAILED: "분석 실패", ALREADY_ANALYZED: "기존 분석 재사용", COOLDOWN: "재시도 대기" })[value] || "상태 확인 필요";
  }

  function preSpecificationAgency(record) {
    return record.demandAgency || record.orderingAgency || "기관 미확인";
  }

  function renderPreSpecificationCard(record, { source }) {
    const stored = source === "stored" || record.alreadyStored;
    const documentLabel = record.documentCount === null ? "문서 수 확인 중" : `문서 ${formatNumber(record.documentCount)}개`;
    const linkedLabel = record.linkedBidNoticeNos.length
      ? `연결입찰 ${record.linkedBidNoticeNos.slice(0, 2).join(", ")}${record.linkedBidNoticeNos.length > 2 ? ` 외 ${record.linkedBidNoticeNos.length - 2}건` : ""}`
      : "연결입찰 없음";
    const analysis = record.analysis?.status ? `<span class="prespec-analysis-badge prespec-analysis-badge--${escapeAttribute(record.analysis.status.toLowerCase())}">${escapeHtml(preSpecificationAnalysisLabel(record.analysis.status))}</span>` : "";
    const action = stored
      ? `<button class="button button--secondary" type="button" data-prespec-detail="${escapeAttribute(record.registryNo)}">저장본 상세</button>`
      : `<button class="button button--prespec" type="button" data-prespec-save="${escapeAttribute(record.registryNo)}" ${state.prespec.live.saving.has(record.registryNo) ? "disabled" : ""}>${state.prespec.live.saving.has(record.registryNo) ? '<span class="button-spinner" aria-hidden="true"></span>저장 중' : "선택 저장"}</button>`;
    return `<article class="prespec-card prespec-card--${escapeAttribute(source)}" role="listitem">
      <div class="prespec-card__head"><span class="prespec-status prespec-status--${escapeAttribute(record.status.toLowerCase())}">${escapeHtml(preSpecificationStatusLabel(record.status))}</span>${analysis}<small>${escapeHtml(record.registryNo)}</small></div>
      <h4>${escapeHtml(record.title)}</h4>
      <p>${escapeHtml(preSpecificationAgency(record))}</p>
      <dl><div><dt>의견마감</dt><dd>${escapeHtml(formatShortDateTime(record.opinionDeadline))}</dd></div><div><dt>예산</dt><dd>${escapeHtml(formatBudget(record.budgetAmount))}</dd></div><div><dt>첨부</dt><dd>${escapeHtml(documentLabel)}</dd></div><div><dt>저장상태</dt><dd>${stored ? "PAI 저장됨" : "미저장"}</dd></div></dl>
      <div class="prespec-linked ${record.linkedBidNoticeNos.length ? "is-linked" : ""}">${escapeHtml(linkedLabel)}</div>
      <footer><small>${source === "stored" ? "저장 자료 · 외부 조회 0회 · 문서 분석 0회" : "나라장터 조회 · 문서 분석 0회"}</small>${action}</footer>
    </article>`;
  }

  async function loadStoredPreSpecifications({ force = false } = {}) {
    const stored = state.prespec.stored;
    if (stored.loading || (stored.loaded && !force)) return;
    const sequence = ++stored.requestSequence;
    stored.loading = true;
    stored.error = null;
    renderStoredPreSpecifications();
    const params = new URLSearchParams({ limit: "50" });
    const keywords = els.prespecStoredSearchInput.value.trim();
    const statusFilter = els.prespecStoredStatusFilter.value;
    if (keywords) params.set("search_keywords", keywords);
    if (statusFilter) params.set("status", statusFilter);
    try {
      const payload = unwrapObject(await apiRequest(`/pre-specifications?${params}`));
      if (sequence !== stored.requestSequence) return;
      stored.records = arrayValue(payload.items).map(normalizePreSpecification);
      stored.truncated = Boolean(payload.truncated);
      stored.loaded = true;
      renderStoredPreSpecifications();
      void enrichStoredPreSpecificationDetails(stored.records, sequence);
    } catch (error) {
      if (sequence !== stored.requestSequence) return;
      stored.error = error;
      stored.loaded = true;
      renderStoredPreSpecifications();
    } finally {
      if (sequence === stored.requestSequence) {
        stored.loading = false;
        renderStoredPreSpecifications();
      }
    }
  }

  async function enrichStoredPreSpecificationDetails(records, sequence) {
    for (let offset = 0; offset < records.length; offset += 6) {
      const batch = records.slice(offset, offset + 6);
      const results = await Promise.allSettled(batch.map(async (record) => {
        const cached = state.prespec.details.get(record.registryNo);
        if (cached?.sourceDigest === record.sourceDigest) return cached;
        const payload = await apiRequest(`/pre-specifications/${encodeURIComponent(record.registryNo)}`);
        const detail = normalizePreSpecification(unwrapObject(payload));
        state.prespec.details.set(record.registryNo, detail);
        return detail;
      }));
      if (sequence !== state.prespec.stored.requestSequence) return;
      results.forEach((result, index) => {
        if (result.status !== "fulfilled") return;
        batch[index].documentCount = result.value.documentCount;
        batch[index].analysis = result.value.analysis;
      });
      renderStoredPreSpecifications();
    }
  }

  function renderStoredPreSpecifications() {
    const stored = state.prespec.stored;
    els.prespecStoredSubmitButton.disabled = stored.loading;
    if (stored.loading) {
      els.prespecStoredSummary.textContent = "저장 자료를 검색하고 있습니다. 외부 조회 0회 · 문서 분석 0회";
      els.prespecStoredState.hidden = false;
      els.prespecStoredState.innerHTML = '<span class="spinner" aria-hidden="true"></span><strong>저장 자료를 확인하고 있습니다</strong>';
    } else if (stored.error) {
      els.prespecStoredSummary.textContent = "저장 자료 검색에 실패했습니다.";
      els.prespecStoredState.hidden = false;
      els.prespecStoredState.innerHTML = `<strong>저장된 사전규격을 불러오지 못했습니다</strong><p>${escapeHtml(editorErrorMessage(stored.error))}</p>`;
    } else if (stored.loaded && !stored.records.length) {
      els.prespecStoredSummary.textContent = "조건에 맞는 저장 사전규격이 없습니다. 외부 조회 0회 · 문서 분석 0회";
      els.prespecStoredState.hidden = false;
      els.prespecStoredState.innerHTML = "<strong>저장 결과가 없습니다</strong><p>나라장터 사전규격 검색에서 필요한 건을 선택 저장할 수 있습니다.</p>";
    } else {
      els.prespecStoredSummary.textContent = stored.loaded
        ? `저장 자료 ${formatNumber(stored.records.length)}건${stored.truncated ? " · 표시 상한 도달" : ""} · 외부 조회 0회 · 문서 분석 0회`
        : "저장된 사전규격을 불러오는 중입니다.";
      els.prespecStoredState.hidden = Boolean(stored.records.length);
    }
    els.prespecStoredList.innerHTML = stored.records.map((record) => renderPreSpecificationCard(record, { source: "stored" })).join("");
    renderDataSource();
  }

  async function searchLivePreSpecifications(event) {
    event.preventDefault();
    const live = state.prespec.live;
    if (live.loading) return;
    const query = els.prespecLiveQuery.value.trim().replace(/\s+/g, " ");
    const fromDate = els.prespecLiveFromDate.value;
    const toDate = els.prespecLiveToDate.value;
    const span = dateSpanDays(fromDate, toDate);
    if (query.length < 2) {
      showToast("검색어 확인 필요", "나라장터 검색어를 2자 이상 입력해 주세요.", "error");
      return;
    }
    if (span === null || span < 0 || span > 30) {
      showToast("검색 기간 확인 필요", "사전규격 검색은 한 번에 최대 31일입니다.", "error");
      return;
    }
    const headers = await manualAnalysisAuthHeaders();
    if (!headers) return;
    const sequence = ++live.requestSequence;
    live.loading = true;
    live.error = null;
    live.query = query;
    live.fromDate = fromDate;
    live.toDate = toDate;
    live.limit = Number(els.prespecLiveLimit.value) || 25;
    renderLivePreSpecifications();
    try {
      const payload = unwrapObject(await apiRequest("/prespec-discovery/search", {
        method: "POST",
        headers,
        timeoutMs: EXTERNAL_PPS_REQUEST_TIMEOUT_MS,
        body: JSON.stringify({ query, from_date: fromDate, to_date: toDate, limit: live.limit }),
      }));
      if (sequence !== live.requestSequence) return;
      live.records = arrayValue(payload.candidates).map(normalizePreSpecification);
      live.apiCalls = numberOrNull(payload.api_calls) ?? 0;
      live.fetched = numberOrNull(payload.fetched) ?? 0;
      live.truncated = Boolean(payload.truncated);
      live.warnings = arrayValue(payload.warnings).map(String);
      live.searched = true;
      renderLivePreSpecifications();
    } catch (error) {
      if (sequence !== live.requestSequence) return;
      if (error?.status === 401) state.manualAnalysisToken = "";
      live.error = error;
      live.searched = true;
      renderLivePreSpecifications();
    } finally {
      if (sequence === live.requestSequence) {
        live.loading = false;
        renderLivePreSpecifications();
      }
    }
  }

  function renderLivePreSpecifications() {
    const live = state.prespec.live;
    els.prespecLiveSearchButton.disabled = live.loading;
    if (live.loading) {
      els.prespecLiveSearchButton.innerHTML = '<span class="button-spinner" aria-hidden="true"></span>검색 중';
      els.prespecLiveSummary.textContent = "나라장터 사전규격을 조회하고 있습니다. 문서 분석 0회";
      els.prespecLiveState.hidden = false;
      els.prespecLiveState.innerHTML = '<span class="spinner" aria-hidden="true"></span><strong>나라장터를 조회하고 있습니다</strong><p>검색 결과는 자동 저장되지 않습니다.</p>';
    } else {
      els.prespecLiveSearchButton.textContent = "나라장터 검색";
      if (live.error) {
        els.prespecLiveSummary.textContent = "나라장터 사전규격 검색에 실패했습니다.";
        els.prespecLiveState.hidden = false;
        els.prespecLiveState.innerHTML = `<strong>검색 결과를 불러오지 못했습니다</strong><p>${escapeHtml(editorErrorMessage(live.error))}</p>`;
      } else if (live.searched && !live.records.length) {
        els.prespecLiveSummary.textContent = `검색 결과 0건 · 나라장터 조회 ${formatNumber(live.apiCalls)}회 · 문서 분석 0회`;
        els.prespecLiveState.hidden = false;
        els.prespecLiveState.innerHTML = "<strong>검색 결과가 없습니다</strong><p>검색어 또는 최대 31일의 등록 기간을 바꿔 보세요.</p>";
      } else if (live.searched) {
        els.prespecLiveSummary.textContent = `검색 결과 ${formatNumber(live.records.length)}건 · 나라장터 조회 ${formatNumber(live.apiCalls)}회 · 문서 분석 0회${live.truncated ? " · 일부 결과" : ""}`;
        els.prespecLiveState.hidden = true;
      } else {
        els.prespecLiveSummary.textContent = "버튼을 누르기 전에는 나라장터 API를 호출하지 않습니다.";
        els.prespecLiveState.hidden = false;
      }
    }
    els.prespecLiveList.innerHTML = live.records.map((record) => renderPreSpecificationCard(record, { source: "live" })).join("");
    renderDataSource();
  }

  function handlePreSpecificationAction(event) {
    const detailButton = event.target.closest("[data-prespec-detail]");
    if (detailButton) {
      void openPreSpecificationDetail(detailButton.dataset.prespecDetail);
      return;
    }
    const saveButton = event.target.closest("[data-prespec-save]");
    if (saveButton) void saveLivePreSpecification(saveButton.dataset.prespecSave);
  }

  async function saveLivePreSpecification(registryNo) {
    const live = state.prespec.live;
    const record = live.records.find((item) => item.registryNo === registryNo);
    if (!record || live.saving.has(registryNo)) return;
    const headers = await manualAnalysisAuthHeaders();
    if (!headers) return;
    live.saving.add(registryNo);
    renderLivePreSpecifications();
    try {
      const response = unwrapObject(await apiRequest("/prespec-discovery/save", {
        method: "POST",
        headers,
        timeoutMs: EXTERNAL_PPS_REQUEST_TIMEOUT_MS,
        body: JSON.stringify({
          query: live.query,
          from_date: live.fromDate,
          to_date: live.toDate,
          limit: live.limit,
          registry_no: record.registryNo,
          selection_token: record.selectionToken,
        }),
      }));
      record.alreadyStored = true;
      state.prespec.details.delete(registryNo);
      showToast("사전규격 저장 완료", `${record.title} · 저장됨 · 문서 분석 0회`, "success");
      await loadStoredPreSpecifications({ force: true });
      await openPreSpecificationDetail(registryNo, { force: true });
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      showToast("사전규격 저장 실패", editorErrorMessage(error), "error");
    } finally {
      live.saving.delete(registryNo);
      renderLivePreSpecifications();
    }
  }

  function openPreSpecificationHelp() {
    state.prespec.helpTrigger = document.activeElement;
    els.prespecHelpButton.setAttribute("aria-expanded", "true");
    els.prespecHelpDialog.showModal();
  }

  function restorePreSpecificationHelpFocus() {
    els.prespecHelpButton.setAttribute("aria-expanded", "false");
    const trigger = state.prespec.helpTrigger;
    state.prespec.helpTrigger = null;
    if (trigger?.isConnected) trigger.focus();
  }

  async function openPreSpecificationDetail(registryNo, { force = false } = {}) {
    if (!registryNo) return;
    const cached = state.prespec.details.get(registryNo);
    if (!els.prespecDetailDialog.open) els.prespecDetailDialog.showModal();
    if (cached && !force) {
      state.prespec.selectedDetail = cached;
      renderPreSpecificationDetail(cached);
      return;
    }
    state.prespec.detailLoading = true;
    els.prespecDetailTitle.textContent = "사전규격 상세";
    els.prespecDetailMeta.textContent = registryNo;
    els.prespecDetailBody.innerHTML = '<div class="prespec-detail-loading"><span class="spinner" aria-hidden="true"></span><strong>저장본 상세를 불러오고 있습니다</strong></div>';
    els.prespecAnalysisButton.disabled = true;
    try {
      const payload = unwrapObject(await apiRequest(`/pre-specifications/${encodeURIComponent(registryNo)}`));
      const detail = normalizePreSpecification(payload);
      state.prespec.details.set(registryNo, detail);
      state.prespec.selectedDetail = detail;
      state.prespec.detailLoading = false;
      renderPreSpecificationDetail(detail);
    } catch (error) {
      els.prespecDetailBody.innerHTML = `<div class="prespec-detail-error"><strong>상세를 불러오지 못했습니다</strong><p>${escapeHtml(editorErrorMessage(error))}</p></div>`;
      els.prespecAnalysisStatus.innerHTML = "<p>저장본을 확인한 뒤 분석할 수 있습니다.</p>";
    } finally {
      state.prespec.detailLoading = false;
      if (state.prespec.selectedDetail?.registryNo === registryNo) {
        renderPreSpecificationAnalysisStatus(state.prespec.selectedDetail);
      }
    }
  }

  function closePreSpecificationDetail() {
    if (els.prespecDetailDialog.open) els.prespecDetailDialog.close();
    state.prespec.selectedDetail = null;
  }

  function renderPreSpecificationDetail(detail) {
    const documents = detail.documents;
    const documentMarkup = documents.length
      ? documents.map((document) => {
        const label = `첨부 ${formatNumber(document.slot ?? 0)}`;
        return document.safeUrl
          ? `<a href="${escapeAttribute(document.safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)} 원문</a>`
          : `<span>${escapeHtml(label)} · 안전 링크 미확인</span>`;
      }).join("")
      : "<span>저장된 공개 첨부가 없습니다.</span>";
    const keywords = detail.matchedKeywords.length
      ? detail.matchedKeywords.map((value) => `<span>${escapeHtml(value)}</span>`).join("")
      : "<span>저장 키워드 없음</span>";
    els.prespecDetailTitle.textContent = detail.title;
    els.prespecDetailMeta.textContent = `${detail.registryNo} · ${preSpecificationStatusLabel(detail.status)}`;
    els.prespecDetailBody.innerHTML = `<div class="prespec-detail-facts">
        <div><span>기관</span><strong>${escapeHtml(preSpecificationAgency(detail))}</strong></div><div><span>의견마감</span><strong>${escapeHtml(formatShortDateTime(detail.opinionDeadline))}</strong></div><div><span>예산</span><strong>${escapeHtml(formatBudget(detail.budgetAmount))}</strong></div><div><span>문서</span><strong>${formatNumber(documents.length)}개</strong></div><div><span>저장 버전</span><strong>${detail.currentVersion ? `v${formatNumber(detail.currentVersion)}` : "—"}</strong></div><div><span>연결입찰</span><strong>${escapeHtml(detail.linkedBidNoticeNos.join(", ") || "없음")}</strong></div>
      </div><div class="prespec-detail-keywords">${keywords}</div><div class="prespec-detail-documents"><h3>저장 문서</h3>${documentMarkup}</div>${renderPreSpecificationAnalysisResult(detail.analysis)}`;
    renderPreSpecificationAnalysisStatus(detail);
  }

  function renderPreSpecificationAnalysisResult(analysis) {
    if (!analysis?.result) return "";
    const extractions = arrayValue(analysis.result.extractions);
    const summaries = extractions.map((item) => stringValue(item?.summary)).filter(Boolean);
    const requirements = extractions.flatMap((item) => arrayValue(item?.requirements)).slice(0, 12);
    const requirementMarkup = requirements.length
      ? `<ul>${requirements.map((item) => `<li><strong>${escapeHtml(stringValue(item?.normalized_condition, "요구조건"))}</strong><span>${escapeHtml(requirementCategoryLabel(item?.category))}${item?.mandatory ? " · 필수" : ""}</span></li>`).join("")}</ul>`
      : "<p>구조화된 요구조건이 없거나 사람 검토가 필요합니다.</p>";
    const documentAudits = arrayValue(analysis.documents);
    const completion = derivePreSpecificationCompletion({
      documentsTotal: numberOrNull(analysis.result.documents_total) ?? documentAudits.length,
      documentsAccepted: numberOrNull(analysis.result.documents_accepted),
      documentsProcessed: numberOrNull(analysis.result.documents_processed),
      fallbackProcessed: documentAudits.length,
      declaredComplete: analysis.status === "COMPLETED",
    });
    const resultHeading = completion.completed ? "완료된 분석 결과" : "분석 상태 확인 필요";
    const auditStatusLabels = { ACCEPTED: "확인 완료", COMPLETED: "확인 완료", REVIEW: "확인 필요", PARTIAL: "일부 확인", FAILED: "처리 실패" };
    const auditMarkup = documentAudits.length
      ? `<div class="prespec-analysis-audits">${documentAudits.map((item) => {
        const status = stringValue(item?.status).toUpperCase();
        const reason = ANALYSIS_REASON_LABELS[stringValue(item?.reason_code).toUpperCase()] || "문서 근거 확인";
        return `<span>${escapeHtml(`첨부 ${numberOrNull(item?.slot) ?? "—"} · ${auditStatusLabels[status] || "상태 확인 필요"} · ${reason}`)}</span>`;
      }).join("")}</div>`
      : "";
    return `<section class="prespec-analysis-result"><div class="prespec-analysis-result__head"><h3>${resultHeading}</h3><span>확인 완료 ${formatNumber(completion.documentsAccepted)} / 전체 ${formatNumber(completion.documentsTotal)} 문서</span></div>${summaries.map((summary) => `<p>${escapeHtml(summary)}</p>`).join("")}${requirementMarkup}${auditMarkup}<p class="prespec-analysis-boundary">요구조건 사전 구조화 결과이며 입찰 GO/NO-GO 판정이 아닙니다.</p></section>`;
  }

  function derivePreSpecificationCompletion({ documentsTotal, documentsAccepted, documentsProcessed, fallbackProcessed = 0, declaredComplete = false }) {
    const total = Math.max(numberOrNull(documentsTotal) ?? 0, 0);
    const accepted = numberOrNull(documentsAccepted);
    const processed = numberOrNull(documentsProcessed) ?? accepted ?? Math.max(numberOrNull(fallbackProcessed) ?? 0, 0);
    const coverageComplete = total > 0 && processed >= total && accepted !== null && accepted >= total;
    return {
      documentsTotal: total,
      documentsAccepted: accepted ?? 0,
      documentsProcessed: processed,
      coverageComplete,
      completed: Boolean(declaredComplete && coverageComplete),
    };
  }

  function requirementCategoryLabel(value) {
    return ({
      ENTITY: "업체 요건",
      INDUSTRY_CODE: "업종 요건",
      CERTIFICATION: "인증 요건",
      DIRECT_PRODUCTION: "직접생산확인",
      REGION: "지역 요건",
      PERFORMANCE: "수행실적",
      PERSONNEL: "투입인력",
      FACILITY: "시설·장비",
      CONSORTIUM: "공동수급",
      SANCTION: "제재 여부",
      SUBMISSION: "제출 요건",
      OTHER: "기타 요건",
    })[String(value || "").toUpperCase()] || "분류 확인 필요";
  }

  function renderPreSpecificationAnalysisStatus(detail) {
    const active = state.prespec.analysis.registryNo === detail.registryNo ? state.prespec.analysis : null;
    const response = active?.response;
    const statusValue = stringValue(response?.outcome || detail.analysis?.status).toUpperCase();
    const polling = Boolean(active?.polling);
    const storedResult = detail.analysis?.result || {};
    const documentsTotal = numberOrNull(response?.documents_total) ?? numberOrNull(storedResult.documents_total) ?? detail.documents.length;
    const documentsAccepted = numberOrNull(response?.documents_accepted) ?? numberOrNull(storedResult.documents_accepted);
    const documentsProcessed = numberOrNull(response?.documents_processed) ?? numberOrNull(storedResult.documents_processed);
    const analysisCalls = numberOrNull(response?.openai_calls) ?? 0;
    const declaredComplete = detail.analysis?.status === "COMPLETED" || ["COMPLETED", "ALREADY_ANALYZED"].includes(statusValue);
    const completion = derivePreSpecificationCompletion({
      documentsTotal,
      documentsAccepted,
      documentsProcessed,
      fallbackProcessed: arrayValue(detail.analysis?.documents).length,
      declaredComplete,
    });
    const displayStatus = declaredComplete && !completion.coverageComplete ? "REVIEW" : statusValue;
    const message = declaredComplete && !completion.coverageComplete
      ? `전체 ${formatNumber(completion.documentsTotal)}개 문서 중 처리 ${formatNumber(completion.documentsProcessed)}개·확인 완료 ${formatNumber(completion.documentsAccepted)}개로 완료 조건과 맞지 않습니다.`
      : stringValue(response?.message) || (detail.analysis ? "저장된 최신 분석 상태입니다." : "분석은 자동 실행되지 않습니다. 필요할 때만 비용 상한을 확인하고 실행하세요.");
    els.prespecAnalysisStatus.innerHTML = `<div class="prespec-analysis-status ${polling ? "is-polling" : ""}">${polling ? '<span class="spinner" aria-hidden="true"></span>' : ""}<div><strong>${escapeHtml(preSpecificationAnalysisLabel(displayStatus))}</strong><p>${escapeHtml(message)}</p><small>처리 문서 ${formatNumber(completion.documentsProcessed)}/${formatNumber(completion.documentsTotal)} · 분석 요청 ${formatNumber(analysisCalls)}회${polling ? ` · 상태 확인 ${formatNumber(active.polls)}/${PRESPEC_ANALYSIS_MAX_POLLS}` : ""}</small></div></div>`;
    els.prespecAnalysisButton.disabled = state.prespec.detailLoading || polling || completion.completed;
    els.prespecAnalysisButton.textContent = completion.completed ? "분석 결과 저장됨" : polling ? "분석 처리 중" : detail.analysis ? "문서 다시 분석" : "문서 분석 실행";
  }

  function confirmPreSpecificationAnalysis(detail) {
    return window.confirm(`${detail.title}\n\n사전규격 첨부문서를 분석합니다.\n- 현재 저장 문서 ${formatNumber(detail.documents.length)}개\n- 문서당 최대 2회\n- 사전규격 1건당 총 최대 10회\n- 중복 실행 잠금과 공고별 재시도 대기 적용\n- 결과는 요구조건 구조화이며 GO 판정이 아님\n\n분석을 시작할까요?`);
  }

  async function requestPreSpecificationAnalysis() {
    const detail = state.prespec.selectedDetail;
    if (!detail || state.prespec.analysis.polling || !confirmPreSpecificationAnalysis(detail)) return;
    const headers = await manualAnalysisAuthHeaders();
    if (!headers) return;
    els.prespecAnalysisButton.disabled = true;
    try {
      const response = unwrapObject(await apiRequest(`/pre-specifications/${encodeURIComponent(detail.registryNo)}/analysis`, {
        method: "POST",
        headers,
        timeoutMs: 30000,
        body: JSON.stringify({ run_extraction: true }),
      }));
      state.prespec.analysis = { registryNo: detail.registryNo, analysisId: stringValue(response.analysis_id), polling: response.outcome === "QUEUED", polls: 0, response };
      renderPreSpecificationAnalysisStatus(detail);
      if (response.outcome === "QUEUED" && response.analysis_id) {
        void pollPreSpecificationAnalysis(detail.registryNo, response.analysis_id, headers);
      } else {
        await refreshPreSpecificationDetailAfterAnalysis(detail.registryNo);
        showToast("사전규격 분석 상태", stringValue(response.message, preSpecificationAnalysisLabel(response.outcome)), response.outcome === "FAILED" ? "error" : "success");
      }
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      showToast("사전규격 분석 요청 실패", editorErrorMessage(error), "error");
      if (state.prespec.selectedDetail) renderPreSpecificationAnalysisStatus(state.prespec.selectedDetail);
    }
  }

  async function pollPreSpecificationAnalysis(registryNo, analysisId, headers) {
    const deadline = Date.now() + PRESPEC_ANALYSIS_POLL_MAX_MS;
    for (let attempt = 1; attempt <= PRESPEC_ANALYSIS_MAX_POLLS && Date.now() < deadline; attempt += 1) {
      await delay(Math.min(PRESPEC_ANALYSIS_POLL_INTERVAL_MS, Math.max(0, deadline - Date.now())));
      if (Date.now() >= deadline) break;
      const active = state.prespec.analysis;
      if (active.registryNo !== registryNo || active.analysisId !== analysisId || !active.polling) return;
      active.polls = attempt;
      try {
        const response = unwrapObject(await apiRequest(`/pre-specifications/${encodeURIComponent(registryNo)}/analysis/${encodeURIComponent(analysisId)}`, { headers, timeoutMs: 15000 }));
        active.response = response;
        active.polling = response.outcome === "QUEUED";
        if (state.prespec.selectedDetail?.registryNo === registryNo && els.prespecDetailDialog.open) renderPreSpecificationAnalysisStatus(state.prespec.selectedDetail);
        if (!active.polling) {
          await refreshPreSpecificationDetailAfterAnalysis(registryNo);
          showToast("사전규격 분석 완료", stringValue(response.message, preSpecificationAnalysisLabel(response.outcome)), ["FAILED", "REVIEW"].includes(response.outcome) ? "error" : "success");
          return;
        }
      } catch (error) {
        if (error?.status === 401) state.manualAnalysisToken = "";
        active.polling = false;
        if (state.prespec.selectedDetail?.registryNo === registryNo) renderPreSpecificationAnalysisStatus(state.prespec.selectedDetail);
        showToast("사전규격 분석 상태 확인 실패", editorErrorMessage(error), "error");
        return;
      }
    }
    const active = state.prespec.analysis;
    if (active.registryNo === registryNo && active.analysisId === analysisId) {
      active.polling = false;
      active.response = { ...active.response, outcome: "QUEUED", message: "약 2분 동안 상태를 확인했습니다. 탭을 새로고침하지 않아도 저장본 상세에서 다시 확인할 수 있습니다." };
      if (state.prespec.selectedDetail?.registryNo === registryNo) renderPreSpecificationAnalysisStatus(state.prespec.selectedDetail);
      showToast("분석이 계속 진행 중입니다", "자동 상태 확인은 종료했지만 서버 작업은 계속될 수 있습니다.", "error");
    }
  }

  async function refreshPreSpecificationDetailAfterAnalysis(registryNo) {
    try {
      const payload = unwrapObject(await apiRequest(`/pre-specifications/${encodeURIComponent(registryNo)}`));
      const detail = normalizePreSpecification(payload);
      state.prespec.details.set(registryNo, detail);
      const storedRecord = state.prespec.stored.records.find((item) => item.registryNo === registryNo);
      if (storedRecord) {
        storedRecord.documentCount = detail.documentCount;
        storedRecord.analysis = detail.analysis;
      }
      if (state.prespec.selectedDetail?.registryNo === registryNo) {
        state.prespec.selectedDetail = detail;
        renderPreSpecificationDetail(detail);
      }
      renderStoredPreSpecifications();
    } catch (_error) {
      // The completed polling response remains visible even if the detail
      // projection cannot be refreshed immediately.
    }
  }

  async function loadPerformanceEditor({ force = false } = {}) {
    if (state.performanceEditor.loading || (state.performanceEditor.loaded && !force)) return;
    const headers = await manualAnalysisAuthHeaders();
    if (!headers) return;
    state.performanceEditor.loading = true;
    els.performanceEditorUnlockButton.disabled = true;
    els.performanceEditorState.hidden = false;
    els.performanceEditorState.innerHTML = '<span class="spinner" aria-hidden="true"></span><strong>운영 실적을 불러오고 있습니다</strong>';
    try {
      const payload = unwrapObject(await apiRequest("/performance-records?limit=200", { headers }));
      state.performanceEditor.records = arrayValue(payload.records).map(normalizeEditablePerformance);
      state.performanceEditor.total = Math.max(numberOrNull(payload.total) ?? state.performanceEditor.records.length, 0);
      state.performanceEditor.loaded = true;
      renderPerformanceEditor();
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      els.performanceEditorState.hidden = false;
      els.performanceEditorState.innerHTML = `<strong>운영 실적을 불러오지 못했습니다</strong><p>${escapeHtml(editorErrorMessage(error))}</p>`;
      showToast("운영 실적 조회 실패", editorErrorMessage(error), "error");
    } finally {
      state.performanceEditor.loading = false;
      els.performanceEditorUnlockButton.disabled = false;
    }
  }

  function normalizeEditablePerformance(raw = {}) {
    return {
      id: stringValue(raw.id), recordKey: stringValue(raw.record_key), recordStatus: stringValue(raw.record_status, "DRAFT").toUpperCase(),
      projectName: stringValue(raw.project_name), agency: stringValue(raw.agency), division: stringValue(raw.division), overview: stringValue(raw.overview),
      contractDate: stringValue(raw.contract_date), startDate: stringValue(raw.start_date), endDate: stringValue(raw.end_date),
      contractAmount: numberOrNull(raw.contract_amount), vatBasis: stringValue(raw.vat_basis, "UNKNOWN").toUpperCase(),
      completed: Boolean(raw.completed), sharePct: numberOrNull(raw.share_pct) ?? 100,
      certificateStatus: stringValue(raw.certificate_status, "NOT_REQUESTED").toUpperCase(),
      evidenceReference: stringValue(raw.evidence_reference), keywords: arrayValue(raw.keywords).map(String),
      revision: numberOrNull(raw.revision) ?? 1, updatedAt: stringValue(raw.updated_at),
    };
  }

  function renderPerformanceEditor() {
    const editor = state.performanceEditor;
    els.performanceEditorCreateButton.hidden = !editor.loaded;
    els.performanceEditorUnlockButton.textContent = editor.loaded ? "목록 새로고침" : "운영 실적 열기";
    els.performanceEditorSummary.textContent = editor.loaded
      ? `직접 등록 실적 ${formatNumber(editor.total)}건 · 공개 실적 자료 ${formatNumber(state.performance.summary?.recordCount || 0)}건과 별도 관리`
      : "공개 실적 자료와 분리된 운영 실적입니다. 운영 키로 초안을 등록하거나 검증·보관 상태를 관리하세요.";
    els.performanceEditorState.hidden = editor.records.length > 0;
    if (!editor.records.length && editor.loaded) {
      els.performanceEditorState.innerHTML = "<strong>직접 등록한 실적이 없습니다</strong><p>새 실적 등록으로 초안을 만든 뒤 근거를 검토해 검증 완료하세요.</p>";
    }
    els.performanceEditorList.innerHTML = editor.records.map((record, index) => `
      <article class="operator-record" role="listitem">
        <div><span class="record-status record-status--${escapeAttribute(record.recordStatus.toLowerCase())}">${escapeHtml(recordStatusLabel(record.recordStatus))}</span><small>rev.${formatNumber(record.revision)}</small></div>
        <h4>${escapeHtml(record.projectName)}</h4>
        <p>${escapeHtml(record.agency || "발주기관 미입력")} · ${escapeHtml(record.division || "수행부서 미입력")}</p>
        <dl><div><dt>계약일</dt><dd>${escapeHtml(formatPerformanceDate(record.contractDate))}</dd></div><div><dt>계약금액</dt><dd>${escapeHtml(formatBudget(record.contractAmount))}</dd></div><div><dt>수행</dt><dd>${record.completed ? "완료" : "진행/미확인"} · 지분 ${formatScore(record.sharePct)}%</dd></div></dl>
        <footer><small>${escapeHtml(record.evidenceReference || "근거 참조 미입력")}</small><button class="button button--secondary" type="button" data-edit-performance="${index}">수정</button></footer>
      </article>`).join("");
  }

  function handlePerformanceEditorAction(event) {
    const button = event.target.closest("[data-edit-performance]");
    if (!button) return;
    const record = state.performanceEditor.records[Number(button.dataset.editPerformance)];
    if (record) openPerformanceRecordDialog(record);
  }

  function openPerformanceRecordDialog(record = null) {
    state.performanceEditor.editingRecord = record;
    els.performanceRecordDialogTitle.textContent = record ? "회사 실적 수정" : "회사 실적 등록";
    els.performanceRecordProject.value = record?.projectName || "";
    els.performanceRecordAgency.value = record?.agency || "";
    els.performanceRecordDivision.value = record?.division || "";
    els.performanceRecordStatus.value = record?.recordStatus || "DRAFT";
    els.performanceRecordContractDate.value = record?.contractDate || "";
    els.performanceRecordStartDate.value = record?.startDate || "";
    els.performanceRecordEndDate.value = record?.endDate || "";
    els.performanceRecordAmount.value = record?.contractAmount ?? "";
    els.performanceRecordVat.value = record?.vatBasis || "UNKNOWN";
    els.performanceRecordShare.value = record?.sharePct ?? 100;
    els.performanceRecordCertificate.value = record?.certificateStatus || "NOT_REQUESTED";
    els.performanceRecordCompleted.checked = Boolean(record?.completed);
    els.performanceRecordEvidence.value = record?.evidenceReference || "";
    els.performanceRecordKeywords.value = arrayValue(record?.keywords).join(", ");
    els.performanceRecordOverview.value = record?.overview || "";
    els.performanceRecordDialog.dataset.requestKey = record ? "" : newIdempotencyKey("performance");
    els.performanceRecordSaveButton.textContent = record ? "수정 저장" : "초안 저장";
    els.performanceRecordDialog.showModal();
    window.requestAnimationFrame(() => els.performanceRecordProject.focus());
  }

  function closePerformanceRecordDialog() {
    if (els.performanceRecordDialog.open) els.performanceRecordDialog.close();
    state.performanceEditor.editingRecord = null;
  }

  async function savePerformanceRecord(event) {
    event.preventDefault();
    const record = state.performanceEditor.editingRecord;
    const headers = await manualAnalysisAuthHeaders();
    if (!headers) return;
    const payload = {
      record_status: els.performanceRecordStatus.value,
      project_name: els.performanceRecordProject.value.trim(), agency: els.performanceRecordAgency.value.trim(), division: els.performanceRecordDivision.value.trim(),
      overview: nullableText(els.performanceRecordOverview.value), contract_date: nullableText(els.performanceRecordContractDate.value),
      start_date: nullableText(els.performanceRecordStartDate.value), end_date: nullableText(els.performanceRecordEndDate.value),
      contract_amount: nullableNumber(els.performanceRecordAmount.value), vat_basis: els.performanceRecordVat.value,
      completed: els.performanceRecordCompleted.checked, share_pct: nullableNumber(els.performanceRecordShare.value) ?? 100,
      certificate_status: els.performanceRecordCertificate.value, evidence_reference: nullableText(els.performanceRecordEvidence.value),
      keywords: els.performanceRecordKeywords.value.split(",").map((value) => value.trim()).filter(Boolean),
    };
    const path = record ? `/performance-records/${encodeURIComponent(record.id)}` : "/performance-records";
    if (record) payload.expected_updated_at = record.updatedAt;
    else payload.idempotency_key = els.performanceRecordDialog.dataset.requestKey || newIdempotencyKey("performance");
    els.performanceRecordSaveButton.disabled = true;
    try {
      await apiRequest(path, { method: record ? "PATCH" : "POST", headers, body: JSON.stringify(payload) });
      closePerformanceRecordDialog();
      await loadPerformanceEditor({ force: true });
      showToast(record ? "회사 실적 수정 완료" : "회사 실적 등록 완료", "운영 실적과 공개 실적 자료는 분리되어 관리됩니다.", "success");
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      showToast("회사 실적 저장 실패", editorErrorMessage(error), "error");
    } finally {
      els.performanceRecordSaveButton.disabled = false;
    }
  }

  async function loadResultLearning({ force = false } = {}) {
    if (state.resultLearning.loading || (state.resultLearning.loaded && !force)) return;
    const headers = await manualAnalysisAuthHeaders();
    if (!headers) return;
    state.resultLearning.loading = true;
    els.resultLearningUnlockButton.disabled = true;
    els.resultLearningState.hidden = false;
    els.resultLearningState.innerHTML = '<span class="spinner" aria-hidden="true"></span><strong>결과 기록을 불러오고 있습니다</strong>';
    const params = new URLSearchParams({ scope: "ENDED", limit: String(state.resultLearning.limit), offset: String(state.resultLearning.offset) });
    const q = els.resultLearningSearchInput.value.trim();
    if (q) params.set("q", q);
    if (els.resultLearningOutcomeFilter.value) params.set("outcome_status", els.resultLearningOutcomeFilter.value);
    if (els.resultLearningRecordFilter.value) params.set("record_status", els.resultLearningRecordFilter.value);
    try {
      const payload = unwrapObject(await apiRequest(`/result-learning?${params}`, { headers }));
      state.resultLearning.records = arrayValue(payload.records).map(normalizeResultLearningNotice);
      state.resultLearning.total = Math.max(numberOrNull(payload.total) ?? state.resultLearning.records.length, 0);
      state.resultLearning.loaded = true;
      renderResultLearning();
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      els.resultLearningState.hidden = false;
      els.resultLearningState.innerHTML = `<strong>결과 기록을 불러오지 못했습니다</strong><p>${escapeHtml(editorErrorMessage(error))}</p>`;
      showToast("결과 학습 조회 실패", editorErrorMessage(error), "error");
    } finally {
      state.resultLearning.loading = false;
      els.resultLearningUnlockButton.disabled = false;
    }
  }

  function normalizeResultLearningNotice(raw = {}) {
    const outcome = raw.latest_outcome && typeof raw.latest_outcome === "object" ? raw.latest_outcome : null;
    return {
      noticeKey: stringValue(raw.notice_key), bidNoticeNo: stringValue(raw.bid_notice_no), title: stringValue(raw.title), agency: stringValue(raw.agency),
      deadline: stringValue(raw.deadline), noticeStatus: stringValue(raw.notice_status),
      outcome: outcome ? {
        id: stringValue(outcome.id), outcomeKey: stringValue(outcome.outcome_key), recordStatus: stringValue(outcome.record_status, "DRAFT").toUpperCase(),
        revision: numberOrNull(outcome.revision) ?? 1, status: stringValue(outcome.status).toUpperCase(), submittedBidAmount: numberOrNull(outcome.submitted_bid_amount),
        submittedBidRate: numberOrNull(outcome.submitted_bid_rate), winningBidAmount: numberOrNull(outcome.winning_bid_amount), winningBidRate: numberOrNull(outcome.winning_bid_rate),
        technicalScore: numberOrNull(outcome.technical_score), priceScore: numberOrNull(outcome.price_score), totalScore: numberOrNull(outcome.total_score), rank: numberOrNull(outcome.rank),
        winnerName: stringValue(outcome.winner_name), lossReason: stringValue(outcome.loss_reason), source: stringValue(outcome.source), sourceReference: stringValue(outcome.source_reference),
        basisOutcomeId: stringValue(outcome.basis_outcome_id), basisSource: stringValue(outcome.basis_source),
        operatorNote: stringValue(outcome.operator_note), occurredAt: stringValue(outcome.occurred_at), updatedAt: stringValue(outcome.updated_at),
      } : null,
    };
  }

  function renderResultLearning() {
    const data = state.resultLearning;
    els.resultLearningUnlockButton.textContent = data.loaded ? "목록 새로고침" : "결과 기록 열기";
    els.resultLearningSummary.textContent = data.loaded ? `대상 공고 ${formatNumber(data.total)}건` : "운영 키로 결과 기록을 불러오세요.";
    els.resultLearningState.hidden = data.records.length > 0;
    if (data.loaded && !data.records.length) els.resultLearningState.innerHTML = "<strong>조건에 맞는 공고가 없습니다</strong><p>필터를 바꾸거나 종료 공고 수집 상태를 확인해 주세요.</p>";
    els.resultLearningList.innerHTML = data.records.map((notice, index) => {
      const outcome = notice.outcome;
      const outcomeLabel = outcome ? resultStatusLabel(outcome.status) : "결과 미입력";
      return `<article class="operator-record result-record" role="listitem">
        <div><span class="record-status record-status--${escapeAttribute((outcome?.recordStatus || "missing").toLowerCase())}">${escapeHtml(outcome ? recordStatusLabel(outcome.recordStatus) : "미입력")}</span><small>${escapeHtml(resultNoticeStatusLabel(notice.noticeStatus))}</small></div>
        <h4>${escapeHtml(notice.title)}</h4><p>${escapeHtml(notice.agency)} · ${escapeHtml(notice.bidNoticeNo)}</p>
        <dl><div><dt>입찰 결과</dt><dd>${escapeHtml(outcomeLabel)}</dd></div><div><dt>우리 투찰</dt><dd>${escapeHtml(formatBudget(outcome?.submittedBidAmount))}</dd></div><div><dt>낙찰금액</dt><dd>${escapeHtml(formatBudget(outcome?.winningBidAmount))}</dd></div></dl>
        <footer><small>${escapeHtml(outcome ? `${resultSourceLabel(outcome.source)}${outcome.basisSource ? ` · 기준 ${resultSourceLabel(outcome.basisSource)}` : ""} · ${outcome.sourceReference || "근거 미입력"}` : "종료 공고 · 결과 확인 필요")}</small><button class="button button--primary" type="button" data-edit-result="${index}">${outcome ? (outcome.source === "MANUAL_UI" ? "결과 수정" : "검토본 만들기") : "결과 입력"}</button></footer>
      </article>`;
    }).join("");
    const start = data.total ? data.offset + 1 : 0;
    const end = data.offset + data.records.length;
    const pages = Math.max(Math.ceil(data.total / data.limit), 1);
    const page = Math.floor(data.offset / data.limit) + 1;
    els.resultLearningPageRange.textContent = `${formatNumber(start)}–${formatNumber(end)} / ${formatNumber(data.total)}건`;
    els.resultLearningPageLabel.textContent = `${page} / ${pages}`;
    els.resultLearningPreviousButton.disabled = data.loading || data.offset <= 0;
    els.resultLearningNextButton.disabled = data.loading || data.offset + data.limit >= data.total;
    els.resultLearningPagination.hidden = !data.loaded || data.total <= data.limit;
    renderDataSource();
  }

  function handleResultLearningAction(event) {
    const button = event.target.closest("[data-edit-result]");
    if (!button) return;
    const notice = state.resultLearning.records[Number(button.dataset.editResult)];
    if (notice) openResultLearningDialog(notice);
  }

  function openResultLearningDialog(notice) {
    const outcome = notice.outcome;
    const isManualRecord = outcome?.source === "MANUAL_UI";
    state.resultLearning.editingNotice = notice;
    state.resultLearning.editingOutcome = outcome;
    els.resultLearningDialogTitle.textContent = !outcome ? "입찰 결과 입력" : (isManualRecord ? "입찰 결과 수정" : "자동 환류 결과 검토본 만들기");
    els.resultLearningDialogNotice.textContent = `${notice.title} · ${notice.bidNoticeNo}`;
    els.resultLearningStatus.value = outcome?.status || "NO_BID";
    els.resultLearningRecordStatus.value = outcome?.recordStatus || "DRAFT";
    els.resultLearningSubmittedAmount.value = outcome?.submittedBidAmount ?? "";
    els.resultLearningSubmittedRate.value = outcome?.submittedBidRate ?? "";
    els.resultLearningWinningAmount.value = outcome?.winningBidAmount ?? "";
    els.resultLearningWinningRate.value = outcome?.winningBidRate ?? "";
    els.resultLearningTechnicalScore.value = outcome?.technicalScore ?? "";
    els.resultLearningPriceScore.value = outcome?.priceScore ?? "";
    els.resultLearningTotalScore.value = outcome?.totalScore ?? "";
    els.resultLearningRank.value = outcome?.rank ?? "";
    els.resultLearningWinner.value = outcome?.winnerName || "";
    els.resultLearningOccurredAt.value = outcome?.occurredAt ? outcome.occurredAt.slice(0, 10) : "";
    els.resultLearningLossReason.value = outcome?.lossReason || "";
    els.resultLearningSourceReference.value = outcome?.sourceReference || "";
    els.resultLearningOperatorNote.value = outcome?.operatorNote || "";
    els.resultLearningDialog.dataset.requestKey = isManualRecord ? "" : newIdempotencyKey("result");
    els.resultLearningDialog.showModal();
    window.requestAnimationFrame(() => els.resultLearningStatus.focus());
  }

  function closeResultLearningDialog() {
    if (els.resultLearningDialog.open) els.resultLearningDialog.close();
    state.resultLearning.editingNotice = null;
    state.resultLearning.editingOutcome = null;
  }

  async function saveResultLearning(event) {
    event.preventDefault();
    const notice = state.resultLearning.editingNotice;
    const outcome = state.resultLearning.editingOutcome;
    if (!notice) return;
    const isManualRecord = outcome?.source === "MANUAL_UI";
    const headers = await manualAnalysisAuthHeaders();
    if (!headers) return;
    const payload = {
      record_status: els.resultLearningRecordStatus.value, status: els.resultLearningStatus.value,
      submitted_bid_amount: nullableNumber(els.resultLearningSubmittedAmount.value), submitted_bid_rate: nullableNumber(els.resultLearningSubmittedRate.value),
      winning_bid_amount: nullableNumber(els.resultLearningWinningAmount.value), winning_bid_rate: nullableNumber(els.resultLearningWinningRate.value),
      technical_score: nullableNumber(els.resultLearningTechnicalScore.value), price_score: nullableNumber(els.resultLearningPriceScore.value), total_score: nullableNumber(els.resultLearningTotalScore.value),
      rank: nullableNumber(els.resultLearningRank.value), winner_name: nullableText(els.resultLearningWinner.value), loss_reason: nullableText(els.resultLearningLossReason.value),
      source_reference: nullableText(els.resultLearningSourceReference.value), operator_note: nullableText(els.resultLearningOperatorNote.value),
      occurred_at: els.resultLearningOccurredAt.value ? `${els.resultLearningOccurredAt.value}T00:00:00+09:00` : null,
    };
    const path = isManualRecord ? `/result-learning/${encodeURIComponent(outcome.id)}` : "/result-learning";
    if (isManualRecord) payload.expected_updated_at = outcome.updatedAt;
    else {
      payload.notice_key = notice.noticeKey;
      payload.idempotency_key = els.resultLearningDialog.dataset.requestKey || newIdempotencyKey("result");
      if (outcome) payload.basis_outcome_id = outcome.id;
    }
    els.resultLearningSaveButton.disabled = true;
    try {
      await apiRequest(path, { method: isManualRecord ? "PATCH" : "POST", headers, body: JSON.stringify(payload) });
      closeResultLearningDialog();
      await loadResultLearning({ force: true });
      showToast(
        isManualRecord ? "결과 기록 수정 완료" : (outcome ? "담당자 검토본 저장 완료" : "결과 기록 저장 완료"),
        !isManualRecord && outcome ? "자동 환류 원본은 변경하지 않고 검토본을 별도로 저장했습니다." : "검증 상태와 출처를 함께 저장했습니다.",
        "success",
      );
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      showToast("결과 기록 저장 실패", editorErrorMessage(error), "error");
    } finally {
      els.resultLearningSaveButton.disabled = false;
    }
  }

  function changeResultLearningPage(direction) {
    const next = state.resultLearning.offset + direction * state.resultLearning.limit;
    if (next < 0 || next >= state.resultLearning.total) return;
    state.resultLearning.offset = next;
    void loadResultLearning({ force: true });
    els.resultLearningSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function nullableText(value) { const cleaned = String(value || "").trim(); return cleaned || null; }
  function nullableNumber(value) { const cleaned = String(value ?? "").trim(); return cleaned === "" ? null : Number(cleaned); }
  function newIdempotencyKey(prefix) { return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`; }
  function recordStatusLabel(value) { return ({ DRAFT: "초안", VALIDATED: "검증 완료", ARCHIVED: "보관" })[value] || "상태 확인 필요"; }
  function resultStatusLabel(value) { return ({ NO_BID: "미참여", SUBMITTED: "제출", WON: "낙찰", LOST: "미낙찰", CANCELLED: "취소" })[value] || "결과 확인 필요"; }
  function resultNoticeStatusLabel(value) { return ({ OPEN: "진행", CLOSED: "공고 종료", EXPIRED: "입찰마감 경과", CANCELLED: "취소공고" })[String(value || "").toUpperCase()] || "상태 확인 필요"; }
  function resultSourceLabel(value) { return ({ MANUAL_UI: "담당자 입력", PPS_AUTO_FEEDBACK: "나라장터 자동 환류", AUTOMATED: "자동 환류", PPS: "나라장터 결과", PUBLIC_DATA: "공개 자료", MIGRATED: "이전 기록" })[String(value || "").toUpperCase()] || "출처 확인 필요"; }
  function editorErrorMessage(error) { return humanizeError(error); }

  function formatAwardBusinessNumberInput() {
    const digits = els.awardBusinessNumber.value.replace(/\D/g, "").slice(0, 10);
    els.awardBusinessNumber.value = formatBusinessNumber(digits);
    if (digits !== "1058201810" && els.awardCompanyName.value.trim() === "한국능률협회") {
      els.awardCompanyName.value = "";
    } else if (digits === "1058201810" && !els.awardCompanyName.value.trim()) {
      els.awardCompanyName.value = "한국능률협회";
    }
  }

  function normalizeCompanyAward(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    return {
      scope: stringValue(source.scope, "service").toLowerCase(),
      bidNoticeNo: stringValue(firstValue(source.bid_notice_no, source.bidNoticeNo), "공고번호 미확인"),
      revisionNo: stringValue(firstValue(source.revision_no, source.revisionNo), "00"),
      classificationNo: stringValue(firstValue(source.classification_no, source.classificationNo)),
      rebidNo: stringValue(firstValue(source.rebid_no, source.rebidNo)),
      title: stringValue(source.title, "공고명 미확인"),
      participantCount: numberOrNull(firstValue(source.participant_count, source.participantCount)),
      winnerName: stringValue(firstValue(source.winner_name, source.winnerName), "낙찰업체명 미확인"),
      awardAmount: numberOrNull(firstValue(source.award_amount, source.awardAmount)),
      awardRate: numberOrNull(firstValue(source.award_rate, source.awardRate)),
      openedAt: stringValue(firstValue(source.opened_at, source.openedAt)),
      agency: stringValue(source.agency, "발주기관 미확인"),
      registeredAt: stringValue(firstValue(source.registered_at, source.registeredAt)),
      awardedAt: stringValue(firstValue(source.awarded_at, source.awardedAt)),
    };
  }

  async function searchCompanyAwards(event) {
    event?.preventDefault?.();
    if (state.companyAwards.loading) return;
    const businessNumber = els.awardBusinessNumber.value.trim();
    const digits = businessNumber.replace(/\D/g, "");
    const startDate = els.awardStartDate.value;
    const endDate = els.awardEndDate.value;
    const scopes = els.awardScopeInputs.filter((input) => input.checked).map((input) => input.value);
    if (digits.length !== 10) {
      showToast("사업자등록번호 확인 필요", "숫자 10자리 사업자등록번호를 입력해 주세요.", "warning");
      return;
    }
    const span = dateSpanDays(startDate, endDate);
    if (span === null || span < 0) {
      showToast("조회 기간 확인 필요", "시작일과 종료일을 올바르게 입력해 주세요.", "warning");
      return;
    }
    if (!scopes.length) {
      showToast("업무 구분 확인 필요", "용역·물품·공사·외자 중 하나 이상을 선택해 주세요.", "warning");
      return;
    }
    // PPS calls this a one-month bound, but a 30-day inclusive range that
    // crosses February is rejected.  Keep the browser-side estimate aligned
    // with the server's universally safe 28-day windows.
    const estimatedCalls = Math.ceil((span + 1) / 28) * scopes.length;
    if (estimatedCalls > 60) {
      showToast("조회 범위가 너무 큽니다", "기간이나 업무 구분을 줄여 주세요. 한 번의 조회는 외부 요청 60회로 제한됩니다.", "warning");
      return;
    }
    const authHeaders = await manualAnalysisAuthHeaders();
    if (!authHeaders) {
      showToast("낙찰 결과 조회 취소", "4자리 운영 PIN이 입력되지 않았습니다.", "warning");
      return;
    }

    state.companyAwards.loading = true;
    state.companyAwards.error = null;
    setCompanyAwardsLoading(true);
    try {
      const payload = unwrapObject(await apiRequest("/company-awards/search", {
        method: "POST",
        headers: authHeaders,
        timeoutMs: EXTERNAL_PPS_REQUEST_TIMEOUT_MS,
        body: JSON.stringify({
          business_number: businessNumber,
          start_date: startDate,
          end_date: endDate,
          scopes,
          max_pages_per_window: 1,
        }),
      }));
      state.companyAwards.company = firstObject(payload.company);
      state.companyAwards.records = arrayValue(payload.records).map(normalizeCompanyAward);
      state.companyAwards.count = Math.max(numberOrNull(payload.count) ?? state.companyAwards.records.length, 0);
      state.companyAwards.apiCalls = Math.max(numberOrNull(payload.api_calls) ?? 0, 0);
      state.companyAwards.truncated = booleanValue(payload.truncated) ?? false;
      state.companyAwards.partial = booleanValue(payload.partial) ?? false;
      state.companyAwards.warnings = arrayValue(payload.warnings).map((warning) => stringValue(warning)).filter(Boolean);
      state.companyAwards.searched = true;
      state.companyAwards.error = null;
      const resolvedName = stringValue(state.companyAwards.company.name);
      if (resolvedName && !els.awardCompanyName.value.trim()) els.awardCompanyName.value = resolvedName;
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      state.companyAwards.error = error;
      showToast("낙찰 결과 검색 실패", humanizeError(error), "error");
    } finally {
      state.companyAwards.loading = false;
      renderCompanyAwardsView();
    }
  }

  function setCompanyAwardsLoading(isLoading) {
    els.awardResultsLoadingState.hidden = !isLoading;
    els.awardResultsErrorState.hidden = true;
    els.awardResultsEmptyState.hidden = true;
    els.awardResultsList.hidden = isLoading;
    els.awardSearchButton.disabled = isLoading;
    [
      els.awardCompanyName,
      els.awardBusinessNumber,
      els.awardStartDate,
      els.awardEndDate,
      ...els.awardScopeInputs,
    ].forEach((control) => { control.disabled = isLoading; });
    if (state.currentView === "awards") els.refreshButton.disabled = isLoading;
    if (isLoading) els.awardResultsSummary.textContent = "선택한 기간의 나라장터 낙찰 결과를 조회하고 있습니다.";
  }

  function renderCompanyAwardsView() {
    const data = state.companyAwards;
    setCompanyAwardsLoading(false);
    if (data.error) {
      els.awardResultsList.replaceChildren();
      els.awardResultsList.hidden = true;
      els.awardResultsEmptyState.hidden = true;
      els.awardResultsErrorState.hidden = false;
      els.awardResultsErrorMessage.textContent = humanizeError(data.error);
      els.awardResultsSummary.textContent = "외부 낙찰 API 연결 상태와 조회 조건을 확인해 주세요.";
      renderDataSource();
      return;
    }

    const count = data.records.length;
    const displayName = stringValue(data.company?.name, els.awardCompanyName.value.trim() || "조회 회사");
    els.awardResultsErrorState.hidden = true;
    els.awardResultsLoadingState.hidden = true;
    els.awardResultsList.hidden = count === 0;
    els.awardResultsEmptyState.hidden = count !== 0 || !data.searched;
    if (!data.searched) {
      els.awardResultsEmptyState.hidden = false;
      els.awardResultsList.replaceChildren();
      els.awardResultsSummary.textContent = "조회 조건을 확인한 뒤 검색을 실행하세요.";
      renderDataSource();
      return;
    }
    const limitLabel = data.truncated || data.partial ? " · 조회 상한/부분 결과" : "";
    els.awardResultsSummary.textContent = `${displayName} 낙찰 결과 ${formatNumber(data.count)}건 · 나라장터 API ${formatNumber(data.apiCalls)}회${limitLabel}`;
    if (!count) {
      const title = els.awardResultsEmptyState.querySelector("strong");
      const copy = els.awardResultsEmptyState.querySelector("p");
      if (title) title.textContent = "조건에 맞는 낙찰 결과가 없습니다";
      if (copy) copy.textContent = "조회 기간, 업무 구분 또는 사업자등록번호를 확인해 주세요.";
      els.awardResultsList.replaceChildren();
    } else {
      const warnings = data.warnings.length
        ? `<div class="award-result-warnings" role="note">${data.warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`).join("")}</div>`
        : "";
      els.awardResultsList.innerHTML = `${warnings}${data.records.map(renderCompanyAwardCard).join("")}`;
    }
    renderDataSource();
  }

  function renderCompanyAwardCard(record) {
    const scopeLabels = { service: "용역", goods: "물품", construction: "공사", foreign: "외자" };
    const opened = record.openedAt || record.awardedAt || record.registeredAt;
    const rate = record.awardRate === null ? "낙찰률 미확인" : `${formatNumber(record.awardRate, 4)}%`;
    const participants = record.participantCount === null ? "참가수 미확인" : `${formatNumber(record.participantCount)}개사`;
    return `<article class="award-result-card" role="listitem">
      <div class="award-result-card__head">
        <span>${escapeHtml(scopeLabels[record.scope] || record.scope)}</span>
        <time datetime="${escapeAttribute(opened)}">${escapeHtml(formatShortDateTime(opened))}</time>
      </div>
      <h4>${escapeHtml(record.title)}</h4>
      <p class="award-result-card__agency">${escapeHtml(record.agency)} · ${escapeHtml(record.bidNoticeNo)}-${escapeHtml(record.revisionNo)}</p>
      <dl>
        <div><dt>낙찰업체</dt><dd>${escapeHtml(record.winnerName)}</dd></div>
        <div><dt>낙찰금액</dt><dd>${escapeHtml(formatBudget(record.awardAmount))}</dd></div>
        <div><dt>낙찰률</dt><dd>${escapeHtml(rate)}</dd></div>
        <div><dt>참가업체</dt><dd>${escapeHtml(participants)}</dd></div>
      </dl>
      <footer><span>나라장터 낙찰 관측</span><small>수행·완료·실적증명 여부 미확인</small></footer>
    </article>`;
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

  function normalizeNotice(raw = {}, index = 0, options = {}) {
    const source = unwrapObject(raw);
    const declaredAnalysisState = stringValue(
      firstValue(source.analysis_state, source.analysisState, source.ingestion_state),
      "",
    ).toUpperCase();
    const evaluationCandidate = firstObject(
      source.latest_evaluation,
      source.latestEvaluation,
    );
    const evaluation = ["PENDING", "REVIEW", "COLLECTED", "VERSIONED"].includes(declaredAnalysisState)
      ? {}
      : evaluationCandidate;
    const historicalEvaluation = firstObject(source.historical_evaluation, source.historicalEvaluation);
    const deadline = firstValue(source.deadline, source.close_at, source.closeAt, source.bid_close_date, source.bidClseDt, null);
    const rawNoticeStatus = stringValue(firstValue(source.status, source.notice_status), "").toUpperCase();
    const rawProviderDisposition = stringValue(firstValue(source.provider_disposition, source.providerDisposition), "").toUpperCase();
    const deadlineDate = validDate(deadline);
    const endedForHistory = rawProviderDisposition === "CANCELLED"
      || ["CLOSED", "EXPIRED"].includes(rawNoticeStatus)
      || Boolean(rawNoticeStatus === "OPEN" && deadlineDate && deadlineDate.getTime() < Date.now());
    const hasCurrentEvaluation = Boolean(firstValue(
      evaluation.id,
      evaluation.evaluated_at,
      evaluation.evaluatedAt,
      evaluation.eligibility,
    ));
    const allowLegacyCurrentProjection = options.allowLegacyCurrentProjection === true;
    const allowCurrentProjection = hasCurrentEvaluation || allowLegacyCurrentProjection;
    const useHistoricalEvaluation = !hasCurrentEvaluation
      && endedForHistory
      && Boolean(firstValue(
        historicalEvaluation.id,
        historicalEvaluation.evaluated_at,
        historicalEvaluation.evaluatedAt,
        historicalEvaluation.eligibility,
      ));
    const displayEvaluation = useHistoricalEvaluation ? historicalEvaluation : evaluation;
    const explanation = firstObject(
      displayEvaluation.explanation,
      allowCurrentProjection ? source.explanation : null,
    );
    const atomicResults = arrayValue(firstValue(
      displayEvaluation.atomic_results,
      displayEvaluation.atomicResults,
      allowCurrentProjection ? source.atomic_results : null,
      [],
    ));
    const versions = arrayValue(firstValue(source.versions, source.notice_versions, [])).map(normalizeVersion);
    const latestVersion = versions.slice().sort((a, b) => b.versionNo - a.versionNo)[0] || null;
    const decisions = arrayValue(firstValue(source.decisions, source.decision_history, [])).map(normalizeDecisionRecord);
    const latestDecision = decisions.slice().sort((a, b) => nullableDateSort(b.createdAt, a.createdAt))[0] || {};
    const noticeKey = stringValue(
      firstValue(source.notice_key, source.noticeKey, source.id, source.bid_notice_no, source.bidNtceNo),
      `notice-${index + 1}`,
    );
    const collectedAt = firstValue(source.collected_at, source.collectedAt, source.created_at, source.createdAt, null);
    const category = stringValue(firstValue(source.category, source.business_category, source.notice_type), "용역");
    const sourceKind = normalizeSourceKind(firstValue(source.source_kind, source.sourceKind, source.data_source), noticeKey, category);
    const analysisState = normalizeAnalysisState(firstValue(source.analysis_state, source.ingestion_state, source.analysisState), hasCurrentEvaluation, source.status);
    const rawRequirements = arrayValue(firstValue(source.requirements, source.eligibility_requirements, source.conditions, []));
    const requirements = mergeRequirementsAndAtomics(rawRequirements, atomicResults);
    const rawEvidence = arrayValue(firstValue(source.evidence, source.evidences, source.source_evidence, []));
    const rawDocumentAnalyses = arrayValue(firstValue(
      source.document_analyses,
      source.documentAnalyses,
      displayEvaluation.document_analyses,
      displayEvaluation.documentAnalyses,
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
      eligibilityStatus: normalizeEligibility(firstValue(displayEvaluation.eligibility, allowCurrentProjection ? firstValue(source.eligibility_status, source.eligibilityStatus, source.eligibility) : null)),
      qualificationStatus: stringValue(firstValue(source.qualification_status, source.qualificationStatus), ""),
      readinessScore: numberOrNull(firstValue(displayEvaluation.readiness_score, displayEvaluation.readinessScore, allowCurrentProjection ? firstValue(source.readiness_score, source.readinessScore, source.fit_score, source.fitScore) : null)),
      readinessStatus: normalizeReadiness(firstValue(displayEvaluation.readiness_status, displayEvaluation.status, allowCurrentProjection ? source.readiness_status : null)),
      evidenceCoverage: numberOrNull(firstValue(displayEvaluation.evidence_coverage, displayEvaluation.evidenceCoverage, allowCurrentProjection ? firstValue(source.evidence_coverage, source.evidenceCoverage, source.coverage) : null)),
      riskScore: numberOrNull(firstValue(displayEvaluation.risk_score, displayEvaluation.riskScore, allowCurrentProjection ? firstValue(source.risk_score, source.riskScore, source.risk) : null)),
      riskBand: normalizeRecommendation(firstValue(displayEvaluation.risk_band, displayEvaluation.band, allowCurrentProjection ? source.risk_band : null)),
      recommendation: normalizeRecommendation(useHistoricalEvaluation
        ? firstValue(displayEvaluation.risk_band, displayEvaluation.band, displayEvaluation.recommendation)
        : allowCurrentProjection
          ? firstValue(source.recommendation, source.ai_recommendation, source.recommended_decision, evaluation.risk_band, evaluation.band)
          : null),
      recommendationConditions: arrayValue(firstValue(source.recommendation_conditions, source.recommendationConditions, []))
        .map((value) => stringValue(value).replace(/\s+/g, " ").trim()).filter(Boolean).slice(0, 8),
      recommendationEvidenceCount: Math.max(0, numberOrNull(firstValue(
        source.recommendation_evidence_count,
        source.recommendationEvidenceCount,
      )) ?? 0),
      decision: normalizeDecision(firstValue(latestDecision.choice, source.decision, source.manager_decision, source.human_decision)),
      // Public detail responses redact private history to []; that is not a known empty history.
      decisionReadStatus: options.operatorDecisionsLoaded === true
        || (state.accessMode === "SERVER_AUTHENTICATED" && Array.isArray(source.decisions))
        ? "KNOWN" : "UNKNOWN",
      decisionComment: stringValue(firstValue(latestDecision.rationale, source.decision_comment, source.comment, source.manager_comment), ""),
      decidedBy: stringValue(firstValue(latestDecision.actorLabel, source.decided_by, source.decider), ""),
      decidedAt: firstValue(latestDecision.createdAt, source.decided_at, source.decision_at, null),
      resultStatus: stringValue(firstValue(source.result_status, source.award_result, source.outcome), ""),
      hasBidOutcome: booleanValue(firstValue(source.has_bid_outcome, source.hasBidOutcome))
        ?? Boolean(stringValue(firstValue(source.result_status, source.award_result, source.outcome), "")),
      noticeStatus: stringValue(firstValue(source.status, source.notice_status), ""),
      providerDisposition: stringValue(firstValue(source.provider_disposition, source.providerDisposition), "").toUpperCase(),
      providerEventKind: stringValue(firstValue(source.provider_event_kind, source.providerEventKind), ""),
      providerChangedAt: firstValue(source.provider_changed_at, source.providerChangedAt, null),
      isNew: booleanValue(source.is_new) ?? (isRecent(collectedAt, 48) || String(source.status || "").toUpperCase() === "OPEN"),
      summary: stringValue(firstValue(source.summary, source.ai_summary, source.brief), buildEvaluationSummary(displayEvaluation, explanation)),
      category,
      method: stringValue(firstValue(source.method, source.contract_method, source.cntrctCnclsMthdNm), "확인 필요"),
      region: stringValue(firstValue(source.region, source.location_restriction), "전국"),
      requirements,
      evidence,
      awardHistory: history,
      quantitative: arrayValue(firstValue(
        displayEvaluation.quantitative,
        displayEvaluation.quantitative_items,
        allowCurrentProjection ? firstValue(source.quantitative, source.quantitative_items, source.score_items) : null,
        [],
      ))
        .map(normalizeQuantItem),
      riskAxes: normalizeRiskAxes(firstValue(
        displayEvaluation.risk_dimensions,
        displayEvaluation.risk_axes,
        allowCurrentProjection ? firstValue(source.risk_dimensions, source.risk_axes, source.riskAxes, source.risks) : null,
        [],
      )),
      actions: arrayValue(firstValue(source.actions, source.next_actions, source.review_actions, [])).map((value) => stringValue(value)).filter(Boolean),
      pipeline: firstValue(source.pipeline, source.analysis_pipeline, null),
      evaluationId: stringValue(firstValue(evaluation.id, allowCurrentProjection ? source.evaluation_id : null), ""),
      // A legacy stored evaluation can be hidden from current analysis UI
      // while still being the server's concurrency token for human decisions.
      decisionEvaluationId: stringValue(evaluationCandidate.id, ""),
      reasonCode: stringValue(firstValue(evaluation.reason_code, evaluation.reasonCode), ""),
      evaluatedAt: firstValue(evaluation.evaluated_at, evaluation.evaluatedAt, null),
      historicalAnalysis: useHistoricalEvaluation,
      historicalQualification: firstObject(source.historical_qualification, source.historicalQualification),
      historicalEvaluatedAt: useHistoricalEvaluation
        ? firstValue(historicalEvaluation.evaluated_at, historicalEvaluation.evaluatedAt, null)
        : null,
      historicalAnalysisReason: stringValue(firstValue(
        source.historical_evaluation_reason,
        source.historicalEvaluationReason,
      ), "공고 변경 전 당시 판정으로 보관합니다."),
      analysisState,
      analysisReasonCode: analysisReason.code,
      analysisReason: analysisReason.message,
      analysisAttempted: booleanValue(firstValue(source.analysis_attempted, source.analysisAttempted)) ?? false,
      analysisAttachmentCount: numberOrNull(firstValue(source.analysis_attachment_count, source.analysisAttachmentCount)) ?? 0,
      analysisAttachmentsAudited: numberOrNull(firstValue(source.analysis_attachments_audited, source.analysisAttachmentsAudited)) ?? 0,
      analysisAttachmentsAccepted: numberOrNull(firstValue(source.analysis_attachments_accepted, source.analysisAttachmentsAccepted)) ?? 0,
      analysisAttachmentCoverageComplete: booleanValue(firstValue(source.analysis_attachment_coverage_complete, source.analysisAttachmentCoverageComplete)) ?? false,
      analysisUpdatedAt: firstValue(source.analysis_updated_at, source.analysisUpdatedAt, null),
      sourceKind,
      isSynthetic: sourceKind === "SYNTHETIC",
      explanation,
      atomicResults,
      versions,
      latestVersion,
      decisions,
      documentAnalyses,
      attachmentAnalysisStatuses: arrayValue(source.attachment_analysis_statuses),
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
      analysisStateSnapshot: stringValue(firstValue(source.analysis_state_snapshot, source.analysisStateSnapshot), ""),
      analysisSnapshot: firstObject(source.analysis_snapshot, source.analysisSnapshot),
      createdAt: firstValue(source.created_at, source.createdAt, null),
    };
  }

  function buildEvaluationSummary(evaluation, explanation) {
    const eligibility = normalizeEligibility(evaluation.eligibility);
    const reasonCode = stringValue(firstValue(evaluation.reason_code, evaluation.reasonCode), "");
    const reviewCodes = arrayValue(firstValue(explanation.review_codes, explanation.reviewCodes, [])).map((value) => stringValue(value)).filter(Boolean);
    const defaultFails = arrayValue(firstValue(explanation.default_fail_details, explanation.defaultFailDetails, []));
    if (["PASS", "PASS_CURRENT", "PASS_EXCEPTION"].includes(eligibility)) {
      return "필수 참가조건과 현재 확인된 회사 정보가 일치합니다. 마감일 기준 증빙과 제출 준비 상태를 확인하세요.";
    }
    if (eligibility === "REVIEW") {
      const codes = reviewCodes.length ? ` (${reviewCodes.join(", ")})` : reasonCode ? ` (${reasonCode})` : "";
      return `바로 확정할 수 없는 조건${codes}이 있습니다. 담당자가 원문과 최신 증빙을 확인해야 합니다.`;
    }
    if (eligibility === "FAIL") {
      const condition = stringValue(firstValue(defaultFails[0]?.failed_condition, defaultFails[0]?.failedCondition), "필수 참가 조건");
      return `${condition}을 충족하지 못한 것으로 확인되었습니다. 원문 조건과 마감일 당시 회사 정보를 다시 확인하세요.`;
    }
    return "평가 결과가 아직 생성되지 않았습니다. 공고 버전과 자격 조건을 등록한 뒤 규칙 기반 평가를 실행하세요.";
  }

  function normalizeRequirement(item, index) {
    if (typeof item === "string") {
      return { id: `req-${index + 1}`, title: item, description: "근거 확인 필요", status: "UNKNOWN", mandatory: true, evidenceId: "" };
    }
    const source = item && typeof item === "object" ? item : {};
    return {
      id: stringValue(firstValue(source.id, source.requirement_key, source.rule_id), `req-${index + 1}`),
      title: stringValue(firstValue(source.title, source.label, source.name, source.condition, source.requirement), `자격 조건 ${index + 1}`),
      description: stringValue(firstValue(source.message, source.description, source.detail, source.reason, source.explanation, source.source_excerpt), "근거 확인 필요"),
      status: normalizeEligibility(firstValue(source.result, source.status, source.judgement)),
      mandatory: booleanValue(source.mandatory) ?? true,
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
        const score = numberOrNull(firstValue(item?.score, item?.value));
        return {
          label: stringValue(firstValue(item?.label, item?.name, item?.axis), `리스크 ${index + 1}`),
          score: score === null ? null : clamp(score, 0, 100),
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
      return Object.entries(value).map(([label, valueScore]) => {
        const score = numberOrNull(valueScore);
        return { label: labels[label] || label, score: score === null ? null : clamp(score, 0, 100) };
      });
    }
    return [];
  }

  function normalizeDashboard(payload, notices) {
    const source = unwrapObject(payload);
    const kpis = source.kpis && typeof source.kpis === "object" ? source.kpis : source;
    const totals = firstObject(source.totals, kpis.totals);
    const eligibilityCounts = firstObject(source.eligibility_counts, source.eligibilityCounts);
    const readinessCounts = firstObject(source.readiness_counts, source.readinessCounts);
    const workQueues = globalNoticeSearchActive() ? {} : firstObject(source.work_queue_counts);
    const derived = deriveDashboard(notices);
    return {
      newCount: numberOrNull(firstValue(kpis.new_count, kpis.newCount, kpis.new_notices, kpis.new, totals.active, totals.notices)) ?? derived.newCount,
      // Dashboard work queues use explicit stored qualification. Global
      // analysis totals still include missing evaluations and failed notices.
      failCount: numberOrNull(workQueues.fail) ?? derived.failCount,
      reviewCount: derived.reviewCount,
      qualityReviewCount: derived.qualityReviewCount,
      // These clickable KPIs must match their OPEN-only board filters. The
      // backend aggregate can include already-closed notices with a future
      // deadline, so use the loaded notice projection for both counts.
      goCount: derived.goCount,
      urgentCount: derived.urgentCount,
      cancelledCount: numberOrNull(workQueues.cancelled) ?? derived.cancelledCount,
      endedCount: numberOrNull(firstValue(kpis.ended_count, kpis.endedCount, kpis.visible_ended_count, kpis.visibleEndedCount)) ?? derived.endedCount,
      resultMissingCount: numberOrNull(workQueues.result_missing) ?? derived.resultMissingCount,
      undecidedCount: derived.undecidedCount,
      totalNotices: numberOrNull(totals.notices) ?? notices.length,
      totalEvaluations: numberOrNull(totals.evaluations) ?? notices.filter((notice) => notice.evaluationId).length,
      totalDecisions: numberOrNull(totals.decisions) ?? notices.filter((notice) => notice.decision).length,
      eligibilityCounts,
      readinessCounts,
      analysisStatistics: source.analysis_statistics || null,
      lastSync: firstValue(source.last_sync, source.lastSync) || null,
      generatedAt: firstValue(source.generated_at, source.generatedAt) || null,
      systemStatus: stringValue(firstValue(source.system_status, source.status), "online"),
      syntheticWarning: stringValue(firstValue(source.synthetic_data_warning, source.syntheticWarning), ""),
    };
  }

  function dashboardWithoutGlobalTotals(notices, previous = {}) {
    // A filtered board cannot prove whole-database counts. Retain an observed
    // aggregate after a mutation, or show unavailable until the API responds.
    const result = deriveDashboard(notices);
    for (const key of ["totalNotices", "totalEvaluations", "totalDecisions", "failCount", "cancelledCount", "endedCount", "resultMissingCount"]) {
      result[key] = numberOrNull(previous[key]);
    }
    result.analysisStatistics = previous.analysisStatistics || null;
    result.lastSync = previous.lastSync || null;
    result.generatedAt = previous.generatedAt || null;
    return result;
  }

  function deriveDashboard(notices) {
    return {
      totalNotices: notices.length,
      totalEvaluations: notices.filter((notice) => notice.evaluationId).length,
      totalDecisions: notices.filter((notice) => notice.decision).length,
      newCount: notices.filter((notice) => noticeLifecycleStatus(notice) === "OPEN").length,
      failCount: notices.filter((notice) => matchesDashboardQueue(notice, "fail")).length,
      reviewCount: notices.filter((notice) => matchesDashboardQueue(notice, "review")).length,
      qualityReviewCount: notices.filter((notice) => noticeLifecycleStatus(notice) === "OPEN" && isDocumentQualityReview(notice)).length,
      goCount: notices.filter((notice) => noticeLifecycleStatus(notice) === "OPEN" && effectiveRecommendation(notice) === "GO").length,
      urgentCount: notices.filter((notice) => matchesDashboardQueue(notice, "urgent")).length,
      cancelledCount: notices.filter((notice) => matchesDashboardQueue(notice, "cancelled")).length,
      endedCount: notices.filter(isVisibleEndedNotice).length,
      resultMissingCount: notices.filter((notice) => matchesDashboardQueue(notice, "result-missing")).length,
      undecidedCount: operatorDecisionListAvailable(notices)
        ? notices.filter((notice) => noticeLifecycleStatus(notice) === "OPEN" && !notice.decision).length
        : null,
      lastSync: null,
      generatedAt: null,
      systemStatus: "online",
    };
  }

  function dashboardEligibilityStatus(notice) {
    // Current qualification and retained cancellation history are separate.
    // In particular, UNKNOWN/NOT_EVALUATED must never become REVIEW here.
    if (isCancelledNotice(notice)) {
      const historical = notice.historicalQualification;
      return historical?.scope === "LAST_VALID_STORED_EVALUATION"
        && historical.evaluated_at
        && ["PASS", "REVIEW", "FAIL"].includes(historical.eligibility)
        ? historical.eligibility : "NOT_EVALUATED";
    }
    if (notice.sourceKind === "PPS" && !notice.analysisAttachmentCoverageComplete) return "NOT_EVALUATED";
    if (["PASS", "REVIEW", "FAIL", "NOT_EVALUATED"].includes(notice.qualificationStatus)) return notice.qualificationStatus;
    return notice.analysisState === "EVALUATED"
      && !notice.historicalAnalysis
      && ["PASS", "REVIEW", "FAIL"].includes(notice.eligibilityStatus)
      ? notice.eligibilityStatus : "NOT_EVALUATED";
  }

  function matchesDashboardQueue(notice, queue) {
    const eligibility = dashboardEligibilityStatus(notice);
    if (queue === "fail") return !isCancelledNotice(notice) && eligibility === "FAIL";
    if (!["PASS", "REVIEW"].includes(eligibility)) return false;
    if (queue === "cancelled") return isCancelledNotice(notice);
    if (isCancelledNotice(notice)) return false;
    if (queue === "result-missing") return isVisibleEndedNotice(notice) && !notice.hasBidOutcome;
    if (noticeLifecycleStatus(notice) !== "OPEN") return false;
    if (queue === "review") return needsAnalysisOrReview(notice);
    if (queue === "urgent") {
      const days = daysUntil(notice.deadline);
      return days !== null && days >= 0 && days <= URGENT_DEADLINE_DAYS;
    }
    return false;
  }

  function renderAll() {
    renderKpis();
    renderNavigationCounts();
    renderNoticeSearchScope();
    applyFilters();
    renderDataSource();
  }

  function renderKpis() {
    const data = state.dashboard;
    // Preserve the existing DOM ID while replacing the total-stored card.
    els.kpiNew.textContent = displayNumber(data.failCount);
    els.kpiReview.textContent = displayNumber(data.reviewCount);
    els.kpiGo.textContent = displayNumber(data.goCount);
    els.kpiUrgent.textContent = displayNumber(data.urgentCount);
    els.kpiResultMissing.textContent = displayNumber(data.resultMissingCount);
    els.kpiEnded.textContent = displayNumber(data.cancelledCount);
    els.kpiNewTrend.textContent = state.source === "demo" ? "데모" : "자격 FAIL";
    els.kpiReviewTrend.textContent = "처리 필요";
    els.kpiGoTrend.textContent = "AI 판단";
    renderAnalysisProgress(data.analysisStatistics);
  }

  function renderAnalysisProgress(stats) {
    if (!els.analysisProgress) return;
    const valueIds = ["analysisAttachmentValue", "analysisEligibilityValue", "analysisScoreValue"];
    if (!stats || stats.scope !== "OPEN_PPS_NOT_CANCELLED") {
      valueIds.forEach((id) => { els[id].textContent = "—"; });
      els.analysisProgressScope.textContent = state.source === "api" && state.sourceReason
        ? "전체 통계 조회 실패 · 공고 목록은 조회됐습니다. 상단 새로고침으로 다시 조회해 주세요."
        : "전체 통계 조회 대기 · 상단 새로고침으로 다시 조회할 수 있습니다.";
      ["analysisAttachmentDetail", "analysisEligibilityDetail", "analysisScoreDetail"].forEach((id) => { els[id].textContent = "집계 결과가 아직 없습니다."; });
      return;
    }
    const count = (value) => Number.isInteger(value) && value >= 0 ? value : 0;
    const n = count(stats.notice_count);
    const ratio = (done, total) => `${formatNumber(done)} / ${formatNumber(total)} · ${total ? (done / total * 100).toFixed(1) : "0.0"}%`;
    const eligibility = stats.eligibility_counts || {};
    const scores = stats.score_counts || {};
    const analysis = stats.analysis_state_counts || {};
    const recordedNotices = stats.recorded_attempt_notice_count;
    const recordedFiles = stats.recorded_attempt_attachment_count;
    const history = Number.isInteger(recordedNotices) && Number.isInteger(recordedFiles)
      ? `누적 처리 ${formatNumber(count(recordedNotices))}개 공고·${formatNumber(count(recordedFiles))}개 파일 · ` : "";
    els.analysisProgressScope.textContent = `진행 중인 나라장터 공고 ${formatNumber(n)}건 · 취소 제외 · ${history}현재 기준 분석 시도 ${formatNumber(count(stats.attempted_notice_count))}건 · 현재 기준 대기 ${formatNumber(count(analysis.PENDING))}건 · ${formatKstDateTime(state.dashboard.generatedAt)} 기준`;
    els.analysisAttachmentValue.textContent = ratio(count(stats.accepted_attachment_count), count(stats.attachment_count));
    els.analysisAttachmentDetail.textContent = `현재 기준 파일 ${formatNumber(count(stats.audited_attachment_count))}개 검증 · 전체 첨부 성공 ${formatNumber(count(analysis.ANALYZED))}개 공고 · 첨부 검토 ${formatNumber(count(analysis.REVIEW))}개 공고`;
    els.analysisEligibilityValue.textContent = ratio(count(eligibility.PASS) + count(eligibility.FAIL), n);
    els.analysisEligibilityDetail.textContent = `PASS ${count(eligibility.PASS)} · FAIL ${count(eligibility.FAIL)} · 검토 ${count(eligibility.REVIEW)} · 미평가 ${count(eligibility.NOT_EVALUATED)}`;
    els.analysisScoreValue.textContent = ratio(count(stats.score_range_notice_count), n);
    els.analysisScoreDetail.textContent = `확정 ${count(scores.CONFIRMED)} · 추정 ${count(scores.ESTIMATED)} · 일부 미산정 ${count(scores.UNSCORABLE)} · 검토 ${count(scores.REVIEW)} · 미평가 ${count(scores.NOT_EVALUATED)}. 최신 원문에 연결된 저장 결과이며 점수 범위가 있는 공고를 포함합니다.`;
  }

  function renderNavigationCounts() {
    els.navNewCount.textContent = displayNumber(state.dashboard.newCount) + "건";
    els.navReviewCount.textContent = displayNumber(state.dashboard.reviewCount) + "건";
    const decisionCount = operatorDecisionListAvailable()
      ? state.notices.filter((notice) => noticeLifecycleStatus(notice) === "OPEN" && !notice.decision).length
      : null;
    els.navDecisionCount.textContent = decisionCount === null ? "—" : `${formatNumber(decisionCount)}건`;
    els.navDecisionCount.setAttribute("aria-label", decisionCount === null
      ? "담당자 판단 목록 조회 필요" : `현재 조회 공고 중 미결정 ${formatNumber(decisionCount)}건`);
  }

  function renderDataSource() {
    if (state.noticeSearchMode === "prespec") {
      const stored = state.prespec.stored.records.length;
      const live = state.prespec.live;
      els.dataSourceLabel.textContent = live.searched
        ? `사전규격 · 저장 자료 ${formatNumber(stored)}건 · 나라장터 조회 ${formatNumber(live.apiCalls)}회 · 문서 분석 0회`
        : `사전규격 저장 자료 ${formatNumber(stored)}건 · 외부 조회 0회 · 문서 분석 0회`;
      return;
    }
    if (state.currentView === "closed") {
      els.dataSourceLabel.textContent = state.resultLearning.loaded
        ? `결과 학습 DB · 대상 공고 ${formatNumber(state.resultLearning.total)}건 · 자동 환류/담당자 입력 구분`
        : "결과 학습 DB · 운영 키로 조회";
      return;
    }
    if (state.currentView === "awards") {
      const data = state.companyAwards;
      els.dataSourceLabel.textContent = data.searched
        ? `나라장터 낙찰정보 실시간 조회 · 외부 조회 ${formatNumber(data.apiCalls)}회 · PAI 미저장`
        : "나라장터 낙찰정보 · 버튼 실행 시에만 외부 조회";
      return;
    }
    if (state.currentView === "performance") {
      const summary = state.performance.summary;
      els.dataSourceLabel.textContent = summary
        ? `공개·비식별 자료 · ${stringValue(summary.datasetVersion, "버전 미확인")} · 저장본 ${formatNumber(summary.recordCount)}건`
        : "공개·비식별 실적 자료 확인 중";
      return;
    }
    if (state.source === "api") {
      const syntheticCount = state.notices.filter((notice) => notice.isSynthetic).length;
      const ppsCount = state.notices.filter((notice) => notice.sourceKind === "PPS").length;
      const manualCount = state.notices.filter((notice) => notice.sourceKind === "MANUAL").length;
      const composition = `조달청 ${formatNumber(ppsCount)}건 · 수동 ${formatNumber(manualCount)}건 · 합성 ${formatNumber(syntheticCount)}건`;
      const accessLabel = state.accessMode === "PUBLIC_READ_ONLY" ? " · 공개 읽기 전용" : "";
      els.dataSourceLabel.textContent = state.sourceReason ? `실시간 연결${accessLabel} · ${composition} · ${state.sourceReason}` : `실시간 연결${accessLabel} · ${composition}`;
    } else if (state.source === "demo") {
      els.dataSourceLabel.textContent = "데모 예시 데이터 · 실제 판정 결과가 아닙니다";
    } else if (state.source === "error") {
      els.dataSourceLabel.textContent = "운영 서버 연결 오류 · 재시도 필요";
    } else {
      els.dataSourceLabel.textContent = "데이터 출처 확인 중";
    }
  }

  function applyFilters() {
    if (state.loading) return;
    renderNoticeSearchScope();
    const query = els.searchInput.value.trim().replace(/\s+/g, " ").toLocaleLowerCase("ko-KR");
    const globalSearch = Boolean(query);
    const serverBackedSearch = globalSearch && state.source === "api";
    const eligibility = els.eligibilityFilter.value;
    const recommendation = els.recommendationFilter.value;
    // Anonymous projections omit private decisions; absence is not proof of indecision.
    const decisionAccessAllowed = state.source === "demo"
      || (state.source === "api" && state.accessMode === "SERVER_AUTHENTICATED");
    const decisionFilterAvailable = operatorDecisionListAvailable();
    const decisionFilterMessage = !decisionAccessAllowed
      ? state.operatorDecisionEnabled
        ? "공개 화면의 공고 목록에는 담당자 판단이 포함되지 않아 분류할 수 없습니다. 운영 PIN으로 개별 공고 상세의 판단을 확인하거나 저장할 수 있습니다."
        : "공개 화면의 공고 목록에는 담당자 판단이 포함되지 않아 분류할 수 없습니다. 현재 서버에서는 판단 저장을 제공하지 않습니다."
      : !decisionFilterAvailable
        ? "담당자 판단 목록을 불러와야 사용할 수 있습니다. 일부 공고의 조회 결과를 전체 판단으로 분류하지 않습니다."
        : "AI 추천과 구분한 담당자의 저장된 판단으로 분류합니다.";
    els.operatorDecisionFilter.disabled = !decisionFilterAvailable;
    els.operatorDecisionFilter.title = decisionFilterMessage;
    els.operatorDecisionFilter.options[0].textContent = decisionFilterAvailable
      ? "담당자 판단 전체" : decisionAccessAllowed ? "담당자 판단 조회 필요" : "판단 조회 권한 필요";
    els.operatorDecisionFilterHelp.textContent = decisionFilterMessage;
    if (!decisionFilterAvailable) els.operatorDecisionFilter.value = "all";
    const operatorDecision = els.operatorDecisionFilter.value;
    const sort = els.sortSelect.value;

    let notices = state.notices.filter((notice) => {
      if (["fail", "review", "urgent", "cancelled", "result-missing"].includes(state.currentView)
        && !matchesDashboardQueue(notice, state.currentView)) return false;
      if (!globalSearch) {
        if (["all", "new", "review", "undecided", "go", "urgent"].includes(state.currentView) && noticeLifecycleStatus(notice) !== "OPEN") return false;
        if (state.currentView === "go" && effectiveRecommendation(notice) !== "GO") return false;
        if (state.currentView === "ended" && !isVisibleEndedNotice(notice)) return false;
        if (state.currentView === "undecided" && decisionFilterAvailable && operatorDecision === "all" && notice.decision) return false;
        if (state.currentView === "closed" && !notice.resultStatus) return false;
      }
      const qualification = ["fail", "review", "urgent", "cancelled", "result-missing"].includes(state.currentView)
        ? dashboardEligibilityStatus(notice) : effectiveEligibilityStatus(notice);
      if (eligibility !== "all" && qualification !== eligibility) return false;
      if (recommendation !== "all" && effectiveRecommendation(notice) !== recommendation) return false;
      const decision = notice.decision || (hasKnownOperatorDecision(notice) ? "UNDECIDED" : "UNAVAILABLE");
      if (operatorDecision !== "all" && decision !== operatorDecision) return false;
      const searchable = `${notice.title} ${notice.agency} ${notice.noticeNumber} ${notice.noticeKey}`
        .replace(/\s+/g, " ")
        .toLocaleLowerCase("ko-KR");
      // API results already reflect the server's stored-notice matching
      // contract (including multi-token Korean searches). Reapplying an
      // exact browser substring check would incorrectly hide valid results.
      if (query && !serverBackedSearch && !searchable.includes(query)) return false;
      return true;
    });

    notices = notices.slice().sort((a, b) => compareNotices(a, b, sort));
    state.filteredNotices = notices;
    renderNoticeList();
  }

  function hasKnownOperatorDecision(notice) {
    return operatorDecisionReadStatus(notice) === "KNOWN";
  }

  function operatorDecisionReadStatus(notice) {
    if (state.source === "demo") return "KNOWN";
    return notice?.decisionReadStatus || "UNKNOWN";
  }

  function operatorDecisionListAvailable(notices = state.notices) {
    return (state.source === "demo" || (state.source === "api" && state.accessMode === "SERVER_AUTHENTICATED"))
      && notices.every(hasKnownOperatorDecision);
  }

  function operatorDecisionLabel(notice) {
    const status = operatorDecisionReadStatus(notice);
    const saved = notice?.decision ? DECISION_LABELS[notice.decision] || "보류" : "";
    if (saved) return saved + ({ LOADING: " · 확인 중", ERROR: " · 재조회 실패", UNKNOWN: " · 최신 확인 필요" }[status] || "");
    return { KNOWN: "미결정", LOADING: "판단 확인 중", ERROR: "판단 조회 실패", UNKNOWN: "판단 조회 필요" }[status] || "판단 조회 필요";
  }

  function preserveOperatorDecision(notice, previous) {
    if (!previous?.decision || hasKnownOperatorDecision(notice)) return notice;
    // Retain the last authenticated/saved value as a display snapshot, never as fresh list data.
    return {
      ...notice,
      decision: previous.decision,
      decisionComment: previous.decisionComment,
      decidedBy: previous.decidedBy,
      decidedAt: previous.decidedAt,
      decisions: previous.decisions,
    };
  }

  function operatorDecisionDetailText(notice) {
    const saved = notice.decision
      ? [operatorDecisionLabel(notice), notice.decidedBy, notice.decidedAt ? formatKstDateTime(notice.decidedAt) : ""].filter(Boolean).join(" · ")
      : operatorDecisionLabel(notice);
    if (isCancelledNotice(notice)) return notice.decision
      ? `취소 공고 · 과거 판단 기록(참고용): ${saved}`
      : "취소 공고 · 담당자 판단을 새로 저장할 수 없습니다.";
    const analysisNote = !decisionAnalysisComplete(notice)
      ? " 현재 공고 분석 전 · 판단 사유를 입력하면 지금도 기록할 수 있고, 서버가 당시 분석 상태를 함께 남깁니다." : "";
    return `${notice.decision ? "저장된 판단: " : ""}${saved}.${analysisNote}`;
  }

  function updateOperatorDecisionReadState(notice, status) {
    const updated = { ...notice, decisionReadStatus: status };
    const index = state.notices.findIndex((item) => item.noticeKey === notice.noticeKey);
    if (index >= 0) state.notices[index] = updated;
    if (state.selectedNotice?.noticeKey === notice.noticeKey) {
      state.selectedNotice = updated;
      // Refresh status only, preserving an in-progress choice or comment.
      els.decisionExisting.textContent = operatorDecisionDetailText(updated);
      const metric = els.decisionSummary.querySelector("[data-operator-decision-summary] strong");
      if (metric) metric.textContent = operatorDecisionLabel(updated);
      els.analysisPipeline.innerHTML = renderPipeline(updated);
    }
    renderNavigationCounts();
    applyFilters();
    return updated;
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
    const eligibility = effectiveEligibilityStatus(notice);
    if (notice.analysisState === "EVALUATED" && eligibility.startsWith("PASS")) return 0;
    if (isActionableEligibilityReview(notice)) return 1;
    if (notice.analysisState === "EVALUATED" && eligibility === "FAIL") return 3;
    return 2;
  }

  function isDocumentQualityReview(notice) {
    if (notice.analysisState !== "EVALUATED" || notice.eligibilityStatus !== "REVIEW") return false;
    const code = String(notice.analysisReasonCode || "").toUpperCase();
    return [
      "ATTACHMENT_MANIFEST_MISSING",
      "ATTACHMENT_NONE",
      "ATTACHMENT_COVERAGE_INCOMPLETE",
      "HWP_ONLY_UNSUPPORTED",
      "HWPX_EXTRACT_FAILED",
      "PDF_EXTRACT_FAILED",
      "DOCUMENT_EXTRACT_FAILED",
      "UNSUPPORTED_ATTACHMENT",
      "OPENAI_REVIEW",
      "UNVERIFIED_QUOTE",
      "QUOTE_UNVERIFIED",
      "PARTIAL",
    ].includes(code);
  }

  function isActionableEligibilityReview(notice) {
    return notice.analysisState === "EVALUATED"
      && effectiveEligibilityStatus(notice) === "REVIEW"
      && !isDocumentQualityReview(notice);
  }

  function needsAnalysisOrReview(notice) {
    if (noticeLifecycleStatus(notice) !== "OPEN") return false;
    if (notice.sourceKind === "PPS" && !notice.analysisAttachmentCoverageComplete) return true;
    if (notice.analysisState !== "EVALUATED") return true;
    return isActionableEligibilityReview(notice);
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
    if (state.noticeSearchMode === "prespec") {
      els.noticePanel.hidden = true;
      renderPpsDiscovery();
      return;
    }
    const count = state.filteredNotices.length;
    const total = state.notices.length;
    const globalSearch = globalNoticeSearchActive();
    const ownerLabel = els.departmentSelect.selectedOptions[0]?.textContent || "전사 공통";
    const keywordLabel = els.priorityKeywordInput.value.trim();
    const context = keywordLabel ? `${ownerLabel} · 관심 키워드 ${keywordLabel}` : ownerLabel;
    els.noticeSummary.textContent = state.noticeSearchMode === "pps"
      ? "수집 DB와 분리된 나라장터 용역 공고 조회입니다. 저장 전에는 판단 결과가 없습니다."
      : globalSearch
        ? count === total
          ? `저장된 전체 공고 검색 결과 ${formatNumber(total)}건입니다.`
          : `저장된 전체 공고 검색 결과 ${formatNumber(total)}건 중 현재 필터에 ${formatNumber(count)}건이 표시됩니다.`
        : count === total
          ? `총 ${formatNumber(total)}건 · ${context} 기준 우선순위입니다.`
          : `전체 ${formatNumber(total)}건 중 ${formatNumber(count)}건이 표시됩니다.`;
    if (state.currentView === "undecided" && state.noticeSearchMode === "stored") {
      els.noticeSummary.textContent = operatorDecisionListAvailable()
        ? `현재 조회 범위의 담당자 판단 · ${formatNumber(count)}건 표시`
        : "담당자 판단 목록 조회 필요 · 아래는 공고 탐색 목록이며 미결정 공고 수가 아닙니다.";
    }

    els.noticePanel.hidden = state.noticeSearchMode !== "stored";
    els.loadingState.hidden = true;
    els.errorState.hidden = true;
    els.emptyState.hidden = count !== 0;
    els.noticeTableWrap.hidden = count === 0 || state.layout !== "table";
    els.noticeCardGrid.hidden = count === 0 || state.layout !== "cards";
    renderPpsDiscovery();

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
    const cancelled = isCancelledNotice(notice);
    const historicalAnalyzed = notice.historicalAnalysis && !cancelled;
    const displayAnalyzed = !cancelled && (analyzed || historicalAnalyzed);
    const pendingLabel = notice.analysisState === "ANALYZED" ? "판단 대기 사유" : "미분석 사유";
    const readiness = displayAnalyzed ? formatScore(notice.readinessScore) : "미산정";
    const readinessClass = displayAnalyzed ? scoreClass(notice.readinessScore) : "is-unknown";
    return `
      <tr class="notice-row ${operatorDecisionClass(notice)}" data-notice-key="${escapeAttribute(notice.noticeKey)}">
        <td>
          <button class="notice-title-button" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 전체 상세 보기">
            <span class="notice-title">${escapeHtml(notice.title)}</span>
            <span class="notice-meta">${sourceKindBadge(notice)}${noticeLifecycleBadge(notice)}<span>${escapeHtml(notice.demandAgency || notice.agency)}</span></span>
            ${notice.historicalAnalysis ? `<span class="notice-analysis-reason" title="${escapeAttribute(notice.historicalAnalysisReason)}">당시 판정 참고 · ${escapeHtml(truncateText(notice.historicalAnalysisReason, 120))}</span>` : analyzed ? "" : `<span class="notice-analysis-reason" title="${escapeAttribute(notice.analysisReason)}">${pendingLabel} · ${escapeHtml(truncateText(notice.analysisReason, 120))}</span>`}
            ${departmentPriorityBadge(notice)}
          </button>
          ${manualAnalysisAction(notice, "table")}
        </td>
        <td><span class="deadline ${deadline.urgent ? "is-urgent" : ""}">${escapeHtml(deadline.relative)}<small>${escapeHtml(deadline.date)} ${escapeHtml(deadline.time || "시각 미확인")} KST</small></span></td>
        <td><span class="budget-cell">${escapeHtml(formatBudget(notice.budget))}</span></td>
        <td>${analysisStatusPill(notice)}</td>
        <td><div class="score-cell ${readinessClass}"><strong class="${displayAnalyzed ? "" : "metric-pending"}">${displayAnalyzed ? `${readiness}/100` : readiness}</strong><span class="mini-bar" aria-hidden="true"><span style="width:${displayAnalyzed ? clamp(notice.readinessScore ?? 0, 0, 100) : 0}%"></span></span></div></td>
        <td><span class="risk-score ${displayAnalyzed ? riskClass(notice.riskScore) : "is-unknown"}">${displayAnalyzed ? riskDisplayValue(notice) : "미산정"}</span></td>
        <td>${analysisRecommendationPill(notice)}</td>
        <td>${operatorDecisionIndicator(notice)}</td>
        <td>
          <button class="detail-link-button" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 전체 상세 보기">
            전체 상세 보기
          </button>
        </td>
      </tr>`;
  }

  function renderNoticeCard(notice) {
    const deadline = deadlineInfo(notice.deadline);
    const analyzed = notice.analysisState === "EVALUATED";
    const cancelled = isCancelledNotice(notice);
    const historicalAnalyzed = notice.historicalAnalysis && !cancelled;
    const displayAnalyzed = !cancelled && (analyzed || historicalAnalyzed);
    const pendingLabel = notice.analysisState === "ANALYZED" ? "판단 대기 사유" : "미분석 사유";
    return `
      <article class="notice-card ${operatorDecisionClass(notice)}" data-notice-key="${escapeAttribute(notice.noticeKey)}">
        <button class="notice-card__body" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 전체 상세 보기">
          <span class="notice-card__head">
            <span>${sourceKindBadge(notice)} ${noticeLifecycleBadge(notice)} ${analysisStatusPill(notice)}</span>
            <span class="notice-card__deadline ${deadline.urgent ? "is-urgent" : ""}">${escapeHtml(deadline.relative)}</span>
          </span>
          <h3>${escapeHtml(notice.title)}</h3>
          <p>${escapeHtml(notice.demandAgency || notice.agency)} · 추정가격 ${escapeHtml(formatBudget(notice.budget))}</p>
          ${notice.historicalAnalysis ? `<span class="notice-card__analysis-reason">당시 판정 참고 · ${escapeHtml(truncateText(notice.historicalAnalysisReason, 140))}</span>` : analyzed ? "" : `<span class="notice-card__analysis-reason">${pendingLabel} · ${escapeHtml(truncateText(notice.analysisReason, 140))}</span>`}
          ${departmentPriorityBadge(notice)}
          <span class="notice-card__metrics">
            <span class="notice-card__metric"><small>${historicalAnalyzed ? "당시 제출 준비도" : "제출 준비도"}</small><strong class="${displayAnalyzed ? "" : "metric-pending"}">${displayAnalyzed ? `${formatScore(notice.readinessScore)}/100` : "미산정"}</strong></span>
            <span class="notice-card__metric"><small>${historicalAnalyzed ? "당시 리스크" : "리스크"}</small><strong class="${displayAnalyzed && notice.riskScore !== null ? "" : "metric-pending"}">${displayAnalyzed ? riskDisplayValue(notice) : "미산정"}</strong></span>
          </span>
        </button>
        <footer class="notice-card__foot">
          <span class="notice-card__axes">${analysisRecommendationPill(notice)}${operatorDecisionIndicator(notice)}</span>
          <span class="notice-card__actions">
            ${manualAnalysisAction(notice, "card")}
            <button class="detail-link-button" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 전체 상세 보기">전체 상세 보기</button>
          </span>
        </footer>
      </article>`;
  }

  function manualAnalysisAvailability(
    notice,
    { stored = true, canonicalStateKnown = true } = {},
  ) {
    const unavailable = (code, label, reason) => ({
      enabled: false,
      code,
      label,
      reason,
      recomputeCurrent: false,
    });
    if (!notice) {
      return unavailable(
        "NOTICE_STATE_UNKNOWN",
        "공고 상태 확인 필요",
        "저장된 공고 상태를 확인한 뒤 분석할 수 있습니다.",
      );
    }
    if (notice.sourceKind !== "PPS") {
      return unavailable(
        "NON_PPS_NOTICE",
        "조달청 공고만 분석 가능",
        "첨부 전체 분석은 조달청에서 수집한 실제 공고만 지원합니다.",
      );
    }
    const lifecycle = noticeLifecycleStatus(notice);
    if (lifecycle === "CANCELLED") {
      return unavailable(
        "NOTICE_CANCELLED",
        "취소 공고 · 분석 불가",
        "조달청에서 취소된 공고는 새 분석이나 재판단을 실행할 수 없습니다.",
      );
    }
    if (lifecycle !== "OPEN") {
      return unavailable(
        "NOTICE_ENDED",
        "마감 공고 · 분석 불가",
        "마감 또는 종료된 공고는 새 분석이나 재판단을 실행할 수 없습니다.",
      );
    }
    if (!stored) {
      return unavailable(
        "NOTICE_NOT_STORED",
        "저장 후 분석 가능",
        "PAI에 공고를 먼저 저장한 뒤 첨부 전체 분석을 실행할 수 있습니다.",
      );
    }
    if (state.source !== "api") {
      return unavailable(
        "API_MODE_REQUIRED",
        "운영 서버 연결 필요",
        "운영 서버에 연결된 화면에서만 공고 분석을 실행할 수 있습니다.",
      );
    }
    if (!state.manualAnalysisEnabled) {
      return unavailable(
        "MANUAL_ANALYSIS_DISABLED",
        "분석 기능 비활성",
        state.manualAnalysisUnavailableReason || "현재 서버에서 수동 분석 기능을 사용할 수 없습니다.",
      );
    }
    if (!canonicalStateKnown) {
      return {
        enabled: true,
        code: "CANONICAL_STATE_CHECK",
        label: "상태 확인·분석",
        reason: "저장된 공고의 최신 상태를 확인한 뒤 필요한 분석을 실행합니다.",
        recomputeCurrent: false,
      };
    }
    const quantitativeRetry = quantitativeRuleRetryRequired(notice);
    const recomputeCurrent = notice.analysisState === "EVALUATED"
      && notice.analysisAttachmentCoverageComplete
      && !isDocumentQualityReview(notice)
      && !quantitativeRetry;
    if (recomputeCurrent) {
      return {
        enabled: true,
        code: "RECOMPUTE_CURRENT",
        label: "저장 근거 재판단",
        reason: "현재 저장된 첨부 근거를 다시 사용해 자격·정량 판단을 갱신합니다.",
        recomputeCurrent: true,
      };
    }
    if (quantitativeRetry) {
      return {
        enabled: true,
        code: "QUANTITATIVE_RETRY",
        label: "정량 근거 재검증",
        reason: "첨부는 확인됐지만 공고별 정량표 근거가 검토 상태라 해당 추출을 한 번 다시 확인합니다.",
        recomputeCurrent: false,
      };
    }
    return {
      enabled: true,
      code: "ANALYSIS_AVAILABLE",
      label: manualAnalysisLabel(notice),
      reason: "현재 공고의 공개 첨부와 저장 근거를 확인해 판단을 갱신합니다.",
      recomputeCurrent: false,
    };
  }

  function manualAnalysisLabel(notice, running = false) {
    // Previous embedded-client labels: "판단 실행", "첨부 전체 재분석".
    if (running) return "첨부 분석 중…";
    if (quantitativeRuleRetryRequired(notice)) return "정량 근거 재검증";
    if (
      notice.analysisState === "EVALUATED"
      && notice.analysisAttachmentCoverageComplete
      && !isDocumentQualityReview(notice)
    ) return "저장 근거 재판단";
    if (notice.analysisState === "ANALYZED" && notice.analysisAttachmentCoverageComplete) return "저장 근거로 판단";
    if (isDocumentQualityReview(notice) || notice.analysisAttempted) {
      return "첨부 전체 상태 재검증";
    }
    return "첨부 전체 분석·판단";
  }

  function confirmManualAnalysis(notice, availability = manualAnalysisAvailability(notice)) {
    const total = Math.max(Number(notice.analysisAttachmentCount) || 0, 0);
    const audited = Math.max(Number(notice.analysisAttachmentsAudited) || 0, 0);
    const pending = Math.max(total - audited, 0);
    const policyMax = Math.max(
      Number(firstValue(
        state.manualAnalysisPolicy?.max_attachments,
        state.manualAnalysisPolicy?.maxAttachments,
      )) || 10,
      1,
    );
    const recomputeCurrent = availability.recomputeCurrent;
    const evaluationOnly = recomputeCurrent || (
      notice.analysisState === "ANALYZED"
      && notice.analysisAttachmentCoverageComplete
      && !quantitativeRuleRetryRequired(notice)
    );
    const knownScope = pending
      ? `현재 남은 첨부 ${formatNumber(pending)}개`
      : `첨부 목록 재확인(서버 상한 ${formatNumber(policyMax)}개)`;
    const retryingReviewed = !evaluationOnly && notice.analysisAttempted;
    const usage = recomputeCurrent
      ? "현재 첨부 검증이 완료되어 문서 재분석 없이 저장된 근거로 자격·정량 판단만 다시 계산합니다."
      : evaluationOnly
      ? "현재 첨부 검증이 완료되어 문서 재분석 없이 저장된 근거로 판단만 실행합니다."
      : retryingReviewed
        ? `${knownScope} · 이미 승인된 첨부는 재사용하고, 현재 검토/실패 첨부와 미검증 첨부만 이번 요청에서 한 번씩 재검증합니다. 문서 분석 요청 상한은 ${formatNumber(policyMax * 2)}회입니다.`
        : `${knownScope} · 실행 중 첨부 목록이 갱신되는 경우까지 포함해 문서 분석 요청 상한은 ${formatNumber(policyMax * 2)}회입니다. 이미 검증됐거나 재사용 가능한 문서는 실제 요청이 더 적거나 0회일 수 있습니다.`;
    return requestAnalysisConfirmation(
      `${notice.title}\n\n모든 공개 첨부를 확인한 뒤 자격·정량 판단을 갱신합니다.\n${usage}`,
      recomputeCurrent ? "재판단 시작" : "분석 시작",
    );
  }

  function requestAnalysisConfirmation(message, actionLabel) {
    if (document.getElementById("manualAnalysisConfirmationDialog")) return Promise.resolve(false);
    const dialog = document.createElement("dialog");
    dialog.id = "manualAnalysisConfirmationDialog";
    dialog.className = "source-link-dialog analysis-confirmation-dialog";
    dialog.setAttribute("aria-label", "공고 분석 실행 확인");
    const form = document.createElement("form");
    form.method = "dialog";
    form.className = "source-link-dialog__panel";
    const header = document.createElement("header");
    header.className = "source-link-dialog__header";
    const title = document.createElement("h2");
    title.textContent = "분석 범위와 사용량 확인";
    header.append(title);
    const body = document.createElement("div");
    body.className = "source-link-dialog__body";
    const description = document.createElement("p");
    description.style.whiteSpace = "pre-wrap";
    description.textContent = message;
    description.id = "manualAnalysisConfirmationDescription";
    dialog.setAttribute("aria-describedby", description.id);
    body.append(description);
    const actions = document.createElement("div");
    actions.className = "source-link-dialog__actions";
    const cancel = document.createElement("button");
    cancel.type = "submit";
    cancel.value = "cancel";
    cancel.textContent = "취소";
    cancel.className = "button button--ghost";
    const confirm = document.createElement("button");
    confirm.type = "submit";
    confirm.value = "confirm";
    confirm.textContent = actionLabel;
    confirm.className = "button button--primary";
    actions.append(cancel, confirm);
    form.append(header, body, actions);
    dialog.append(form);
    document.body.append(dialog);
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        const accepted = dialog.returnValue === "confirm";
        dialog.remove();
        resolve(accepted);
      }, { once: true });
      if (typeof dialog.showModal !== "function") {
        dialog.remove();
        resolve(false);
        return;
      }
      dialog.showModal();
      cancel.focus();
    });
  }

  function evaluationOnlyManualAnalysis(notice, availability = manualAnalysisAvailability(notice)) {
    return availability.recomputeCurrent || (
      notice.analysisState === "ANALYZED"
      && notice.analysisAttachmentCoverageComplete
      && !quantitativeRuleRetryRequired(notice)
    );
  }

  async function manualAnalysisAuthHeaders() {
    if (!state.manualAnalysisAuthRequired) return {};
    if (!state.manualAnalysisToken) {
      try {
        state.manualAnalysisToken = window.sessionStorage.getItem("pai-loop-operator-pin") || "";
      } catch (_) {
        state.manualAnalysisToken = "";
      }
    }
    if (!state.manualAnalysisToken) {
      const supplied = await requestManualAnalysisToken();
      if (!supplied?.trim()) return null;
      state.manualAnalysisToken = supplied.trim();
      try {
        window.sessionStorage.setItem("pai-loop-operator-pin", state.manualAnalysisToken);
      } catch (_) {
        // A private or policy-restricted browser may disable session storage.
      }
    }
    return { "X-PAI-Manual-Token": state.manualAnalysisToken };
  }

  function clearManualAnalysisToken() {
    state.manualAnalysisToken = "";
    try {
      window.sessionStorage.removeItem("pai-loop-operator-pin");
    } catch (_) {
      // Keep the in-memory clear effective even when storage is unavailable.
    }
  }

  function requestManualAnalysisToken() {
    const dialog = els.manualAnalysisTokenDialog;
    const input = els.manualAnalysisTokenInput;
    if (!dialog || !input || typeof dialog.showModal !== "function") {
      showToast("운영 PIN 입력 불가", "현재 브라우저에서는 보안 입력창을 열 수 없습니다.", "error");
      return Promise.resolve(null);
    }
    input.value = "";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        const token = dialog.returnValue === "confirm" ? input.value : "";
        input.value = "";
        resolve(token || null);
      }, { once: true });
      dialog.showModal();
      window.requestAnimationFrame(() => input.focus());
    });
  }

  function manualAnalysisAction(notice, context) {
    const availability = manualAnalysisAvailability(notice);
    const running = state.manualAnalysisRequests.get(notice.noticeKey) === "running";
    const label = running ? manualAnalysisLabel(notice, true) : availability.label;
    const disabled = running || !availability.enabled;
    return `<button class="manual-analysis-action manual-analysis-action--${escapeAttribute(context)}" type="button" data-manual-analysis data-notice-key="${escapeAttribute(notice.noticeKey)}" ${disabled ? "disabled" : ""} title="${escapeAttribute(availability.reason)}" aria-label="${escapeAttribute(notice.title)} ${escapeAttribute(label)}${availability.enabled ? "" : ` · ${escapeAttribute(availability.reason)}`}">${running ? '<span class="button-spinner" aria-hidden="true"></span>' : ""}${escapeHtml(label)}</button>`;
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
      els.ppsDiscoverySection.hidden = state.noticeSearchMode !== "pps";
      els.noticePanel.hidden = state.noticeSearchMode !== "stored";
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
      els.systemStatusText.textContent = "데이터 불러오기 완료";
    } else if (mode === "demo") {
      els.systemStatusDot.classList.add("is-demo");
      els.systemStatusText.textContent = "예시 데이터";
    } else if (mode === "error") {
      els.systemStatusDot.classList.add("is-error");
      els.systemStatusText.textContent = "데이터 불러오기 실패";
    } else if (mode === "partial") {
      els.systemStatusDot.classList.add("is-error");
      els.systemStatusText.textContent = "데이터 불러오기 일부 오류";
    } else if (mode === "delayed") {
      els.systemStatusDot.classList.add("is-demo");
      els.systemStatusText.textContent = "데이터 불러오는 중 · 지연";
    } else {
      els.systemStatusText.textContent = "데이터 불러오는 중";
    }
    if (mode === "demo") {
      els.lastSyncText.textContent = "기능 확인용 예시 화면";
      return;
    }
    if (state.dashboard.lastSync) state.lastSuccessfulSyncAt = state.dashboard.lastSync;
    const sync = state.lastSuccessfulSyncAt;
    const queryAt = state.lastSuccessfulQueryAt;
    els.lastSyncText.textContent = sync
      ? `최근 동기화 ${formatKstDateTime(sync)}`
      : queryAt ? `최근 조회 ${formatKstDateTime(queryAt)} · 동기화 시각 미확인` : "동기화 시각 확인 중";
  }

  function showDemoBanner(reason) {
    if (state.currentView !== "all") {
      els.demoBanner.hidden = true;
      return;
    }
    els.demoBanner.hidden = false;
    els.demoBannerTitle.textContent = state.source === "api" ? "합성 데이터가 포함되어 있습니다." : "데모 데이터로 보고 있습니다.";
    els.demoBannerReason.textContent = reason;
    els.retryApiButton.textContent = state.source === "api" ? "데이터 새로고침" : "실데이터 다시 연결";
  }

  function hideDemoBanner() {
    els.demoBanner.hidden = true;
  }

  function normalizeFrontendView(view) {
    if (Object.prototype.hasOwnProperty.call(VIEW_ROUTE_MAP, view)) return view;
    return "all";
  }

  function normalizeRoutePath(path) {
    try {
      const decoded = decodeURIComponent(String(path || "/"));
      const trimmed = decoded.split("?")[0].replace(/\/+$/, "");
      return trimmed || "/";
    } catch (_error) {
      return "/";
    }
  }

  function routeViewFromLocation(path = window.location.pathname) {
    const normalized = normalizeRoutePath(path).toLowerCase();
    if (ROUTE_VIEW_MAP[normalized]) return ROUTE_VIEW_MAP[normalized];
    const firstSegment = normalized.split("/").filter(Boolean)[0];
    return firstSegment ? ROUTE_VIEW_MAP[`/${firstSegment.toLowerCase()}`] || "all" : "all";
  }

  function routeForView(view) {
    return VIEW_ROUTE_MAP[normalizeFrontendView(view)] || "/";
  }

  function syncRouteForView(view, { replace = false } = {}) {
    const nextPath = normalizeRoutePath(routeForView(view));
    const currentPath = normalizeRoutePath(window.location.pathname);
    if (currentPath === nextPath) return;
    const url = new URL(window.location.href);
    url.pathname = nextPath;
    if (replace) history.replaceState({}, "", url);
    else history.pushState({}, "", url);
  }

  function isNoticeListView(view = state.currentView) {
    const listViews = ["all", "new", "review", "go", "urgent", "fail", "cancelled", "ended", "result-missing", "undecided", "closed", "collected", "awards"];
    return listViews.includes(view);
  }

  function setView(view, {
    syncRoute = true,
    replaceRoute = false,
    noticeSearchMode = null,
    focusMain = true,
  } = {}) {
    const nextView = normalizeFrontendView(view);
    const clearedServerFilters = resetNoticeFiltersForView();
    const previousView = state.currentView;
    if (previousView !== nextView) {
      if (state.selectedNotice) clearNoticeRoute();
      closeDetail({ updateRoute: false });
    }
    if (["stored", "pps", "prespec"].includes(noticeSearchMode)) {
      state.noticeSearchMode = noticeSearchMode;
    } else if (nextView === "prespec") {
      state.noticeSearchMode = "prespec";
    } else if (nextView !== "new" || previousView === "prespec") {
      state.noticeSearchMode = "stored";
    }
    state.currentView = nextView;
    const titles = {
      all: ["오늘 해야 할 일", "오늘의 확인 항목"],
      collected: ["수집 공고", "수집된 전체 공고"],
      new: ["공고 탐색", "진행중인 공고 조회"],
      review: ["검토 대기", "PASS·REVIEW 중 첨부·자격 확인이 필요한 공고"],
      go: ["GO 후보", "GO 추천 공고"],
      urgent: [`마감 임박 (${URGENT_DEADLINE_DAYS}일)`, `${URGENT_DEADLINE_DAYS}일 이내 마감 공고`],
      fail: ["FAIL 공고", "저장된 현재 자격 판정이 FAIL인 공고"],
      cancelled: ["취소공고", "당시 자격 판정 PASS·REVIEW인 취소 공고"],
      ended: ["종료·취소 공고", "마감·종료·취소된 전체 공고와 당시 분석 이력"],
      "result-missing": ["결과 입력 필요 공고", "PASS·REVIEW 중 입찰마감 후 결과를 기록해야 할 공고"],
      undecided: ["담당자 판단", "공고별 판단 확인"],
      prespec: ["공고 탐색", "사전규격 탐색"],
      closed: ["결과 기록", "결과가 확인된 공고"],
      awards: ["낙찰 분석", "회사별 낙찰 결과"],
      performance: ["회사 실적", "회사 수행 실적"],
    };
    els.pageTitle.textContent = titles[nextView]?.[0] || titles.all[0];
    els.noticeHeading.textContent = titles[nextView]?.[1] || titles.all[1];
    const navigationView = nextView === "prespec"
      ? "new"
      : ["collected", "go", "urgent", "fail", "cancelled", "ended", "result-missing"].includes(nextView) ? "all" : nextView;
    const showDashboardCards = nextView === "all";
    els.navItems.forEach((item) => {
      const active = item.dataset.view === navigationView;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    revealActiveNavigationGroup(navigationView);
    els.kpiViewButtons.forEach((button) => {
      const active = button.dataset.kpiView === view;
      button.setAttribute("aria-pressed", String(active));
      button.closest(".kpi-card")?.classList.toggle("is-active", active);
    });
    const prespecView = nextView === "prespec";
    const resultLearningView = nextView === "closed";
    const performanceView = nextView === "performance";
    const awardsView = nextView === "awards";
    const customView = resultLearningView || performanceView || awardsView;
    els.opportunityHero.hidden = !showDashboardCards;
    els.opportunityKpis.hidden = !showDashboardCards;
    if (els.analysisProgress) els.analysisProgress.hidden = !showDashboardCards;
    els.noticeSection.hidden = customView;
    els.resultLearningSection.hidden = !resultLearningView;
    els.awardResultsSection.hidden = !awardsView;
    els.performanceSection.hidden = !performanceView;
    els.replayButton.hidden = true;
    renderNoticeSearchMode();
    els.footerDisclaimer.textContent = prespecView
      ? "사전규격 분석은 요구조건 사전 검토용이며 입찰 참여 GO/NO-GO 판정을 실행하지 않습니다."
      : resultLearningView
      ? "결과 학습은 출처가 있는 검증 완료 기록만 후속 분석 사실로 사용합니다."
      : awardsView
      ? "낙찰 결과는 낙찰 사실 조회용이며 사업 수행·완료·실적증명서 발급 여부를 확정하지 않습니다."
      : performanceView
        ? "공개 실적은 유사 후보 탐색용이며, 공고별 인정실적·인정금액·정량점수를 확정하지 않습니다."
        : "PAI는 담당자의 판단을 돕는 도구이며 자동 입찰을 수행하지 않습니다.";
    if (prespecView) {
      els.demoBanner.hidden = true;
      renderPreSpecificationView();
      if (!state.prespec.stored.loaded && !state.prespec.stored.loading) void loadStoredPreSpecifications();
    } else if (resultLearningView) {
      els.demoBanner.hidden = true;
      renderResultLearning();
      if (!state.resultLearning.loaded && !state.resultLearning.loading) void loadResultLearning();
    } else if (performanceView) {
      els.demoBanner.hidden = true;
      if (!state.performance.loaded && !state.performance.loading) void loadPerformance();
      else if (state.performance.loaded) renderPerformanceView();
    } else if (awardsView) {
      els.demoBanner.hidden = true;
      renderCompanyAwardsView();
    } else if (state.source === "demo") {
      showDemoBanner(state.sourceReason);
    } else if (state.dashboard.syntheticWarning && state.notices.some((notice) => notice.isSynthetic)) {
      showDemoBanner(state.dashboard.syntheticWarning);
    } else {
      hideDemoBanner();
    }
    if (syncRoute) {
      syncRouteForView(nextView, { replace: replaceRoute });
    }
    closeMobileMenu();
    if (!customView && !prespecView) {
      const desiredStatusScope = noticeStatusScopeForView(nextView);
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
    if (focusMain) els.mainContent.focus({ preventScroll: true });
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
    els.operatorDecisionFilter.value = "all";
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
    if (event.target.closest("[data-manual-analysis]")) return;
    const explicitTarget = event.target.closest("[data-open-notice]");
    const row = event.target.closest("[data-notice-key]");
    if (!row) return;
    if (!explicitTarget && event.target.closest("button, a, input, select, textarea")) return;
    openDetail(row.dataset.noticeKey, explicitTarget || row);
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
    let notice = state.notices.find((item) => item.noticeKey === noticeKey);
    if (!notice && state.source !== "demo") {
      try {
        notice = await hydrateNoticeByKey(noticeKey);
      } catch (error) {
        showToast("분석 요청 불가", `저장된 공고 상태를 확인하지 못했습니다 · ${humanizeError(error)}`, "error");
        return;
      }
    }
    if (
      state.source === "api"
      && ["ANALYZED", "EVALUATED"].includes(notice?.analysisState)
      && notice.analysisAttachmentCoverageComplete
    ) {
      await loadQuantitativeEstimate(noticeKey, { force: true });
    }
    const availability = manualAnalysisAvailability(notice);
    if (!availability.enabled) {
      showToast("분석 요청 불가", availability.reason, "warning");
      return;
    }
    if (!await confirmManualAnalysis(notice, availability)) return;
    const authHeaders = await manualAnalysisAuthHeaders();
    if (!authHeaders) {
      showToast("분석 요청 취소", "4자리 운영 PIN이 입력되지 않았습니다.", "warning");
      return;
    }
    const evaluationOnly = evaluationOnlyManualAnalysis(notice, availability);
    const requestBody = { run_extraction: !evaluationOnly };
    if (availability.recomputeCurrent) requestBody.recompute_current = true;
    if (!evaluationOnly && notice.analysisAttempted) requestBody.retry_reviewed = true;

    state.manualAnalysisRequests.set(noticeKey, "running");
    if (!state.loading) renderNoticeList();
    if (state.selectedNotice?.noticeKey === noticeKey) renderManualAnalysisDetailAction(state.selectedNotice);
    try {
      let payload = unwrapObject(await apiRequest(
        `/notices/${encodeURIComponent(noticeKey)}/analysis/request`,
        {
          method: "POST",
          headers: authHeaders,
          body: JSON.stringify(requestBody),
        },
      ));
      if (stringValue(payload.outcome).toUpperCase() === "QUEUED") {
        const requestId = stringValue(payload.request_id);
        if (!requestId) throw new Error("분석 요청 식별자가 없습니다.");
        for (let poll = 0; poll < MANUAL_ANALYSIS_MAX_POLLS; poll += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, MANUAL_ANALYSIS_POLL_INTERVAL_MS));
          payload = unwrapObject(await apiRequest(
            `/notices/${encodeURIComponent(noticeKey)}/analysis/requests/${encodeURIComponent(requestId)}`,
            { headers: authHeaders },
          ));
          if (stringValue(payload.outcome).toUpperCase() !== "QUEUED") break;
        }
        if (stringValue(payload.outcome).toUpperCase() === "QUEUED") {
          throw new Error("분석이 계속 진행 중입니다. 잠시 후 공고 상태를 다시 확인해 주세요.");
        }
      }
      const outcome = stringValue(payload.outcome).toUpperCase();
      const callCount = Math.max(Number(payload.openai_calls) || 0, 0);
      const message = `${stringValue(payload.message, "분석 상태를 갱신했습니다.")} · 문서 분석 ${formatNumber(callCount)}회`;
      const reloadQuantitative = quantitativeEstimateIsVisible(noticeKey);
      await loadApplicationData({ forceApi: true });
      invalidateQuantitativeEstimate(noticeKey, { forceReload: reloadQuantitative });
      if (outcome === "COOLDOWN") {
        showToast("최근 분석 결과 사용", message, "warning");
      } else if (outcome === "REVIEW") {
        showToast("분석 요청 처리 완료", message, "warning");
      } else {
        showToast(outcome === "ALREADY_ANALYZED" ? "기존 분석 결과 사용" : "공고 분석 완료", message, "success");
      }
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
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

  async function hydrateNoticeByKey(noticeKey, { force = false } = {}) {
    const existingIndex = state.notices.findIndex((notice) => notice.noticeKey === noticeKey);
    const existing = existingIndex >= 0 ? state.notices[existingIndex] : null;
    if (existing && !force) return existing;
    const payload = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}`);
    const detail = normalizeNotice(unwrapObject(payload));
    const mergedSource = existing
      ? { ...existing.raw, ...detail.raw, notice_key: noticeKey }
      : { ...detail.raw, notice_key: noticeKey };
    if (existing) {
      if (!detail.departmentRanking && existing.raw.department_ranking) {
        mergedSource.department_ranking = existing.raw.department_ranking;
      }
      if (!detail.topDepartmentRankings.length && existing.raw.top_department_rankings) {
        mergedSource.top_department_rankings = existing.raw.top_department_rankings;
      }
      if (!detail.departmentReviewCandidates.length && existing.raw.department_review_candidates) {
        mergedSource.department_review_candidates = existing.raw.department_review_candidates;
      }
      if (!detail.regionRouting.length && existing.raw.region_routing) {
        mergedSource.region_routing = existing.raw.region_routing;
      }
    }
    const hydrated = preserveOperatorDecision(normalizeNotice(mergedSource), existing);
    if (existingIndex >= 0) state.notices[existingIndex] = hydrated;
    else state.notices.push(hydrated);
    return hydrated;
  }

  async function hydrateOperatorDecisions(notice) {
    if (
      state.source !== "api"
      || state.writeControlsEnabled
      || !state.operatorDecisionEnabled
      || !notice?.noticeKey
    ) return notice;
    let storedToken = "";
    try {
      storedToken = window.sessionStorage.getItem("pai-loop-operator-pin") || state.manualAnalysisToken || "";
    } catch (_) {
      storedToken = state.manualAnalysisToken;
    }
    if (!storedToken) return notice;
    state.manualAnalysisToken = storedToken;
    notice = updateOperatorDecisionReadState(notice, "LOADING");
    try {
      const decisions = await apiRequest(
        `/operator-decisions/notices/${encodeURIComponent(notice.noticeKey)}`,
        { headers: { "X-PAI-Manual-Token": storedToken } },
      );
      if (!Array.isArray(decisions)) throw new Error("담당자 판단 응답을 확인하지 못했습니다.");
      const decisionSource = { ...notice.raw, decisions };
      // The successful history response is authoritative, including a genuinely empty history.
      for (const key of ["decision", "manager_decision", "human_decision", "decision_comment", "comment", "manager_comment", "decided_by", "decider", "decided_at", "decision_at"]) delete decisionSource[key];
      const merged = normalizeNotice(decisionSource, 0, { operatorDecisionsLoaded: true });
      const index = state.notices.findIndex((item) => item.noticeKey === notice.noticeKey);
      if (index >= 0) state.notices[index] = merged;
      return updateOperatorDecisionReadState(merged, "KNOWN");
    } catch (error) {
      if (error?.status === 401 || error?.status === 429) clearManualAnalysisToken();
      return updateOperatorDecisionReadState(notice, "ERROR");
    }
  }

  async function openDetail(noticeKey, trigger = null, { updateRoute = true } = {}) {
    let baseNotice = state.notices.find((notice) => notice.noticeKey === noticeKey);
    let hydratedFromRoute = false;
    if (!baseNotice && state.source !== "demo") {
      try {
        baseNotice = await hydrateNoticeByKey(noticeKey);
        hydratedFromRoute = true;
      } catch (error) {
        showToast("저장된 공고를 열 수 없습니다", humanizeError(error), "error");
        const routeKey = new URLSearchParams(window.location.search).get("notice");
        if (routeKey === noticeKey) clearNoticeRoute();
        return;
      }
    }
    if (!baseNotice) return;
    state.selectedNotice = baseNotice;
    state.selectedTrigger = trigger || document.activeElement;
    renderDetail(baseNotice);
    els.detailDrawer.classList.add("is-open");
    els.detailDrawer.setAttribute("aria-hidden", "false");
    els.drawerScrim.hidden = true;
    document.body.classList.add("is-locked");
    selectTab("overview");
    requestAnimationFrame(() => els.closeDetailButton.focus({ preventScroll: true }));
    window.setTimeout(() => {
      if (els.detailDrawer.classList.contains("is-open") && !els.detailDrawer.contains(document.activeElement)) {
        els.closeDetailButton.focus({ preventScroll: true });
      }
    }, 280);
    void loadTeamsMockLogs(noticeKey);

    if (updateRoute) updateNoticeRoute(noticeKey);
    if (baseNotice.documentAnalyses.length) void loadPrivateMatchPreview(noticeKey);
    if (state.source !== "api") return;

    state.detailLoading = true;
    els.drawerLoading.hidden = false;
    try {
      let merged = hydratedFromRoute
        ? baseNotice
        : await hydrateNoticeByKey(noticeKey, { force: true });
      merged = await hydrateOperatorDecisions(merged);
      state.selectedNotice = merged;
      renderDetail(merged);
      if (merged.documentAnalyses.length) void loadPrivateMatchPreview(noticeKey);
      applyFilters();
    } catch (error) {
      showToast("상세정보 연결 오류", `${humanizeError(error)} · 목록에 포함된 정보로 표시합니다.`, "warning");
    } finally {
      state.detailLoading = false;
      els.drawerLoading.hidden = true;
      requestAnimationFrame(() => {
        if (els.detailDrawer.classList.contains("is-open") && !els.detailDrawer.contains(document.activeElement)) {
          els.closeDetailButton.focus({ preventScroll: true });
        }
      });
    }
  }

  function moveDetailSelection(delta) {
    const current = state.selectedNotice;
    if (!current || !Number.isInteger(delta) || delta === 0) return;
    const notices = state.filteredNotices.length ? state.filteredNotices : state.notices;
    const currentIndex = notices.findIndex((notice) => notice.noticeKey === current.noticeKey);
    const next = notices[currentIndex + delta];
    if (!next) return;
    const listTrigger = state.selectedTrigger;
    void openDetail(next.noticeKey, listTrigger, { updateRoute: true });
  }

  function renderDetailPosition(notice) {
    const notices = state.filteredNotices.length ? state.filteredNotices : state.notices;
    const index = notices.findIndex((item) => item.noticeKey === notice.noticeKey);
    const hasPosition = index >= 0;
    els.detailPosition.textContent = hasPosition
      ? `${formatNumber(index + 1)} / ${formatNumber(notices.length)} · J/K 이동`
      : "현재 공고";
    els.previousNoticeButton.disabled = !hasPosition || index === 0;
    els.nextNoticeButton.disabled = !hasPosition || index === notices.length - 1;
  }

  function renderDetail(notice) {
    // Previous cancelled-copy expression: cancelled ? "과거 분석 참고".
    const deadline = deadlineInfo(notice.deadline);
    const requirements = eligibilityRequirementsForDisplay(notice);
    const evidence = notice.evidence;
    const analyzed = notice.analysisState === "EVALUATED";
    const cancelled = isCancelledNotice(notice);
    const historicalAnalyzed = notice.historicalAnalysis && !cancelled;
    const displayAnalyzed = !cancelled && (analyzed || historicalAnalyzed);
    const qualityReview = isDocumentQualityReview(notice);
    const effectiveEligibility = effectiveEligibilityStatus(notice);
    const eligibilitySummaryClass = cancelled || !analyzed || qualityReview
      ? "summary-metric--pending"
      : `summary-metric--eligibility summary-metric--eligibility-${effectiveEligibility.toLowerCase()}`;
    els.detailSourceBadge.textContent = sourceKindLabel(notice, true);
    els.detailSourceBadge.classList.toggle("is-demo", notice.isSynthetic);
    els.detailNoticeId.textContent = `공고번호 ${notice.noticeNumber}`;
    renderDetailPosition(notice);
    els.openSourceDialogButton.title = notice.sourceUrl ? "나라장터 원문 링크 확인" : "공개 가능한 원문 링크 상태 확인";
    renderManualAnalysisDetailAction(notice);
    els.detailTitle.textContent = notice.title;
    els.detailAgency.textContent = notice.demandAgency
      ? `수요기관 ${notice.demandAgency}${notice.agency && notice.agency !== notice.demandAgency ? ` · 공고기관 ${notice.agency}` : ""}`
      : `수요기관 ${notice.agency}`;
    els.detailTags.innerHTML = [
      `<span class="detail-tag">${escapeHtml(notice.category)}</span>`,
      `<span class="detail-tag">${escapeHtml(notice.region)}</span>`,
      isEndedNotice(notice) ? `<span class="detail-tag detail-tag--ended ${isCancelledNotice(notice) ? "detail-tag--cancelled" : ""}">${escapeHtml(noticeLifecycleLabel(notice))}</span>` : "",
      notice.historicalAnalysis ? '<span class="detail-tag detail-tag--ended">당시 판정 참고</span>' : "",
      deadline.urgent ? `<span class="detail-tag detail-tag--urgent">${escapeHtml(deadline.relative)}</span>` : "",
    ].join("");
    els.detailFacts.innerHTML = [
      detailFact("입찰서 제출 마감", `${deadline.absolute || deadline.date} ${deadline.time}`.trim() + (deadline.time ? " KST" : "")),
      detailFact("추정가격", formatBudget(notice.budget)),
      detailFact("계약방식", notice.method),
    ].join("");
    els.decisionSummary.innerHTML = [
      summaryMetric("참가자격", cancelled ? "취소 공고" : displayAnalyzed ? analysisStatusLabel(notice) : "미분석", eligibilitySummaryClass),
      summaryMetric("AI 판단", analysisRecommendationLabel(notice), cancelled || !analyzed || qualityReview ? "summary-metric--pending" : "summary-metric--recommendation"),
      summaryMetric("담당자 판단", operatorDecisionLabel(notice), notice.decision ? "summary-metric--operator" : "summary-metric--pending", true),
      summaryMetric(notice.historicalAnalysis ? "당시 제출 준비도" : "제출 준비도", cancelled ? "현재 판단 미제공" : qualityReview ? "근거 보완 후 산정" : displayAnalyzed ? `${formatScore(notice.readinessScore)}/100` : "미산정", cancelled || !analyzed || qualityReview ? "summary-metric--pending" : ""),
      summaryMetric("판정 근거", evidence.length ? `${formatNumber(evidence.length)}건 연결` : "연결 확인 필요", evidence.length ? "" : "summary-metric--pending"),
      awardHistorySummaryMetric(notice),
    ].join("");
    renderRecommendationCondition(notice);
    els.analysisPipeline.innerHTML = renderPipeline(notice);
    els.detailSummary.textContent = cancelled
      ? `${notice.analysisReason || "취소 공고로 현재 입찰 검토와 담당자 판단 대상에서 제외되었습니다."}${notice.historicalAnalysis ? " 과거 분석은 현재 상태가 아닌 ‘당시 판정 참고’로만 제공합니다." : ""}`
      : notice.historicalAnalysis
      ? `${notice.historicalAnalysisReason} ${notice.summary || "당시 종합 판정값을 참고용으로 표시합니다."}`
      : qualityReview
      ? notice.analysisReason || "원문 근거 검증을 보완한 뒤 자격과 추천을 확정합니다."
      : analyzed ? notice.summary : notice.analysisReason;
    els.briefEvidenceLabel.innerHTML = cancelled
      ? `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M8 12h8" /></svg>${notice.historicalAnalysis ? "취소 · 당시 근거 참고" : "취소 공고"}`
      : qualityReview
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M12 8v4M12 16h.01" /></svg>근거 보완'
      : notice.historicalAnalysis && evidence.length
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>당시 근거 연결'
      : displayAnalyzed && evidence.length
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>근거 연결'
      : notice.historicalAnalysis
        ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M8 12h8" /></svg>당시 판정'
        : '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M12 8v4M12 16h.01" /></svg>분석 대기';
    els.briefEvidenceLabel.classList.toggle("is-pending", cancelled || qualityReview || !analyzed || !evidence.length);
    renderDocumentAnalyses(notice);
    els.evidenceCount.textContent = String(evidence.length);
    renderEligibilityPanel(notice, requirements);
    renderActions(notice);
    els.evidenceList.innerHTML = evidence.length
      ? evidence.map(renderEvidence).join("")
      : emptyPanel("연결된 원문 근거가 없습니다", "근거가 없는 결과는 확정 판정으로 사용하지 마세요.");
    renderQuantAndRisk(notice);
    renderAwardHistoryPanel(notice);
    if (state.source === "api") void loadStoredAwardHistory(notice.noticeKey);
    renderTeamsPreview(notice);
    renderExistingDecision(notice);
    els.drawerScroll.scrollTop = 0;
  }

  function renderRecommendationCondition(notice) {
    const conditional = ["CONDITIONAL_GO", "HOLD"].includes(notice.recommendation)
      && notice.analysisState === "EVALUATED"
      && !isCancelledNotice(notice)
      && !isDocumentQualityReview(notice);
    const conditions = arrayValue(notice.recommendationConditions);
    els.recommendationCondition.hidden = !conditional;
    if (!conditional) {
      els.recommendationCondition.replaceChildren();
      return;
    }
    const hasPublishedCondition = conditions.length > 0;
    const heading = hasPublishedCondition
      ? `조건부 GO · 확인할 조건 ${formatNumber(conditions.length)}건`
      : "권고 보류 · 조건 근거 없음";
    const conditionContent = hasPublishedCondition
      ? `<ul>${conditions.map((condition) => `<li>${escapeHtml(condition)}</li>`).join("")}</ul>`
      : "<span>조건부 판단을 표시할 공개 근거가 없습니다. 담당자가 원문을 확인하세요.</span>";
    els.recommendationCondition.innerHTML = `<strong>${escapeHtml(heading)}</strong>${conditionContent}`;
  }

  function renderManualAnalysisDetailAction(notice) {
    const availability = manualAnalysisAvailability(notice);
    const running = state.manualAnalysisRequests.get(notice.noticeKey) === "running";
    const label = running ? manualAnalysisLabel(notice, true) : availability.label;
    els.manualAnalyzeButton.hidden = false;
    els.manualAnalyzeButton.disabled = running || !availability.enabled;
    els.manualAnalyzeButton.dataset.noticeKey = notice.noticeKey;
    els.manualAnalyzeButton.dataset.availabilityCode = availability.code;
    els.manualAnalyzeButton.querySelector("span").textContent = label;
    els.manualAnalyzeButton.title = availability.reason;
    els.manualAnalyzeButton.setAttribute(
      "aria-label",
      `${notice.title} ${label}${availability.enabled ? "" : ` · ${availability.reason}`}`,
    );
  }

  function detailFact(label, value) {
    return `<div class="detail-fact"><small>${escapeHtml(label)}</small><strong title="${escapeAttribute(value)}">${escapeHtml(value)}</strong></div>`;
  }

  function summaryMetric(label, value, className, operatorDecision = false) {
    return `<div class="summary-metric ${className}"${operatorDecision ? " data-operator-decision-summary" : ""}><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`;
  }

  function awardHistorySummaryState(notice) {
    const meta = state.awardHistoryMeta[notice.noticeKey] || {};
    if (meta.status === "loading") return { value: "확인 중", pending: true };
    if (meta.status === "error") return { value: "확인 실패", pending: true };
    if (notice.awardHistory.length) return { value: `${formatNumber(notice.awardHistory.length)}건`, pending: false };
    if (["empty", "ready"].includes(meta.status) || state.source === "demo") {
      return { value: "저장본 0건", pending: false };
    }
    return { value: "확인 전", pending: true };
  }

  function awardHistorySummaryMetric(notice) {
    const summary = awardHistorySummaryState(notice);
    return `<div class="summary-metric ${summary.pending ? "summary-metric--pending" : ""}" data-history-summary><small>최근 3년 이력</small><strong>${escapeHtml(summary.value)}</strong></div>`;
  }

  function updateAwardHistorySummaryMetric(notice) {
    const metric = els.decisionSummary.querySelector("[data-history-summary]");
    if (!metric) return;
    const summary = awardHistorySummaryState(notice);
    metric.classList.toggle("summary-metric--pending", summary.pending);
    const value = metric.querySelector("strong");
    if (value) value.textContent = summary.value;
  }

  function renderDocumentAnalyses(notice) {
    const statuses = arrayValue(notice.attachmentAnalysisStatuses);
    const currentNames = new Set(statuses.filter((item) => item.state === "ANALYZED").map((item) => item.document_name));
    const analyses = [
      ...notice.documentAnalyses.filter((item) => !statuses.length || currentNames.has(item.documentName)),
      ...statuses.filter((item) => item.state !== "ANALYZED").map((item, index) => normalizeDocumentAnalysis({
        document_name: item.document_name,
        status: item.state,
        summary: item.reason,
        needs_review: item.state === "REVIEW",
      }, index)),
    ];
    const reviewCount = analyses.filter((item) => item.needsReview).length;
    const pendingCount = analyses.filter((item) => item.status === "PENDING").length;
    const pointInTime = notice.historicalAnalysis ? "당시 " : "";
    els.documentAnalysisState.className = "document-analysis-state";
    renderPrivateMatchPreview(notice);

    if (analyses.length) {
      els.documentAnalysisState.textContent = reviewCount || pendingCount
        ? `${pointInTime}검토 ${reviewCount}건 · 분석 대기 ${pendingCount}건`
        : `${pointInTime}구조화 완료`;
      els.documentAnalysisState.classList.add(reviewCount || pendingCount ? "is-review" : "is-ready");
      els.documentAnalysisList.innerHTML = analyses.map((item) => {
        const statusLabel = item.status === "PENDING"
          ? `${pointInTime}분석 대기`
          : item.needsReview
          ? `${pointInTime}검토 필요`
          : ["FAILED", "ERROR"].includes(item.status) ? `${pointInTime}분석 오류` : `${pointInTime}분석 완료`;
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
      description = "종합 평가는 완료됐지만 현재 서버 응답에는 안전하게 표시할 문서별 요약이 없습니다.";
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
      const noVerifiedPolicy = error?.status === 422;
      state.privateMatchPreviews[noticeKey] = {
        status: noVerifiedPolicy ? "unavailable" : "error",
        message: noVerifiedPolicy
          ? "검증된 공개 자격정책이 아직 없습니다. 종합 판단을 임의로 보완하지 않습니다."
          : humanizeError(error),
        data: null,
      };
      if (!noVerifiedPolicy && force) showToast("회사 데이터 매칭 조회 오류", humanizeError(error), "warning");
    } finally {
      if (state.selectedNotice?.noticeKey === noticeKey) {
        renderPrivateMatchPreview(notice);
        renderEligibilityPanel(notice);
        renderActions(notice);
      }
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

  function eligibilityRequirementsForDisplay(notice) {
    const storedRequirements = arrayValue(notice?.requirements);
    if (storedRequirements.length) return storedRequirements;

    // Public detail responses intentionally omit materialised company values
    // and internal evidence identifiers. Only the separately validated
    // ELIGIBILITY projection may supplement this panel; the complete four-
    // class policy preview remains in its own section below.
    const preview = state.privateMatchPreviews[notice?.noticeKey];
    if (preview?.status !== "ready") return [];
    // A failed AND condition must not erase independently satisfied conditions.
    // Keep incomplete/stale document projections provisional, independently of
    // the persisted aggregate verdict (which this adapter never changes).
    const currentEvidence = notice.analysisState === "EVALUATED"
      && !isDocumentQualityReview(notice);
    return arrayValue(preview.data?.matches)
      .filter((item) => item?.category === "ELIGIBILITY")
      .map((item, index) => {
        const outcome = stringValue(item.outcome).toUpperCase();
        const publicProfileMatches = ["PASS_CURRENT", "PASS_EXCEPTION"].includes(outcome);
        const status = publicProfileMatches
          ? currentEvidence ? outcome : "REVIEW"
          : outcome === "FAIL_CONFIRMED" && currentEvidence
            ? "FAIL"
            : outcome === "REVIEW" || item.blocking
            ? "REVIEW"
            : "UNKNOWN";
        const description = publicProfileMatches
          ? currentEvidence
            ? outcome === "PASS_EXCEPTION"
              ? "허용 예외에 따라 현재 충족합니다. 예외 적용 조건과 마감일 기준 증빙을 다시 확인하세요."
              : "현재 회사 정보와 일치합니다. 공고 마감일 기준으로 최신 증빙을 다시 확인하세요."
            : "공개 회사정보와 일치하지만 현재 첨부 검증이 완료되지 않았습니다. 공고 마감일 기준으로 다시 확인하세요."
          : stringValue(item.message, "공개 정책 보조 근거입니다. 공고 마감일 기준으로 최신 증빙을 확인하세요.");
        return {
          id: stringValue(item.requirementId, `public-eligibility-${index + 1}`),
          title: stringValue(item.condition, `자격 조건 ${index + 1}`),
          description,
          status,
          evidenceId: "",
          reasonCode: outcome,
          source: "PUBLIC_POLICY_SUPPLEMENT",
        };
      });
  }

  function publicEligibilityPolicyPending(notice) {
    if (state.source !== "api" || !notice?.documentAnalyses?.length) return false;
    const status = state.privateMatchPreviews[notice.noticeKey]?.status || "idle";
    return ["idle", "loading"].includes(status);
  }

  function renderEligibilityPanel(notice, requirements = eligibilityRequirementsForDisplay(notice)) {
    // The aggregate pill always comes from the persisted analysis. Public
    // policy matches are supplemental cards and never rewrite this headline.
    els.eligibilityOverall.innerHTML = analysisStatusPill(notice);
    if (requirements.length) {
      els.requirementList.innerHTML = requirements.map(renderRequirement).join("");
      return;
    }
    if (publicEligibilityPolicyPending(notice)) {
      els.requirementList.innerHTML = emptyPanel(
        "공개 자격 판정을 불러오는 중입니다",
        "검증된 공고 조건과 공개 회사 프로필을 안전하게 연결하고 있습니다.",
      );
      return;
    }
    const preview = state.privateMatchPreviews[notice.noticeKey];
    if (preview?.status === "unavailable") {
      els.requirementList.innerHTML = emptyPanel(
        "검증된 공개 자격정책이 없습니다",
        "공개 보조 근거가 없으므로 종합 판단을 PASS로 보완하지 않습니다.",
      );
      return;
    }
    if (preview?.status === "ready") {
      els.requirementList.innerHTML = emptyPanel(
        "참가 자격으로 분류된 조건이 없습니다",
        "행동 필요·체크리스트·정보 항목은 조건별 검토 내용에서 확인하세요.",
      );
      return;
    }
    els.requirementList.innerHTML = emptyPanel(
      "구조화된 자격 조건이 없습니다",
      "공개 검증된 첨부파일 분석이 준비되면 조건별 판정이 표시됩니다.",
    );
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
        loading: ["조회 중", "회사 기준과 공고 조건을 비교하고 있습니다", "적격성·행동필요·체크리스트·정보를 분리합니다."],
        waiting: ["준비 대기", "판단 기준 적용 준비가 필요합니다", preview?.message || "공고문 구조화 분석을 먼저 완료하세요."],
        unavailable: ["정책 없음", "검증된 공개 자격정책이 없습니다", preview?.message || "종합 판단은 저장된 분석 상태를 그대로 유지합니다."],
        error: ["연결 오류", "온라인 공개 프로필 판정을 불러오지 못했습니다", preview?.message || "잠시 후 다시 확인해 주세요."],
      }[status] || ["확인 대기", "온라인 공개 프로필 판정 준비", "잠시 후 다시 확인해 주세요."];
      els.privateMatchBadge.textContent = content[0];
      els.privateMatchBadge.classList.add(status === "loading" ? "is-loading" : status === "error" ? "is-error" : "is-review");
      els.privateMatchBody.innerHTML = `<div class="private-match-waiting"><strong>${escapeHtml(content[1])}</strong><span>${escapeHtml(content[2])}</span></div>`;
      els.privateMatchNote.textContent = "증빙 원문은 공개하지 않습니다.";
      return;
    }

    const data = preview.data;
    els.privateMatchBadge.textContent = data.blockingActions ? `확인 전 보류 ${data.blockingActions}건` : "회사 기준 적용";
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
    els.privateMatchNote.textContent = "증빙 원문은 공개하지 않습니다. 체크리스트와 정보는 그 자체로 참가자격 ‘확인 필요’를 만들지 않습니다.";
  }

  function privateMatchMetric(label, value, unit) {
    return `<div class="private-match-metric"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)} <span>${escapeHtml(unit)}</span></strong></div>`;
  }

  function renderPrivateMatchItem(item) {
    const outcomeLabels = {
      PASS_CURRENT: "현재 충족 · 마감일 재확인",
      PASS_EXCEPTION: "조건부 충족 · 적용조건 재확인",
      FAIL_CONFIRMED: "미충족",
      BLOCK_UNTIL_CONFIRMED: "확인 전 보류",
      READY: "체크 준비",
      CHECK_REQUIRED: "체크 필요",
      ACKNOWLEDGED: "정보 확인",
      INFORMATION: "정보",
      REVIEW: "확인 필요",
    };
    const stateClass = ({
      PASS_CURRENT: "is-pass_current",
      PASS_EXCEPTION: "is-pass_exception",
      FAIL_CONFIRMED: "is-blocking",
      REVIEW: "is-review",
    })[item.outcome] || (item.blocking
      ? "is-blocking"
      : item.category === "CHECKLIST"
        ? "is-action"
        : "is-unmapped");
    const evidenceMarkup = item.evidence
      ? `<div class="private-match-candidates"><span>공개 근거</span><span>${escapeHtml(item.evidence.name)}</span><span>${escapeHtml(item.evidence.fileName)}</span></div>`
      : item.companyFactKey
        ? '<div class="private-match-candidates"><span>판단 기준</span><span>공개 회사정보 연결</span></div>'
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
    const cancelled = isCancelledNotice(notice);
    const historicalAnalyzed = notice.historicalAnalysis && !cancelled;
    const displayAnalyzed = !cancelled && (analyzed || historicalAnalyzed);
    const effectiveEligibility = effectiveEligibilityStatus(notice);
    const version = notice.latestVersion;
    const extractionComplete = notice.analysisAttachmentCount > 0
      ? notice.analysisAttachmentCoverageComplete
        && notice.analysisAttachmentsAccepted === notice.analysisAttachmentCount
      : version
        ? version.documentComplete !== false && version.extractionStatus === "COMPLETE"
        : hasDocuments;
    const coverageRemaining = Math.max(0, notice.analysisAttachmentCount - notice.analysisAttachmentsAccepted);
    const extractionDetail = notice.analysisAttachmentCount > 0
      ? `첨부 ${notice.analysisAttachmentCount} · 확인 ${notice.analysisAttachmentsAudited} · 분석 ${notice.analysisAttachmentsAccepted} · 보완 ${coverageRemaining}`
      : version
        ? `v${version.versionNo} · ${version.extractionConfidence === null ? version.extractionStatus : `${Math.round(version.extractionConfidence)}%`}`
        : hasDocuments ? `${notice.evidence.length}개 근거` : "확인 필요";
    const steps = [
      { name: "공고 수집", detail: "원문 보존", status: "done" },
      { name: "첨부 추출", detail: extractionDetail, status: extractionComplete ? "done" : "review" },
      { name: "규칙 판정", detail: cancelled ? "취소 · 현재 판단 비활성" : displayAnalyzed ? analysisStatusLabel(notice) : hasRules ? "분석 대기" : "조건 대기", status: cancelled ? "pending" : displayAnalyzed ? (["REVIEW", "UNKNOWN"].includes(effectiveEligibility) ? "review" : "done") : "pending" },
      { name: "담당자 결정", detail: cancelled ? "취소 · 저장 비활성" : operatorDecisionLabel(notice), status: cancelled ? "pending" : notice.decision && hasKnownOperatorDecision(notice) ? "done" : "pending" },
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
      : requirement.status === "PASS_CURRENT"
        ? '<circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2" />'
        : requirement.status === "PASS_EXCEPTION"
          ? '<path d="M12 7v6M12 17h.01" />'
      : requirement.status === "FAIL"
        ? '<path d="m7 7 10 10M17 7 7 17" />'
        : '<path d="M12 7v6M12 17h.01" />';
    return `
      <div class="requirement-item is-${escapeAttribute(mode)}">
        <span class="requirement-icon" aria-hidden="true"><svg viewBox="0 0 24 24">${icon}</svg></span>
        <span class="requirement-copy"><strong>${escapeHtml(requirement.title)}</strong><small>${escapeHtml(requirement.description)}</small></span>
        <span class="requirement-status">${escapeHtml(STATUS_LABELS[requirement.status] || STATUS_LABELS.UNKNOWN)}</span>
        ${requirement.evidenceId ? '<button type="button" class="evidence-jump" data-evidence-jump>근거 보기</button>' : ''}
      </div>`;
  }

  function submissionCheckItemsForDisplay(notice) {
    // This is a source index, not an eligibility rule or a yes/no classifier.
    // Keep complete quotes (including exceptions and negation) for human review.
    const definitions = [
      ["electronic-bid", "전자입찰 여부", (text) => /전자입찰|입찰(?:서)?.{0,30}전자(?:적|적으로|제출|방식)/.test(text)],
      ["bid-deadline", "입찰 마감일", (text) => /입찰(?:서)?(?:의)?(?:접수|제출)?(?:마감|기한|기간|일시)/.test(text)],
      ["proposal-deadline", "제안서 마감일", (text) => /제안서(?:의)?(?:접수|제출)?(?:마감|기한|기간|일시)/.test(text.replace(/가격제안서/g, "가격서"))],
      ["proposal-method", "제안서 제출 유형", (text) => text.split(/[.!?;。]/).some((clause) => /제안서/.test(clause) && /제출|접수/.test(clause) && /전자|온라인|방문|우편|직접|대면|나라장터|이메일|e-?발주시스템|e-?mail/i.test(clause))],
      ["bid-security", "입찰보증보험", (text) => /입찰보증(?:금|보험|증권|서)/.test(text)],
      ["lead-presentation", "총괄책임자 PT 진행 여부", (text) => /총괄책임자|사업책임자|연구책임자|사업관리자|총괄PM/i.test(text) && /발표|설명회|프레젠테이션|(?:^|[^a-z])PT(?:[^a-z]|$)/i.test(text)],
      ["training-venue", "연수원·강의장 보유 여부", (text) => /연수원|강의장|교육장|교육시설/.test(text) && /보유|소유|임차|대관|임대|확보/.test(text)],
      ["personnel", "참여인력 자격조건", (text) => /참여인력|투입인력|참여자|연구진|연구원|책임자|강사|수행인력|전문인력/.test(text) && /자격|학위|학사|석사|박사|경력|전공|자격증|재직|상근|\d+명/.test(text)],
      ["settlement", "정산 여부", (text) => /정산/.test(text)],
      ["nonprofit-profit", "비영리 이윤제외", (text) => /비영리/.test(text) && /이윤|이익/.test(text)],
    ];
    const raw = firstObject(notice.raw);
    const analyses = arrayValue(firstValue(raw.document_analyses, raw.documentAnalyses, []));
    const statuses = arrayValue(notice.attachmentAnalysisStatuses);
    const quoteKey = (value) => stringValue(value).replace(/\s+/g, " ").trim().toLocaleLowerCase("ko-KR");
    const documentNames = analyses.map((item, index) => normalizeDocumentAnalysis(item, index).documentName);
    const sources = flattenDocumentEvidence(analyses).filter((item) => {
      // Public attachment status identifies documents by filename. Ambiguous
      // duplicate names cannot prove which current attachment owns the quote.
      if (documentNames.filter((name) => name === item.file).length !== 1) return false;
      const current = statuses.filter((status) => status.document_name === item.file);
      return !statuses.length || (current.length === 1 && current[0].state === "ANALYZED");
    });
    const items = definitions.map(([id, label, matches]) => {
      const seen = new Set();
      const anchors = sources.filter((item) => {
        const key = [item.file, item.page, quoteKey(item.quote)].join("|");
        if (seen.has(key) || !submissionQuoteMatches(id, matches, item)) return false;
        seen.add(key);
        return true;
      }).map((item) => {
        const evidence = arrayValue(notice.evidence).filter((candidate) => (
          candidate.file === item.file && candidate.page === item.page
          && quoteKey(candidate.quote) === quoteKey(item.quote)
        ));
        return {
          quote: item.quote,
          location: `${item.file} · ${item.page}`,
          evidenceId: evidence.length === 1 ? evidence[0].id : "",
          sourceUrl: "",
        };
      });
      return { id, label, sources: anchors };
    });
    // Notice.deadline is the public bid deadline. Never substitute it for a
    // missing proposal deadline, even when the two often happen to coincide.
    if (validDate(notice.deadline)) {
      items.find((item) => item.id === "bid-deadline").sources.unshift({
        quote: formatKstDateTime(notice.deadline),
        location: "공고 기본정보 · 입찰서 제출 마감",
        evidenceId: "",
        sourceUrl: safeHttpUrl(notice.sourceUrl),
      });
    }
    return items;
  }

  function submissionQuoteMatches(id, matches, item) {
    const quote = item.quote.replace(/\s+/g, "");
    if (matches(quote)) return true;
    // A verified source section may identify what 'submission' means. Keep
    // the original quote and location; never use a generated summary here.
    const section = stringValue(item.page).replace(/[\s()]/g, "");
    const proposalSection = /(?:기술)?제안서(?:제출|접수)/.test(section) && !/가격제안/.test(section);
    if (id === "proposal-deadline" && proposalSection) return /(?:제출|접수)(?:일시|기한|기간|마감)/.test(quote);
    if (id === "proposal-method" && proposalSection) return matches(`제안서${quote}`);
    if (id === "bid-deadline" && !proposalSection && /(?:가격제안입찰|가격제안|입찰)(?:서)?(?:제출|접수)/.test(section)) {
      return /(?:제출|접수)(?:일시|기한|기간|마감)/.test(quote);
    }
    return false;
  }

  function renderSubmissionCheckItem(item) {
    const sources = item.sources.map((source) => `
      <div class="submission-check-source">
        <p>${escapeHtml(source.quote)}</p>
        <small>${escapeHtml(source.location)}</small>
        ${source.evidenceId ? `<button type="button" class="evidence-jump" data-evidence-jump="${escapeAttribute(source.evidenceId)}">근거 보기</button>` : ""}
        ${source.sourceUrl ? `<a class="evidence-jump" href="${escapeAttribute(source.sourceUrl)}" target="_blank" rel="noopener noreferrer">공고 원문</a>` : ""}
      </div>`).join("");
    return `<li data-submission-check="${escapeAttribute(item.id)}"><strong>${escapeHtml(item.label)}</strong>${sources || '<p class="submission-check-missing">원문 확인 필요</p>'}</li>`;
  }

  function renderActions(notice) {
    if (isCancelledNotice(notice)) {
      els.actionCard.hidden = true;
      els.actionList.innerHTML = "";
      return;
    }
    els.actionCard.hidden = false;
    els.actionList.innerHTML = submissionCheckItemsForDisplay(notice).map(renderSubmissionCheckItem).join("");
  }

  function renderEvidence(item) {
    const confidence = item.confidence;
    const statusClass = item.status === "PROVISIONAL" ? "is-provisional" : item.status === "MISSING" ? "is-missing" : "";
    const statusLabel = item.status === "VERIFIED" ? "검증됨" : item.status === "PROVISIONAL" ? "" : "누락";
    return `
      <article class="evidence-card" id="evidence-${escapeAttribute(item.id)}">
        <div class="evidence-card__head">
          <span class="evidence-file"><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6V3Z" /><path d="M14 3v5h5" /></svg><span>${escapeHtml(item.file)}</span></span>
          ${statusLabel ? `<span class="evidence-status ${statusClass}">${escapeHtml(statusLabel)}</span>` : ""}
        </div>
        <blockquote class="evidence-quote">“${escapeHtml(item.quote)}”</blockquote>
        <div class="evidence-card__foot">
          <span>${escapeHtml(item.page)}</span>
          <span class="confidence"><span>추출 신뢰도 ${confidence === null ? "—" : `${Math.round(confidence)}%`}</span><span class="mini-bar" aria-hidden="true"><span style="width:${confidence ?? 0}%"></span></span></span>
        </div>
      </article>`;
  }

  function quantitativeEstimateIsVisible(noticeKey) {
    return state.selectedNotice?.noticeKey === noticeKey
      && els.tabButtons.some((button) => button.dataset.tab === "quant" && button.getAttribute("aria-selected") === "true");
  }

  function quantitativeRuleRetryRequired(notice) {
    if (!notice?.noticeKey || !notice.analysisAttachmentCoverageComplete) return false;
    const result = state.quantitativeEstimates[notice.noticeKey]?.data;
    return stringValue(result?.rule_source_status).toUpperCase() === "INCOMPLETE"
      && stringValue(result?.activation_status).toUpperCase() === "REVIEW_REQUIRED";
  }

  function invalidateQuantitativeEstimate(noticeKey, { forceReload = false } = {}) {
    if (!noticeKey) return;
    delete state.quantitativeEstimates[noticeKey];
    if (forceReload && state.source === "api") {
      void loadQuantitativeEstimate(noticeKey, { force: true });
    }
  }

  async function loadQuantitativeEstimate(noticeKey, { force = false } = {}) {
    if (state.source !== "api") return;
    const current = state.quantitativeEstimates[noticeKey];
    if (!force && ["loading", "ready"].includes(current?.status)) return;
    const requestToken = Symbol(noticeKey);
    state.quantitativeEstimates[noticeKey] = { status: "loading", data: null, message: "", requestToken };
    if (state.selectedNotice?.noticeKey === noticeKey) renderQuantAndRisk(state.selectedNotice);
    try {
      const data = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}/quantitative-estimate`);
      if (state.quantitativeEstimates[noticeKey]?.requestToken !== requestToken) return;
      state.quantitativeEstimates[noticeKey] = { status: "ready", data, message: "", requestToken };
    } catch (error) {
      if (state.quantitativeEstimates[noticeKey]?.requestToken !== requestToken) return;
      state.quantitativeEstimates[noticeKey] = { status: "error", data: null, message: humanizeError(error), requestToken };
    } finally {
      if (state.quantitativeEstimates[noticeKey]?.requestToken !== requestToken) return;
      if (state.selectedNotice?.noticeKey === noticeKey) {
        renderQuantAndRisk(state.selectedNotice);
        renderManualAnalysisDetailAction(state.selectedNotice);
      }
    }
  }

  function renderQuantAndRisk(notice) {
    renderRiskPanel(notice);
    renderQuantitativeDiagnosticsControl(notice);
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

  function renderQuantitativeDiagnosticsControl(notice) {
    document.getElementById("quantitativeDiagnosticsControl")?.remove();
    if (state.source !== "api" || !state.manualAnalysisEnabled) return;
    const noticeKey = notice.noticeKey;
    const container = document.createElement("details");
    container.id = "quantitativeDiagnosticsControl";
    const summary = document.createElement("summary");
    summary.textContent = "운영자 정량 진단";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-secondary";
    button.textContent = "정량 검증 사유 확인";
    const retryButton = document.createElement("button");
    retryButton.type = "button";
    retryButton.className = "btn btn-secondary";
    retryButton.textContent = "검토 중인 정량 첨부 재추출";
    retryButton.hidden = true;
    const retryNotice = document.createElement("p");
    retryNotice.textContent = "재추출은 현재 검토 대상만 한 번씩 다시 분석합니다. 공고당 문서 분석 요청은 최대 20회이며, 실행 후 새 결과를 확인하세요.";
    retryNotice.hidden = true;
    const statisticsButton = document.createElement("button");
    statisticsButton.type = "button";
    statisticsButton.className = "btn btn-secondary";
    statisticsButton.textContent = "전체 공고 분석 통계 조회";
    const output = document.createElement("pre");
    output.style.whiteSpace = "pre-wrap";
    output.style.overflowWrap = "anywhere";
    output.setAttribute("aria-live", "polite");
    container.append(summary, button, statisticsButton, retryNotice, retryButton, output);
    els.quantSeparationNote.insertAdjacentElement("afterend", container);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const headers = await manualAnalysisAuthHeaders();
        if (!headers) return;
        output.textContent = "현재 첨부와 배점표 검증 상태를 확인하고 있습니다.";
        const data = await apiRequest(
          `/notices/${encodeURIComponent(noticeKey)}/analysis/quantitative-diagnostics`,
          { method: "POST", headers },
        );
        if (!container.isConnected || state.selectedNotice?.noticeKey !== noticeKey) return;
        // The PIN-only endpoint returns bounded, redacted structural facts.
        // Render as text, never HTML, and do not persist diagnostics locally.
        output.textContent = JSON.stringify(data, null, 2);
        const retryable = data.review_candidate_count > 0
          && notice.analysisAttachmentCoverageComplete
          && noticeLifecycleStatus(notice) === "OPEN";
        retryButton.hidden = !retryable;
        retryNotice.hidden = !retryable;
      } catch (error) {
        if (container.isConnected) output.textContent = humanizeError(error);
      } finally {
        button.disabled = false;
      }
    });
    retryButton.addEventListener("click", async () => {
      retryButton.disabled = true;
      button.disabled = true;
      statisticsButton.disabled = true;
      try {
        const headers = await manualAnalysisAuthHeaders();
        if (!headers) return;
        output.textContent = "검토 중인 정량 첨부의 재추출을 요청합니다.";
        let result = await apiRequest(
          `/notices/${encodeURIComponent(noticeKey)}/analysis/request`,
          { method: "POST", headers, body: JSON.stringify({ run_extraction: true, retry_reviewed: true }) },
        );
        for (let poll = 0; result.outcome === "QUEUED" && poll < MANUAL_ANALYSIS_MAX_POLLS; poll += 1) {
          output.textContent = "첨부를 재추출하고 검증하고 있습니다. 요청을 중복 실행하지 마세요.";
          await new Promise((resolve) => window.setTimeout(resolve, MANUAL_ANALYSIS_POLL_INTERVAL_MS));
          result = await apiRequest(
            `/notices/${encodeURIComponent(noticeKey)}/analysis/requests/${encodeURIComponent(result.request_id)}`,
            { headers },
          );
        }
        if (!container.isConnected) return;
        output.textContent = JSON.stringify(result, null, 2);
        // Keep the completed operator response visible; refresh the ordinary
        // estimate on the next explicit tab visit instead of destroying it.
        invalidateQuantitativeEstimate(noticeKey);
      } catch (error) {
        if (container.isConnected) output.textContent = humanizeError(error);
      } finally {
        retryButton.disabled = false;
        button.disabled = false;
        statisticsButton.disabled = false;
      }
    });
    statisticsButton.addEventListener("click", async () => {
      statisticsButton.disabled = true;
      button.disabled = true;
      retryButton.disabled = true;
      try {
        const headers = await manualAnalysisAuthHeaders();
        if (!headers) return;
        const report = await collectAnalysisStatistics(headers, (message) => {
          if (container.isConnected) output.textContent = message;
        });
        if (container.isConnected) output.textContent = JSON.stringify(report, null, 2);
      } catch (error) {
        if (container.isConnected) output.textContent = humanizeError(error);
      } finally {
        statisticsButton.disabled = false;
        button.disabled = false;
        retryButton.disabled = false;
      }
    });
  }

  async function collectAnalysisStatistics(headers, onProgress) {
    const startedAt = new Date().toISOString();
    const stored = new Map();
    for (let offset = 0; ; offset += 200) {
      onProgress(`저장 공고 ${stored.size}건 확인 · 통계 조회는 문서 분석 API를 실행하지 않습니다.`);
      const page = await apiRequest(`/notices?limit=200&offset=${offset}`, { timeoutMs: NOTICE_REQUEST_TIMEOUT_MS });
      if (!Array.isArray(page)) throw new Error("공고 목록 응답을 확인할 수 없습니다.");
      for (const item of page) stored.set(item.notice_key, item);
      if (page.length < 200) break;
    }
    const notices = [...stored.values()].filter((item) => item.status === "OPEN"
      && item.provider_disposition !== "CANCELLED" && new Date(item.deadline).getTime() >= Date.now());
    const rows = [];
    for (const notice of notices) {
      onProgress(`진행 공고 ${rows.length}/${notices.length}건 집계 · 첨부·자격·정량 검증 상태를 조회합니다.`);
      const row = {
        notice_key: notice.notice_key,
        title: notice.title,
        analysis_state: notice.analysis_state,
        analysis_reason_code: notice.analysis_reason_code,
        analysis_attempted: notice.analysis_attempted,
        expected_attachments: notice.analysis_attachment_count,
        audited_attachments: notice.analysis_attachments_audited,
        accepted_attachments: notice.analysis_attachments_accepted,
        attachment_coverage_complete: notice.analysis_attachment_coverage_complete,
        eligibility: notice.latest_evaluation?.eligibility || "NOT_EVALUATED",
        profile_status: "NOT_QUERIED",
        activation_status: "NOT_QUERIED",
        score_status: "NOT_QUERIED",
        available_candidates: 0,
        review_candidates: 0,
        issues: [],
        read_errors: [],
      };
      // The notice list already proves that unattempted rows have no current
      // attachment analysis. Avoid hundreds of redundant detail reads.
      if (notice.analysis_attempted) {
        try {
          const estimate = await apiRequest(`/notices/${encodeURIComponent(notice.notice_key)}/quantitative-estimate`);
          row.activation_status = estimate.activation_status;
          row.score_status = estimate.overall_status;
          row.estimated_points = estimate.estimated_points;
          row.lower_points = estimate.lower_points;
          row.upper_points = estimate.upper_points;
          row.total_max_points = estimate.total_max_points;
          row.evidence_coverage_pct = estimate.evidence_coverage_pct;
          row.criteria_count = estimate.criteria.length;
        } catch (_error) {
          row.read_errors.push("QUANTITATIVE_ESTIMATE_READ_FAILED");
        }
        try {
          const diagnostic = await apiRequest(
            `/notices/${encodeURIComponent(notice.notice_key)}/analysis/quantitative-diagnostics`,
            { method: "POST", headers },
          );
          row.profile_status = diagnostic.profile_status;
          row.available_candidates = diagnostic.available_candidate_count;
          row.review_candidates = diagnostic.review_candidate_count;
          row.issues = diagnostic.issues;
        } catch (_error) {
          row.read_errors.push("QUANTITATIVE_DIAGNOSTIC_READ_FAILED");
        }
      }
      rows.push(row);
    }
    return summarizeAnalysisStatistics(rows, {
      started_at: startedAt, finished_at: new Date().toISOString(), stored_notice_count: stored.size,
    });
  }

  function summarizeAnalysisStatistics(rows, metadata) {
    const counts = (key) => rows.reduce((result, row) => {
      const value = String(row[key] ?? "UNKNOWN");
      result[value] = (result[value] || 0) + 1;
      return result;
    }, {});
    const sum = (key) => rows.reduce((total, row) => total + (Number(row[key]) || 0), 0);
    const issueNotices = {};
    for (const row of rows) {
      for (const code of new Set(row.issues.map((issue) => issue.code))) {
        issueNotices[code] = (issueNotices[code] || 0) + 1;
      }
    }
    return {
      ...metadata,
      scope: "진행 중인 현재 공고 · 조회 시간 구간 동안의 관측값이며 단일 DB 스냅샷이 아닙니다.",
      open_notice_count: rows.length,
      attempted_notice_count: rows.filter((row) => row.analysis_attempted).length,
      complete_attachment_notice_count: rows.filter((row) => row.attachment_coverage_complete).length,
      expected_attachment_count: sum("expected_attachments"),
      audited_attachment_count: sum("audited_attachments"),
      accepted_attachment_count: sum("accepted_attachments"),
      analysis_states: counts("analysis_state"),
      analysis_reasons: counts("analysis_reason_code"),
      eligibility: counts("eligibility"),
      quantitative_profiles: counts("profile_status"),
      quantitative_activation: counts("activation_status"),
      quantitative_score_status: counts("score_status"),
      available_candidate_count: sum("available_candidates"),
      review_candidate_count: sum("review_candidates"),
      quantitative_issue_notice_counts: issueNotices,
      read_error_notice_count: rows.filter((row) => row.read_errors.length).length,
      rows,
    };
  }

  function renderRiskPanel(notice) {
    const cancelled = isCancelledNotice(notice);
    const analyzed = notice.historicalAnalysis || (notice.analysisState === "EVALUATED" && !cancelled);
    const risk = notice.riskScore;
    const axes = analyzed ? notice.riskAxes : [];
    const verifiedAxes = axes.filter((axis) => numberOrNull(axis.score) !== null).length;
    const riskReady = risk !== null && verifiedAxes >= 4;
    els.riskTotalLabel.textContent = !analyzed
      ? "분석 전"
      : !riskReady
        ? `산정 보류 · 확인된 축 ${verifiedAxes}/6`
        : `${notice.historicalAnalysis ? "당시 " : ""}총점 ${Math.round(risk)}/100`;
    els.riskBars.innerHTML = axes.length
      ? axes.map((axis) => {
        const score = numberOrNull(axis.score);
        return `
        <div class="risk-row ${score === null ? "is-unknown" : riskClass(score)}">
          <strong>${escapeHtml(axis.label)}</strong>
          <span class="risk-bar" aria-label="${escapeAttribute(axis.label)} ${score === null ? "미확인" : `${Math.round(score)}점`}"><span style="width:${score === null ? 0 : clamp(score, 0, 100)}%"></span></span>
          <span>${score === null ? "미확인" : Math.round(score)}</span>
        </div>`;
      }).join("")
      : emptyPanel(analyzed ? "리스크 근거가 아직 부족합니다" : "아직 리스크 분석 전입니다", analyzed ? "임의의 0점 대신 근거가 확보된 위험 축만 계산합니다. 자격·실행·경쟁·수익성·운영·문서 근거 연결이 필요합니다." : "수집된 공고의 첨부·조건 분석이 완료된 뒤 실제 산정값을 표시합니다.");
  }

  function renderQuantitativePending(status, message) {
    const loading = status === "loading";
    els.scoreOverview.innerHTML = [
      quantSummaryCard("예상 점수 범위", "미산정", loading ? "배점표 확인 중" : "공고별 산식 필요", "score-card--readiness"),
      quantSummaryCard("회사 증빙 확정률", "0%", "확정 항목 배점 ÷ 전체 정량 배점", "score-card--coverage"),
      quantSummaryCard("정량 준비도", "산정 보류", "참가자격과 별도", "score-card--risk"),
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
      quantSummaryCard("정량 데이터", "예시", "실제 공고 점수 아님", "score-card--risk"),
    ].join("");
    els.quantSourceStatus.className = "quant-source-status is-demo";
    els.quantSourceStatus.textContent = "명시적 데모";
    els.quantOpinion.textContent = "현재 화면은 합성 예시입니다. 실제 공고의 정량점수로 사용하지 마세요.";
    els.quantSourceAnchor.textContent = "실제 제안요청서 원문 위치 없음";
    els.quantAssumptionList.innerHTML = "<li>데모 데이터는 화면 동작 확인 전용입니다.</li>";
    els.quantTableBody.innerHTML = notice.quantitative.length
      ? notice.quantitative.map(renderQuantRow).join("")
      : `<tr><td colspan="4">${emptyPanel("정량 산식이 연결되지 않았습니다", "실제 평가표 구조화 후 확정점수 또는 예상 범위를 제공합니다.")}</td></tr>`;
    els.quantObservationList.innerHTML = emptyPanel("실제 공개 근거 없음", "명시적 데모에서는 공개 실적을 점수로 적용하지 않습니다.");
    els.quantSeparationNote.textContent = "예시 데이터 · 정량 준비도는 참가자격과 GO/NO-GO 판단을 바꾸지 않는 별도 보조지표입니다.";
  }

  function renderQuantitativeEstimate(data) {
    const total = numberOrNull(data.total_max_points);
    const lower = numberOrNull(data.lower_points);
    const upper = numberOrNull(data.upper_points);
    const coverage = numberOrNull(data.evidence_coverage_pct) ?? 0;
    const readiness = numberOrNull(data.readiness_pct);
    const ruleSource = String(data.rule_source_status || "").toUpperCase();
    const legacySourceMap = {
      AVAILABLE: "SOURCE_VALIDATED",
      INCOMPLETE: "INCOMPLETE",
      MISSING: "MISSING",
      NOT_APPLICABLE: "NOT_APPLICABLE",
    };
    const sourceValidation = data.source_validation_status || legacySourceMap[ruleSource] || "REVIEW_REQUIRED";
    const activation = data.activation_status || "REVIEW_REQUIRED";
    const sourceMissing = sourceValidation === "MISSING" || ruleSource === "MISSING";
    const notApplicable = sourceValidation === "NOT_APPLICABLE" || activation === "NOT_APPLICABLE" || ruleSource === "NOT_APPLICABLE";
    const sourceDetail = sourceMissing
      ? "배점표 미확보"
      : notApplicable
        ? "정량평가 비적용"
        : total === null
          ? "배점표 발견 · 검증 보류"
          : "원문상 조건부 하한~상한";
    const range = lower === null || upper === null || total === null
      ? "미산정"
      : `${formatNumber(lower, 1)}${lower === upper ? "" : `–${formatNumber(upper, 1)}`} / ${formatNumber(total, 1)}`;
    els.scoreOverview.innerHTML = [
      quantSummaryCard("예상 점수 범위", range, sourceDetail, "score-card--readiness"),
      quantSummaryCard("회사 증빙 확정률", `${formatNumber(coverage, 1)}%`, "확정 항목 배점 ÷ 전체 정량 배점", "score-card--coverage"),
      quantSummaryCard("정량 준비도", quantReadinessLabel(data.readiness_band), readiness === null ? "산정 불가" : `하한 기준 ${formatNumber(readiness, 1)}%`, "score-card--risk"),
    ].join("");

    const sourceLabels = {
      SOURCE_VALIDATED: "원문 검증 완료",
      REVIEW_REQUIRED: "원문 추가 확인",
      INCOMPLETE: "원문 일부",
      MISSING: "배점표 미확보",
      NOT_APPLICABLE: "정량평가 비적용",
    };
    const activationLabels = {
      AUTO_ACTIVE: "규칙 자동 활성",
      PARTIAL_ACTIVE: "검증 항목만 부분 산정",
      REVIEW_REQUIRED: "자동 산정 보류",
      NOT_APPLICABLE: "산정 비적용",
    };
    els.quantSourceStatus.className = `quant-source-status is-${String(activation).toLowerCase().replaceAll("_", "-")}`;
    const scoreStatusLabel = data.overall_status === "UNSCORABLE" && lower !== null && upper !== null
      ? "일부 항목 미산정"
      : quantStatusLabel(data.overall_status);
    els.quantSourceStatus.textContent = `${sourceLabels[sourceValidation] || "원문 추가 확인"} · ${activationLabels[activation] || "자동 산정 보류"} · ${scoreStatusLabel}`;
    els.quantOpinion.textContent = data.opinion || "정량 의견이 없습니다.";
    const anchor = data.source_anchor;
    els.quantSourceAnchor.textContent = anchor
      ? `${anchor.document_label} · ${anchor.page ? `원문 ${anchor.page}쪽 · ` : ""}${anchor.section}`
      : sourceMissing
        ? "연결된 정량평가표 원문 위치 없음"
        : sourceValidation === "SOURCE_VALIDATED"
          ? "원문 위치 검증 완료 · 공개 화면 비공개"
          : notApplicable
            ? "정량평가 비적용 확인"
            : "배점표 후보 확인 · 원문 검증 보류";
    const activationReasonLabels = {
      FACT_DIMENSIONS_UNMODELED: "인정기간·유사사업·VAT·역할 등 점수 산출조건이 아직 구조화되지 않았습니다.",
      FACT_KEY_AMBIGUOUS: "여러 평가항목이 같은 회사 사실 키를 사용해 값의 적용 대상을 구분할 수 없습니다.",
      ALTERNATIVE_TABLE_AMBIGUOUS: "적용 대상이 다른 복수 평가표 중 하나를 기계적으로 선택할 수 없습니다.",
      CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE: "현재 공고의 모든 첨부 검증이 끝나지 않았습니다.",
      BRACKETS_NOT_EXHAUSTIVE_OR_OVERLAPPING: "배점 구간에 공백 또는 중복이 있습니다.",
      UNIT_NOT_SOURCE_BOUND: "산정 단위를 원문 인용에서 정확히 확인할 수 없습니다.",
      BOUND_UNIT_INCONSISTENT: "배점 구간별 단위가 누락되었거나 서로 다른 환산 단위를 사용합니다.",
      UNSUPPORTED_UNIT: "현재 자동 산정에서 지원하지 않는 단위입니다.",
      UNSUPPORTED_SCORING_DSL: "현재 자동 산정에서 지원하지 않는 산식입니다.",
      AMBIGUOUS_RULE: "평가기준 문구가 여러 방식으로 해석되어 자동 계산을 보류했습니다.",
      BRACKET_COMPARATOR_MISMATCH: "배점 구간의 비교기호가 서로 일치하지 않습니다.",
      BRACKET_LITERAL_MISMATCH: "배점 구간의 기준 문구를 원문과 일치시킬 수 없습니다.",
      BRACKET_NUMBER_MISMATCH: "배점 구간의 숫자를 원문과 일치시킬 수 없습니다.",
      COMPARATOR_GRAMMAR_UNSUPPORTED: "현재 엔진이 지원하지 않는 비교식입니다.",
      CRITERION_LITERAL_MISMATCH: "평가항목 명칭을 원문과 일치시킬 수 없습니다.",
      EXTRACTION_DECLARED_INCOMPLETE: "첨부 추출 결과가 일부 불완전하다고 표시되었습니다.",
      MAX_POINTS_LITERAL_MISMATCH: "최대 배점을 원문과 일치시킬 수 없습니다.",
      OVERLAPPING_BRACKETS: "배점 구간이 서로 겹칩니다.",
      REQUIRED_EVIDENCE_INCOMPLETE: "점수 계산에 필요한 회사 증빙이 아직 충분하지 않습니다.",
      TABLE_TOTAL_INCOMPLETE: "평가표 총점을 완전하게 확인하지 못했습니다.",
      UNKNOWN_METRIC: "제안서·제품·수기평가 항목이라 회사 사실만으로 자동 계산할 수 없습니다.",
      PUBLIC_ANALYSIS_REVIEW_REQUIRED: "저장된 최신 분석에 미확정 항목이 있어 점수 범위로 표시합니다.",
    };
    const activationReasons = Array.isArray(data.activation_reasons)
      ? data.activation_reasons.map((item) => `자동 산정 보류: ${activationReasonLabels[item] || "원문과 산정 규칙을 추가로 확인해야 합니다."}`)
      : [];
    const quantitativeAssumptions = [...(Array.isArray(data.assumptions) ? data.assumptions : []), ...activationReasons];
    els.quantAssumptionList.innerHTML = quantitativeAssumptions.length
      ? quantitativeAssumptions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")
      : "<li>추가 가정 없음</li>";
    const emptyCriteria = sourceMissing
      ? emptyPanel("정량점수를 표시하지 않습니다", "배점표와 인정 산식이 확보될 때까지 확인 필요로 유지합니다.")
      : notApplicable
        ? emptyPanel("정량평가 비적용", "이 공고에는 회사 정량점수를 적용하지 않습니다.")
        : total !== null
          ? emptyPanel("최신 정량 합계 저장본", "회사 사실값과 항목별 원문 근거는 공개하지 않고, 최신 분석의 합계와 범위만 표시합니다.")
        : emptyPanel("자동 산정 가능한 항목 없음", "배점표는 확인했지만 수기 기술평가 또는 검증 보류 항목에 임의 점수를 넣지 않습니다.");
    const publicEvidenceHidden = !sourceMissing && (
      data.ruleset_version === "public-quantitative-summary-v1"
      || (Array.isArray(data.assumptions) && data.assumptions.some((item) => String(item).includes("공개 화면")))
    );
    els.quantTableBody.innerHTML = Array.isArray(data.criteria) && data.criteria.length
      ? data.criteria.map((item) => renderQuantitativeEstimateRow(item, { publicEvidenceHidden })).join("")
      : `<tr><td colspan="4">${emptyCriteria}</td></tr>`;
    els.quantObservationList.innerHTML = Array.isArray(data.evidence_observations) && data.evidence_observations.length
      ? data.evidence_observations.map(renderQuantObservation).join("")
      : publicEvidenceHidden
        ? emptyPanel("내부 검증 근거 보존", "회사 사실값과 원문·내부 증빙은 공개 화면에서 숨깁니다.")
        : emptyPanel("적용 전 공개 근거 없음", "공고별 배점 산식과 연결된 공개 근거가 없습니다.");
    els.quantSeparationNote.textContent = data.separation_notice || "정량 준비도는 참가자격과 GO/NO-GO 판단을 바꾸지 않습니다.";
  }

  function quantSummaryCard(label, value, detail, className) {
    return `<div class="score-card quant-summary-card ${className}"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong><span class="quant-card-detail">${escapeHtml(detail)}</span></div>`;
  }

  function quantStatusLabel(status) {
    return ({ CONFIRMED: "확정", ESTIMATED: "잠정 범위", UNSCORABLE: "산정 불가", REVIEW: "검토 필요" })[status] || "검토 필요";
  }

  function quantReadinessLabel(value) {
    return ({ GREEN: "준비됨", YELLOW: "보완 필요", RED: "위험", GRAY: "산정 보류" })[String(value || "").toUpperCase()] || "산정 보류";
  }

  function renderQuantitativeEstimateRow(item, { publicEvidenceHidden = false } = {}) {
    const lower = numberOrNull(item.lower_points);
    const upper = numberOrNull(item.upper_points);
    const range = lower === null || upper === null
      ? "—"
      : `${formatNumber(lower, 1)}${lower === upper ? "" : `–${formatNumber(upper, 1)}`}점`;
    const anchor = item.source_anchor;
    const source = anchor
      ? `${anchor.page ? `원문 ${anchor.page}쪽` : "원문 위치 확인됨"}`
      : publicEvidenceHidden ? "원문 위치 세부 비공개" : "원문 위치 없음";
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
      <span class="quant-observation-evidence">${item.evidence_key ? "공개 근거 연결됨" : "근거 확인 필요"}</span>
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
    if (state.selectedNotice?.noticeKey === noticeKey) {
      renderAwardHistoryPanel(notice);
      updateAwardHistorySummaryMetric(notice);
    }

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
      if (state.selectedNotice?.noticeKey === noticeKey) {
        renderAwardHistoryPanel(state.selectedNotice);
        updateAwardHistorySummaryMetric(state.selectedNotice);
      }
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
      els.historyStatusText.textContent = "PAI 서버에 저장된 낙찰 후보를 읽고 있습니다.";
    } else if (status === "ready" || status === "stored") {
      els.historyStatusLabel.textContent = `저장본 ${items.length}건`;
      els.historyStatusLabel.classList.add("is-ready");
      els.historyStatusText.textContent = "저장된 제목 유사 후보이며 동일 사업 확정 이력이 아닙니다.";
    } else if (status === "error") {
      els.historyStatusLabel.textContent = items.length ? `저장본 ${items.length}건` : "미수집";
      els.historyStatusLabel.classList.add("is-error");
      els.historyStatusText.textContent = items.length
        ? `저장 이력 재조회 실패 · 상세 응답의 저장본을 표시합니다. ${meta.message}`
        : `저장 이력을 확인하지 못했습니다. 외부 조회는 시작하지 않았습니다. ${meta.message}`;
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
      els.historyConcentration.innerHTML = `<p class="section-kicker">낙찰자 집중도</p><h4>집중도 계산 중</h4><p>저장 이력을 읽고 있습니다.</p>`;
      els.historyPrediction.innerHTML = `<p class="section-kicker">가격 범위 참고</p><h4>가격 전략 계산 중</h4><p>외부 조회 없이 저장값만 사용합니다.</p>`;
      els.historyCoverage.innerHTML = `<p class="section-kicker">확인 가능한 자료</p><h4>자료 항목 확인 중</h4><p>누락값은 사실로 보간하지 않습니다.</p>`;
      els.historyWarnings.innerHTML = "";
      return;
    }
    if (!intelligence) {
      const note = status === "demo" ? "데모 이력에는 서버 계산 결과를 적용하지 않습니다." : "저장된 분석 결과가 없습니다.";
      els.historyConcentration.innerHTML = `<p class="section-kicker">낙찰자 집중도</p><h4>산정 불가</h4><p>${escapeHtml(note)}</p>`;
      els.historyPrediction.innerHTML = `<p class="section-kicker">가격 범위 참고</p><h4>예측 미제공</h4><p>유효 낙찰률 3건 이상과 명시적 기준금액이 필요합니다.</p>`;
      els.historyCoverage.innerHTML = `<p class="section-kicker">자료 적용 범위</p><h4>확인 가능한 값만 표시</h4><p>낙찰금액과 투찰금액, 기술점수와 가격점수는 서로 대체하지 않습니다.</p>`;
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
      <p class="section-kicker">경쟁 리스크 · 참가자격과 별도</p><h4>경쟁·집중 리스크 ${escapeHtml(competitionBand)}</h4>
      <strong class="history-intel-value">${competitionAvailable ? formatNumber(competition.score, 1) : "—"} <small>${competitionAvailable ? "/ 100" : "미산정"}</small></strong>
      <p>HHI ${hhi === null ? "미산정" : formatNumber(hhi, 0)} · 상위 수주 비중 ${top ? `${formatNumber(top.share * 100, 1)}%` : "미확인"} · 참여 중앙값 ${participantMedian === null ? "미확인" : `${formatNumber(participantMedian, 1)}곳`}</p>
      <small>${escapeHtml(competition?.rationale || "필수 사실 커버리지가 부족해 점수를 보류했습니다.")}</small>
      <small>신뢰도 ${escapeHtml(confidenceLabel(competition?.confidence))} · ${top ? `표본 상위 ${escapeHtml(top.winner_name)} ${formatNumber(top.count)}건` : "낙찰자 표본 없음"} · 법적 독점 판정 아님</small>`;

    const award = prediction?.award_rate;
    const submitted = prediction?.submitted_bid_rate;
    const amount = prediction?.award_amount_range;
    const pricingMethod = intelligence?.pricing_method;
    const awardAvailable = award?.status === "MODEL_ESTIMATE";
    els.historyPrediction.innerHTML = `
      <p class="section-kicker">가격 범위 참고 · 의사결정 참고</p><h4>예측 낙찰률 ${awardAvailable ? `${formatNumber(award.center, 2)}%` : "미제공"}</h4>
      <strong class="history-intel-value">${awardAvailable ? `${formatNumber(award.range_low, 2)}–${formatNumber(award.range_high, 2)}%` : "표본 부족"}</strong>
      <p>${amount ? `예상 낙찰금액 ${escapeHtml(formatBudget(amount.low))}–${escapeHtml(formatBudget(amount.high))}` : "기준/추정금액이 없거나 표본이 부족해 금액 범위를 산정하지 않았습니다."}</p>
      <p class="history-pricing-method">${pricingMethod ? `문서 근거 가격평가: ${escapeHtml(pricingMethod.method?.at_or_above_80_percent || "산식 확인")}` : "현재 공고 문서와 정확히 일치하는 가격평가 산식 근거 없음"}</p>
      <small>신뢰도 ${escapeHtml(confidenceLabel(award?.confidence))} · ${formatNumber(award?.sample_count || 0)}건 · 투찰률 ${submitted?.status === "MODEL_ESTIMATE" ? `${formatNumber(submitted.center, 2)}%` : "별도 표본 부족"}</small>
      <small>${escapeHtml(award?.rationale || "유효 표본이 부족해 예측 근거를 제시하지 않습니다.")} · ${escapeHtml(award?.method || "산정 안 함")}</small>`;

    const coverageCell = (label, key) => {
      const field = coverage?.[key];
      return `<span><strong>${escapeHtml(label)}</strong><small>${formatNumber(field?.available || 0)} / ${formatNumber(field?.total || intelligence.record_count || 0)}건</small></span>`;
    };
    els.historyCoverage.innerHTML = `
      <p class="section-kicker">확인 가능한 자료</p><h4>사실 항목 충족도</h4>
      <div class="history-coverage-grid">${coverageCell("낙찰자", "winner")}${coverageCell("참여기관 수", "participant_count")}${coverageCell("낙찰금액", "award_amount")}${coverageCell("예정가격", "estimated_price")}${coverageCell("투찰금액", "submitted_bid_price")}${coverageCell("기술점수", "technical_score")}${coverageCell("가격점수", "price_score")}</div>`;
    const warnings = [...new Set([
      ...(Array.isArray(intelligence.warnings) ? intelligence.warnings : []),
      ...(Array.isArray(competition?.warnings) ? competition.warnings : []),
    ])];
    els.historyWarnings.innerHTML = warnings.length ? `<strong>해석 주의</strong><ul>${warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("")}</ul>` : "";
  }

  function confidenceLabel(value) {
    return ({ HIGH: "높음", MEDIUM: "보통", LOW: "낮음", INSUFFICIENT: "근거 부족" })[String(value || "").toUpperCase()] || "근거 부족";
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
    const cancelled = isCancelledNotice(notice);
    const qualityReview = isDocumentQualityReview(notice);
    const effectiveEligibility = effectiveEligibilityStatus(notice);
    const deadline = deadlineInfo(notice.deadline);
    els.teamsMockSource.textContent = sourceKindLabel(notice, true);
    els.teamsMockTitle.textContent = notice.title;
    els.teamsMockAgency.textContent = notice.agency;
    els.teamsMockStatus.textContent = cancelled
      ? "취소 공고 · 현재 검토 제외"
      : analyzed
      ? qualityReview ? "근거 보완 · 판단 보류" : `자격 ${STATUS_LABELS[effectiveEligibility]}`
      : notice.analysisState === "FAILED" ? "분석 오류 · 재처리 필요" : "수집 완료 · 분석 대기";
    els.teamsMockDeadline.textContent = `${deadline.relative} · ${deadline.date}`;
    els.teamsMockReason.textContent = cancelled
      ? notice.analysisReason || "취소 공고로 현재 추천과 담당자 판단을 제공하지 않습니다."
      : analyzed
      ? qualityReview
        ? truncateText(notice.analysisReason || "원문 근거 검증을 보완한 뒤 자격과 추천을 확정합니다.", 180)
        : truncateText(notice.summary, 180)
      : "아직 규칙 기반 평가가 실행되지 않았습니다. 준비도·리스크·추천값을 임의로 생성하지 않고 분석 대기 상태만 알립니다.";
    els.teamsMockReadiness.textContent = cancelled ? "과거 분석 참고" : qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.readinessScore)} / 100` : "미산정";
    els.teamsMockRisk.textContent = cancelled ? "과거 분석 참고" : qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.riskScore)} / 100` : "미산정";
    els.teamsMockRecommendation.textContent = analysisRecommendationLabel(notice);
    els.teamsPreviewDecisionButton.disabled = cancelled || !state.writeControlsEnabled;
    els.teamsMockJson.textContent = JSON.stringify(buildAdaptiveCardPayload(notice), null, 2);
    els.teamsMockSendButton.disabled = !state.writeControlsEnabled || Boolean(state.teamsLogMeta[notice.noticeKey]?.sending);
    renderTeamsMockLogs(notice.noticeKey);
  }

  function buildAdaptiveCardPayload(notice) {
    const analyzed = notice.analysisState === "EVALUATED";
    const cancelled = isCancelledNotice(notice);
    const qualityReview = isDocumentQualityReview(notice);
    const effectiveEligibility = effectiveEligibilityStatus(notice);
    const deadline = deadlineInfo(notice.deadline);
    return {
      type: "AdaptiveCard",
      $schema: "http://adaptivecards.io/schemas/adaptive-card.json",
      version: "1.5",
      msteams: { width: "Full" },
      body: [
        {
          type: "TextBlock",
          text: cancelled ? "PAI · 취소 공고 알림" : "PAI · 새 입찰 검토 알림",
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
            { title: "분석 상태", value: cancelled ? "취소 공고 · 현재 검토 제외" : analyzed ? qualityReview ? "근거 보완 · 판단 보류" : `자격 ${STATUS_LABELS[effectiveEligibility]}` : "수집 완료 · 분석 대기" },
            { title: "마감", value: `${deadline.relative} · ${deadline.date}` },
            { title: "준비도", value: cancelled ? "과거 분석 참고" : qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.readinessScore)} / 100` : "미산정" },
            { title: "리스크", value: cancelled ? "과거 분석 참고" : qualityReview ? "근거 보완 후 산정" : analyzed ? `${formatScore(notice.riskScore)} / 100` : "미산정" },
            { title: "추천", value: analysisRecommendationLabel(notice) },
          ],
        },
        {
          type: "TextBlock",
          text: cancelled
            ? notice.analysisReason || "취소 공고로 현재 추천과 담당자 판단을 제공하지 않습니다."
            : analyzed
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
        ...(cancelled ? [] : [
          { type: "Action.Submit", title: "담당자 판단", data: { action: "OPEN_DECISION", notice_key: notice.noticeKey } },
        ]),
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
      showToast("서버 mock 기록 완료", "PAI 서버 로그에 저장했습니다. Teams 외부 전송은 발생하지 않았습니다.", "success");
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
      loading: "PAI 서버 mock 기록 불러오는 중",
      server: `서버 mock ${serverLogCount}건${fallbackLogCount ? ` · 브라우저 fallback ${fallbackLogCount}건` : ""} · Teams 외부 전송 없음`,
      readonly: "공개 읽기 전용 · 내부 mock 로그 비공개",
      fallback: serverLogCount
        ? `서버 연결 실패 · 이전 서버 기록 ${serverLogCount}건 · 브라우저 fallback ${fallbackLogCount}건`
        : `서버 미기록 · 브라우저 fallback ${fallbackLogCount}건만 표시`,
    };
    els.teamsLogStorageLabel.textContent = storageLabels[meta.status] || storageLabels.idle;
    els.teamsLogStorageLabel.title = meta.error || "";

    if (meta.status === "loading" && !logs.length) {
      els.teamsMockLogList.innerHTML = '<li class="teams-log-empty"><strong><span class="teams-log-status is-loading">불러오는 중</span></strong><span>PAI 서버의 mock 기록을 확인하고 있습니다.</span></li>';
      return;
    }

    els.teamsMockLogList.innerHTML = logs.length
      ? logs.map((item) => {
        const fallback = item.origin === "LOCAL_FALLBACK";
        const status = fallback ? "LOCAL_FALLBACK" : item.status || "MOCK_RECORDED";
        const boundary = fallback
          ? `브라우저 fallback · 서버 미기록${item.errorReason ? ` · ${item.errorReason}` : ""}`
          : "PAI 서버 mock 기록 · Teams 외부 전송 없음";
        return `
        <li class="teams-log-item">
          <div class="teams-log-item__head"><span class="teams-log-status ${fallback ? "is-fallback" : ""}">${escapeHtml(status)}</span><time datetime="${escapeAttribute(item.timestamp)}">${escapeHtml(formatShortDateTime(item.timestamp))}</time></div>
          <strong title="${escapeAttribute(item.title)}">${escapeHtml(item.title)}</strong>
          <p>${escapeHtml(boundary)}${item.correlationId ? ` · ID ${escapeHtml(truncateText(item.correlationId, 34))}` : ""}</p>
        </li>`;
      }).join("")
      : `<li class="teams-log-empty"><strong>${meta.status === "readonly" ? "공개 화면에서는 내부 mock 로그를 표시하지 않습니다" : "아직 mock 기록이 없습니다"}</strong><span>${meta.status === "fallback" ? "서버 연결 실패 시 기록한 브라우저 fallback도 없습니다." : meta.status === "readonly" ? "Teams 승인이 끝난 뒤 사내 로그인 환경에서 사용할 수 있습니다." : "버튼을 누르면 Teams 전송 없이 PAI 서버 mock 로그에만 기록됩니다."}</span></li>`;
  }

  function focusDecisionDockFromPreview() {
    const notice = state.selectedNotice;
    if (!notice) return;
    if (isCancelledNotice(notice)) {
      showToast("취소 공고입니다", "취소된 공고에는 담당자 판단을 새로 저장할 수 없습니다.", "warning");
      return;
    }
    if (!canWriteDecision()) {
      showToast("판단 저장 권한이 없습니다", "현재 서버의 운영 권한 설정을 확인해 주세요.", "warning");
      return;
    }
    selectTab("overview");
    setDecisionDockExpanded(true);
    if (!decisionAnalysisComplete(notice)) {
      els.commentField.hidden = false;
      els.toggleCommentButton.setAttribute("aria-expanded", "true");
      showToast("분석 전에도 판단을 기록할 수 있습니다", "분석이 완료되지 않았으므로 판단 사유를 입력해 주세요.", "warning");
    }
    els.decisionForm.scrollIntoView({ behavior: "smooth", block: "end" });
    els.decisionInputs[0]?.focus();
  }

  function renderExistingDecision(notice) {
    if (state.decisionDockNoticeKey !== notice.noticeKey) {
      state.decisionDockNoticeKey = notice.noticeKey;
      setDecisionDockExpanded(false);
    }
    const analyzed = decisionAnalysisComplete(notice);
    const cancelled = isCancelledNotice(notice);
    els.decisionInputs.forEach((input) => {
      input.checked = notice.decision === input.value || (notice.decision === "CONDITIONAL_GO" && input.value === "HOLD");
      input.disabled = cancelled || !canWriteDecision();
    });
    els.decisionComment.value = notice.decisionComment;
    els.commentCount.textContent = String(notice.decisionComment.length);
    // An unanalysed notice always needs a written reason, so keep the field open.
    els.commentField.hidden = analyzed && !notice.decisionComment;
    els.toggleCommentButton.setAttribute("aria-expanded", String(!analyzed || Boolean(notice.decisionComment)));
    els.decisionExisting.textContent = operatorDecisionDetailText(notice);
    els.toggleCommentButton.disabled = cancelled || !canWriteDecision();
    els.decisionComment.disabled = cancelled || !canWriteDecision();
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
    const noticeKey = state.selectedNotice?.noticeKey;
    state.selectedNotice = null;
    const replacement = noticeKey
      ? [...document.querySelectorAll("[data-notice-key]")].find((node) => node.dataset.noticeKey === noticeKey)
      : null;
    const focusTarget = trigger && document.contains(trigger)
      ? trigger
      : replacement?.querySelector("[data-open-notice]") || replacement;
    if (focusTarget && typeof focusTarget.focus === "function") focusTarget.focus();
  }

  function trapDrawerFocus(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDetail();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...els.detailDrawer.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')]
      .filter((node) => !node.closest("[hidden]") && node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!focusable.includes(document.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function setDecisionDockExpanded(expanded) {
    els.decisionDockBody.hidden = !expanded;
    els.decisionDockToggle.setAttribute("aria-expanded", String(expanded));
    els.decisionDockToggle.textContent = expanded ? "판단 영역 접기" : "판단 영역 펼치기";
  }

  function toggleCommentField() {
    if (isCancelledNotice(state.selectedNotice)) return;
    setDecisionDockExpanded(true);
    const willOpen = els.commentField.hidden;
    els.commentField.hidden = !willOpen;
    els.toggleCommentButton.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) els.decisionComment.focus();
  }

  function decisionAnalysisComplete(notice) {
    return notice?.analysisState === "EVALUATED"
      && (notice.sourceKind !== "PPS" || notice.analysisAttachmentCoverageComplete === true);
  }

  function updateDecisionButton() {
    const selectedChoice = els.decisionInputs.find((input) => input.checked)?.value || "";
    const selected = Boolean(selectedChoice);
    const analyzed = decisionAnalysisComplete(state.selectedNotice);
    const cancelled = isCancelledNotice(state.selectedNotice);
    const overrideNeedsReason = selectedChoice === "GO"
      && (["FAIL", "REVIEW", "UNKNOWN"].includes(effectiveEligibilityStatus(state.selectedNotice)) || effectiveRecommendation(state.selectedNotice) !== "GO");
    // An unanalysed, failed or expired notice has no current judgement to lean
    // on, so the operator's own reason is what makes the record accountable.
    const reasonRequired = overrideNeedsReason || (Boolean(state.selectedNotice) && !analyzed);
    const overrideReasonMissing = reasonRequired && !els.decisionComment.value.trim();
    if (reasonRequired) {
      if (overrideReasonMissing) setDecisionDockExpanded(true);
      els.commentField.hidden = false;
      els.toggleCommentButton.setAttribute("aria-expanded", "true");
    }
    els.saveDecisionButton.disabled = cancelled || !canWriteDecision() || !state.selectedNotice || overrideReasonMissing;
    els.saveDecisionButton.classList.toggle("is-awaiting-selection", !selected);
    els.saveDecisionButton.title = !selected
      ? "먼저 참여, 보류, 불참 중 담당자 판단을 선택해 주세요."
      : overrideReasonMissing
        ? (analyzed
          ? "참가자격 또는 AI 판단과 다른 참여 결정을 기록하려면 사유가 필요합니다."
          : "분석이 완료되지 않은 상태의 판단을 기록하려면 사유가 필요합니다.")
        : "";
    els.saveDecisionButton.textContent = cancelled
      ? "취소 공고 · 저장 불가"
      : !canWriteDecision()
      ? "현재 판단 저장 미제공"
      : overrideReasonMissing
      ? (analyzed ? "참여 사유를 입력하세요" : "판단 사유를 입력하세요")
      : !selected
      ? "먼저 최종 판단을 선택하세요"
      : analyzed
      ? (state.writeControlsEnabled ? "선택한 판단 저장" : "운영 PIN으로 선택한 판단 저장")
      : "분석 전 판단 기록";
  }

  async function saveDecision(event) {
    event.preventDefault();
    const notice = state.selectedNotice;
    if (!notice) return;
    if (isCancelledNotice(notice)) {
      showToast("취소 공고입니다", "취소된 공고에는 담당자 판단을 새로 저장할 수 없습니다.", "warning");
      return;
    }
    if (!canWriteDecision()) {
      showToast("판단 저장 권한이 없습니다", "현재 서버의 운영 권한 설정을 확인해 주세요.", "warning");
      return;
    }
    const decision = els.decisionInputs.find((input) => input.checked)?.value;
    if (!decision) {
      setDecisionDockExpanded(true);
      showToast("담당자 판단을 먼저 선택해 주세요", "참여, 보류, 불참 중 하나를 선택한 뒤 저장할 수 있습니다.", "warning");
      els.decisionInputs[0]?.focus();
      return;
    }
    const analyzed = decisionAnalysisComplete(notice);
    const comment = els.decisionComment.value.trim();
    if (decision === "GO" && analyzed && (["FAIL", "REVIEW", "UNKNOWN"].includes(effectiveEligibilityStatus(notice)) || effectiveRecommendation(notice) !== "GO") && !comment) {
      setDecisionDockExpanded(true);
      els.commentField.hidden = false;
      els.toggleCommentButton.setAttribute("aria-expanded", "true");
      showToast("참여 사유가 필요합니다", "참가자격 또는 AI 판단과 다른 참여 결정을 기록하려면 사유를 입력해 주세요.", "warning");
      els.decisionComment.focus();
      return;
    }
    if (!analyzed && !comment) {
      setDecisionDockExpanded(true);
      els.commentField.hidden = false;
      els.toggleCommentButton.setAttribute("aria-expanded", "true");
      showToast("판단 사유가 필요합니다", "분석이 완료되지 않은 상태의 판단은 사유를 입력해야 기록할 수 있습니다.", "warning");
      els.decisionComment.focus();
      return;
    }
    const rationale = comment || `${DECISION_LABELS[decision]} 판단을 기록했습니다.`;
    // Send the identifier the screen actually showed. Omitting it is only
    // valid when no current evaluation exists; the server still rejects a
    // stale identifier and one belonging to another notice.
    const evaluationId = notice.evaluationId || notice.decisionEvaluationId;
    const payload = {
      ...(evaluationId ? { evaluation_id: evaluationId } : {}),
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
        const operatorHeaders = state.writeControlsEnabled ? {} : await manualAnalysisAuthHeaders();
        if (!operatorHeaders) {
          showToast("판단 저장 취소", "4자리 운영 PIN이 입력되지 않았습니다.", "warning");
          return;
        }
        const path = state.writeControlsEnabled
          ? `/notices/${encodeURIComponent(notice.noticeKey)}/decisions`
          : `/operator-decisions/notices/${encodeURIComponent(notice.noticeKey)}`;
        result = await apiRequest(path, {
          method: "POST",
          headers: operatorHeaders,
          body: JSON.stringify(payload),
        });
      }
      const response = unwrapObject(result);
      const updated = {
        ...notice,
        decisionReadStatus: "KNOWN",
        decision: normalizeDecision(firstValue(response.choice, response.decision, response.manager_decision, decision)) || decision,
        decisionComment: stringValue(firstValue(response.rationale, response.comment, response.decision_comment, rationale), rationale),
        decidedBy: stringValue(firstValue(response.actor_label, response.actorLabel, response.decided_by, response.decider, DECIDER_NAME), DECIDER_NAME),
        decidedAt: firstValue(response.created_at, response.createdAt, response.decided_at, response.updated_at, new Date().toISOString()),
      };
      const index = state.notices.findIndex((item) => item.noticeKey === notice.noticeKey);
      if (index >= 0) state.notices[index] = updated;
      state.selectedNotice = updated;
      await refreshDashboardAfterMutation();
      renderExistingDecision(updated);
      renderPipelineIntoExisting(updated);
      renderAll();
      setDecisionDockExpanded(false);
      els.decisionDockToggle.focus({ preventScroll: true });
      showToast(
        state.source === "demo" ? "데모 판단 반영" : "판단을 저장했습니다",
        state.source === "demo" ? "현재 브라우저에서만 반영되며 서버에는 저장되지 않습니다." : `${DECISION_LABELS[updated.decision]} 결정과 의견이 기록되었습니다.`,
        state.source === "demo" ? "warning" : "success",
      );
    } catch (error) {
      if (error?.status === 401) state.manualAnalysisToken = "";
      if (error?.status === 409 && String(error?.message || "").includes("평가가 갱신")) {
        try {
          const refreshed = await hydrateNoticeByKey(notice.noticeKey, { force: true });
          state.selectedNotice = refreshed;
          renderDetail(refreshed);
          applyFilters();
        } catch (_) {
          // Keep the explicit stale-evaluation error when the refresh also fails.
        }
      }
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
    if (document.querySelector("dialog[open]")) return;
    if (els.detailDrawer.classList.contains("is-open")) {
      if (["Tab", "Escape"].includes(event.key)) {
        trapDrawerFocus(event);
        return;
      }
      if (isEditableTarget(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === "j" || key === "k") {
        event.preventDefault();
        moveDetailSelection(key === "j" ? 1 : -1);
        return;
      }
    }
    if (event.key === "/" && !isEditableTarget(event.target)) {
      event.preventDefault();
      if (state.currentView === "closed") els.resultLearningSearchInput.focus();
      else if (state.currentView === "performance") els.performanceSearchInput.focus();
      else if (state.noticeSearchMode === "prespec") els.prespecStoredSearchInput.focus();
      else els.searchInput.focus();
    }
  }

  function updateNoticeRoute(noticeKey) {
    const url = new URL(window.location.href);
    url.searchParams.set("notice", noticeKey);
    history.pushState({ noticeKey }, "", url);
  }

  function noticeDetailHref(noticeKey) {
    const url = new URL(window.location.href);
    url.searchParams.set("notice", noticeKey);
    return `${url.pathname}${url.search}${url.hash}`;
  }

  function clearNoticeRoute() {
    const url = new URL(window.location.href);
    url.searchParams.delete("notice");
    history.replaceState({}, "", url);
  }

  function openNoticeFromRoute() {
    if (!isNoticeListView()) return;
    const key = new URLSearchParams(window.location.search).get("notice");
    if (key) void openDetail(key, null, { updateRoute: false });
  }

  function handleRouteChange() {
    const routeView = routeViewFromLocation();
    if (routeView !== state.currentView) {
      setView(routeView, { syncRoute: false });
    }
    const key = new URLSearchParams(window.location.search).get("notice");
    if (key && isNoticeListView()) {
      void openDetail(key, null, { updateRoute: false });
    } else {
      closeDetail({ updateRoute: false });
    }
  }

  async function copyCurrentNoticeLink() {
    const notice = state.selectedNotice;
    if (!notice) return;
    const url = new URL(noticeDetailHref(notice.noticeKey), window.location.origin).href;
    try {
      await navigator.clipboard.writeText(url);
      showToast("링크를 복사했습니다", "저장된 PAI 공고 상세 링크가 클립보드에 복사되었습니다.", "success");
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
      ? `${sourceHost}의 공식 공고를 새 탭에서 엽니다. PAI 분석 화면은 그대로 유지됩니다.`
      : "공개 가능한 나라장터 원문 링크가 아직 연결되지 않았습니다. 공고번호로 나라장터에서 다시 확인해 주세요.";
    if (sourceUrl) {
      els.sourceLinkOpenAnchor.href = sourceUrl;
      els.sourceLinkOpenAnchor.removeAttribute("aria-disabled");
      els.sourceLinkOpenAnchor.tabIndex = 0;
      els.sourceLinkOpenAnchor.textContent = "나라장터 원문 열기";
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
    if (isCancelledNotice(notice)) {
      const historical = dashboardEligibilityStatus(notice);
      const label = historical === "NOT_EVALUATED" ? "당시 자격 미확인" : `당시 ${historical}`;
      return `<span class="analysis-state" title="취소 공고로 과거 자격 판정을 현재 상태로 사용하지 않습니다">취소 공고 · ${label}</span>`;
    }
    if (notice.historicalAnalysis) {
      const value = STATUS_LABELS[notice.eligibilityStatus] ? notice.eligibilityStatus : "UNKNOWN";
      return `<span class="status-pill status-pill--${value.toLowerCase()}" title="${escapeAttribute(notice.historicalAnalysisReason)}">당시 ${escapeHtml(STATUS_LABELS[value])}</span>`;
    }
    if (isDocumentQualityReview(notice)) return '<span class="analysis-state" title="원문 근거 검증을 보완해야 참가자격을 판단할 수 있습니다">분석 보완</span>';
    if (notice.analysisState === "EVALUATED") return statusPill(effectiveEligibilityStatus(notice));
    if (notice.analysisState === "ANALYZED") return '<span class="analysis-state" title="첨부 분석은 완료됐지만 현재 판단이 저장되지 않았습니다">판단 대기</span>';
    if (notice.analysisState === "FAILED") return '<span class="analysis-state analysis-state--error">분석 오류</span>';
    return '<span class="analysis-state">미분석</span>';
  }

  function analysisRecommendationPill(notice) {
    if (isCancelledNotice(notice)) return aiJudgmentMarkup("추천 비활성", "취소 공고", "pending");
    if (notice.historicalAnalysis) return aiJudgmentMarkup("당시 판정 참고", "현재 판단 아님", "pending");
    if (isDocumentQualityReview(notice)) return aiJudgmentMarkup("권고 보류", "근거 보완 필요", "pending");
    if (notice.analysisState === "EVALUATED") return recommendationPill(effectiveRecommendation(notice), notice);
    if (notice.analysisState === "ANALYZED") return aiJudgmentMarkup("판단 전", "평가 저장 대기", "pending");
    return aiJudgmentMarkup("분석 전", "근거 확인 전", "pending");
  }

  function sourceKindBadge(notice) {
    const className = notice.sourceKind === "SYNTHETIC" ? "source-kind-badge--synthetic" : notice.sourceKind === "MANUAL" ? "source-kind-badge--manual" : "source-kind-badge--real";
    return `<span class="source-kind-badge ${className}">${escapeHtml(sourceKindLabel(notice))}</span>`;
  }

  function sourceKindLabel(notice, detailed = false) {
    if (notice.sourceKind === "SYNTHETIC") return detailed ? "합성 회귀 데이터" : "합성";
    if (notice.sourceKind === "MANUAL") return detailed ? "수동 등록 공고" : "수동";
    return detailed ? "나라장터 실공고" : "실제";
  }

  function noticeLifecycleStatus(notice) {
    const providerDisposition = stringValue(notice?.providerDisposition).toUpperCase();
    if (providerDisposition === "CANCELLED") return "CANCELLED";
    const status = stringValue(notice?.noticeStatus).toUpperCase();
    if (status === "CLOSED") return "CLOSED";
    if (status === "EXPIRED") return "EXPIRED";
    const deadline = validDate(notice?.deadline);
    if (status === "OPEN" && deadline && deadline.getTime() < Date.now()) return "EXPIRED";
    return "OPEN";
  }

  function isEndedNotice(notice) {
    return ["CANCELLED", "CLOSED", "EXPIRED"].includes(noticeLifecycleStatus(notice));
  }

  function isCancelledNotice(notice) {
    return noticeLifecycleStatus(notice) === "CANCELLED";
  }

  function isVisibleEndedNotice(notice) {
    // Earlier clients keyed visibility to isCancelledNotice(notice) and
    // notice.analysisState === "EVALUATED". The current contract includes
    // every ended notice while keeping those markers documented here.
    return isEndedNotice(notice);
  }

  function noticeLifecycleLabel(notice) {
    const lifecycle = noticeLifecycleStatus(notice);
    if (lifecycle === "CANCELLED") return "취소공고";
    return lifecycle === "CLOSED" ? "공고 종료" : "입찰마감 경과";
  }

  function noticeLifecycleBadge(notice) {
    if (!isEndedNotice(notice)) return "";
    const lifecycle = noticeLifecycleStatus(notice);
    const modifier = lifecycle === "CANCELLED"
      ? "notice-lifecycle-badge--cancelled"
      : lifecycle === "EXPIRED"
        ? "notice-lifecycle-badge--expired"
        : "";
    return `<span class="notice-lifecycle-badge ${modifier}">${noticeLifecycleLabel(notice)}</span>`;
  }

  function recommendationPill(recommendation, notice = null) {
    const value = RECOMMENDATION_LABELS[recommendation] ? recommendation : "UNKNOWN";
    const className = value === "GO" ? "go" : ["CONDITIONAL_GO", "HOLD", "DEFERRED"].includes(value) ? "conditional" : value === "NO_GO" ? "no" : "unknown";
    const conditions = arrayValue(notice?.recommendationConditions);
    const conditional = ["CONDITIONAL_GO", "HOLD"].includes(value);
    const label = conditional && !conditions.length ? "권고 보류" : RECOMMENDATION_LABELS[value];
    const evidenceCount = Math.max(0, numberOrNull(notice?.recommendationEvidenceCount) ?? 0);
    const evidenceLabel = evidenceCount ? `근거 ${formatNumber(evidenceCount)}건` : "근거 수 확인 필요";
    const condition = conditional && conditions.length
      ? `${truncateText(conditions[0], 120)}${conditions.length > 1 ? ` · 외 ${formatNumber(conditions.length - 1)}건` : ""}`
      : "";
    return aiJudgmentMarkup(label, evidenceLabel, className, condition, conditions.join(" / "));
  }

  function effectiveEligibilityStatus(notice) {
    const severity = { PASS: 0, PASS_CURRENT: 1, PASS_EXCEPTION: 2, REVIEW: 3, UNKNOWN: 3, FAIL: 4 };
    const normalize = (value) => {
      const status = String(value || "UNKNOWN").toUpperCase();
      return STATUS_LABELS[status] ? status : "UNKNOWN";
    };
    const candidates = [normalize(notice?.eligibilityStatus)];
    arrayValue(notice?.requirements).forEach((requirement) => {
      if (requirement?.mandatory !== false) candidates.push(normalize(requirement?.status));
    });
    const worst = candidates.reduce((current, candidate) => (
      severity[candidate] > severity[current] ? candidate : current
    ), "PASS");
    return worst === "UNKNOWN" ? "REVIEW" : worst;
  }

  function effectiveRecommendation(notice) {
    const value = RECOMMENDATION_LABELS[notice?.recommendation] ? notice.recommendation : "UNKNOWN";
    if (value === "GO" && ["FAIL", "REVIEW", "UNKNOWN"].includes(effectiveEligibilityStatus(notice))) return "DEFERRED";
    if (["CONDITIONAL_GO", "HOLD"].includes(value) && !arrayValue(notice?.recommendationConditions).length) return "DEFERRED";
    return value;
  }

  function aiJudgmentMarkup(label, meta, tone = "unknown", condition = "", conditionTitle = condition) {
    const icons = {
      go: '<path d="m5 12 4 4L19 6" />',
      conditional: '<path d="M12 7v6M12 17h.01" />',
      no: '<path d="m7 7 10 10M17 7 7 17" />',
      pending: '<circle cx="12" cy="12" r="7" />',
      unknown: '<path d="M12 7v6M12 17h.01" />',
    };
    return `<span class="ai-judgment ai-judgment--${escapeAttribute(tone)}"><span class="ai-judgment__main"><svg viewBox="0 0 24 24" aria-hidden="true">${icons[tone] || icons.unknown}</svg><span><small>AI 판단</small><strong>${escapeHtml(label)}</strong></span></span><small class="ai-judgment__evidence">${escapeHtml(meta)}</small>${condition ? `<small class="ai-judgment__condition" title="${escapeAttribute(conditionTitle)}">조건 · ${escapeHtml(condition)}</small>` : ""}</span>`;
  }

  function operatorDecisionIndicator(notice) {
    const label = operatorDecisionLabel(notice);
    const tone = notice.decision === "GO" ? "participate" : notice.decision === "NO_GO" ? "decline" : notice.decision ? "hold" : hasKnownOperatorDecision(notice) ? "undecided" : "unavailable";
    return `<span class="operator-decision operator-decision--${tone}"><small>담당자</small><strong>${escapeHtml(label)}</strong></span>`;
  }

  function operatorDecisionClass(notice) {
    if (notice.decision === "GO") return "decision-participate";
    if (notice.decision === "NO_GO") return "decision-decline";
    if (notice.decision) return "decision-hold";
    return hasKnownOperatorDecision(notice) ? "decision-undecided" : "";
  }

  function emptyPanel(title, copy) {
    return `<div class="empty-panel"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(copy)}</p></div>`;
  }

  function deadlineInfo(value) {
    const date = validDate(value);
    if (!date) return { date: "마감 미확인", absolute: "마감 미확인", time: "", relative: "일정 확인 필요", urgent: false };
    const days = daysUntil(value);
    let relative = "마감됨";
    if (days === 0) relative = "오늘 마감";
    else if (days === 1) relative = "내일 마감";
    else if (days > 1) relative = `D-${days}`;
    else if (days < 0) relative = `D+${Math.abs(days)}`;
    return {
      date: new Intl.DateTimeFormat("ko-KR", { month: "2-digit", day: "2-digit", weekday: "short" }).format(date),
      absolute: new Intl.DateTimeFormat("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date),
      time: new Intl.DateTimeFormat("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false }).format(date),
      relative,
      urgent: days !== null && days >= 0 && days <= URGENT_DEADLINE_DAYS,
    };
  }

  function daysUntil(value) {
    const date = validDate(value);
    if (!date) return null;
    const kstDayNumber = (input) => {
      const parts = Object.fromEntries(
        new Intl.DateTimeFormat("en-CA", {
          timeZone: "Asia/Seoul",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
        }).formatToParts(input).map((part) => [part.type, part.value]),
      );
      return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)) / 86400000;
    };
    return kstDayNumber(date) - kstDayNumber(new Date());
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
    const verifiedAxes = arrayValue(notice?.riskAxes).filter((axis) => numberOrNull(axis?.score) !== null).length;
    return notice.riskScore === null || verifiedAxes < 4
      ? `산정 보류 · ${verifiedAxes}/6`
      : `${formatScore(notice.riskScore)}/100`;
  }

  function analysisStatusLabel(notice) {
    if (isCancelledNotice(notice)) return "취소 공고";
    if (notice.historicalAnalysis) return `당시 ${STATUS_LABELS[notice.eligibilityStatus] || "미확인"}`;
    if (notice.analysisState === "ANALYZED") return "판단 대기";
    return isDocumentQualityReview(notice) ? "근거 보완" : STATUS_LABELS[effectiveEligibilityStatus(notice)];
  }

  function analysisRecommendationLabel(notice) {
    if (isCancelledNotice(notice)) return "취소 · 추천 비활성";
    if (notice.historicalAnalysis) return "당시 판정 참고";
    if (isDocumentQualityReview(notice)) return "판단 보류";
    if (notice.analysisState === "EVALUATED") {
      return RECOMMENDATION_LABELS[effectiveRecommendation(notice)];
    }
    if (notice.analysisState === "ANALYZED") return "판단 전";
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

  function formatKstDateTime(value) {
    const date = validDate(value);
    if (!date) return "—";
    return `${new Intl.DateTimeFormat("ko-KR", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date)} KST`;
  }

  function formatCalendarDate(value) {
    const date = validDate(value);
    if (!date) return "미확인";
    return new Intl.DateTimeFormat("ko-KR", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
  }

  function formatDateInputValue(value) {
    const date = validDate(value);
    if (!date) return "";
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function dateSpanDays(fromDate, toDate) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(fromDate) || !/^\d{4}-\d{2}-\d{2}$/.test(toDate)) return null;
    const [fromYear, fromMonth, fromDay] = fromDate.split("-").map(Number);
    const [toYear, toMonth, toDay] = toDate.split("-").map(Number);
    const start = Date.UTC(fromYear, fromMonth - 1, fromDay);
    const end = Date.UTC(toYear, toMonth - 1, toDay);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
    return Math.round((end - start) / 86400000);
  }

  function formatBusinessNumber(value) {
    const digits = String(value || "").replace(/\D/g, "").slice(0, 10);
    if (digits.length <= 3) return digits;
    if (digits.length <= 5) return `${digits.slice(0, 3)}-${digits.slice(3)}`;
    return `${digits.slice(0, 3)}-${digits.slice(3, 5)}-${digits.slice(5)}`;
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
    if (["PASS_EXCEPTION", "EXCEPTION_PASS", "CONDITIONAL_PASS"].includes(normalized)) return "PASS_EXCEPTION";
    if (["PASS_CURRENT", "CURRENT_PASS"].includes(normalized)) return "PASS_CURRENT";
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
    if (["EVALUATED", "COMPLETE"].includes(normalized)) return "EVALUATED";
    // The API's ANALYZED state proves current attachment coverage, not that a
    // current deterministic evaluation was persisted. Preserve that stage so
    // the judgement action can finish the pipeline without document rework.
    if (normalized === "ANALYZED") return "ANALYZED";
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

    const providerDisposition = stringValue(firstValue(source.provider_disposition, source.providerDisposition)).toUpperCase();
    if (providerDisposition === "CANCELLED") {
      return { code: "CANCELLED", message: "조달청 취소 공고로 확인되어 현재 입찰 검토 대상에서 제외되었습니다." };
    }
    if (analysisState === "ANALYZED" && code === "ANALYZED") {
      return { code: "EVALUATION_MISSING", message: ANALYSIS_REASON_LABELS.EVALUATION_MISSING };
    }
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
    if (/failed to fetch|networkerror|load failed|timeout/i.test(message)) return "네트워크 연결을 확인한 뒤 다시 시도해 주세요.";
    const status = Number(error?.status);
    if (status === 401) return "인증 정보가 만료되었거나 올바르지 않습니다.";
    if (status === 403) return "이 작업을 실행할 권한이 없습니다.";
    if (status === 404) return "요청한 자료를 찾을 수 없습니다.";
    if (status === 409) return "다른 변경사항이 먼저 저장되었습니다. 최신 상태를 불러온 뒤 다시 시도해 주세요.";
    if (status === 422) return "입력값과 필수 항목을 다시 확인해 주세요.";
    if (status === 429) return "요청이 많습니다. 잠시 뒤 다시 시도해 주세요.";
    if (status >= 500) return "서버 처리에 시간이 걸리고 있습니다. 잠시 뒤 다시 시도해 주세요.";
    return "요청을 완료하지 못했습니다. 잠시 뒤 다시 시도해 주세요.";
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
