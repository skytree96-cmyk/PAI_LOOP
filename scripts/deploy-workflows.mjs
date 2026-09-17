import fs from "node:fs/promises";
import path from "node:path";
import { assertNativeGatewayWorkflow, isNativeAnthropicNode, nativeNodeName,
  assertPendingNativeSelection, assertPendingNativeInactive, nativeCanaryWorkflowKeys } from "./native-gateway-contract.mjs";
import { assertAwardAutomationWorkflow, awardWorkflowKey, awardHttpNodeNames } from "./award-automation-workflow-contract.mjs";

const validateOnly = process.argv.includes("--validate-only");
const onlyArgument = process.argv.find((argument) => argument.startsWith("--only="));
const onlyKey = onlyArgument?.slice("--only=".length) || undefined;
const rootDirectory = process.cwd();
const claudeGatewayKey = "pai-loop-13-claude-extraction-gateway";
const claudeGatewayWorkflowName = "PAI_LOOP 13 - Claude Extraction Gateway";
const claudeWebhookNodeName = "Claude Extraction Webhook";
const claudeOldModelNodeName = "Claude Sonnet 4.6";
const claudeNewModelNodeName = "Claude Sonnet 5";
const claudeModelNodeType = "@n8n/n8n-nodes-langchain.lmChatAnthropic";
const claudeOldModelId = "claude-sonnet-4-6";
const claudeNewModelId = "claude-sonnet-5";
const claudeMigrationUpstreamKeys = [
  "pai-loop-10-daily-opportunity-briefing",
  "pai-loop-11-analysis-backfill",
  claudeGatewayKey,
];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function loadWorkflowDefinitions() {
  const manifestPath = path.join(rootDirectory, "manifest.json");
  const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
  assert(manifest.manifestVersion === 2, "manifest.json must use manifestVersion 2");
  assert(manifest.workflows && typeof manifest.workflows === "object", "manifest.json must contain workflows");

  const manifestFiles = new Set();
  const workflowNames = new Set();
  const definitions = [];

  for (const [key, config] of Object.entries(manifest.workflows)) {
    assert(config && typeof config === "object", `${key}: manifest entry must be an object`);
    assert(typeof config.file === "string" && config.file.endsWith(".json"), `${key}: file must be a JSON path`);
    assert(config.publish === true || config.publish === false, `${key}: publish must be an explicit boolean`);

    const normalizedFile = config.file.replaceAll("\\", "/");
    assert(normalizedFile.startsWith("workflows/") && !normalizedFile.includes(".."), `${key}: file must stay under workflows/`);
    assert(!manifestFiles.has(normalizedFile), `${key}: duplicate workflow file ${normalizedFile}`);
    manifestFiles.add(normalizedFile);

    const absoluteFile = path.join(rootDirectory, ...normalizedFile.split("/"));
    const workflow = JSON.parse(await fs.readFile(absoluteFile, "utf8"));
    validateWorkflow(key, workflow);
    assert(!workflowNames.has(workflow.name), `${key}: duplicate n8n workflow name ${workflow.name}`);
    workflowNames.add(workflow.name);
    definitions.push({ key, config, workflow });
  }

  const filesOnDisk = (await fs.readdir(path.join(rootDirectory, "workflows")))
    .filter((file) => file.endsWith(".json"))
    .map((file) => `workflows/${file}`);
  const untracked = filesOnDisk.filter((file) => !manifestFiles.has(file));
  assert(untracked.length === 0, `Workflow JSON missing from manifest.json: ${untracked.join(", ")}`);

  return definitions;
}

function reachableNodeNames(workflow, startName, branchChoices = {}) {
  const visited = new Set();
  const pending = [startName];
  while (pending.length) {
    const name = pending.pop();
    if (visited.has(name)) continue;
    visited.add(name);
    const groups = workflow.connections?.[name] ?? {};
    for (const outputs of Object.values(groups)) {
      const selectedLane = branchChoices[name];
      const lanes = selectedLane == null ? outputs : [outputs[selectedLane] ?? []];
      for (const lane of lanes) {
        for (const connection of lane) pending.push(connection.node);
      }
    }
  }
  return visited;
}

