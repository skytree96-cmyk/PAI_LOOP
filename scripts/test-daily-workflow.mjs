import assert from "node:assert/strict";
import fs from "node:fs/promises";


const workflow = JSON.parse(
  await fs.readFile("workflows/pai-loop-10-daily-opportunity-briefing.json", "utf8"),
);
const nodes = new Map(workflow.nodes.map((node) => [node.name, node]));

assert.doesNotMatch(
  nodes.get("Validate Complete Dry-Run Contract")?.parameters?.jsCode ?? "",
  /\bnew URL\b/,
  "n8n Code sandbox does not expose the URL constructor",
);

function executeCodeNodeItems(name, input = {}, globals = {}) {
  const node = nodes.get(name);
  assert(node, `Missing Code node: ${name}`);
  assert.equal(node.type, "n8n-nodes-base.code", `${name} must be a Code node`);
  // This harness executes only the network-free manual path.  n8n globals not
  // used by that path are fail-closed stubs so an accidental dependency is
  // detected instead of silently mocked.
  const blocked = new Proxy({}, {
    get(_target, property) {
      throw new Error(`Offline harness forbids runtime global access: ${String(property)}`);
    },
  });
  // eslint-disable-next-line no-new-func
  const run = new Function("$json", "$env", "$node", "$now", "$input", node.parameters.jsCode);
  const result = run(
    input,
    globals.env ?? blocked,
    globals.node ?? blocked,
    globals.now ?? blocked,
    globals.input ?? blocked,
  );
  assert(Array.isArray(result), `${name} must return an item array`);
  return result.map((item, index) => {
    assert(item && typeof item.json === "object", `${name}[${index}] must contain json`);
    return item.json;
  });
}

function executeCodeNode(name, input = {}, globals = {}) {
  const result = executeCodeNodeItems(name, input, globals);
  assert.equal(result.length, 1, `${name} must return one item`);
  return result[0];
}

const scheduledPreview = executeCodeNode("Scheduled Runtime Gates", {}, { env: {} });
assert.equal(scheduledPreview.runtime.apiBaseUrl, "https://pai-loop-demo.onrender.com");
assert.equal(scheduledPreview.runtime.webBaseUrl, "https://pai-loop-demo.onrender.com");
assert.equal(scheduledPreview.runtime.dailyLiveEnabled, false);
assert.equal(scheduledPreview.runtime.analysisBatchEnabled, false);
assert.equal(scheduledPreview.runtime.analysisBatchWriteEnabled, false);
assert.equal(scheduledPreview.runtime.maxAnalysisBatchNotices, 5);
assert.equal(scheduledPreview.runtime.retentionLiveEnabled, false);
assert.equal(scheduledPreview.runtime.awardRefreshEnabled, false);
assert.equal(scheduledPreview.runtime.awardRefreshWriteEnabled, false);
assert.equal(scheduledPreview.runtime.teamsMockLogEnabled, false);
assert.equal(scheduledPreview.runtime.maxAwardRefreshNotices, 3);

const scheduledAnalysisDry = executeCodeNode("Scheduled Runtime Gates", {}, {
  env: {
    PAI_LOOP_DAILY_LIVE_ENABLED: "true",
    PAI_LOOP_ANALYSIS_BATCH_ENABLED: "true",
  },
});
assert.equal(scheduledAnalysisDry.runtime.dailyLiveEnabled, true);
assert.equal(scheduledAnalysisDry.runtime.analysisBatchEnabled, true);
assert.equal(scheduledAnalysisDry.runtime.analysisBatchWriteEnabled, false);

const httpNodes = workflow.nodes.filter((node) => node.type === "n8n-nodes-base.httpRequest");
assert.equal(httpNodes.length, 7);
for (const node of httpNodes) {
  assert.equal(node.parameters.authentication, "genericCredentialType", `${node.name}: auth mode`);
  assert.equal(node.parameters.genericAuthType, "httpHeaderAuth", `${node.name}: auth type`);
  assert.equal(node.credentials, undefined, `${node.name}: source must not contain credential IDs`);
  const headers = node.parameters.headerParameters?.parameters ?? [];
  assert.equal(
    headers.some((header) => String(header.name).toLowerCase() === "x-pai-loop-api-key"),
    false,
    `${node.name}: API key header must come from the credential store`,
  );
}

