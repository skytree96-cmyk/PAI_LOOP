import assert from "node:assert/strict";
import fs from "node:fs/promises";

const workflow = JSON.parse(await fs.readFile("workflows/pai-loop-12-teams-daily-delivery.json", "utf8"));
const manifest = JSON.parse(await fs.readFile("manifest.json", "utf8"));
const nodes = new Map(workflow.nodes.map((node) => [node.name, node]));

function one(name, input = {}, globals = {}) {
  const target = nodes.get(name);
  assert(target && target.type === "n8n-nodes-base.code", `missing Code node ${name}`);
  const blocked = new Proxy({}, { get(_target, key) { throw new Error(`blocked global ${String(key)}`); } });
  // eslint-disable-next-line no-new-func
  const inputItems = globals.items ?? [{ json: input }];
  const inputApi = {
    all: () => inputItems,
    first: () => inputItems[0],
  };
  const run = new Function("$json", "$vars", "$node", "$execution", "$input", target.parameters.jsCode);
  const result = run(
    input,
    globals.vars ?? {},
    globals.node ?? blocked,
    globals.execution ?? { id: "test-execution", mode: "trigger" },
    inputApi,
  );
  assert(Array.isArray(result) && result.length === 1 && result[0]?.json);
  return result[0].json;
}

function reservationResponse(envelope, ownerEnvelope = envelope) {
  return {
    id: "persistent-reservation-record",
    notice_key: envelope.delivery.reservation.anchorNoticeKey,
    channel: "teams",
    delivery_mode: "mock",
    status: "MOCK_RECORDED",
    correlation_id: envelope.delivery.correlationId,
    card: ownerEnvelope.delivery.reservation.body.card,
    created_at: new Date().toISOString(),
  };
}

function workflowFingerprint(source) {
  let h1 = 0x811c9dc5;
  let h2 = 0x9e3779b9;
  for (let index = 0; index < source.length; index += 1) {
    const code = source.charCodeAt(index);
    h1 = Math.imul(h1 ^ code, 0x01000193);
    h2 = Math.imul(h2 ^ code, 0x85ebca6b);
  }
  return `${(h1 >>> 0).toString(16).padStart(8, "0")}${(h2 >>> 0).toString(16).padStart(8, "0")}`;
}

const targets = (name, lane = 0) => (
  workflow.connections?.[name]?.main?.[lane] ?? []
).map((item) => item.node);

const scheduled = nodes.get("Every Day 09:00 KST");
assert.equal(scheduled.type, "n8n-nodes-base.scheduleTrigger");
assert.equal(scheduled.parameters.rule.interval[0].expression, "0,15,30,45 9-10 * * *");
assert.equal(workflow.settings.timezone, "Asia/Seoul");

const configTable = nodes.get("Read Teams Delivery Config");
assert.equal(configTable.type, "n8n-nodes-base.dataTable");
assert.equal(configTable.typeVersion, 1.1);
assert.equal(configTable.parameters.resource, "row");
assert.equal(configTable.parameters.operation, "get");
assert.deepEqual(configTable.parameters.dataTableId, {
  __rl: true,
  mode: "name",
  value: "pai_loop_teams_delivery_config",
});
assert.equal(configTable.parameters.returnAll, true);
assert.equal(configTable.alwaysOutputData, true);
assert.equal(configTable.retryOnFail, false);
assert.equal(configTable.onError, "continueRegularOutput");
assert.equal(configTable.credentials, undefined);
assert.deepEqual(targets("Run Offline Teams Preview"), ["Build Offline Delivery Fixture"]);
const liveTestTrigger = nodes.get("Run Live Teams Test");
assert.equal(liveTestTrigger.type, "n8n-nodes-base.manualTrigger");
assert.deepEqual(targets("Run Live Teams Test"), ["Mark Manual Live Test Mode"]);
assert.deepEqual(targets("Every Day 09:00 KST"), ["Mark Scheduled Live Mode"]);
assert.deepEqual(targets("Mark Manual Live Test Mode"), ["Mark Config-Gated Delivery Mode"]);
assert.deepEqual(targets("Mark Scheduled Live Mode"), ["Mark Config-Gated Delivery Mode"]);
assert.deepEqual(targets("Mark Config-Gated Delivery Mode"), ["Read Teams Delivery Config"]);
assert.deepEqual(targets("Read Teams Delivery Config"), ["Validate Teams Delivery Config"]);
assert.deepEqual(targets("Approved Teams Push Gate Open?", 0), ["Scheduled Teams Attempt?"]);
assert.deepEqual(targets("Scheduled Teams Attempt?", 0), ["Fetch Today's Daily Analysis Readiness"]);
assert.deepEqual(targets("Scheduled Teams Attempt?", 1), ["Fetch Stored Briefing for Teams"]);
assert.deepEqual(targets("Fetch Today's Daily Analysis Readiness"), ["Validate Today's Daily Analysis Readiness"]);
assert.deepEqual(targets("Validate Today's Daily Analysis Readiness"), ["Today's Daily Analysis Ready?"]);
assert.deepEqual(targets("Today's Daily Analysis Ready?", 0), ["Fetch Stored Briefing for Teams"]);
assert.deepEqual(targets("Today's Daily Analysis Ready?", 1), ["Record Scheduled Readiness Skip"]);
assert.deepEqual(targets("Validate Stored Briefing Contract"), ["Attach Scheduled Readiness Context"]);
assert.deepEqual(targets("Attach Scheduled Readiness Context"), ["Build Sanitized Dedupe Envelope"]);
assert.deepEqual(targets("Build Sanitized Dedupe Envelope"), ["Stabilize Scheduled Daily Correlation"]);
assert.deepEqual(targets("Stabilize Scheduled Daily Correlation"), ["New Sanitized Delivery Needed?"]);