function validateRepositorySafetyContracts(definitions) {
  for (const { key, workflow } of definitions) {
    for (const node of workflow.nodes) {
      assert(!node.credentials, `${key}/${node.name}: credential IDs must not be committed`);
    }
  }

  const daily = definitions.find(({ key }) => key === "pai-loop-10-daily-opportunity-briefing");
  assert(daily, "daily operator workflow is required");
  assert(daily.config.operatorEntryPoint === true, "daily workflow must be the explicit operator entry point");
  const continuation = definitions.find(({ key }) => key === "pai-loop-11-analysis-backfill");
  assert(continuation, "analysis continuation workflow is required");
  const teamsDelivery = definitions.find(({ key }) => key === "pai-loop-12-teams-daily-delivery");
  assert(teamsDelivery, "independent Teams delivery workflow is required");
  const claudeGateway = definitions.find(({ key }) => key === "pai-loop-13-claude-extraction-gateway");
  assert(claudeGateway, "isolated Claude extraction gateway workflow is required");
  const awardAutomation = definitions.find(({ key }) => key === awardWorkflowKey);
  assert(awardAutomation, "isolated award automation workflow is required");
  assertAwardAutomationWorkflow(awardAutomation.workflow, awardAutomation.config);
  for (const definition of definitions) {
    if (
      definition.key === daily.key
      || definition.key === continuation.key
      || definition.key === teamsDelivery.key
      || definition.key === claudeGateway.key
      || definition.key === awardAutomation.key
    ) continue;
    assert(
      definition.config.publish === false,
      `${definition.key}: only explicitly validated workflows 10 through 14 may be published`,
    );
  }
  if (continuation.config.publish === true) {
    assert(
      continuation.config.promotionState === "verified-live-e2e",
      "workflow 11 may publish only after verified-live-e2e promotion",
    );
  } else {
    assert(
      continuation.config.promotionState === "awaiting-live-e2e",
      "inactive workflow 11 must remain awaiting-live-e2e",
    );
  }
  const validTeamsPromotion = (
    teamsDelivery.config.publish === false
    && teamsDelivery.config.promotionState === "awaiting-live-e2e"
  ) || (
    teamsDelivery.config.publish === true
    && teamsDelivery.config.promotionState === "verified-live-e2e"
  );
  assert(
    validTeamsPromotion,
    "workflow 12 may publish only after credential binding, target selection, and verified-live-e2e",
  );
  const validClaudePromotion = (
    claudeGateway.config.publish === false
    && claudeGateway.config.promotionState === "awaiting-live-e2e"
  ) || (
    claudeGateway.config.publish === true
    && claudeGateway.config.promotionState === "verified-live-e2e"
  ) || (
    claudeGateway.config.publish === true
    && claudeGateway.config.contractVersion === "claude-extraction-gateway-2.0-native-json"
    && claudeGateway.config.promotionState === "awaiting-native-live-e2e"
    && claudeGateway.config.nativeCanaryState === "awaiting-root-synthetic-schema-probe"
  );
  assert(
    validClaudePromotion,
    "workflow 13 must retain a verified release or explicitly identify the pending native canary",
  );

  assertNativeGatewayWorkflow(claudeGateway.workflow);
  assert(daily.workflow.settings?.timezone === "Asia/Seoul", "daily workflow timezone must be Asia/Seoul");
  const schedules = daily.workflow.nodes.filter(
    (node) => node.type === "n8n-nodes-base.scheduleTrigger",
  );
  assert(schedules.length === 1, "daily workflow must have exactly one schedule trigger");
  assert(
    schedules[0].parameters?.rule?.interval?.[0]?.expression === "30 7 * * *",
    "daily workflow schedule must be 07:30 every day",
  );

  const manualName = "Run Complete Offline Dry-Run";
  const manualReachable = reachableNodeNames(daily.workflow, manualName, {
    // Manual fixture sets teamsMockLogEnabled=false.  Pin the known false lane
    // so this graph audit proves that path has no HTTP boundary.
    "Backend Teams Mock Log Gate Open?": 1,
  });
  const nodeByName = new Map(daily.workflow.nodes.map((node) => [node.name, node]));
  const manualHttpNodes = [...manualReachable]
    .map((name) => nodeByName.get(name))
    .filter((node) => node?.type === "n8n-nodes-base.httpRequest");
  assert(
    manualHttpNodes.length === 0,
    `daily manual dry-run must not reach HTTP nodes: ${manualHttpNodes.map((node) => node.name).join(", ")}`,
  );

  const httpNodes = daily.workflow.nodes.filter(
    (node) => node.type === "n8n-nodes-base.httpRequest",
  );
  assert(httpNodes.length === 10, "daily live branch must expose exactly ten protected backend HTTP boundaries");
  for (const node of httpNodes) {
    const url = String(node.parameters?.url ?? "");
    assert(
      url.includes("runtime.apiBaseUrl") || url.includes("Scheduled Runtime Gates"),
      `daily/${node.name}: HTTP URL must resolve from the approved backend runtime`,
    );
    assert(
      !/https?:\/\/(?:[^'\"}\s]*\.)?(?:microsoft|office|powerautomate|openai|data\.go\.kr)/i.test(url),
      `daily/${node.name}: direct provider or Teams URL is forbidden`,
    );
    assert(
      node.parameters?.authentication === "genericCredentialType"
        && node.parameters?.genericAuthType === "httpHeaderAuth",
      `daily/${node.name}: protected backend calls must require Generic Header Auth`,
    );
    const headers = node.parameters?.headerParameters?.parameters ?? [];
    assert(
      !headers.some((header) => String(header.name).toLowerCase() === "x-pai-loop-api-key"),
      `daily/${node.name}: API key must come from the n8n credential, not workflow JSON`,
    );
  }

  const serialised = JSON.stringify(daily.workflow);
  assert(
    serialised.includes("PAI_LOOP_EMERGENCY_DISABLE"),
    "daily workflow must expose the one fail-closed emergency disable",
  );
  assert(
    serialised.includes("executionMode: dailyLiveEnabled ? 'scheduled-live' : 'scheduled-emergency-disabled'"),
    "daily workflow must default scheduled execution to live when emergency disable is absent",
  );
  assert(
    serialised.includes("https://pai-yd7xtctmra-an.a.run.app"),
    "daily workflow must contain the public Render origin fallback",
  );
  assert(serialised.includes("retentionDays: 7"), "daily workflow must declare seven-day retention");
  assert(
    serialised.includes("/api/v1/notices/analysis/batch"),
    "daily workflow must route PPS notice keys through the backend batch analysis endpoint",
  );
  assert(
    serialised.includes("/api/v1/outcome-feedback/pps/refresh")
      && serialised.includes("max_notices: 10")
      && serialised.includes("max_pages_per_notice: 1")
      && serialised.includes("PPS_OUTCOME_FEEDBACK_UNAVAILABLE"),
    "daily workflow must run bounded, fail-soft PPS outcome feedback after analysis",
  );
  assert(
    serialised.includes("maxAnalysisBatchNotices: 1")
      && serialised.includes("maxAttachmentsPerNotice: 10")
      && serialised.includes("maxBacklogRetryNotices: 50")
      && serialised.includes("max_total: 3012")
      && serialised.includes("enrichment.attachments_discovered * 2"),
    "daily analysis must use one-notice chunks, all ten provider slots, and two OpenAI calls per discovered attachment",
  );
  assert(
    serialised.includes("created_notice_keys")
      && serialised.includes("updated_notice_keys")
      && serialised.includes("refresh_notice_keys")
      && serialised.includes("retry_notice_keys")
      && serialised.includes("retry_epoch")
      && serialised.includes("request_token")
      && serialised.includes("source_ingestion_job_id")
      && serialised.includes("source_material_notice_keys")
      && serialised.includes("$execution.id")
      && serialised.includes("execution_limit: 5")
      && serialised.includes("max_continuations: 768")
      && serialised.includes("segment_id")
      && serialised.includes("chunk_indices")
      && serialised.includes("refusing silent truncation")
      && serialised.includes("splitInBatches"),
    "daily analysis must durably lease exact created+updated keys without silent truncation",
  );
  assert(
    serialised.includes("useProfileKeywords: true")
      && serialised.includes("profileDepartmentIds: []")
      && serialised.includes("collectionWindowDays: 8")
      && serialised.includes("'교육','컨설팅','연수','포럼','위탁 운영'")
      && serialised.includes("ppsPageSize: 999")
      && serialised.includes("ppsMaxPages: 3")
      && serialised.includes("PPS collection window must cover exactly eight calendar days")
      && serialised.includes("page-limited PPS ingestion must be PARTIAL"),
    "daily ingestion must query the eight-day recovery window, paginate the organization profile, and fail closed on page caps",
  );
  assert(
    serialised.includes("use_profile_keywords:")
      && serialised.includes("profile_department_ids:")
      && serialised.includes("department_coverage_count")
      && serialised.includes("enrich_missing:")
      && serialised.includes("max_attachments_per_notice:"),
    "daily HTTP payloads must carry the v1.3 ingestion and enrichment contract",
  );
  const analysisNode = daily.workflow.nodes.find(
    (node) => node.name === "Analyze Evaluate and Snapshot PPS Notices",
  );
  const ppsNode = daily.workflow.nodes.find(
    (node) => node.name === "Refresh PPS Notices Behind Gate",
  );
  const awardNode = daily.workflow.nodes.find(
    (node) => node.name === "Refresh Bounded Three-Year Award History",
  );
  const outcomeFeedbackNode = daily.workflow.nodes.find(
    (node) => node.name === "Refresh PPS Outcome Feedback Fail-Soft",
  );
  assert(
    ppsNode?.parameters?.options?.timeout === 600000,
    "daily organization-profile PPS ingestion must have a bounded ten-minute n8n timeout",
  );
  assert(
    analysisNode?.parameters?.options?.timeout === 600000,
    "daily top-three enrichment must have a bounded ten-minute n8n timeout",
  );
  assert(
    analysisNode?.retryOnFail === true
      && analysisNode?.maxTries === 2
      && analysisNode?.waitBetweenTries >= 1500
      && analysisNode?.waitBetweenTries <= 3000,
    "daily analysis chunk request must safely retry its exact leased chunk once",
  );
  assert(
    awardNode?.parameters?.options?.timeout === 600000
      && serialised.includes("maxAwardRefreshNotices: 1")
      && serialised.includes("Math.min(3, Math.max(1"),
    "daily award refresh must default to one, hard-cap at three, and use a ten-minute request window",
  );
  const dailyRuntime = daily.workflow.nodes.find(node => node.name === "Scheduled Runtime Gates");
  const scheduledAwardGates = new Function("$env", dailyRuntime.parameters.jsCode)({})[0].json.runtime;
  assert(scheduledAwardGates.awardRefreshEnabled === false && scheduledAwardGates.awardRefreshWriteEnabled === false,
    "W10 scheduled award hook must stay disabled while W14 owns award automation");
  assert(
    outcomeFeedbackNode?.parameters?.options?.timeout === 120000
      && outcomeFeedbackNode?.retryOnFail === false
      && outcomeFeedbackNode?.alwaysOutputData === true
      && outcomeFeedbackNode?.onError === "continueRegularOutput",
    "daily outcome feedback must use a bounded, non-blocking backend request",
  );
  const targets = (source, lane = 0) =>
    (daily.workflow.connections?.[source]?.main?.[lane] ?? []).map((connection) => connection.node);
  assert(
    JSON.stringify(targets("Validate PPS Ingestion Contract"))
      === JSON.stringify(["Preview or Apply Seven-Day Log Retention"]),
    "validated PPS notices must enter retention and award preparation before analysis",
  );
  assert(
    JSON.stringify(targets("Validate Batch Analysis Contract"))
      === JSON.stringify(["Process Daily Chunks Serially"])
      && JSON.stringify(targets("Verify Batch Analysis Aggregate Invariants"))
        === JSON.stringify(["Finalize Daily Analysis Segment"])
      && JSON.stringify(targets("Validate Daily Continuation State"))
        === JSON.stringify(["Refresh PPS Outcome Feedback Fail-Soft"])
      && JSON.stringify(targets("Record Batch Analysis Skipped"))
        === JSON.stringify(["Refresh PPS Outcome Feedback Fail-Soft"])
      && JSON.stringify(targets("Refresh PPS Outcome Feedback Fail-Soft"))
        === JSON.stringify(["Normalize PPS Outcome Feedback"])
      && JSON.stringify(targets("Normalize PPS Outcome Feedback"))
        === JSON.stringify(["Fetch Ranked Seven-Day Briefing"]),
    "analysis completion or explicit skip must run fail-soft outcome feedback before the final briefing",
  );
  assert(
    JSON.stringify(targets("Validate Seven-Day Retention Contract"))
      === JSON.stringify(["Fetch Award Candidates from Seven-Day Briefing"])
      && JSON.stringify(targets("Validate Award Refresh Batch"))
        === JSON.stringify(["Build Bounded Batch Analysis Plan"])
      && JSON.stringify(targets("Record Award Refresh Skipped"))
        === JSON.stringify(["Build Bounded Batch Analysis Plan"]),
    "award refresh or explicit skip must precede analysis snapshot generation",
  );
  assert(serialised.includes("actualTeamsRequestSent: false"), "daily workflow must keep Teams delivery mocked");
  assert(
    daily.config.contractVersion === "daily-briefing-1.6",
    "daily workflow manifest contractVersion must be daily-briefing-1.6",
  );

  const teamsSerialised = JSON.stringify(teamsDelivery.workflow);
  assert(
    teamsDelivery.config.contractVersion === "teams-delivery-1.3",
    "workflow 12 must use the teams-delivery-1.3 contract",
  );
  assert(
    teamsDelivery.workflow.settings?.timezone === "Asia/Seoul",
    "Teams delivery workflow timezone must be Asia/Seoul",
  );
  const teamsSchedules = teamsDelivery.workflow.nodes.filter(
    (node) => node.type === "n8n-nodes-base.scheduleTrigger",
  );
  assert(
    teamsSchedules.length === 1
      && JSON.stringify((teamsSchedules[0].parameters?.rule?.interval ?? []).map((item) => item.expression)) === JSON.stringify(["30,45 8 * * *", "*/15 9 * * *", "0,15 10 * * *"]),
    "Teams delivery must first attempt at 08:30 and retry every 15 minutes through 10:15 Asia/Seoul",
  );
  const teamsByName = new Map(
    teamsDelivery.workflow.nodes.map((node) => [node.name, node]),
  );
  const teamsConfig = teamsByName.get("Read Teams Delivery Config");
  assert(
    teamsConfig?.type === "n8n-nodes-base.dataTable"
      && teamsConfig.typeVersion === 1.1
      && teamsConfig.parameters?.resource === "row"
      && teamsConfig.parameters?.operation === "get"
      && teamsConfig.parameters?.returnAll === true
      && teamsConfig.parameters?.dataTableId?.mode === "name"
      && teamsConfig.parameters?.dataTableId?.value === "pai_loop_teams_delivery_config"
      && teamsConfig.alwaysOutputData === true
      && teamsConfig.retryOnFail === false
      && teamsConfig.onError === "continueRegularOutput"
      && !teamsConfig.credentials,
    "workflow 12 config must use the literal named Data Table without tenant IDs or credentials",
  );
  const teamsBackend = teamsByName.get("Fetch Stored Briefing for Teams");
  assert(
    teamsBackend?.type === "n8n-nodes-base.httpRequest"
      && teamsBackend.parameters?.authentication === "genericCredentialType"
      && teamsBackend.parameters?.genericAuthType === "httpHeaderAuth"
      && String(teamsBackend.parameters?.url ?? "").includes("/api/v1/operations/daily-briefing"),
    "workflow 12 may read only the protected stored daily briefing backend boundary",
  );
  const teamsReadinessBackend = teamsByName.get("Fetch Today's Daily Analysis Readiness");
  assert(
    teamsReadinessBackend?.type === "n8n-nodes-base.httpRequest"
      && teamsReadinessBackend.parameters?.authentication === "genericCredentialType"
      && teamsReadinessBackend.parameters?.genericAuthType === "httpHeaderAuth"
      && String(teamsReadinessBackend.parameters?.url ?? "").includes("/api/v1/operations/teams-daily-readiness")
      && teamsReadinessBackend.retryOnFail === false
      && teamsReadinessBackend.onError === "continueRegularOutput",
    "workflow 12 scheduled path must use the protected read-only daily readiness boundary and fail closed",
  );
  const teamsReservation = teamsByName.get("Reserve Persistent Teams Correlation");
  assert(
    teamsReservation?.type === "n8n-nodes-base.httpRequest"
      && teamsReservation.parameters?.authentication === "genericCredentialType"
      && teamsReservation.parameters?.genericAuthType === "httpHeaderAuth"
      && String(teamsReservation.parameters?.url ?? "").includes("delivery.reservation.endpointPath")
      && String(teamsReservation.parameters?.body ?? "").includes("delivery.reservation.body")
      && teamsReservation.retryOnFail === false
      && teamsReservation.onError === "continueRegularOutput",
    "workflow 12 must reserve the persistent backend correlation and fail closed on reservation errors",
  );
  const teamsSend = teamsByName.get("Send Sanitized Teams Briefing");
  assert(
    teamsSend?.type === "n8n-nodes-base.microsoftTeams"
      && teamsSend.typeVersion === 2
      && teamsSend.parameters?.resource === "channelMessage"
      && teamsSend.parameters?.operation === "create"
      && teamsSend.parameters?.contentType === "html"
      && teamsSend.parameters?.options?.includeLinkToWorkflow === false,
    "workflow 12 actual boundary must use the native Microsoft Teams v2 channel-message node",
  );
  assert(
    teamsSend.parameters?.teamId?.mode === "id"
      && teamsSend.parameters?.teamId?.value === "={{ $json.runtime.target.teamId }}"
      && teamsSend.parameters?.channelId?.mode === "id"
      && teamsSend.parameters?.channelId?.value === "={{ $json.runtime.target.channelId }}",
    "workflow 12 sink target must come from the validated runtime dataflow",
  );
  assert(
    teamsSend.retryOnFail === false && teamsSend.onError === "continueRegularOutput",
    "Teams delivery failure must not retry or restart collection and analysis",
  );
  const teamsTargets = (source, lane = 0) => (
    teamsDelivery.workflow.connections?.[source]?.main?.[lane] ?? []
  ).map((connection) => connection.node);
  const teamsLiveTestTrigger = teamsByName.get("Run Live Teams Test");
  const teamsManualMode = teamsByName.get("Mark Manual Live Test Mode");
  const teamsScheduledMode = teamsByName.get("Mark Scheduled Live Mode");
  assert(
    teamsLiveTestTrigger?.type === "n8n-nodes-base.manualTrigger"
      && teamsManualMode?.type === "n8n-nodes-base.code"
      && teamsScheduledMode?.type === "n8n-nodes-base.code"
      && String(teamsManualMode.parameters?.jsCode ?? "").includes("requestedMode: 'manual-live-test'")
      && String(teamsManualMode.parameters?.jsCode ?? "").includes("triggerSource: 'manual-live-test'")
      && String(teamsScheduledMode.parameters?.jsCode ?? "").includes("requestedMode: 'scheduled-live'")
      && String(teamsScheduledMode.parameters?.jsCode ?? "").includes("triggerSource: 'schedule'")
      && !teamsSerialised.includes("$execution.mode")
      && !teamsSerialised.includes("schedule-manual-test")
      && JSON.stringify(teamsTargets("Run Live Teams Test")) === JSON.stringify(["Mark Manual Live Test Mode"])
      && JSON.stringify(teamsTargets("Every Day 08:30 KST")) === JSON.stringify(["Mark Scheduled Live Mode"])
      && JSON.stringify(teamsTargets("Mark Manual Live Test Mode")) === JSON.stringify(["Mark Config-Gated Delivery Mode"])
      && JSON.stringify(teamsTargets("Mark Scheduled Live Mode")) === JSON.stringify(["Mark Config-Gated Delivery Mode"]),
    "workflow 12 must derive manual-test and scheduled modes from separate constant trigger branches",
  );
  assert(
    JSON.stringify(teamsTargets("New Sanitized Delivery Needed?", 0))
      === JSON.stringify(["Reserve Persistent Teams Correlation"])
      && JSON.stringify(teamsTargets("Persistent Teams Reservation Acquired?", 0))
        === JSON.stringify(["Send Sanitized Teams Briefing"])
      && JSON.stringify(teamsTargets("Persistent Teams Reservation Acquired?", 1))
        === JSON.stringify(["Record Preview or Duplicate Suppressed"]),
    "the Teams sink must be reachable only after a successful persistent reservation",
  );
  assert(
    JSON.stringify(teamsTargets("Approved Teams Push Gate Open?", 0))
      === JSON.stringify(["Scheduled Teams Attempt?"])
      && JSON.stringify(teamsTargets("Scheduled Teams Attempt?", 0))
        === JSON.stringify(["Fetch Today's Daily Analysis Readiness"])
      && JSON.stringify(teamsTargets("Scheduled Teams Attempt?", 1))
        === JSON.stringify(["Fetch Stored Briefing for Teams"])
      && JSON.stringify(teamsTargets("Today's Daily Analysis Ready?", 0))
        === JSON.stringify(["Fetch Stored Briefing for Teams"])
      && JSON.stringify(teamsTargets("Today's Daily Analysis Ready?", 1))
        === JSON.stringify(["Record Scheduled Readiness Skip"]),
    "scheduled delivery must gate briefing/reservation/Teams behind readiness while manual-live stays separate",
  );
  assert(
    teamsDelivery.workflow.nodes.filter((node) => (
      node.type === "n8n-nodes-base.dataTable"
      || node.type === "n8n-nodes-base.httpRequest"
      || node.type === "n8n-nodes-base.microsoftTeams"
    )).length === 5,
    "workflow 12 must expose one named config table read, readiness read, briefing read, persistent reservation, and one Teams boundary",
  );
  assert(
    teamsDelivery.workflow.nodes.filter((node) => (
      node.type === "n8n-nodes-base.httpRequest"
      || node.type === "n8n-nodes-base.microsoftTeams"
    )).length === 4,
    "workflow 12 must expose readiness and briefing reads, one persistent reservation, and one Teams boundary",
  );
  assert(
    teamsSerialised.includes("pai_loop_teams_delivery_config")
      && teamsSerialised.includes("push_enabled")
      && teamsSerialised.includes("approval_state")
      && teamsSerialised.includes("live_test_enabled")
      && teamsSerialised.includes("emergency_disabled")
      && teamsSerialised.includes("CONFIG_DUPLICATE_KEY")
      && teamsSerialised.includes("CONFIG_UNKNOWN_KEY")
      && teamsSerialised.includes("/notifications/teams/mock")
      && teamsSerialised.includes("paiLoopDeliveryReservation")
      && teamsSerialised.includes("DUPLICATE_PERSISTENT_SUPPRESSED")
      && teamsSerialised.includes("FAILED_NON_BLOCKING")
      && teamsSerialised.includes("replace(/&/g, '&amp;')")
      && teamsSerialised.includes("bounded(notice?.top_departments, 3)")
      && teamsSerialised.includes("bounded(notice?.department_review_candidates, 3)")
      && teamsSerialised.includes("bounded(notice?.region_routing, 2)")
      && teamsSerialised.includes("business_score','department_score','score")
      && teamsSerialised.includes("routing_score','department_score','score")
      && teamsSerialised.includes("<strong>공고명:</strong>")
      && teamsSerialised.includes("<strong>발주처:</strong>")
      && teamsSerialised.includes("<strong>마감일:</strong>")
      && teamsSerialised.includes("<strong>추정금액:</strong>")
      && teamsSerialised.includes("<strong>참가자격:</strong>")
      && teamsSerialised.includes("<strong>리스크:</strong>")
      && teamsSerialised.includes("<strong>추천 부서:</strong>")
      && teamsSerialised.includes("<hr>")
      && teamsSerialised.includes("추가검토")
      && teamsSerialised.includes("지역 라우팅")
      && teamsSerialised.includes("기준 충족 없음")
      && teamsSerialised.includes("24 * 1024"),
    "workflow 12 must retain strict approval gates, labeled notice sections, bounded recommendation routing, sanitizer, persistent reservation, and non-blocking failure contracts",
  );
  assert(
    !teamsSerialised.includes("graph.microsoft.com")
      && !teamsSerialised.includes("access_token")
      && !teamsSerialised.includes("client_secret")
      && !teamsSerialised.includes("$vars")
      && !teamsSerialised.includes("cachedResultUrl")
      && !teamsSerialised.includes("/datatables/"),
    "workflow 12 export must not embed Graph URLs, Variables, table IDs, tenant URLs, or credential material",
  );
  const teamsManualReachable = reachableNodeNames(
    teamsDelivery.workflow,
    "Run Offline Teams Preview",
    {
      "New Sanitized Delivery Needed?": 1,
    },
  );
  const teamsManualExternal = [...teamsManualReachable]
    .map((name) => teamsByName.get(name))
    .filter((node) => (
      node?.type === "n8n-nodes-base.dataTable"
      || node?.type === "n8n-nodes-base.httpRequest"
      || node?.type === "n8n-nodes-base.microsoftTeams"
    ));
  assert(
    teamsManualExternal.length === 0,
    `workflow 12 manual preview must make zero config/backend/Teams calls: ${teamsManualExternal.map((node) => node.name).join(", ")}`,
  );

  const continuationSerialised = JSON.stringify(continuation.workflow);
  const continuationHttp = continuation.workflow.nodes.filter(
    (node) => node.type === "n8n-nodes-base.httpRequest",
  );
  assert(
    continuationHttp.length === 3
      && continuationHttp.every((node) => (
        node.parameters?.authentication === "genericCredentialType"
        && node.parameters?.genericAuthType === "httpHeaderAuth"
      )),
    "workflow 11 must expose exactly three protected backend HTTP boundaries",
  );
  assert(
    continuation.config.contractVersion === "analysis-backfill-1.2"
      && continuationSerialised.includes("executionLimit: 30")
      && continuationSerialised.includes("maxTotal: 3000")
      && continuationSerialised.includes("includeRetryable: true")
      && continuationSerialised.includes("maxContinuations: 768")
      && continuationSerialised.includes("queueName: 'ANY'")
      && continuationSerialised.includes("resumeOnly: true")
      && continuationSerialised.includes("response.openai_calls > enrichment.attachments_discovered * 2")
      && continuationSerialised.includes("segment_id")
      && continuationSerialised.includes("chunk_indices"),
    "workflow 11 must use retryable recovery, the per-attachment two-call cap, and resumable one-notice chunks",
  );
  const continuationChunkNode = continuation.workflow.nodes.find(
    (node) => node.name === "Analyze One Bounded Chunk",
  );
  assert(
    continuationChunkNode?.retryOnFail === true
      && continuationChunkNode?.maxTries === 2
      && continuationChunkNode?.waitBetweenTries >= 1500
      && continuationChunkNode?.waitBetweenTries <= 3000,
    "workflow 11 analysis chunk request must safely retry its exact leased chunk once",
  );
  const continuationSchedules = continuation.workflow.nodes.filter(
    (node) => node.type === "n8n-nodes-base.scheduleTrigger",
  );
  assert(
    continuationSchedules.length === 1
      && continuationSchedules[0].parameters?.rule?.interval?.[0]?.expression === "* * * * *",
    "workflow 11 continuation schedule must poll every minute",
  );

  const preservationProbe = preserveRemoteNodeCredentials(
    "credential-preservation-probe",
    {
      nodes: [
        { name: "same", type: "n8n-nodes-base.httpRequest" },
        { name: "same-teams", type: "n8n-nodes-base.microsoftTeams" },
        { name: "type-changed", type: "n8n-nodes-base.code" },
        { name: "new", type: "n8n-nodes-base.httpRequest" },
      ],
    },
    {
      nodes: [
        {
          name: "same",
          type: "n8n-nodes-base.httpRequest",
          credentials: { httpHeaderAuth: { id: "opaque-probe", name: "probe" } },
        },
        {
          name: "type-changed",
          type: "n8n-nodes-base.httpRequest",
          credentials: { httpHeaderAuth: { id: "must-not-copy", name: "probe" } },
        },
        {
          name: "same-teams",
          type: "n8n-nodes-base.microsoftTeams",
          credentials: {
            microsoftTeamsOAuth2Api: { id: "opaque-teams-probe", name: "Microsoft Teams account" },
          },
        },
      ],
    },
  );
  assert(
    preservationProbe.nodes[0].credentials?.httpHeaderAuth?.id === "opaque-probe",
    "exact node-name/type credential preservation failed",
  );
  assert(
    preservationProbe.nodes[1].credentials?.microsoftTeamsOAuth2Api?.id === "opaque-teams-probe",
    "exact native Teams node credential preservation failed",
  );
  assert(
    !preservationProbe.nodes[2].credentials && !preservationProbe.nodes[3].credentials,
    "credential preservation must reject type changes and new nodes",
  );
  const approvedMigrationProbe = preserveRemoteNodeCredentials(
    claudeGatewayKey,
    {
      name: claudeGatewayWorkflowName,
      nodes: [{
        name: claudeNewModelNodeName,
        type: claudeModelNodeType,
        parameters: { model: { value: claudeNewModelId } },
      }],
    },
    {
      name: claudeGatewayWorkflowName,
      nodes: [{
        name: claudeOldModelNodeName,
        type: claudeModelNodeType,
        parameters: { model: { value: claudeOldModelId } },
        credentials: { anthropicApi: { id: "opaque-anthropic-probe", name: "approved" } },
      }],
    },
  );
  assert(
    approvedMigrationProbe.nodes[0].credentials?.anthropicApi?.id === "opaque-anthropic-probe",
    "the exact W13 Sonnet 4.6 to Sonnet 5 credential migration failed",
  );
  const rejectedMigrationProbe = preserveRemoteNodeCredentials(
    "unapproved-workflow",
    {
      name: "Unapproved workflow",
      nodes: [{
        name: claudeNewModelNodeName,
        type: claudeModelNodeType,
        parameters: { model: { value: claudeNewModelId } },
      }],
    },
    {
      name: "Unapproved workflow",
      nodes: [{
        name: claudeOldModelNodeName,
        type: claudeModelNodeType,
        parameters: { model: { value: claudeOldModelId } },
        credentials: { anthropicApi: { id: "must-not-migrate", name: "unapproved" } },
      }],
    },
  );
  assert(
    !rejectedMigrationProbe.nodes[0].credentials,
    "Claude credential migration must remain restricted to the exact W13 contract",
  );
  const nativeTarget = { name: nativeNodeName, type: "n8n-nodes-base.httpRequest" };
  const nativeCredential = { anthropicApi: { id: "SYN-native-reference", name: "SYN approved" } };
  const nativeExact = preserveRemoteNodeCredentials(claudeGatewayKey,
    { nodes: [nativeTarget] }, { nodes: [{ ...nativeTarget, credentials: nativeCredential }] });
  assert(nativeExact.nodes[0].credentials === nativeCredential, "native binding must survive exact name/type preservation");
  for (const prior of [
    { name: nativeNodeName, type: claudeModelNodeType, credentials: nativeCredential },
    { name: claudeNewModelNodeName, type: claudeModelNodeType, credentials: nativeCredential },
    { name: "SYN lookalike native", type: nativeTarget.type, credentials: nativeCredential },
  ]) {
    const rejected = preserveRemoteNodeCredentials(claudeGatewayKey, { nodes: [nativeTarget] }, { nodes: [prior] });
    assert(!rejected.nodes[0].credentials, "native HTTP credential must never migrate by type, fuzzy name, or old model identity");
  }
}

function validateWorkflow(key, workflow) {
  assert(workflow && typeof workflow === "object", `${key}: workflow must be an object`);
  assert(typeof workflow.name === "string" && workflow.name.trim(), `${key}: workflow name is required`);
  assert(Array.isArray(workflow.nodes) && workflow.nodes.length > 0, `${key}: workflow nodes are required`);
  assert(workflow.connections && typeof workflow.connections === "object", `${key}: connections must be an object`);

  const nodeNames = new Set();
  const nodeIds = new Set();
  for (const node of workflow.nodes) {
    assert(typeof node.id === "string" && node.id, `${key}: every node needs an id`);
    assert(typeof node.name === "string" && node.name, `${key}: every node needs a name`);
    assert(typeof node.type === "string" && node.type, `${key}/${node.name}: node type is required`);
    assert(Array.isArray(node.position) && node.position.length === 2, `${key}/${node.name}: position must be [x, y]`);
    assert(!nodeNames.has(node.name), `${key}: duplicate node name ${node.name}`);
    assert(!nodeIds.has(node.id), `${key}: duplicate node id ${node.id}`);
    nodeNames.add(node.name);
    nodeIds.add(node.id);

    if (node.type === "n8n-nodes-base.code") {
      const code = node.parameters?.jsCode;
      assert(typeof code === "string" && code.trim(), `${key}/${node.name}: Code node must contain jsCode`);
      // Parses n8n's top-level return statements without executing the node.
      // eslint-disable-next-line no-new-func
      new Function(code);
    }
  }

  for (const [sourceName, groups] of Object.entries(workflow.connections)) {
    assert(nodeNames.has(sourceName), `${key}: connection source does not exist: ${sourceName}`);
    for (const outputs of Object.values(groups)) {
      assert(Array.isArray(outputs), `${key}/${sourceName}: connection output must be an array`);
      for (const output of outputs) {
        assert(Array.isArray(output), `${key}/${sourceName}: connection lane must be an array`);
        for (const connection of output) {
          assert(nodeNames.has(connection.node), `${key}: connection target does not exist: ${connection.node}`);
        }
      }
    }
  }
}

const definitions = await loadWorkflowDefinitions();
validateRepositorySafetyContracts(definitions);
console.log(`Validated ${definitions.length} workflow definition(s)`);
if (validateOnly) process.exit(0);

if (onlyKey) {
  assert(
    definitions.some(({ key }) => key === onlyKey),
    `--only references an unknown manifest workflow: ${onlyKey}`,
  );
}

const baseUrl = process.env.N8N_BASE_URL?.replace(/\/$/, "");
const apiKey = process.env.N8N_API_KEY;
assert(baseUrl && apiKey, "N8N_BASE_URL and N8N_API_KEY are required unless --validate-only is used");

const headers = {
  "X-N8N-API-KEY": apiKey,
  "Content-Type": "application/json",
  Accept: "application/json",
};

async function request(apiPath, options = {}, allowedStatuses = []) {
  const response = await fetch(`${baseUrl}/api/v1${apiPath}`, { headers, ...options });
  const text = await response.text();
  let body = {};
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { message: text.slice(0, 1000) };
    }
  }

  if (!response.ok && !allowedStatuses.includes(response.status)) {
    const detail = JSON.stringify(body).slice(0, 2000);
    throw new Error(`${options.method ?? "GET"} ${apiPath} failed (${response.status}): ${detail}`);
  }
  return { status: response.status, body };
}