const connectionTargets = (name, lane = 0) =>
  (workflow.connections?.[name]?.main?.[lane] ?? []).map((connection) => connection.node);
assert.deepEqual(connectionTargets("Validate PPS Ingestion Contract"), [
  "Build Bounded Batch Analysis Plan",
]);
assert.deepEqual(connectionTargets("Batch Analysis Gate Open?", 0), [
  "Analyze Evaluate and Snapshot PPS Notices",
]);
assert.deepEqual(connectionTargets("Batch Analysis Gate Open?", 1), [
  "Record Batch Analysis Skipped",
]);
assert.deepEqual(connectionTargets("Validate Batch Analysis Contract"), [
  "Verify Batch Analysis Aggregate Invariants",
]);
assert.deepEqual(connectionTargets("Verify Batch Analysis Aggregate Invariants"), [
  "Preview or Apply Seven-Day Log Retention",
]);
assert.deepEqual(connectionTargets("Record Batch Analysis Skipped"), [
  "Preview or Apply Seven-Day Log Retention",
]);
assert.deepEqual(connectionTargets("Validate Seven-Day Retention Contract"), [
  "Fetch Award Candidates from Seven-Day Briefing",
]);

// Exercise the PPS notice-key -> batch analysis planning and strict response
// validator entirely in memory.  Writes are disabled, so the request contract
// remains a bounded dry-run and reports no persisted artifacts or OpenAI call.
const analysisRuntime = {
  ...scheduledPreview.runtime,
  dailyLiveEnabled: true,
  analysisBatchEnabled: true,
  analysisBatchWriteEnabled: false,
  maxAnalysisBatchNotices: 5,
};
const analysisPlan = executeCodeNode(
  "Build Bounded Batch Analysis Plan",
  {
    runtime: analysisRuntime,
    ingestion: {
      noticeKeys: ["notice-a", "notice-b", "notice-a", "notice-c", "notice-d", "notice-e", "notice-f"],
    },
  },
);
assert.equal(analysisPlan.analysisBatch.status, "PLANNED");
assert.equal(analysisPlan.analysisBatch.requested, 5);
assert.equal(analysisPlan.analysisBatch.dryRun, true);
assert.deepEqual(analysisPlan.analysisBatch.noticeKeys, [
  "notice-a",
  "notice-b",
  "notice-c",
  "notice-d",
  "notice-e",
]);
const analysisResponse = {
  job_id: "offline-analysis",
  status: "COMPLETED",
  dry_run: true,
  requested: 5,
  processed: 5,
  completed: 0,
  skipped: 5,
  failed: 0,
  document_materialized: 0,
  evaluations_created: 0,
  snapshots_refreshed: 0,
  openai_calls: 0,
  results: analysisPlan.analysisBatch.noticeKeys.map((noticeKey) => ({
    notice_key: noticeKey,
    status: "SKIPPED",
    document_status: "DRY_RUN",
    evaluation_status: "DRY_RUN",
    snapshot_status: "DRY_RUN",
    analysis_run_id: null,
    evaluation_id: null,
    notice_version_id: null,
    input_sha256: null,
    reused: false,
    materialized_requirements: 0,
    requirement_snapshots: 0,
    score_snapshots: 0,
    recommendation_snapshots: 0,
    warnings: [],
  })),
  warnings: [],
};
const analysisValidated = executeCodeNode(
  "Validate Batch Analysis Contract",
  analysisResponse,
  { node: { "Build Bounded Batch Analysis Plan": { json: analysisPlan } } },
);
assert.equal(analysisValidated.analysisBatch.status, "COMPLETED");
assert.equal(analysisValidated.analysisBatch.processed, 5);
assert.equal(analysisValidated.analysisBatch.openaiCalls, 0);
const analysisConsistent = executeCodeNode(
  "Verify Batch Analysis Aggregate Invariants",
  analysisValidated,
);
assert.equal(analysisConsistent.analysisBatch.results.length, 5);
assert.deepEqual(analysisValidated.analysisBatch.privacyBoundary, {
  documentBodiesEmitted: false,
  piiFieldsEmitted: false,
});