const readinessNode = nodes.get("Fetch Today's Daily Analysis Readiness");
assert.equal(readinessNode.type, "n8n-nodes-base.httpRequest");
assert.equal(readinessNode.parameters.authentication, "genericCredentialType");
assert.equal(readinessNode.parameters.genericAuthType, "httpHeaderAuth");
assert.match(readinessNode.parameters.url, /operations\/teams-daily-readiness/);
assert.equal(readinessNode.retryOnFail, false);
assert.equal(readinessNode.onError, "continueRegularOutput");
assert.equal(readinessNode.credentials, undefined);

const send = nodes.get("Send Sanitized Teams Briefing");
assert.equal(send.type, "n8n-nodes-base.microsoftTeams");
assert.equal(send.typeVersion, 2);
assert.equal(send.parameters.resource, "channelMessage");
assert.equal(send.parameters.operation, "create");
assert.equal(send.parameters.contentType, "html");
assert.equal(send.parameters.teamId.value, "={{ $json.runtime.target.teamId }}");
assert.equal(send.parameters.channelId.value, "={{ $json.runtime.target.channelId }}");
assert.equal(send.parameters.options.includeLinkToWorkflow, false);
assert.equal(send.retryOnFail, false);
assert.equal(send.onError, "continueRegularOutput");
assert.equal(send.credentials, undefined);

const reservationNode = nodes.get("Reserve Persistent Teams Correlation");
assert.equal(reservationNode.type, "n8n-nodes-base.httpRequest");
assert.equal(reservationNode.parameters.authentication, "genericCredentialType");
assert.equal(reservationNode.parameters.genericAuthType, "httpHeaderAuth");
assert.match(reservationNode.parameters.url, /delivery\.reservation\.endpointPath/);
assert.match(reservationNode.parameters.body, /delivery\.reservation\.body/);
assert.equal(reservationNode.retryOnFail, false);
assert.equal(reservationNode.onError, "continueRegularOutput");
assert.equal(reservationNode.credentials, undefined);
assert.deepEqual(targets("New Sanitized Delivery Needed?", 0), ["Reserve Persistent Teams Correlation"]);
assert.deepEqual(targets("Persistent Teams Reservation Acquired?", 0), ["Send Sanitized Teams Briefing"]);
assert.deepEqual(targets("Persistent Teams Reservation Acquired?", 1), ["Record Preview or Duplicate Suppressed"]);

const scheduledMarker = one(
  "Mark Scheduled Live Mode",
  {},
  { execution: { id: "scheduled-execution" } },
);
assert.equal(scheduledMarker.requestedMode, "scheduled-live");
assert.equal(scheduledMarker.triggerSource, "schedule");
const scheduledMode = one(
  "Mark Config-Gated Delivery Mode",
  scheduledMarker,
  { execution: { id: "scheduled-execution", mode: "manual" } },
);
assert.equal(scheduledMode.requestedMode, "scheduled-live");
assert.equal(scheduledMode.triggerSource, "schedule");
assert.equal(scheduledMode.markerValid, true);
const manualLiveMarker = one(
  "Mark Manual Live Test Mode",
  {},
  { execution: { id: "manual-live-execution" } },
);
assert.equal(manualLiveMarker.requestedMode, "manual-live-test");
assert.equal(manualLiveMarker.triggerSource, "manual-live-test");
const manualLiveMode = one(
  "Mark Config-Gated Delivery Mode",
  manualLiveMarker,
  { execution: { id: "manual-live-execution", mode: "trigger" } },
);
assert.equal(manualLiveMode.requestedMode, "manual-live-test");
assert.equal(manualLiveMode.triggerSource, "manual-live-test");
assert.equal(manualLiveMode.markerValid, true);
const invalidMode = one("Mark Config-Gated Delivery Mode", {
  requestedMode: "manual-live-test",
  triggerSource: "schedule",
});
assert.equal(invalidMode.markerValid, false);

const configDefaults = {
  push_enabled: "false",
  approval_state: "PENDING",
  team_id: "UNSET",
  channel_id: "UNSET",
  live_test_enabled: "false",
  emergency_disabled: "false",
};
const configItems = (overrides = {}, extra = []) => [
  ...Object.entries({ ...configDefaults, ...overrides }).map(([key, value], index) => ({
    json: { id: index + 1, key, value, createdAt: "2026-08-18T00:00:00.000Z" },
  })),
  ...extra,
];
const configNodeContext = (mode) => ({ "Mark Config-Gated Delivery Mode": { json: mode } });
const validateConfig = (mode, overrides = {}, extra = [], items = null) => one(
  "Validate Teams Delivery Config",
  {},
  { node: configNodeContext(mode), items: items ?? configItems(overrides, extra) },
);