async function listAllWorkflows() {
  const workflows = [];
  let cursor;
  for (let page = 0; page < 100; page += 1) {
    const query = new URLSearchParams({ limit: "100" });
    if (cursor) query.set("cursor", cursor);
    const { body } = await request(`/workflows?${query}`);
    assert(Array.isArray(body.data), "n8n list-workflows response did not contain data[]");
    workflows.push(...body.data);
    cursor = body.nextCursor;
    if (!cursor) return workflows;
  }
  throw new Error("Stopped after 100 n8n workflow pages; pagination appears cyclic");
}

function deploymentPayload(workflow) {
  return {
    name: workflow.name,
    nodes: workflow.nodes,
    connections: workflow.connections ?? {},
    settings: workflow.settings ?? {},
    ...(workflow.staticData ? { staticData: workflow.staticData } : {}),
  };
}

function exactNamedNode(workflow, nodeName) {
  const matches = (workflow?.nodes ?? []).filter((node) => node.name === nodeName);
  assert(matches.length <= 1, `${workflow?.name ?? "workflow"}: duplicate node name ${nodeName}`);
  return matches[0];
}

function validCredentialReference(credential) {
  return Boolean(
    credential
      && typeof credential === "object"
      && String(credential.id ?? "").trim()
      && String(credential.name ?? "").trim(),
  );
}

