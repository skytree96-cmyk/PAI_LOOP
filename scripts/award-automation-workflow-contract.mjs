import assert from "node:assert/strict";

export const awardWorkflowKey = "pai-loop-14-award-history-automation";
export const awardContractVersion = "award-refresh-automation-1.0";
export const awardSafetyContract = "award-only-one-notice-zero-ai-v1";
export const awardHttpNodeNames = new Set(["Enroll New and Stale Award Notices", "Refresh One Queued Award Notice"]);

export function awardRuntime(environment) {
  const read = (name) => { try { return String(environment[name] ?? '').trim(); } catch { return ''; } };
  const origin = read('PAI_LOOP_API_BASE_URL') || 'https://pai-loop-demo.onrender.com';
  if (!/^https?:\/\/[A-Za-z0-9.-]+(?::\d+)?\/?$/.test(origin)) throw new Error('Award backend origin must be an HTTP(S) origin without credentials, path, or query');
  const enabled = read('PAI_LOOP_EMERGENCY_DISABLE').toLowerCase() !== 'true';
  return [{ json: { runtime: { apiBaseUrl: origin.replace(/\/$/, ''), enabled } } }];
}

export function awardOfflineFixture() {
  return [{ json: {
    schema_version: 'award-refresh-automation-1.0', status: 'IDLE',
    total: 3, complete: 1, no_results: 1, partial: 0, pending: 1, running: 0,
    failed: 0, unsupported: 0, skipped: 0, unplanned: 0, eligible: 1,
    api_calls_24h: 0, budget_reserved_24h: 0, ai_calls: 0,
    attempted: 0, notice_key: null, job_id: null, api_calls: 0, records: 0,
  } }];
}

export function validateAwardAggregate(input, phase) {
  const body = input?.body ?? input;
  const fail = () => { throw new Error('Award automation aggregate contract failed'); };
  if (!body || typeof body !== 'object' || Array.isArray(body)) fail();
  const countFields = ['total', 'complete', 'no_results', 'partial', 'pending', 'running',
    'failed', 'unsupported', 'skipped', 'unplanned', 'eligible', 'api_calls_24h',
    'budget_reserved_24h', 'ai_calls'];
  const extraFields = phase === 'plan' ? ['enrolled', 'requeued'] : ['attempted', 'notice_key', 'job_id', 'api_calls', 'records'];
  const allowed = ['schema_version', 'status', ...countFields, ...extraFields];
  if (Object.keys(body).length !== allowed.length || Object.keys(body).some(key => !allowed.includes(key))) fail();
  if (body.schema_version !== 'award-refresh-automation-1.0' || body.ai_calls !== 0) fail();
  const count = value => Number.isSafeInteger(value) && value >= 0;
  if (countFields.some(key => !count(body[key]))) fail();
  if (phase === 'plan') {
    if (body.status !== 'PLANNED' || !count(body.enrolled) || !count(body.requeued)) fail();
  } else {
    if (!['COMPLETED', 'PARTIAL', 'FAILED', 'IDLE', 'BUSY', 'DAILY_BUDGET_REACHED'].includes(body.status)) fail();
    if (![0, 1].includes(body.attempted) || !count(body.api_calls) || !count(body.records)) fail();
    for (const field of ['notice_key', 'job_id']) {
      if (body[field] !== null && (typeof body[field] !== 'string' || !body[field] || body[field].length > 160 || /[\u0000-\u001f\u007f]/.test(body[field]))) fail();
    }
  }
  // IDs are validated but never emitted. No source rows or messages enter output.
  const summary = { schema_version: body.schema_version, status: body.status };
  for (const key of countFields) summary[key] = body[key];
  for (const key of extraFields) if (!['notice_key', 'job_id'].includes(key)) summary[key] = body[key];
  return summary;
}

export function awardPlanDecision(input, runtime, validate) {
  const summary = validate(input, 'plan');
  return [{ json: {
    runtime,
    summary,
    canRun: runtime.enabled === true && summary.eligible > 0 && summary.running === 0
      && summary.api_calls_24h + summary.budget_reserved_24h + 150 <= 700,
  } }];
}

const source = fn => fn.toString().replace(/\r\n/g, "\n");
export const codeSources = {
  "Build Scheduled Award Runtime": `${source(awardRuntime)}\nreturn awardRuntime($env);`,
  "Load Offline Award Fixture": `${source(awardOfflineFixture)}\nreturn awardOfflineFixture();`,
  "Validate Award Plan and Budget": `${source(validateAwardAggregate)}\n${source(awardPlanDecision)}\nreturn awardPlanDecision($json, $node['Build Scheduled Award Runtime'].json.runtime, validateAwardAggregate);`,
  "Validate Award Run Aggregate": `${source(validateAwardAggregate)}\nconst summary = validateAwardAggregate($json, 'run');\nif (summary.status === 'FAILED') throw new Error('Award automation backend failed; inspect protected award refresh status');\nreturn [{ json: summary }];`,
  "Record Award Queue Waiting": "return [{ json: { ...$json.summary, status: 'WAITING', attempted: 0, api_calls: 0, records: 0 } }];",
  "Record Award Automation Disabled": "return [{ json: { schema_version: 'award-refresh-automation-1.0', status: 'DISABLED', attempted: 0, api_calls: 0, ai_calls: 0 } }];",
};