const defaultConfig = validateConfig(scheduledMode);
const defaultRuntime = defaultConfig.runtime;
assert.equal(defaultRuntime.executionMode, "scheduled-fail-closed");
assert.equal(defaultRuntime.deliveryGateOpen, false);
assert.equal(defaultRuntime.configValid, true);
assert.equal(defaultRuntime.teamsPushEnabled, false);
assert.equal(defaultRuntime.teamsApprovalState, "PENDING");
assert.equal(defaultRuntime.targetConfigured, false);
assert.equal(defaultConfig.source_calls.configTable, 1);

const approvedConfig = {
  push_enabled: "true",
  approval_state: "APPROVED",
  team_id: "11111111-1111-4111-8111-111111111111",
  channel_id: "19:approved-channel@thread.tacv2",
  live_test_enabled: "true",
  emergency_disabled: "false",
};
const approvedResult = validateConfig(scheduledMode, approvedConfig);
const approvedRuntime = approvedResult.runtime;
assert.equal(approvedRuntime.executionMode, "scheduled-live");
assert.equal(approvedRuntime.deliveryGateOpen, true);
assert.equal(approvedRuntime.target.teamId, approvedConfig.team_id);
assert.equal(approvedRuntime.target.channelId, approvedConfig.channel_id);
const manualLiveRuntime = validateConfig(manualLiveMode, approvedConfig).runtime;
assert.equal(manualLiveRuntime.executionMode, "manual-live-test");
assert.equal(manualLiveRuntime.triggerSource, "manual-live-test");
assert.equal(manualLiveRuntime.deliveryGateOpen, true);
const disabledManualLive = validateConfig(manualLiveMode, { ...approvedConfig, live_test_enabled: "false" });
assert.equal(disabledManualLive.runtime.executionMode, "manual-live-test-fail-closed");
assert.equal(disabledManualLive.runtime.deliveryGateOpen, false);
const invalidModeConfig = validateConfig(invalidMode, approvedConfig);
assert.equal(invalidModeConfig.runtime.configErrorCode, "CONFIG_INVALID_MODE_MARKER");
assert.equal(invalidModeConfig.runtime.deliveryGateOpen, false);

for (const override of [
  { push_enabled: "false" },
  { approval_state: "PENDING" },
  { team_id: "not-a-team" },
  { channel_id: "unsafe/channel" },
  { emergency_disabled: "true" },
]) {
  const runtime = validateConfig(scheduledMode, { ...approvedConfig, ...override }).runtime;
  assert.equal(runtime.deliveryGateOpen, false);
}

const missingConfig = configItems(approvedConfig).filter((item) => item.json.key !== "team_id");
assert.equal(validateConfig(scheduledMode, {}, [], missingConfig).runtime.configErrorCode, "CONFIG_MISSING_KEY");
assert.equal(validateConfig(scheduledMode, {}, [], []).runtime.configErrorCode, "CONFIG_MISSING_KEY");
const duplicateConfig = configItems(approvedConfig, [{ json: { key: "team_id", value: approvedConfig.team_id } }]);
assert.equal(validateConfig(scheduledMode, {}, [], duplicateConfig).runtime.configErrorCode, "CONFIG_DUPLICATE_KEY");
const unknownConfig = configItems(approvedConfig, [{ json: { key: "unexpected", value: "true" } }]);
assert.equal(validateConfig(scheduledMode, {}, [], unknownConfig).runtime.configErrorCode, "CONFIG_UNKNOWN_KEY");
assert.equal(validateConfig(scheduledMode, { ...approvedConfig, push_enabled: "yes" }).runtime.configErrorCode, "CONFIG_INVALID_BOOLEAN");
assert.equal(validateConfig(scheduledMode, { ...approvedConfig, push_enabled: "TRUE" }).runtime.configErrorCode, "CONFIG_INVALID_BOOLEAN");
assert.equal(validateConfig(scheduledMode, { ...approvedConfig, approval_state: "approved" }).runtime.configErrorCode, "CONFIG_INVALID_APPROVAL");
const wrongCaseKey = configItems(approvedConfig).map((item) => (
  item.json.key === "push_enabled" ? { json: { ...item.json, key: "PUSH_ENABLED" } } : item
));
assert.equal(validateConfig(scheduledMode, {}, [], wrongCaseKey).runtime.configErrorCode, "CONFIG_UNKNOWN_KEY");