function approvedClaudeModelMigrationRequired(payload, remote) {
  const source = exactNamedNode(remote, claudeOldModelNodeName);
  const target = exactNamedNode(payload, claudeNewModelNodeName);
  const alreadyMigrated = exactNamedNode(remote, claudeNewModelNodeName);
  return Boolean(
    source
      && !alreadyMigrated
      && source.type === claudeModelNodeType
      && source.parameters?.model?.value === claudeOldModelId
      && target?.type === claudeModelNodeType
      && target.parameters?.model?.value === claudeNewModelId
      && !exactNamedNode(payload, claudeOldModelNodeName),
  );
}

function migrateApprovedClaudeModelCredential(key, payload, remote) {
  if (key !== claudeGatewayKey || !approvedClaudeModelMigrationRequired(payload, remote)) {
    return payload;
  }
  const source = exactNamedNode(remote, claudeOldModelNodeName);
  const credential = source?.credentials?.anthropicApi;
  if (!validCredentialReference(credential)) return payload;
  return {
    ...payload,
    nodes: payload.nodes.map((node) => (
      node.name === claudeNewModelNodeName
        ? { ...node, credentials: { anthropicApi: credential } }
        : node
    )),
  };
}

function preserveRemoteNodeCredentials(key, payload, remote) {
  const remoteByName = new Map((remote?.nodes ?? []).map((node) => [node.name, node]));
  const exactPreserved = {
    ...payload,
    nodes: payload.nodes.map((node) => {
      const prior = remoteByName.get(node.name);
      if (!prior || prior.type !== node.type || !prior.credentials) return node;
      // Credentials are environment-owned.  Preserve only an exact node-name
      // and node-type match; never print or write the IDs back to the repo.
      return { ...node, credentials: prior.credentials };
    }),
  };
  // Model upgrades normally must keep a stable node name.  This is the only
  // approved exception: the exact W13 Sonnet 4.6 node may transfer only its
  // Anthropic credential reference to the exact Sonnet 5 replacement.  No
  // type-, position-, or fuzzy-name matching is permitted.
  return migrateApprovedClaudeModelCredential(key, exactPreserved, remote);
}