export const awardConnections = {
  "Run Award Automation Offline Fixture": { main: [[{ node: "Load Offline Award Fixture", type: "main", index: 0 }]] },
  "Load Offline Award Fixture": { main: [[{ node: "Validate Award Run Aggregate", type: "main", index: 0 }]] },
  "Every Ten Minutes KST": { main: [[{ node: "Build Scheduled Award Runtime", type: "main", index: 0 }]] },
  "Build Scheduled Award Runtime": { main: [[{ node: "Award Automation Enabled?", type: "main", index: 0 }]] },
  "Award Automation Enabled?": { main: [
    [{ node: "Enroll New and Stale Award Notices", type: "main", index: 0 }],
    [{ node: "Record Award Automation Disabled", type: "main", index: 0 }],
  ] },
  "Enroll New and Stale Award Notices": { main: [[{ node: "Validate Award Plan and Budget", type: "main", index: 0 }]] },
  "Validate Award Plan and Budget": { main: [[{ node: "Award Queue and Budget Ready?", type: "main", index: 0 }]] },
  "Award Queue and Budget Ready?": { main: [
    [{ node: "Refresh One Queued Award Notice", type: "main", index: 0 }],
    [{ node: "Record Award Queue Waiting", type: "main", index: 0 }],
  ] },
  "Refresh One Queued Award Notice": { main: [[{ node: "Validate Award Run Aggregate", type: "main", index: 0 }]] },
};

export function awardHttpParameters(phase) {
  return {
    method: "POST",
    url: `={{ $json.runtime.apiBaseUrl + '/api/v1/operations/award-refresh/${phase}' }}`,
    authentication: "genericCredentialType", genericAuthType: "httpHeaderAuth",
    sendHeaders: true,
    headerParameters: { parameters: [{ name: "Accept", value: "application/json" }, { name: "X-PAI-Request-Source", value: "n8n-award-automation-v1" }] },
    sendBody: true, contentType: "raw", rawContentType: "application/json",
    body: phase === "plan" ? '{"refresh_after_days":30}' : '{"max_notices":1,"daily_api_budget":700,"per_notice_api_budget":150}',
    options: { timeout: 520000,
      response: { response: { fullResponse: true, neverError: false, responseFormat: "json" } },
      redirect: { redirect: { followRedirects: false } },
    },
  };
}

export function awardGateParameters(field) {
  return {
    conditions: {
      options: { caseSensitive: true, leftValue: "", typeValidation: "strict", version: 2 },
      conditions: [{ id: field === "runtime.enabled" ? "award-enabled" : "award-ready", leftValue: `={{ $json.${field} }}`, rightValue: true,
        operator: { type: "boolean", operation: "true", singleValue: true } }],
      combinator: "and",
    }, options: {},
  };
}

export function assertAwardAutomationWorkflow(workflow, config) {
  assert.equal(config.contractVersion, awardContractVersion);
  assert.equal(config.safetyContract, awardSafetyContract);
  assert.equal(config.publish, true);
  assert.equal(workflow.name, "PAI_LOOP 14 - Award History Automation");
  const nodes = new Map(workflow.nodes.map(node => [node.name, node]));
  assert.equal(nodes.size, 13); assert.equal(workflow.nodes.length, 13);
  for (const [name, code] of Object.entries(codeSources)) {
    const node = nodes.get(name);
    assert.equal(node?.type, "n8n-nodes-base.code");
    assert.deepEqual(node.parameters, { jsCode: code });
  }
  assert.equal(nodes.get("Run Award Automation Offline Fixture")?.type, "n8n-nodes-base.manualTrigger");
  assert.deepEqual(nodes.get("Run Award Automation Offline Fixture").parameters, {});
  const schedule = nodes.get("Every Ten Minutes KST");
  assert.equal(schedule?.type, "n8n-nodes-base.scheduleTrigger");
  assert.deepEqual(schedule.parameters, { rule: { interval: [{ field: "cronExpression", expression: "*/10 * * * *" }] } });
  assert.equal(nodes.get("Award automation contract")?.type, "n8n-nodes-base.stickyNote");
  for (const [name, phase] of [["Enroll New and Stale Award Notices", "plan"], ["Refresh One Queued Award Notice", "run"]]) {
    const node = nodes.get(name);
    assert.equal(node?.type, "n8n-nodes-base.httpRequest"); assert.equal(node.typeVersion, 4.2);
    assert.deepEqual(node.parameters, awardHttpParameters(phase));
    assert.equal(node.retryOnFail, false);
  }
  for (const [name, field] of [["Award Automation Enabled?", "runtime.enabled"], ["Award Queue and Budget Ready?", "canRun"]]) {
    assert.equal(nodes.get(name)?.type, "n8n-nodes-base.if");
    assert.deepEqual(nodes.get(name).parameters, awardGateParameters(field));
  }
  for (const node of workflow.nodes) {
    assert(!node.credentials && !node.webhookId && !node.disabled && !node.executeOnce && !node.alwaysOutputData && !node.retryOnFail && !node.onError);
  }
  assert.deepEqual(workflow.connections, awardConnections);
  const expectedSettings = {
    executionOrder: "v1", timezone: "Asia/Seoul", executionTimeout: 570,
    saveDataErrorExecution: "none", saveDataSuccessExecution: "none",
    saveManualExecutions: false, saveExecutionProgress: false,
  };
  for (const [name, value] of Object.entries(expectedSettings)) assert.equal(workflow.settings?.[name], value);
}