const todayKst = new Date(Date.now() + 9 * 3600000).toISOString().slice(0, 10);
const readyResponse = {
  schema_version: "1.0",
  status: "READY",
  ready: true,
  reason_code: "DAILY_ANALYSIS_COMPLETE",
  kst_date: todayKst,
  checked_at: new Date().toISOString(),
  retry_after_seconds: null,
  ingestion: {
    job_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    status: "COMPLETED",
    completed: true,
    created: 2,
    updated: 0,
    matched: 2,
  },
  analysis: {
    parent_job_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    parent_status: "COMPLETED",
    terminal: true,
    planned: 2,
    attempted: 2,
    remaining: 0,
    in_flight: 0,
    completed: 2,
    partial: 0,
    failed: 0,
    queue_pending: 0,
  },
  source_calls: { pps: 0, openai: 0, teams: 0 },
};
const validatedReady = one(
  "Validate Today's Daily Analysis Readiness",
  readyResponse,
  { node: { "Validate Teams Delivery Config": { json: approvedResult } } },
);
assert.equal(validatedReady.readiness.ready, true);
assert.equal(validatedReady.readiness.contractValid, true);
assert.deepEqual(validatedReady.source_calls, { configTable: 1, backend: 1, teams: 0 });

const readyEmptyResponse = structuredClone(readyResponse);
readyEmptyResponse.reason_code = "READY_EMPTY";
readyEmptyResponse.ingestion.created = 0;
readyEmptyResponse.analysis = {
  parent_job_id: null,
  parent_status: null,
  terminal: false,
  planned: 0,
  attempted: 0,
  remaining: 0,
  in_flight: 0,
  completed: 0,
  partial: 0,
  failed: 0,
  queue_pending: 0,
};
assert.equal(one(
  "Validate Today's Daily Analysis Readiness",
  readyEmptyResponse,
  { node: { "Validate Teams Delivery Config": { json: approvedResult } } },
).readiness.ready, true);

const runningResponse = structuredClone(readyResponse);
runningResponse.status = "RUNNING";
runningResponse.ready = false;
runningResponse.reason_code = "DAILY_ANALYSIS_RUNNING";
runningResponse.retry_after_seconds = 900;
runningResponse.analysis.parent_status = "RUNNING";
runningResponse.analysis.terminal = false;
runningResponse.analysis.planned = 2;
runningResponse.analysis.attempted = 1;
runningResponse.analysis.remaining = 1;
runningResponse.analysis.completed = 1;
const validatedRunning = one(
  "Validate Today's Daily Analysis Readiness",
  runningResponse,
  { node: { "Validate Teams Delivery Config": { json: approvedResult } } },
);
assert.equal(validatedRunning.readiness.ready, false);
const runningSkip = one("Record Scheduled Readiness Skip", validatedRunning);
assert.equal(runningSkip.delivery.status, "SKIPPED_ANALYSIS_RUNNING");
const runningTerminal = one("Validate Scheduled Readiness Skip", runningSkip);
assert.equal(runningTerminal.status, "DELIVERY_DEFERRED");
assert.deepEqual(runningTerminal.sourceCalls, { configTable: 1, backend: 1, teams: 0 });
assert.equal(runningTerminal.delivery.actualTeamsRequestAttempted, false);
assert.equal(runningTerminal.schedule.firstAttempt, "09:00");
assert.equal(runningTerminal.schedule.lastAttempt, "10:45");
assert.equal(runningTerminal.schedule.maxAttempts, 8);

const malformedReadiness = one(
  "Validate Today's Daily Analysis Readiness",
  { error: { message: "raw-backend-error-must-not-leave" } },
  { node: { "Validate Teams Delivery Config": { json: approvedResult } } },
);
assert.equal(malformedReadiness.readiness.status, "FAILED");
assert.equal(malformedReadiness.readiness.reasonCode, "READINESS_BACKEND_CONTRACT_INVALID");
assert(!JSON.stringify(malformedReadiness).includes("raw-backend-error-must-not-leave"));

const fixture = one("Build Offline Delivery Fixture");
fixture.briefing.notices[0].raw_provider_payload = "PRIVATE-CONTENT-MARKER";
fixture.briefing.notices[0].private_blob = "PRIVATE-BLOB-MARKER";
fixture.briefing.notices[0].contact_email = "PII-EMAIL-MARKER";
fixture.briefing.notices[0].top_departments = [
  { department_name: "AI미래교육본부", business_score: 82, provider_blob: "TOP-PROVIDER-MARKER" },
  { department_name: "역량솔루션본부", business_score: 71.5 },
  { department_name: "일자리창출본부", department_score: 64 },
  { department_name: "표시되면 안 되는 네 번째 부서", business_score: 99 },
];
fixture.briefing.notices[0].department_review_candidates = [
  { department_name: "인재개발센터", business_score: 31, reviewer_email: "REVIEW-PII-MARKER" },
];
fixture.briefing.notices[0].region_routing = [
  { department_name: "중부본부", routing_score: 55, employee_phone: "REGION-PII-MARKER" },
];
const preview = one(
  "Build Sanitized Dedupe Envelope",
  fixture,
  { execution: { id: "preview-execution" } },
);
assert.equal(preview.delivery.status, "PREVIEW_ONLY");
assert.equal(preview.delivery.shouldReserve, false);
assert.equal(preview.delivery.reservation, null);
assert.match(preview.delivery.correlationId, /^teams-daily:\d{4}-\d{2}-\d{2}:[0-9a-f]{16}$/);
assert.equal(preview.adaptiveCard.type, "AdaptiveCard");
const previewSerialised = JSON.stringify({ card: preview.adaptiveCard, html: preview.delivery.htmlMessage });
assert(!previewSerialised.includes("PRIVATE-CONTENT-MARKER"));
assert(!previewSerialised.includes("PRIVATE-BLOB-MARKER"));
assert(!previewSerialised.includes("PII-EMAIL-MARKER"));
assert(!previewSerialised.includes("TOP-PROVIDER-MARKER"));
assert(!previewSerialised.includes("REVIEW-PII-MARKER"));
assert(!previewSerialised.includes("REGION-PII-MARKER"));
assert(Buffer.byteLength(previewSerialised, "utf8") <= 24 * 1024);