function assertClaudeGatewayCredentialBindings(workflow, expectedWorkflow = undefined) {
  assert(workflow?.name === claudeGatewayWorkflowName, "workflow 13 remote name is invalid");
  const webhook = exactNamedNode(workflow, claudeWebhookNodeName);
  const model = exactNamedNode(workflow, nativeNodeName);
  assert(
    webhook?.type === "n8n-nodes-base.webhook"
      && webhook.parameters?.authentication === "headerAuth"
      && validCredentialReference(webhook.credentials?.httpHeaderAuth),
    "workflow 13 webhook must retain one Generic Header credential",
  );
  assert(
    isNativeAnthropicNode(model)
      && Object.keys(model.credentials ?? {}).join(",") === "anthropicApi"
      && validCredentialReference(model.credentials?.anthropicApi),
    "workflow 13 Sonnet 5 node must retain one Anthropic credential",
  );
  if (!expectedWorkflow) return;
  const expectedWebhook = exactNamedNode(expectedWorkflow, claudeWebhookNodeName);
  const expectedModel = exactNamedNode(expectedWorkflow, nativeNodeName);
  assert(
    webhook.credentials.httpHeaderAuth.id === expectedWebhook?.credentials?.httpHeaderAuth?.id,
    "workflow 13 webhook credential changed during PUT",
  );
  assert(
    model.credentials.anthropicApi.id === expectedModel?.credentials?.anthropicApi?.id,
    "workflow 13 Anthropic credential changed during PUT",
  );
}

