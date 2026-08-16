(() => {
  "use strict";

  const API_BASE = (document.documentElement.dataset.apiBase || "/api/v1").replace(/\/$/, "");
  const REQUEST_TIMEOUT_MS = 12000;
  const DECIDER_NAME = "KMA 입찰팀";

  const state = {
    source: "loading",
    sourceReason: "",
    dashboard: {},
    notices: [],
    filteredNotices: [],
    selectedNotice: null,
    selectedTrigger: null,
    currentView: "all",
    layout: window.matchMedia("(max-width: 680px)").matches ? "cards" : "table",
    loading: false,
    detailLoading: false,
    requestSequence: 0,
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

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    detectTeamsContext();
    bindEvents();
    setLayout(state.layout);
    loadApplicationData();
  }

  function cacheElements() {
    const ids = [
      "demoBanner", "demoBannerReason", "retryApiButton", "systemStatusDot", "systemStatusText", "lastSyncText",
      "pageTitle", "mobileMenuButton", "refreshButton", "replayButton", "mainContent", "navNewCount", "navReviewCount",
      "navDecisionCount", "kpiNew", "kpiReview", "kpiGo", "kpiUrgent", "kpiNewTrend", "kpiReviewTrend", "kpiGoTrend",
      "noticeHeading", "noticeSummary", "filterForm", "searchInput", "eligibilityFilter", "recommendationFilter", "sortSelect",
      "resetFiltersButton", "noticePanel", "noticeTableWrap", "noticeTableBody", "noticeCardGrid", "loadingState", "errorState",
      "errorStateMessage", "errorRetryButton", "emptyState", "emptyResetButton", "dataSourceLabel", "sidebarScrim", "drawerScrim",
      "detailDrawer", "drawerLoading", "closeDetailButton", "copyLinkButton", "detailSourceBadge", "detailNoticeId", "drawerScroll",
      "detailTags", "detailTitle", "detailAgency", "detailFacts", "decisionSummary", "analysisPipeline", "evidenceCount",
      "detailSummary", "eligibilityOverall", "requirementList", "actionCard", "actionList", "evidenceList", "scoreOverview",
      "quantTableBody", "riskTotalLabel", "riskBars", "historyList", "decisionForm", "decisionExisting", "toggleCommentButton",
      "commentField", "decisionComment", "commentCount", "saveDecisionButton", "toastRegion", "skeletonRowTemplate",
    ];

    ids.forEach((id) => {
      els[id] = document.getElementById(id);
    });
    els.sidebar = document.querySelector(".sidebar");
    els.navItems = [...document.querySelectorAll(".nav-item[data-view]")];
    els.layoutButtons = [...document.querySelectorAll("[data-layout]")];
    els.tabButtons = [...document.querySelectorAll("[role='tab'][data-tab]")];
    els.tabPanels = [...document.querySelectorAll("[role='tabpanel'][data-panel]")];
    els.decisionInputs = [...document.querySelectorAll("input[name='decision']")];
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
    }
  }

  function bindEvents() {
    els.refreshButton.addEventListener("click", () => loadApplicationData({ forceApi: true }));
    els.retryApiButton.addEventListener("click", () => loadApplicationData({ forceApi: true }));
    els.errorRetryButton.addEventListener("click", () => loadApplicationData({ forceApi: true }));
    els.replayButton.addEventListener("click", runReplay);

    els.filterForm.addEventListener("input", applyFilters);
    els.filterForm.addEventListener("change", applyFilters);
    els.filterForm.addEventListener("reset", () => window.setTimeout(applyFilters, 0));
    els.emptyResetButton.addEventListener("click", resetFilters);

    els.navItems.forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
    els.layoutButtons.forEach((button) => button.addEventListener("click", () => setLayout(button.dataset.layout)));

    els.noticeTableBody.addEventListener("click", handleNoticeActivation);
    els.noticeTableBody.addEventListener("keydown", handleNoticeKeydown);
    els.noticeCardGrid.addEventListener("click", handleNoticeActivation);

    els.mobileMenuButton.addEventListener("click", toggleMobileMenu);
    els.sidebarScrim.addEventListener("click", closeMobileMenu);

    els.closeDetailButton.addEventListener("click", closeDetail);
    els.drawerScrim.addEventListener("click", closeDetail);
    els.copyLinkButton.addEventListener("click", copyCurrentNoticeLink);
    els.detailDrawer.addEventListener("keydown", trapDrawerFocus);

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

    document.addEventListener("keydown", handleGlobalKeydown);
    window.addEventListener("popstate", handleRouteChange);
    window.matchMedia("(max-width: 680px)").addEventListener("change", (event) => {
      if (event.matches) setLayout("cards");
    });
  }

  async function loadApplicationData({ forceApi = false } = {}) {
    const sequence = ++state.requestSequence;
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

    const [dashboardResult, noticesResult] = await Promise.allSettled([
      apiRequest("/dashboard"),
      apiRequest("/notices"),
    ]);

    if (sequence !== state.requestSequence) return;

    if (noticesResult.status === "fulfilled") {
      const list = extractList(noticesResult.value);
      state.notices = list.map(normalizeNotice).filter((notice) => notice.noticeKey);
      state.dashboard = dashboardResult.status === "fulfilled"
        ? normalizeDashboard(dashboardResult.value, state.notices)
        : deriveDashboard(state.notices);
      state.source = "api";
      state.sourceReason = dashboardResult.status === "rejected" ? "일부 운영 지표는 공고 데이터에서 계산했습니다." : "";
      setSystemStatus("online");
      if (state.dashboard.syntheticWarning) showDemoBanner(state.dashboard.syntheticWarning);
      else hideDemoBanner();
      renderAll();
      finishLoading();
      openNoticeFromRoute();
      return;
    }

    const reason = humanizeError(noticesResult.reason);
    activateDemo(`서버 API 연결 실패: ${reason}`);
    finishLoading();
    openNoticeFromRoute();
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
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

    try {
      const response = await fetch(`${API_BASE}${path}`, {
        credentials: "same-origin",
        ...options,
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
        throw new Error(message);
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("요청 시간이 초과되었습니다");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
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
    const rawRequirements = arrayValue(firstValue(source.requirements, source.eligibility_requirements, source.conditions, []));
    const requirements = mergeRequirementsAndAtomics(rawRequirements, atomicResults);
    const rawEvidence = arrayValue(firstValue(source.evidence, source.evidences, source.source_evidence, []));
    const evidence = (rawEvidence.length ? rawEvidence : atomicResults.filter((item) => item?.source_excerpt || item?.source_location))
      .map((item, itemIndex) => normalizeEvidence(item, itemIndex));
    const history = arrayValue(firstValue(source.award_history, source.awardHistory, source.history, []))
      .map(normalizeHistory);

    return {
      raw: source,
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
      category: stringValue(firstValue(source.category, source.business_category, source.notice_type), "용역"),
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
      explanation,
      atomicResults,
      versions,
      latestVersion,
      decisions,
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

  function normalizeHistory(item) {
    const source = item && typeof item === "object" ? item : {};
    return {
      year: stringValue(firstValue(source.year, source.award_year, source.date ? String(source.date).slice(0, 4) : null), "연도 미확인"),
      title: stringValue(firstValue(source.title, source.project_name, source.notice_title), "유사 사업"),
      winner: stringValue(firstValue(source.winner, source.awardee, source.company), "낙찰자 미확인"),
      amount: firstValue(source.amount, source.award_amount, source.contract_amount, null),
      rate: numberOrNull(firstValue(source.rate, source.award_rate, source.bid_rate)),
      agency: stringValue(firstValue(source.agency, source.ordering_agency), ""),
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
      newCount: numberOrNull(firstValue(kpis.new_count, kpis.newCount, kpis.new_notices, kpis.new, totals.notices)) ?? derived.newCount,
      reviewCount: numberOrNull(firstValue(source.pending_review, kpis.review_count, kpis.reviewCount, kpis.needs_review, kpis.review, eligibilityCounts.REVIEW)) ?? derived.reviewCount,
      goCount: numberOrNull(firstValue(kpis.go_count, kpis.goCount, kpis.go_candidates, kpis.go)) ?? derived.goCount,
      urgentCount: numberOrNull(firstValue(source.deadline_soon, kpis.urgent_count, kpis.urgentCount, kpis.urgent)) ?? derived.urgentCount,
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
      newCount: notices.filter((notice) => notice.isNew).length,
      reviewCount: notices.filter((notice) => notice.eligibilityStatus === "REVIEW").length,
      goCount: notices.filter((notice) => notice.recommendation === "GO").length,
      urgentCount: notices.filter((notice) => {
        const days = daysUntil(notice.deadline);
        return days !== null && days >= 0 && days <= 3;
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
    els.kpiNew.textContent = displayNumber(data.newCount);
    els.kpiReview.textContent = displayNumber(data.reviewCount);
    els.kpiGo.textContent = displayNumber(data.goCount);
    els.kpiUrgent.textContent = displayNumber(data.urgentCount);
    els.kpiNewTrend.textContent = state.source === "demo" ? "데모" : "실시간";
    els.kpiReviewTrend.textContent = "REVIEW";
    els.kpiGoTrend.textContent = "추천";
  }

  function renderNavigationCounts() {
    els.navNewCount.textContent = displayNumber(state.dashboard.newCount);
    els.navReviewCount.textContent = displayNumber(state.dashboard.reviewCount);
    els.navDecisionCount.textContent = displayNumber(state.dashboard.undecidedCount);
  }

  function renderDataSource() {
    if (state.source === "api") {
      els.dataSourceLabel.textContent = state.sourceReason ? `실시간 API · ${state.sourceReason}` : "실시간 API 데이터";
    } else if (state.source === "demo") {
      els.dataSourceLabel.textContent = "데모 예시 데이터 · 실제 판정 결과가 아닙니다";
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
      if (state.currentView === "new" && !notice.isNew) return false;
      if (state.currentView === "review" && notice.eligibilityStatus !== "REVIEW") return false;
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
    if (sort === "readiness") return nullableNumberSort(b.readinessScore, a.readinessScore);
    if (sort === "risk") return nullableNumberSort(a.riskScore, b.riskScore);
    if (sort === "newest") return nullableDateSort(b.collectedAt, a.collectedAt);
    return nullableDateSort(a.deadline, b.deadline);
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
    els.noticeSummary.textContent = count === total
      ? `총 ${formatNumber(total)}건의 공고를 우선순위에 따라 확인하세요.`
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
    const readiness = formatScore(notice.readinessScore);
    const readinessClass = scoreClass(notice.readinessScore);
    return `
      <tr class="notice-row" data-notice-key="${escapeAttribute(notice.noticeKey)}">
        <td>
          <button class="notice-title-button" type="button" data-open-notice aria-label="${escapeAttribute(notice.title)} 상세보기">
            <span class="notice-title">${escapeHtml(notice.title)}</span>
            <span class="notice-meta"><span>${escapeHtml(notice.agency)}</span><span class="dot-divider">${escapeHtml(formatBudget(notice.budget))}</span></span>
          </button>
        </td>
        <td><span class="deadline ${deadline.urgent ? "is-urgent" : ""}">${escapeHtml(deadline.date)}<small>${escapeHtml(deadline.relative)}</small></span></td>
        <td>${statusPill(notice.eligibilityStatus)}</td>
        <td><div class="score-cell ${readinessClass}"><strong>${readiness}</strong><span class="mini-bar" aria-hidden="true"><span style="width:${clamp(notice.readinessScore ?? 0, 0, 100)}%"></span></span></div></td>
        <td><span class="risk-score ${riskClass(notice.riskScore)}">${formatScore(notice.riskScore)}</span></td>
        <td>${recommendationPill(notice.recommendation)}</td>
        <td><span class="row-arrow" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6" /></svg></span></td>
      </tr>`;
  }

  function renderNoticeCard(notice) {
    const deadline = deadlineInfo(notice.deadline);
    return `
      <button class="notice-card" type="button" data-notice-key="${escapeAttribute(notice.noticeKey)}" data-open-notice>
        <span class="notice-card__head">
          ${statusPill(notice.eligibilityStatus)}
          <span class="notice-card__deadline ${deadline.urgent ? "is-urgent" : ""}">${escapeHtml(deadline.relative)}</span>
        </span>
        <h3>${escapeHtml(notice.title)}</h3>
        <p>${escapeHtml(notice.agency)} · ${escapeHtml(formatBudget(notice.budget))}</p>
        <span class="notice-card__metrics">
          <span class="notice-card__metric"><small>준비도</small><strong>${formatScore(notice.readinessScore)}</strong></span>
          <span class="notice-card__metric"><small>리스크</small><strong>${formatScore(notice.riskScore)}</strong></span>
        </span>
        <span class="notice-card__foot">${recommendationPill(notice.recommendation)}<span>근거 확인 →</span></span>
      </button>`;
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
      els.systemStatusText.textContent = "운영 API 연결됨";
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
    els.demoBanner.hidden = false;
    els.demoBannerReason.textContent = reason;
    els.retryApiButton.textContent = state.source === "api" ? "데이터 새로고침" : "실데이터 다시 연결";
  }

  function hideDemoBanner() {
    els.demoBanner.hidden = true;
  }

  function setView(view) {
    state.currentView = view;
    const titles = {
      all: ["오늘의 입찰 기회", "검토할 공고"],
      new: ["진행 공고", "현재 진행 중인 공고"],
      review: ["검토 대기", "담당자 확인이 필요한 공고"],
      undecided: ["결정 관리", "아직 결정되지 않은 공고"],
      closed: ["결과 학습", "결과가 확인된 공고"],
    };
    els.pageTitle.textContent = titles[view]?.[0] || titles.all[0];
    els.noticeHeading.textContent = titles[view]?.[1] || titles.all[1];
    els.navItems.forEach((item) => {
      const active = item.dataset.view === view;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    closeMobileMenu();
    applyFilters();
    els.mainContent.focus({ preventScroll: true });
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
    applyFilters();
  }

  function handleNoticeActivation(event) {
    const target = event.target.closest("[data-open-notice]");
    if (!target) return;
    const row = target.closest("[data-notice-key]");
    if (!row) return;
    openDetail(row.dataset.noticeKey, target);
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

    if (updateRoute) updateNoticeRoute(noticeKey);
    if (state.source !== "api") return;

    state.detailLoading = true;
    els.drawerLoading.hidden = false;
    try {
      const payload = await apiRequest(`/notices/${encodeURIComponent(noticeKey)}`);
      const detail = normalizeNotice(unwrapObject(payload));
      const merged = normalizeNotice({ ...baseNotice.raw, ...detail.raw, notice_key: noticeKey });
      const index = state.notices.findIndex((notice) => notice.noticeKey === noticeKey);
      if (index >= 0) state.notices[index] = merged;
      state.selectedNotice = merged;
      renderDetail(merged);
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
    const synthetic = state.source === "demo" || notice.noticeKey.startsWith("SYN-") || notice.category.toUpperCase() === "SYNTHETIC";
    els.detailSourceBadge.textContent = synthetic ? "합성·데모 분석" : "실시간 분석";
    els.detailSourceBadge.classList.toggle("is-demo", synthetic);
    els.detailNoticeId.textContent = `공고번호 ${notice.noticeNumber}`;
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
      summaryMetric("참가 자격", STATUS_LABELS[notice.eligibilityStatus], ""),
      summaryMetric("준비도 / 증빙", `${formatScore(notice.readinessScore)} / ${formatScore(notice.evidenceCoverage)}`, ""),
      summaryMetric("AI 추천", RECOMMENDATION_LABELS[notice.recommendation], "summary-metric--recommendation"),
    ].join("");
    els.analysisPipeline.innerHTML = renderPipeline(notice);
    els.detailSummary.textContent = notice.summary;
    els.eligibilityOverall.innerHTML = statusPill(notice.eligibilityStatus);
    els.evidenceCount.textContent = String(evidence.length);
    els.requirementList.innerHTML = requirements.length
      ? requirements.map(renderRequirement).join("")
      : emptyPanel("구조화된 자격 조건이 없습니다", "첨부파일 분석이 완료되면 조건별 판정이 표시됩니다.");
    renderActions(notice);
    els.evidenceList.innerHTML = evidence.length
      ? evidence.map(renderEvidence).join("")
      : emptyPanel("연결된 원문 근거가 없습니다", "근거가 없는 결과는 확정 판정으로 사용하지 마세요.");
    renderQuantAndRisk(notice);
    els.historyList.innerHTML = notice.awardHistory.length
      ? notice.awardHistory.map(renderHistory).join("")
      : emptyPanel("연결된 낙찰 이력이 없습니다", "조달청 낙찰·계약 API 연계 후 최근 3년 이력을 제공합니다.");
    renderExistingDecision(notice);
    els.drawerScroll.scrollTop = 0;
  }

  function detailFact(label, value) {
    return `<div class="detail-fact"><small>${escapeHtml(label)}</small><strong title="${escapeAttribute(value)}">${escapeHtml(value)}</strong></div>`;
  }

  function summaryMetric(label, value, className) {
    return `<div class="summary-metric ${className}"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`;
  }

  function renderPipeline(notice) {
    const hasDocuments = notice.evidence.length > 0;
    const hasRules = notice.requirements.length > 0;
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
      { name: "규칙 판정", detail: STATUS_LABELS[notice.eligibilityStatus], status: hasRules ? (notice.eligibilityStatus === "REVIEW" ? "review" : "done") : "pending" },
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

  function renderQuantAndRisk(notice) {
    const readiness = notice.readinessScore;
    const coverage = notice.evidenceCoverage;
    const risk = notice.riskScore;
    els.scoreOverview.innerHTML = [
      scoreCard("준비도", readiness, "score-card--readiness"),
      scoreCard("증빙 커버리지", coverage, "score-card--coverage"),
      scoreCard("종합 리스크", risk, "score-card--risk"),
    ].join("");
    els.quantTableBody.innerHTML = notice.quantitative.length
      ? notice.quantitative.map(renderQuantRow).join("")
      : `<tr><td colspan="4"><div class="empty-panel"><strong>정량 산식이 연결되지 않았습니다</strong><p>평가표 구조화 후 확정점수 또는 예상 범위를 제공합니다.</p></div></td></tr>`;
    els.riskTotalLabel.textContent = risk === null ? "총점 —" : `총점 ${Math.round(risk)}`;
    const axes = notice.riskAxes;
    els.riskBars.innerHTML = axes.length
      ? axes.map((axis) => `
        <div class="risk-row ${riskClass(axis.score)}">
          <strong>${escapeHtml(axis.label)}</strong>
          <span class="risk-bar" aria-label="${escapeAttribute(axis.label)} ${Math.round(axis.score)}점"><span style="width:${clamp(axis.score, 0, 100)}%"></span></span>
          <span>${Math.round(axis.score)}</span>
        </div>`).join("")
      : emptyPanel("세부 리스크가 산정되지 않았습니다", "위험 축별 근거가 연결되면 자격·증빙·경쟁·일정·운영·데이터 리스크를 표시합니다.");
  }

  function scoreCard(label, value, className) {
    return `<div class="score-card ${className}"><small>${escapeHtml(label)}</small><strong>${value === null ? "—" : Math.round(value)}<span>${value === null ? "" : " / 100"}</span></strong><span class="progress-bar" aria-hidden="true"><span style="width:${value ?? 0}%"></span></span></div>`;
  }

  function renderQuantRow(item) {
    const statusClass = item.status === "PROVISIONAL" ? "is-provisional" : item.status === "MISSING" ? "is-missing" : "";
    const statusLabel = item.status === "VERIFIED" ? "확정" : item.status === "PROVISIONAL" ? "잠정" : "미확인";
    const expected = item.expectedScore === null || item.expectedScore === undefined ? "—" : String(item.expectedScore);
    return `<tr><td>${escapeHtml(item.label)}</td><td>${item.maxScore === null ? "—" : formatNumber(item.maxScore)}</td><td>${escapeHtml(expected)}</td><td><span class="quant-status ${statusClass}">${statusLabel}</span></td></tr>`;
  }

  function renderHistory(item) {
    return `
      <article class="history-card">
        <span class="history-year">${escapeHtml(item.year)}</span>
        <span class="history-copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.winner)}${item.agency ? ` · ${escapeHtml(item.agency)}` : ""}</span></span>
        <span class="history-price"><strong>${escapeHtml(formatBudget(item.amount))}</strong><span>${item.rate === null ? "낙찰률 미확인" : `낙찰률 ${formatNumber(item.rate, 1)}%`}</span></span>
      </article>`;
  }

  function renderExistingDecision(notice) {
    els.decisionInputs.forEach((input) => {
      input.checked = notice.decision === input.value || (notice.decision === "CONDITIONAL_GO" && input.value === "HOLD");
    });
    els.decisionComment.value = notice.decisionComment;
    els.commentCount.textContent = String(notice.decisionComment.length);
    els.commentField.hidden = !notice.decisionComment;
    els.toggleCommentButton.setAttribute("aria-expanded", String(Boolean(notice.decisionComment)));
    if (notice.decision) {
      const meta = [DECISION_LABELS[notice.decision] || notice.decision, notice.decidedBy, notice.decidedAt ? formatShortDateTime(notice.decidedAt) : ""].filter(Boolean);
      els.decisionExisting.textContent = meta.join(" · ");
    } else {
      els.decisionExisting.textContent = "아직 결정되지 않았습니다.";
    }
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
    els.saveDecisionButton.disabled = !selected || !state.selectedNotice;
  }

  async function saveDecision(event) {
    event.preventDefault();
    const notice = state.selectedNotice;
    const decision = els.decisionInputs.find((input) => input.checked)?.value;
    if (!notice || !decision) return;
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
      els.replayButton.disabled = false;
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
    if (event.key === "/" && !isEditableTarget(event.target)) {
      event.preventDefault();
      els.searchInput.focus();
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
      urgent: days !== null && days >= 0 && days <= 3,
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
          return days !== null && days >= 0 && days <= 3;
        }).length,
        undecided_count: notices.filter((notice) => !notice.decision).length,
        last_sync: now,
      },
      notices,
    };
  }
})();