const requiredHtmlLabels = ["공고명", "발주처", "마감일", "추정금액", "참가자격", "리스크", "추천 부서"];
for (const label of requiredHtmlLabels) {
  assert(preview.delivery.htmlMessage.includes(`<strong>${label}:</strong>`), `missing bold HTML label ${label}`);
}
assert(preview.delivery.htmlMessage.includes("<strong>1. 공고</strong>"));
assert(preview.delivery.htmlMessage.includes("<strong>2. 공고</strong>"));
assert.equal((preview.delivery.htmlMessage.match(/<hr>/g) ?? []).length, 1);
assert(preview.delivery.htmlMessage.includes("AI미래교육본부 · 82점"));
assert(preview.delivery.htmlMessage.includes("역량솔루션본부 · 71.5점"));
assert(preview.delivery.htmlMessage.includes("일자리창출본부 · 64점"));
assert(!preview.delivery.htmlMessage.includes("표시되면 안 되는 네 번째 부서"));
assert(preview.delivery.htmlMessage.includes("인재개발센터 · 추가검토 · 31점"));
assert(preview.delivery.htmlMessage.includes("중부본부 · 지역 라우팅 · 55점"));
assert(preview.delivery.htmlMessage.includes("<strong>추천 부서:</strong> 기준 충족 없음"));

const previewContainers = preview.adaptiveCard.body.filter((item) => item.type === "Container");
assert.equal(previewContainers.length, 2);
assert.equal(previewContainers[0].separator, false);
assert.equal(previewContainers[1].separator, true);
assert.equal(previewContainers[0].items[0].text, "1. 공고");
const firstFacts = new Map(previewContainers[0].items[1].facts.map((fact) => [fact.title, fact.value]));
for (const label of requiredHtmlLabels) assert(firstFacts.has(label), `missing Adaptive FactSet label ${label}`);
assert(firstFacts.get("추천 부서").includes("AI미래교육본부 · 82점"));
assert(firstFacts.get("추천 부서").includes("역량솔루션본부 · 71.5점"));
assert(firstFacts.get("추천 부서").includes("일자리창출본부 · 64점"));
assert(!firstFacts.get("추천 부서").includes("표시되면 안 되는 네 번째 부서"));
assert(firstFacts.get("추가 검토").includes("추가검토"));
assert(firstFacts.get("지역 라우팅").includes("지역 라우팅"));
const secondFacts = new Map(previewContainers[1].items[1].facts.map((fact) => [fact.title, fact.value]));
assert.equal(secondFacts.get("추천 부서"), "기준 충족 없음");

const maximumShape = structuredClone(fixture);
maximumShape.briefing.notices = Array.from({ length: 6 }, (_, noticeIndex) => ({
  notice_key: `MAXIMUM-${noticeIndex}`,
  title: `긴 공고명 ${"가".repeat(240)}`,
  agency: `긴 발주처 ${"나".repeat(160)}`,
  deadline: "2026-08-31T23:59:59+09:00",
  estimated_amount: 999999999999,
  fit: { eligibility: "REVIEW", risk_band: "CONDITIONAL_GO", risk_score: 49.95 },
  top_departments: Array.from({ length: 6 }, (_, index) => ({ department_name: `추천${index}-${"다".repeat(90)}`, business_score: 99 - index })),
  department_review_candidates: Array.from({ length: 6 }, (_, index) => ({ department_name: `검토${index}-${"라".repeat(90)}`, business_score: 40 - index })),
  region_routing: Array.from({ length: 6 }, (_, index) => ({ department_name: `지역${index}-${"마".repeat(90)}`, routing_score: 70 - index })),
}));
maximumShape.briefing.totals = { observed: 6, included: 6 };
assert.throws(
  () => one("Build Sanitized Dedupe Envelope", maximumShape, { execution: { id: "maximum-preview" } }),
  /sanitized Teams payload exceeds the 24KB delivery budget/,
);

const maliciousFixture = structuredClone(fixture);
maliciousFixture.briefing.notices[0].title = '<img src=x onerror="steal()"> & 공고';
const escaped = one(
  "Build Sanitized Dedupe Envelope",
  maliciousFixture,
  { execution: { id: "escaped-preview" } },
);
assert(escaped.delivery.htmlMessage.includes("&lt;img"));
assert(escaped.delivery.htmlMessage.includes("&quot;steal()&quot;"));
assert(!escaped.delivery.htmlMessage.includes("<img"));