function extractSingleBackendCredential(workflow) {
  const credentials = (workflow?.nodes ?? [])
    .filter((node) => node.type === "n8n-nodes-base.httpRequest")
    .map((node) => node.credentials?.httpHeaderAuth)
    .filter(Boolean);
  if (!credentials.length) return undefined;
  const ids = new Set(credentials.map((credential) => String(credential.id ?? "")));
  assert(ids.size === 1 && !ids.has(""), "backend HTTP nodes must share one Generic Header credential");
  return { httpHeaderAuth: credentials[0] };
}

function extractApprovedAwardBackendCredential(remote, approvedDailyWorkflow) {
  assert(remote?.name === approvedDailyWorkflow.name, "award credential source must be the exact daily workflow");
  const approvedNames = new Set(approvedDailyWorkflow.nodes.filter(node =>
    node.type === "n8n-nodes-base.httpRequest"
    && node.parameters?.authentication === "genericCredentialType"
    && node.parameters?.genericAuthType === "httpHeaderAuth").map(node => node.name));
  const references = [];
  for (const name of approvedNames) {
    const node = exactNamedNode(remote, name);
    if (!node || node.type !== "n8n-nodes-base.httpRequest"
      || node.parameters?.authentication !== "genericCredentialType"
      || node.parameters?.genericAuthType !== "httpHeaderAuth") continue;
    const credential = node.credentials?.httpHeaderAuth;
    if (validCredentialReference(credential)) references.push(credential);
  }
  assert(references.length > 0 && new Set(references.map(item => item.id)).size === 1,
    "award automation requires one existing header credential from exact approved daily HTTP nodes");
  return { httpHeaderAuth: references[0] };
}