// Exercise the bounded award branch without invoking an HTTP node.  An enabled
// refresh with writes disabled must select no more than three candidates and
// must materialize only three-year, single-page, dry-run requests.
const awardRuntime = {
  ...scheduledPreview.runtime,
  awardRefreshEnabled: true,
  awardRefreshWriteEnabled: false,
  maxAwardRefreshNotices: 3,
};
const awardPlan = executeCodeNode(
  "Build Bounded Award Refresh Plan",
  {
    body: {
      timezone: "Asia/Seoul",
      window: { days: 7 },
      notices: [
        { notice_key: "notice-a", title: "A", priority_score: 80, fit: { eligibility: "GO" }, award_snapshot: { observations: 0 } },
        { notice_key: "notice-b", title: "B", priority_score: 99, fit: { eligibility: "FAIL" }, award_snapshot: { observations: 0 } },
        { notice_key: "notice-c", title: "C", priority_score: 90, fit: { eligibility: "REVIEW" }, award_snapshot: { observations: 2 } },
        { notice_key: "notice-d", title: "D", priority_score: 10, fit: { eligibility: "GO" }, award_snapshot: { observations: 9 } },
      ],
    },
  },
  {
    node: {
      "Scheduled Runtime Gates": { json: { runtime: awardRuntime } },
      "Validate PPS Ingestion Contract": { json: { ingestion: { noticeKeys: ["notice-d"] } } },
    },
  },
);
assert.equal(awardPlan.awardRefresh.status, "PLANNED");
assert.equal(awardPlan.awardRefresh.selected, 3);
assert.equal(awardPlan.awardRefresh.dryRun, true);
assert.deepEqual(
  awardPlan.awardRefresh.candidates.map((candidate) => candidate.noticeKey),
  ["notice-d", "notice-a", "notice-c"],
);
const awardRequests = executeCodeNodeItems("Expand Bounded Award Refresh Requests", awardPlan);
assert.equal(awardRequests.length, 3);
for (const request of awardRequests) {
  assert.deepEqual(request.request, {
    years: 3,
    page_size: 100,
    max_pages_per_window: 1,
    dry_run: true,
  });
  assert.match(request.idempotencyKey, /^award:daily:/);
}
const awardResponses = awardRequests.map((request, index) => ({
  json: {
    job_id: `offline-${index}`,
    status: "COMPLETED",
    notice_key: request.candidate.noticeKey,
    keyword: "교육 컨설팅",
    window: { from: "2023-08-17", to: "2026-08-17" },
    api_calls: 0,
    fetched: 0,
    created: 0,
    updated: 0,
    duplicates: 0,
    records: 0,
    dry_run: true,
    warnings: [],
  },
}));
const awardBatch = executeCodeNode(
  "Validate Award Refresh Batch",
  {},
  {
    node: { "Build Bounded Award Refresh Plan": { json: awardPlan } },
    input: { all: () => awardResponses },
  },
);
assert.equal(awardBatch.awardRefresh.status, "COMPLETED");
assert.equal(awardBatch.awardRefresh.attempted, 3);
assert.deepEqual(awardBatch.awardRefresh.privacyBoundary, {
  candidateRowsEmitted: false,
  piiFieldsEmitted: false,
});