const storedResponse = {
  generated_at: new Date().toISOString(),
  window: { days: 7, from: "2026-08-11T00:00:00Z", to: "2026-08-18T00:00:00Z" },
  totals: { observed: 1, included: 1 },
  notices: fixture.briefing.notices.slice(0, 1),
};
const storedLiveInput = one("Validate Stored Briefing Contract", storedResponse, {
  node: { "Validate Teams Delivery Config": { json: approvedResult } },
});
const liveInput = one("Attach Scheduled Readiness Context", storedLiveInput, {
  node: { "Validate Today's Daily Analysis Readiness": { json: validatedReady } },
});
assert.equal(liveInput.readiness.ready, true);
assert.deepEqual(liveInput.source_calls, { configTable: 1, backend: 2, teams: 0 });
const firstEnvelope = one(
  "Build Sanitized Dedupe Envelope",
  liveInput,
  { execution: { id: "execution-first" } },
);
const first = one("Stabilize Scheduled Daily Correlation", firstEnvelope);
assert.equal(first.delivery.status, "READY_TO_RESERVE");
assert.equal(first.delivery.shouldReserve, true);
const scheduledFingerprintSource = `${first.runtime.target.teamId}|${first.runtime.target.channelId}|${first.delivery.htmlMessage}`;
assert.equal(first.delivery.fingerprint, workflowFingerprint(scheduledFingerprintSource));
assert.match(first.delivery.correlationId, /^teams-daily:\d{4}-\d{2}-\d{2}:[0-9a-f]{16}$/);
assert.equal(first.delivery.correlationGeneration, undefined);
assert.equal(first.delivery.reservation.ownerToken, "w12:execution-first");
assert.match(first.delivery.reservation.endpointPath, /notifications\/teams\/mock$/);
assert.equal(first.delivery.reservation.body.card.paiLoopDeliveryReservation.ownerToken, "w12:execution-first");
assert.equal(first.delivery.reservation.body.card.paiLoopDeliveryReservation.correlationId, first.delivery.correlationId);
assert.equal(first.delivery.reservation.body.correlation_id, first.delivery.correlationId);

const changedTargetLiveInput = structuredClone(liveInput);
changedTargetLiveInput.runtime.target = {
  teamId: "22222222-2222-4222-8222-222222222222",
  channelId: "19:changed-target@thread.tacv2",
};
const changedTargetEnvelope = one(
  "Build Sanitized Dedupe Envelope",
  changedTargetLiveInput,
  { execution: { id: "execution-changed-target" } },
);
const changedTargetAttempt = one(
  "Stabilize Scheduled Daily Correlation",
  changedTargetEnvelope,
);
assert.notEqual(changedTargetAttempt.delivery.correlationId, first.delivery.correlationId);
assert.equal(
  changedTargetAttempt.delivery.reservation.body.correlation_id,
  changedTargetAttempt.delivery.correlationId,
);
assert.equal(
  changedTargetAttempt.delivery.reservation.body.card.paiLoopDeliveryReservation.correlationId,
  changedTargetAttempt.delivery.correlationId,
);
const changedTargetReservation = one(
  "Validate Persistent Teams Reservation",
  reservationResponse(changedTargetAttempt),
  { node: { "Stabilize Scheduled Daily Correlation": { json: changedTargetAttempt } } },
);
assert.equal(changedTargetReservation.delivery.status, "RESERVATION_ACQUIRED");

const malformedTargetEnvelope = structuredClone(firstEnvelope);
malformedTargetEnvelope.runtime.target.channelId = "<invalid-channel>";
assert.throws(
  () => one("Stabilize Scheduled Daily Correlation", malformedTargetEnvelope),
  /scheduled correlation requires validated Teams target/,
);

const manualStoredInput = one("Validate Stored Briefing Contract", storedResponse, {
  node: { "Validate Teams Delivery Config": { json: { ...approvedResult, runtime: manualLiveRuntime } } },
});
const manualRetestInput = one("Attach Scheduled Readiness Context", manualStoredInput);
assert.deepEqual(manualRetestInput.source_calls, { configTable: 1, backend: 1, teams: 0 });
const manualRetestFirstEnvelope = one(
  "Build Sanitized Dedupe Envelope",
  manualRetestInput,
  { execution: { id: "manual-retest-first" } },
);
const manualRetestSecondEnvelope = one(
  "Build Sanitized Dedupe Envelope",
  manualRetestInput,
  { execution: { id: "manual-retest-second" } },
);
const manualRetestFirst = one("Stabilize Scheduled Daily Correlation", manualRetestFirstEnvelope);
const manualRetestSecond = one("Stabilize Scheduled Daily Correlation", manualRetestSecondEnvelope);
const manualGeneration = "oauth-scope-retest-v1";
const manualFingerprintSource = `${manualRetestFirst.runtime.target.teamId}|${manualRetestFirst.runtime.target.channelId}|${manualRetestFirst.delivery.htmlMessage}|manual-live-test-generation:${manualGeneration}`;
assert.equal(manualRetestFirst.delivery.correlationGeneration, manualGeneration);
assert.equal(manualRetestFirst.delivery.fingerprint, workflowFingerprint(manualFingerprintSource));
assert.match(manualRetestFirst.delivery.correlationId, new RegExp(`^teams-daily:\\d{4}-\\d{2}-\\d{2}:[0-9a-f]{16}:${manualGeneration}$`));
assert.equal(manualRetestSecond.delivery.correlationId, manualRetestFirst.delivery.correlationId);
assert.notEqual(manualRetestFirst.delivery.correlationId, first.delivery.correlationId);
const manualRetestDuplicate = one(
  "Validate Persistent Teams Reservation",
  reservationResponse(manualRetestSecond, manualRetestFirst),
  { node: { "Stabilize Scheduled Daily Correlation": { json: manualRetestSecond } } },
);
assert.equal(manualRetestDuplicate.delivery.status, "DUPLICATE_PERSISTENT_SUPPRESSED");
assert.equal(manualRetestDuplicate.delivery.reservationAcquired, false);