function assertAwardCredentialBindings(workflow, expected) {
  assert(validCredentialReference(expected?.httpHeaderAuth), "award automation backend credential is unavailable");
  for (const name of awardHttpNodeNames) {
    const node = exactNamedNode(workflow, name);
    assert(node?.type === "n8n-nodes-base.httpRequest"
      && node.parameters?.authentication === "genericCredentialType"
      && node.parameters?.genericAuthType === "httpHeaderAuth"
      && Object.keys(node.credentials ?? {}).join(",") === "httpHeaderAuth"
      && validCredentialReference(node.credentials?.httpHeaderAuth)
      && node.credentials.httpHeaderAuth.id === expected.httpHeaderAuth.id,
    "award automation must retain the approved daily backend credential on both exact HTTP nodes");
  }
}

function inheritApprovedBackendCredential(payload, credential, approvedNodeNames) {
  if (!approvedNodeNames.size) return payload;
  assert(credential?.httpHeaderAuth?.id, "approved new backend HTTP nodes require an inherited credential");
  return {
    ...payload,
    nodes: payload.nodes.map((node) => (
      approvedNodeNames.has(node.name) && node.type === "n8n-nodes-base.httpRequest"
        && node.parameters?.authentication === "genericCredentialType"
        && node.parameters?.genericAuthType === "httpHeaderAuth"
        ? { ...node, credentials: node.credentials ?? credential }
        : node
    )),
  };
}

const approvedCredentialInheritance = new Map([
  ["pai-loop-10-daily-opportunity-briefing", new Set([
    "Reserve or Resume Daily Analysis Operation",
    "Finalize Daily Analysis Segment",
    "Refresh PPS Outcome Feedback Fail-Soft",
  ])],
  ["pai-loop-11-analysis-backfill", new Set([
    "Reserve or Resume Backfill Plan",
    "Analyze One Bounded Chunk",
    "Finalize Backfill Audit",
  ])],
  ["pai-loop-12-teams-daily-delivery", new Set([
    "Fetch Today's Daily Analysis Readiness",
    "Fetch Stored Briefing for Teams",
    "Reserve Persistent Teams Correlation",
  ])],
  [awardWorkflowKey, awardHttpNodeNames],
]);

const remoteWorkflows = await listAllWorkflows();
const remoteByName = new Map();
for (const workflow of remoteWorkflows) {
  const matches = remoteByName.get(workflow.name) ?? [];
  matches.push(workflow);
  remoteByName.set(workflow.name, matches);
}

const selectedDefinitions = definitions.filter(
  (definition) => !onlyKey || definition.key === onlyKey,
);
// Inspect the global gateway state even when only a producer was selected.
// W14 has an exact, separately validated graph with only two protected award
// routes. Its isolated deployment cannot change or invoke W10-W13, so an
// unrelated pending Claude migration must not block award maintenance.
const isolatedAwardDeployment = onlyKey === awardWorkflowKey;
const pendingNativeCanary = isolatedAwardDeployment ? false : assertPendingNativeSelection(
  definitions.find(({ key }) => key === claudeGatewayKey).config, onlyKey,
);
const unpublishedClaudeGateway = definitions.find(
  ({ key }) => key === claudeGatewayKey,
)?.config.publish === false;
const wouldPublishClaudeProducer = selectedDefinitions.some(({ key, config }) => (
  (
    key === "pai-loop-10-daily-opportunity-briefing"
    || key === "pai-loop-11-analysis-backfill"
  )
  && config.publish === true
));
assert(
  !(unpublishedClaudeGateway && wouldPublishClaudeProducer),
  "cannot deploy active W10/W11 while workflow 13 remains publish=false; stage W13 alone, verify live Sonnet 5 E2E, then promote W13 first",
);

async function loadRemoteDefinitionForPreflight(definition, required = false) {
  const { key, config, workflow } = definition;
  if (config.n8nWorkflowId) {
    const result = await request(
      `/workflows/${encodeURIComponent(config.n8nWorkflowId)}`,
      {},
      [404],
    );
    if (result.status !== 404) {
      assert(
        result.body.name === workflow.name,
        `${key}: manifest workflow ID belongs to an unexpected remote workflow`,
      );
      return result.body;
    }
  }
  const matches = remoteByName.get(workflow.name) ?? [];
  assert(matches.length <= 1, `${key}: preflight found duplicate exact-name remote workflows`);
  if (!matches.length) {
    assert(!required, `${key}: required remote workflow is missing during Claude migration`);
    return undefined;
  }
  return (await request(`/workflows/${encodeURIComponent(matches[0].id)}`)).body;
}

const claudeGatewayDefinition = definitions.find(({ key }) => key === claudeGatewayKey);
assert(claudeGatewayDefinition, "Claude gateway definition is missing");
const remoteClaudeGateway = isolatedAwardDeployment ? undefined : await loadRemoteDefinitionForPreflight(claudeGatewayDefinition);
if (pendingNativeCanary) {
  const remotes = new Map();
  for (const key of nativeCanaryWorkflowKeys) {
    const definition = definitions.find(item => item.key === key);
    remotes.set(key, key === claudeGatewayKey ? remoteClaudeGateway : await loadRemoteDefinitionForPreflight(definition, true));
  }
  // This check is independent of legacy node detection and precedes all writes.
  assertPendingNativeInactive(remotes);
}
if (remoteClaudeGateway && isNativeAnthropicNode(exactNamedNode(claudeGatewayDefinition.workflow, nativeNodeName))
    && remoteClaudeGateway.nodes?.some(node => ["Claude JSON Extraction", "Claude Sonnet 5", "Claude Sonnet 4.6"].includes(node.name))) {
  // A node-type migration must never silently move a provider credential or
  // publish producers while root is staging the native node in the n8n UI.
  assert(onlyKey === claudeGatewayKey, "native gateway migration requires --only=pai-loop-13-claude-extraction-gateway");
  for (const upstreamKey of claudeMigrationUpstreamKeys) {
    const definition = definitions.find(({ key }) => key === upstreamKey);
    const remote = upstreamKey === claudeGatewayKey ? remoteClaudeGateway : await loadRemoteDefinitionForPreflight(definition, true);
    assert(remote.active === false, `${upstreamKey} must be inactive before native gateway migration`);
  }
}
if (
  remoteClaudeGateway
  && approvedClaudeModelMigrationRequired(
    deploymentPayload(claudeGatewayDefinition.workflow),
    remoteClaudeGateway,
  )
) {
  // This model/credential migration is deliberately a two-stage outage-safe
  // operation.  A normal all-workflow deploy would reactivate W10/W11 before
  // W13 is rebound and verified, so reject it before making any PUT request.
  assert(
    onlyKey === claudeGatewayKey,
    `Sonnet 5 migration requires --only=${claudeGatewayKey}`,
  );
  for (const upstreamKey of claudeMigrationUpstreamKeys) {
    const definition = definitions.find(({ key }) => key === upstreamKey);
    assert(definition, `${upstreamKey}: migration preflight definition is missing`);
    const remote = upstreamKey === claudeGatewayKey
      ? remoteClaudeGateway
      : await loadRemoteDefinitionForPreflight(definition, true);
    assert(
      remote.active === false,
      `${upstreamKey} must be inactive before the Sonnet 5 credential migration`,
    );
  }
}