let item = executeCodeNode("Force Zero-Call Manual Context");
item = executeCodeNode("Build Seven-Day Offline Fixture", item);
item = executeCodeNode("Normalize Optional Quant and Pricing", item);
item.notices[0].quantitative_estimate = {
  total_max_points: 100,
  estimated_points: null,
  lower_points: 76,
  upper_points: 86,
  evidence_coverage_pct: 90,
  readiness_band: "GREEN",
};
item.notices[0].pricing_intelligence = {
  record_count: 4,
  concentration: {
    top_winner: { winner_name: "가상 수행기관 A", count: 3, share: 0.75 },
    hhi_interpretation: "HIGH",
  },
  prediction: {
    award_rate: {
      status: "MODEL_ESTIMATE",
      center: 87.35,
      range_low: 86.9,
      range_high: 87.8,
      confidence: "LOW",
    },
  },
};
item = executeCodeNode("Build Consolidated Teams Adaptive Card", item);
item = executeCodeNode("Annotate Batch Analysis on Card", item);
item = executeCodeNode("Render Score Range and Sample Concentration", item);
const backendMockRequest = executeCodeNode("Build Backend Teams Mock Request", item);
const backendMockRecord = executeCodeNode(
  "Validate Backend Teams Mock Record",
  {
    status: "MOCK_RECORDED",
    channel: "teams",
    delivery_mode: "mock",
    notice_key: backendMockRequest.mockRequest.noticeKey,
    correlation_id: backendMockRequest.mockRequest.correlationId,
    card: backendMockRequest.adaptiveCard,
    id: "offline-record",
  },
  { node: { "Build Backend Teams Mock Request": { json: backendMockRequest } } },
);
assert.equal(backendMockRecord.pushMock.persisted, true);
assert.equal(backendMockRecord.pushMock.actualTeamsRequestSent, false);
assert.equal(backendMockRecord.source_calls.teams_mock_log, 1);
item = executeCodeNode("Record One Push Mock Locally", item);
item = executeCodeNode("Validate End-to-End Analysis Boundary", item);
item = executeCodeNode("Validate Complete Dry-Run Contract", item);

assert.equal(item.status, "DRY_RUN_PASSED");
assert.equal(item.executionMode, "manual-fixture");
assert.deepEqual(item.schedule, {
  cron: "0 9 * * *",
  timezone: "Asia/Seoul",
  workflowActive: false,
});
assert.equal(item.retention.days, 7);
assert.equal(item.actualTeamsRequestSent, false);
assert.equal(item.actualPushSent, false);
assert.equal(item.notificationMock.status, "MOCK_LOCAL_ONLY");
assert.equal(item.notificationMock.persisted, false);
assert.equal(item.awardRefresh.status, "SKIPPED");
assert.equal(item.awardRefresh.attempted, 0);
assert.deepEqual(item.enrichmentContract.analysis_batch_summary, {
  status: "SKIPPED",
  dry_run: true,
  requested: 0,
  processed: 0,
  completed: 0,
  skipped: 0,
  failed: 0,
  document_materialized: 0,
  evaluations_created: 0,
  snapshots_refreshed: 0,
  openai_calls: 0,
});
assert.equal(item.adaptiveCard.type, "AdaptiveCard");
assert.equal(item.adaptiveCard.version, "1.5");
assert.match(item.adaptiveCard.body[1].text, /신규 분석 SKIPPED 0\/0건/);
assert.equal(item.briefing.noticeKeys.length, 2);
const facts = item.adaptiveCard.body
  .filter((block) => block.type === "Container")
  .flatMap((block) => block.items.find((child) => child.type === "FactSet")?.facts ?? []);
assert.equal(
  facts.find((fact) => fact.title === "정량 예상")?.value,
  "76~86 / 100점",
);
const concentration = facts.find((fact) => fact.title === "표본 집중도")?.value ?? "";
assert.match(concentration, /3건 \/ 75\.0%/);
assert.match(concentration, /독점 확정 아님/);
assert.equal(facts.find((fact) => fact.title === "리스크")?.value, "GO · 18.4점");
assert.equal(
  facts.find((fact) => fact.title === "경쟁·집중 리스크")?.value,
  "HIGH · 71.2/100 · MEDIUM",
);
assert.match(JSON.stringify(item.adaptiveCard), /법적 독점 판정이 아닙니다/);
assert.deepEqual(item.externalCalls, {
  backend: 0,
  pps: 0,
  awards: 0,
  openai: 0,
  teams_mock_log: 0,
  teams: 0,
});
console.log("Daily workflow offline E2E passed: 0 backend, 0 PPS, 0 award, 0 OpenAI, 0 Teams calls");