const acquired = one(
  "Validate Persistent Teams Reservation",
  reservationResponse(first),
  { node: { "Stabilize Scheduled Daily Correlation": { json: first } } },
);
assert.equal(acquired.delivery.status, "RESERVATION_ACQUIRED");
assert.equal(acquired.delivery.reservationAcquired, true);
assert.equal(acquired.delivery.reservation.recordId, "persistent-reservation-record");
assert.equal(acquired.source_calls.backend, 3);

const duplicateEnvelope = one(
  "Build Sanitized Dedupe Envelope",
  liveInput,
  { execution: { id: "execution-second" } },
);
const duplicateAttempt = one("Stabilize Scheduled Daily Correlation", duplicateEnvelope);
assert.equal(duplicateAttempt.delivery.correlationId, first.delivery.correlationId);
assert.notEqual(duplicateAttempt.delivery.reservation.ownerToken, first.delivery.reservation.ownerToken);
const duplicateReservation = one(
  "Validate Persistent Teams Reservation",
  reservationResponse(duplicateAttempt, first),
  { node: { "Stabilize Scheduled Daily Correlation": { json: duplicateAttempt } } },
);
assert.equal(duplicateReservation.delivery.status, "DUPLICATE_PERSISTENT_SUPPRESSED");
assert.equal(duplicateReservation.delivery.reservationAcquired, false);

const changedStoredResponse = structuredClone(storedResponse);
changedStoredResponse.notices[0].title = `${changedStoredResponse.notices[0].title} (09:15 저장 갱신)`;
const changedStoredInput = one("Validate Stored Briefing Contract", changedStoredResponse, {
  node: { "Validate Teams Delivery Config": { json: approvedResult } },
});
const changedLiveInput = one("Attach Scheduled Readiness Context", changedStoredInput, {
  node: { "Validate Today's Daily Analysis Readiness": { json: validatedReady } },
});
const changedEnvelope = one(
  "Build Sanitized Dedupe Envelope",
  changedLiveInput,
  { execution: { id: "execution-retry-changed-briefing" } },
);
const changedAttempt = one("Stabilize Scheduled Daily Correlation", changedEnvelope);
assert.notEqual(changedAttempt.delivery.fingerprint, first.delivery.fingerprint);
assert.equal(changedAttempt.delivery.correlationId, first.delivery.correlationId);
const changedReservation = one(
  "Validate Persistent Teams Reservation",
  reservationResponse(changedAttempt, first),
  { node: { "Stabilize Scheduled Daily Correlation": { json: changedAttempt } } },
);
assert.equal(changedReservation.delivery.status, "DUPLICATE_PERSISTENT_SUPPRESSED");
assert.equal(changedReservation.delivery.actualTeamsRequestAttempted, false);

const duplicateTerminal = one(
  "Validate Teams Delivery Outcome",
  one("Record Preview or Duplicate Suppressed", duplicateReservation),
);
assert.equal(duplicateTerminal.status, "DELIVERY_SKIPPED");
assert.equal(duplicateTerminal.delivery.status, "DUPLICATE_PERSISTENT_SUPPRESSED");
assert.equal(duplicateTerminal.delivery.actualTeamsRequestAttempted, false);
assert.deepEqual(duplicateTerminal.sourceCalls, { configTable: 1, backend: 3, teams: 0 });

const reservationFailure = one(
  "Validate Persistent Teams Reservation",
  { error: { message: "raw-reservation-error-must-not-leave" } },
  { node: { "Stabilize Scheduled Daily Correlation": { json: first } } },
);
assert.equal(reservationFailure.delivery.status, "RESERVATION_FAILED_NON_BLOCKING");
assert.equal(reservationFailure.delivery.reservationAcquired, false);
assert(!JSON.stringify(reservationFailure).includes("raw-reservation-error-must-not-leave"));
const reservationFailureTerminal = one(
  "Validate Teams Delivery Outcome",
  one("Record Preview or Duplicate Suppressed", reservationFailure),
);
assert.equal(reservationFailureTerminal.status, "DELIVERY_RESERVATION_FAILED_NON_BLOCKING");
assert.equal(reservationFailureTerminal.delivery.actualTeamsRequestAttempted, false);