let sharedBackendCredential;
if (
  onlyKey
  && onlyKey !== "pai-loop-10-daily-opportunity-briefing"
  && (approvedCredentialInheritance.get(onlyKey)?.size ?? 0) > 0
) {
  const dailyDefinition = definitions.find(
    ({ key }) => key === "pai-loop-10-daily-opportunity-briefing",
  );
  assert(dailyDefinition, "daily workflow is required as the backend credential source");
  let dailyRemote;
  if (dailyDefinition.config.n8nWorkflowId) {
    const result = await request(
      `/workflows/${encodeURIComponent(dailyDefinition.config.n8nWorkflowId)}`,
      {},
      [404],
    );
    if (result.status !== 404) dailyRemote = result.body;
  }
  if (!dailyRemote) {
    const matches = remoteByName.get(dailyDefinition.workflow.name) ?? [];
    assert(
      matches.length === 1,
      "--only deployment requires exactly one remote daily workflow credential source",
    );
    dailyRemote = (await request(`/workflows/${encodeURIComponent(matches[0].id)}`)).body;
  }
  assert(dailyRemote.name === dailyDefinition.workflow.name, "backend credential source must be the exact remote daily workflow");
  sharedBackendCredential = isolatedAwardDeployment
    ? extractApprovedAwardBackendCredential(dailyRemote, dailyDefinition.workflow)
    : extractSingleBackendCredential(dailyRemote);
  assert(
    sharedBackendCredential,
    "--only deployment requires the approved backend credential on remote workflow 10",
  );
}

for (const { key, config, workflow } of selectedDefinitions) {
  let payload = deploymentPayload(workflow);
  let workflowId = config.n8nWorkflowId;
  let remote;

  if (workflowId) {
    const result = await request(`/workflows/${encodeURIComponent(workflowId)}`, {}, [404]);
    if (result.status === 404) {
      console.warn(`${key}: manifest ID ${workflowId} was not found; falling back to exact-name lookup`);
      workflowId = undefined;
    } else {
      remote = result.body;
      assert(
        remote.name === workflow.name,
        `${key}: manifest ID ${workflowId} belongs to \"${remote.name}\", expected \"${workflow.name}\"`,
      );
    }
  }

  if (!workflowId) {
    const matches = remoteByName.get(workflow.name) ?? [];
    assert(matches.length <= 1, `${key}: multiple remote workflows have the exact name \"${workflow.name}\"`);
    if (matches.length === 1) {
      remote = matches[0];
      workflowId = remote.id;
      console.log(`${key}: matched existing workflow by name (${workflowId})`);
    }
  }

  if (workflowId) {
    // Exact-name lookup may come from a compact list response. Fetch the full
    // workflow before PUT so credentials selected in the n8n UI survive.
    if (!remote?.nodes) {
      remote = (await request(`/workflows/${encodeURIComponent(workflowId)}`)).body;
    }
    if (key === "pai-loop-10-daily-opportunity-briefing") {
      sharedBackendCredential = extractSingleBackendCredential(remote);
    }
    // Archived workflows cannot be updated through the public API.  Treat an
    // archived, inactive legacy definition as intentionally retired instead
    // of failing an otherwise idempotent deployment after newer workflows
    // have already been updated.  A publish=true entry must never be skipped.
    if (remote.isArchived === true) {
      assert(config.publish === false, `${key}: archived workflow cannot be published`);
      console.log(`Skipped archived ${key} (${workflowId})`);
      continue;
    }
    payload = preserveRemoteNodeCredentials(key, payload, remote);
    payload = inheritApprovedBackendCredential(
      payload,
      sharedBackendCredential ?? extractSingleBackendCredential(remote),
      approvedCredentialInheritance.get(key) ?? new Set(),
    );
    if (key === claudeGatewayKey) {
      // Fail before mutating the remote workflow if either environment-owned
      // credential cannot be deterministically preserved.
      assertClaudeGatewayCredentialBindings(payload);
    }
    if (key === awardWorkflowKey) assertAwardCredentialBindings(payload, sharedBackendCredential);
    const updated = await request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    remote = updated.body;
    if (key === claudeGatewayKey) {
      // n8n PUT responses can be compact.  Re-read the stored definition and
      // prove both credential references survived before changing activation.
      remote = (await request(`/workflows/${encodeURIComponent(workflowId)}`)).body;
      assertClaudeGatewayCredentialBindings(remote, payload);
    }
    console.log(`Updated ${key} (${workflowId})`);
  } else {
    payload = inheritApprovedBackendCredential(
      payload,
      sharedBackendCredential,
      approvedCredentialInheritance.get(key) ?? new Set(),
    );
    if (key === awardWorkflowKey) assertAwardCredentialBindings(payload, sharedBackendCredential);
    const created = await request("/workflows", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    remote = created.body;
    workflowId = remote.id;
    remoteByName.set(workflow.name, [remote]);
    console.log(`Created ${key} (${workflowId}); future deploys will match it by exact name`);
  }

  if (key === awardWorkflowKey) {
    remote = (await request(`/workflows/${encodeURIComponent(workflowId)}`)).body;
    assertAwardCredentialBindings(remote, sharedBackendCredential);
    // Strip environment-owned references solely for readback contract checking.
    assertAwardAutomationWorkflow({ ...remote, nodes: remote.nodes.map(({ credentials, ...node }) => node) }, config);
    const savedVersionId = remote.versionId;
    assert(typeof savedVersionId === "string" && savedVersionId, "W14 saved version must be identifiable before publication");
    const publishedVersionId = remote.activeVersionId ?? remote.activeVersion?.versionId;
    if (remote.active !== true || publishedVersionId !== savedVersionId) {
      // Updating an active workflow saves a draft. Publish this exact version,
      // otherwise the schedule can continue running the previous node config.
      await request(`/workflows/${encodeURIComponent(workflowId)}/activate`, {
        method: "POST", body: JSON.stringify({ versionId: savedVersionId }),
      });
      console.log(`Activated ${key} (published saved version)`);
    }
    remote = (await request(`/workflows/${encodeURIComponent(workflowId)}`)).body;
    assert(remote.active === true && remote.versionId === savedVersionId
      && (remote.activeVersionId ?? remote.activeVersion?.versionId) === savedVersionId,
    "W14 must publish the exact saved version without concurrent draft changes");
    assertAwardCredentialBindings(remote, sharedBackendCredential);
    assertAwardAutomationWorkflow({ ...remote, nodes: remote.nodes.map(({ credentials, ...node }) => node) }, config);
    continue;
  }

  if (config.publish === true) {
    if (!remote.active) {
      await request(`/workflows/${encodeURIComponent(workflowId)}/activate`, { method: "POST" });
      console.log(`Activated ${key}`);
    }
  } else if (remote.active) {
    await request(`/workflows/${encodeURIComponent(workflowId)}/deactivate`, { method: "POST" });
    console.log(`Deactivated ${key} (safe manifest default)`);
  }
}