const successful = one(
  "Normalize Non-Blocking Teams Result",
  { id: "teams-message-123" },
  { node: { "Validate Persistent Teams Reservation": { json: acquired } } },
);
assert.equal(successful.delivery.status, "SENT");
assert.equal(successful.delivery.reservationAcquired, true);
assert.equal(successful.delivery.messageId, "teams-message-123");
assert.equal(successful.delivery.actualTeamsRequestSent, true);
assert.equal(successful.delivery.htmlMessage, undefined);
const successfulTerminal = one("Validate Teams Delivery Outcome", successful);
assert.equal(successfulTerminal.status, "DELIVERY_SENT");
assert.deepEqual(successfulTerminal.sourceCalls, { configTable: 1, backend: 3, teams: 1 });
const scheduledFinal = one("Normalize Teams Delivery Schedule Contract", successfulTerminal, {
  node: { "Validate Today's Daily Analysis Readiness": { json: validatedReady } },
});
assert.equal(scheduledFinal.schemaVersion, "1.3");
assert.equal(scheduledFinal.schedule.cron, "0,15,30,45 9-10 * * *");
assert.equal(scheduledFinal.schedule.maxAttempts, 8);

const failed = one(
  "Normalize Non-Blocking Teams Result",
  { error: { message: "raw-internal-failure-detail" } },
  { node: { "Validate Persistent Teams Reservation": { json: acquired } } },
);
assert.equal(failed.delivery.status, "FAILED_NON_BLOCKING");
assert.equal(failed.delivery.reservationAcquired, true);
assert.equal(failed.delivery.errorCode, "TEAMS_NODE_ERROR");
assert(!JSON.stringify(failed.delivery).includes("raw-internal-failure-detail"));
assert.equal(one("Validate Teams Delivery Outcome", failed).status, "DELIVERY_FAILED_NON_BLOCKING");

const previewTerminal = one(
  "Validate Teams Delivery Outcome",
  one("Record Preview or Duplicate Suppressed", preview),
);
assert.equal(previewTerminal.status, "PREVIEW_READY");
assert.deepEqual(previewTerminal.sourceCalls, { configTable: 0, backend: 0, teams: 0 });

const failClosed = one(
  "Record Fail-Closed Teams Skip",
  defaultConfig,
);
assert.equal(failClosed.delivery.status, "SKIPPED_PUSH_DISABLED");
assert.equal(one("Validate Teams Delivery Outcome", failClosed).status, "DELIVERY_SKIPPED");

const manualLiveDisabledSkip = one("Record Fail-Closed Teams Skip", disabledManualLive);
assert.equal(manualLiveDisabledSkip.delivery.status, "SKIPPED_LIVE_TEST_DISABLED");
assert.equal(one("Validate Teams Delivery Outcome", manualLiveDisabledSkip).status, "DELIVERY_SKIPPED");

const invalidConfigResult = validateConfig(scheduledMode, {}, [], unknownConfig);
const invalidConfigSkip = one("Record Fail-Closed Teams Skip", invalidConfigResult);
assert.equal(invalidConfigSkip.delivery.status, "SKIPPED_CONFIG_INVALID");
assert.equal(invalidConfigSkip.delivery.errorCode, "CONFIG_UNKNOWN_KEY");
const invalidConfigTerminal = one("Validate Teams Delivery Outcome", invalidConfigSkip);
assert.equal(invalidConfigTerminal.delivery.actualTeamsRequestAttempted, false);
assert.equal(invalidConfigTerminal.sourceCalls.configTable, 1);

const workflowText = JSON.stringify(workflow);
assert(!workflowText.includes("graph.microsoft.com"));
assert(!workflowText.includes("access_token"));
assert(!workflowText.includes("client_secret"));
assert(!workflowText.includes("$vars"));
assert(!workflowText.includes("PAI_LOOP_TEAMS_PUSH_ENABLED"));
assert(!workflowText.includes("PAI_LOOP_TEAMS_TEAM_ID"));
assert(!workflowText.includes("/datatables/"));
assert(workflowText.includes("pai_loop_teams_delivery_config"));
assert(!workflowText.includes("$execution.mode"));
assert(!workflowText.includes("schedule-manual-test"));
assert(workflowText.includes("CONFIG_INVALID_MODE_MARKER"));
assert(workflowText.includes("CONFIG_DUPLICATE_KEY"));
assert(workflowText.includes("CONFIG_UNKNOWN_KEY"));
assert(!workflowText.includes("teamsDeliveryLedger"));
assert(workflowText.includes("paiLoopDeliveryReservation"));
assert(workflowText.includes("DUPLICATE_PERSISTENT_SUPPRESSED"));

const manifestEntry = manifest.workflows["pai-loop-12-teams-daily-delivery"];
assert.deepEqual(
  {
    publish: manifestEntry.publish,
    contractVersion: manifestEntry.contractVersion,
    promotionState: manifestEntry.promotionState,
  },
  { publish: true, contractVersion: "teams-delivery-1.3", promotionState: "verified-live-e2e" },
);

console.log("Teams 09:00 readiness gate, bounded 15-minute retries, stable daily correlation, manual-live separation, sanitizer, persistent reservation, and native sink contracts passed.");
