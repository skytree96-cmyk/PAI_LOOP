import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { createGunzip } from "node:zlib";
import { assertAwardAutomationWorkflow, awardWorkflowKey, validateAwardAggregate } from "./award-automation-workflow-contract.mjs";

const workflow = JSON.parse(await fs.readFile(`workflows/${awardWorkflowKey}.json`, "utf8"));
const manifest = JSON.parse(await fs.readFile("manifest.json", "utf8"));
const config = manifest.workflows[awardWorkflowKey];
assertAwardAutomationWorkflow(workflow, config);
const nodes = new Map(workflow.nodes.map(node => [node.name, node]));
for (const [name, expected] of [
  ["Enroll New and Stale Award Notices", { refresh_after_days: 30 }],
  ["Refresh One Queued Award Notice", { max_notices: 1, daily_api_budget: 700, per_notice_api_budget: 150 }],
]) {
  const parameters = nodes.get(name).parameters;
  assert.equal(parameters.contentType, "json", "raw-body mode returns an unresolved n8n response stream");
  assert.equal(parameters.specifyBody, "json");
  assert.deepEqual(JSON.parse(parameters.jsonBody), expected);
  assert.equal(parameters.options.response.response.responseFormat, "json");
  assert(!("body" in parameters)); assert(!("rawContentType" in parameters));
}
const blocked = new Proxy({}, { get() { throw new Error("SYN external access forbidden"); } });
function code(name, input = {}, env = blocked, context = blocked) {
  return new Function("$json", "$env", "$node", nodes.get(name).parameters.jsCode)(input, env, context)[0].json;
}

// Execute the entire manual path with environment and node access forbidden.
const fixture = code("Load Offline Award Fixture");
const manual = code("Validate Award Run Aggregate", fixture);
assert.equal(manual.status, "IDLE"); assert.equal(manual.attempted, 0);
assert.equal(manual.api_calls, 0); assert.equal(manual.ai_calls, 0);
assert(!("notice_key" in manual)); assert(!("job_id" in manual));
const visited = new Set();
const pending = ["Run Award Automation Offline Fixture"];
while (pending.length) {
  const name = pending.pop();
  if (visited.has(name)) continue;
  visited.add(name);
  assert.notEqual(nodes.get(name).type, "n8n-nodes-base.httpRequest");
  for (const lanes of Object.values(workflow.connections[name] ?? {}))
    for (const lane of lanes) for (const edge of lane) pending.push(edge.node);
}
assert.equal(visited.size, 3);

const runtime = code("Build Scheduled Award Runtime", {}, {}).runtime;
assert.equal(runtime.apiBaseUrl, "https://pai-loop-demo.onrender.com"); assert.equal(runtime.enabled, true);
assert.equal(code("Build Scheduled Award Runtime", {}, { PAI_LOOP_EMERGENCY_DISABLE: "true" }).runtime.enabled, false);
assert.equal(code("Build Scheduled Award Runtime", {}, { PAI_LOOP_API_BASE_URL: "https://syn.example/" }).runtime.apiBaseUrl, "https://syn.example");
const syntheticUserinfoOrigin = new URL("https://syn.example");
syntheticUserinfoOrigin.username = "SYN";
syntheticUserinfoOrigin.password = "SYN-password";
for (const origin of [syntheticUserinfoOrigin.href, "https://syn.example/api", "https://syn.example?x=1", "https://syn.example#x", "ftp://syn.example", "https://syn.example\\other"]) {
  assert.throws(() => code("Build Scheduled Award Runtime", {}, { PAI_LOOP_API_BASE_URL: origin }), /origin/);
}

const plan = { ...fixture, status: "PLANNED", enrolled: 1, requeued: 0 };
for (const field of ["attempted", "notice_key", "job_id", "api_calls", "records"]) delete plan[field];
const decide = value => code("Validate Award Plan and Budget", value, blocked, { "Build Scheduled Award Runtime": { json: { runtime } } });
assert.equal(decide(plan).canRun, true);
assert.equal(decide({ body: plan }).canRun, true);
// Real live failure shape: raw HTTP request mode returned a decompression stream
// in body despite responseFormat=json. Keep rejecting it instead of weakening
// the aggregate allowlist or trying to inspect internal transport buffers.
const unresolvedResponse = createGunzip();
try {
  assert.throws(() => decide({ body: unresolvedResponse }), /aggregate contract failed/);
} finally {
  unresolvedResponse.destroy();
}
for (const value of [{ ...plan, eligible: 0 }, { ...plan, running: 1 }, { ...plan, api_calls_24h: 551 }, { ...plan, budget_reserved_24h: 551 }, { ...plan, api_calls_24h: 500, budget_reserved_24h: 51 }]) {
  assert.equal(decide(value).canRun, false);
}
assert.equal(decide({ ...plan, api_calls_24h: 500, budget_reserved_24h: 50 }).canRun, true);
assert.equal(code("Record Award Queue Waiting", decide({ ...plan, eligible: 0 })).status, "WAITING");
assert.equal(code("Record Award Automation Disabled").ai_calls, 0);
for (const status of ["COMPLETED", "PARTIAL", "IDLE", "BUSY", "DAILY_BUDGET_REACHED"]) {
  assert.equal(code("Validate Award Run Aggregate", { body: { ...fixture, status } }).status, status);
}
assert.throws(() => code("Validate Award Run Aggregate", { ...fixture, status: "FAILED" }), /backend failed/);
const privateValue = "SYN-PRIVATE-DO-NOT-EMIT";
const sanitized = code("Validate Award Run Aggregate", { ...fixture, status: "COMPLETED", attempted: 1, notice_key: privateValue, job_id: "SYN-job", api_calls: 4, records: 8 });
assert(!JSON.stringify(sanitized).includes(privateValue));
const invalid = [null, [], true, { ...fixture, raw_payload: privateValue }, { ...fixture, ai_calls: 1 },
  { ...fixture, status: privateValue }, { ...fixture, attempted: 2 }, { ...fixture, attempted: true },
  { ...fixture, notice_key: { raw: privateValue } }, { ...fixture, job_id: "" }, { ...fixture, job_id: "SYN\nsecret" },
  { ...fixture, schema_version: "wrong" }];
for (const field of ["total", "eligible", "api_calls_24h", "budget_reserved_24h", "api_calls", "records"]) {
  for (const value of [-1, 1.1, "1", true, Infinity, Number.MAX_SAFE_INTEGER + 1]) invalid.push({ ...fixture, [field]: value });
}
for (const value of invalid) assert.throws(() => validateAwardAggregate(value, "run"), error => error.message === "Award automation aggregate contract failed");
for (const field of Object.keys(fixture)) {
  const value = { ...fixture }; delete value[field];
  assert.throws(() => validateAwardAggregate(value, "run"), /aggregate contract failed/);
}

// Safety validation must reject a network branch, extra code, retries, increased
// limits, wrong endpoints, credential material and publication metadata drift.
for (const mutate of [
  item => { item.connections["Load Offline Award Fixture"].main[0][0].node = "Refresh One Queued Award Notice"; },
  item => { item.nodes.find(node => node.name === "Load Offline Award Fixture").parameters.jsCode += "\nfetch('https://syn.example');"; },
  item => { item.nodes.find(node => node.name === "Refresh One Queued Award Notice").retryOnFail = true; },
  item => { item.nodes.find(node => node.name === "Refresh One Queued Award Notice").parameters.jsonBody = '{"max_notices":2}'; },
  item => {
    const parameters = item.nodes.find(node => node.name === "Enroll New and Stale Award Notices").parameters;
    parameters.contentType = "raw"; parameters.rawContentType = "application/json";
    parameters.body = parameters.jsonBody; delete parameters.jsonBody; delete parameters.specifyBody;
  },
  item => { item.nodes.find(node => node.name === "Refresh One Queued Award Notice").parameters.url = "https://syn.example/analysis"; },
  item => { item.nodes.find(node => node.name === "Refresh One Queued Award Notice").credentials = { httpHeaderAuth: { id: privateValue } }; },
  item => { item.settings.saveDataSuccessExecution = "all"; },
  item => { item.nodes.push({ name: "SYN extra", type: "n8n-nodes-base.httpRequest" }); },
]) {
  const changed = structuredClone(workflow); mutate(changed);
  assert.throws(() => assertAwardAutomationWorkflow(changed, config));
}
assert.throws(() => assertAwardAutomationWorkflow(workflow, { ...config, safetyContract: "unapproved" }));
for (const [key, entry] of Object.entries(manifest.workflows)) if (/pai-loop-0[0-4]-/.test(key)) assert.equal(entry.publish, false);

// Fully intercept deploy's fetch in a child process. No HTTP server or network
// is used. Prove isolated W14 create/update and credential identity preservation
// while pending native-Claude metadata remains in the real manifest.
for (const scenario of ["create", "update", "update-active", "wrong-binding", "wrong-source", "lookalike-source", "wrong-source-type"]) {
  const harness = `
    import assert from 'node:assert/strict';
    import fs from 'node:fs/promises';
    const manifest = JSON.parse(await fs.readFile('manifest.json', 'utf8'));
    const dailyKey = 'pai-loop-10-daily-opportunity-briefing';
    const awardKey = '${awardWorkflowKey}';
    const daily = JSON.parse(await fs.readFile(manifest.workflows[dailyKey].file, 'utf8'));
    const award = JSON.parse(await fs.readFile(manifest.workflows[awardKey].file, 'utf8'));
    const scenario = ${JSON.stringify(scenario)};
    const credential = { httpHeaderAuth: { id: 'SYN-private-header-reference', name: 'SYN backend' } };
    daily.id = manifest.workflows[dailyKey].n8nWorkflowId;
    daily.active = false;
    for (const node of daily.nodes) if(node.type === 'n8n-nodes-base.httpRequest') node.credentials = credential;
    if(scenario === 'wrong-source') daily.name = 'SYN wrong source';
    if(scenario === 'lookalike-source') for(const node of daily.nodes) node.name = 'SYN lookalike ' + node.name;
    if(scenario === 'wrong-source-type') for(const node of daily.nodes) if(node.credentials) node.type = 'SYN wrong type';
    let remote = scenario === 'create' ? undefined : {...award, id:'SYN-award-workflow', active:scenario==='update-active', versionId:'SYN-prior-version', activeVersionId:scenario==='update-active'?'SYN-prior-version':null};
    if(remote) for(const node of remote.nodes) if(node.type === 'n8n-nodes-base.httpRequest') node.credentials = scenario === 'wrong-binding' ? {httpHeaderAuth:{id:'SYN-wrong-reference',name:'SYN wrong'}} : credential;
    const mutations = [];
    const response = body => ({ok:true,status:200,text:async()=>JSON.stringify(body)});
    globalThis.fetch = async (url, options = {}) => {
      const parsed = new URL(url); assert.equal(parsed.origin,'https://syn-n8n.invalid');
      const route = parsed.pathname; const method = options.method ?? 'GET';
      if(method === 'GET' && route === '/api/v1/workflows') return response({data:[{id:daily.id,name:daily.name}, ...(remote?[{id:remote.id,name:remote.name}]:[])]});
      if(method === 'GET' && route === '/api/v1/workflows/' + daily.id) return response(daily);
      if(method === 'GET' && route === '/api/v1/workflows/SYN-award-workflow') return response(remote);
      mutations.push([method,route]);
      if((method === 'POST' && route === '/api/v1/workflows') || (method === 'PUT' && route === '/api/v1/workflows/SYN-award-workflow')) {
        const payload = JSON.parse(options.body); assert.equal(payload.name,award.name);
        remote = {...payload,id:'SYN-award-workflow',active:remote?.active??false,versionId:'SYN-saved-version',activeVersionId:remote?.activeVersionId??null}; return response(remote);
      }
      if(method === 'POST' && route === '/api/v1/workflows/SYN-award-workflow/activate') {
        assert.deepEqual(JSON.parse(options.body),{versionId:'SYN-saved-version'});
        remote.active=true;remote.activeVersionId='SYN-saved-version';return response(remote);
      }
      throw new Error('SYN unapproved route');
    };
    process.argv.push('--only=' + awardKey);
    let error; try { await import('./scripts/deploy-workflows.mjs'); } catch(caught) { error=caught; }
    if(['create','update','update-active'].includes(scenario)) {
      if(error) throw error;
      assert.equal(mutations.length,2); assert.equal(mutations[0][0],scenario==='create'?'POST':'PUT');
      assert.equal(mutations[1][1],'/api/v1/workflows/SYN-award-workflow/activate');
      assert.equal(remote.active,true);
      assert.equal(remote.activeVersionId,remote.versionId,'W14 must run the saved draft after active updates');
      for(const node of remote.nodes) if(node.type==='n8n-nodes-base.httpRequest') assert.deepEqual(node.credentials,credential);
    } else {assert(error,'SYN invalid credential source must fail');assert.deepEqual(mutations,[]);}
    assert.equal(daily.active,false);
  `;
  const result = spawnSync(process.execPath, ["--input-type=module", "--eval", harness], {
    encoding: "utf8", env: { ...process.env, N8N_BASE_URL: "https://syn-n8n.invalid", N8N_API_KEY: "SYN-api-key" }, timeout: 30000,
  });
  assert.equal(result.status, 0, `${scenario}: ${result.stderr}`);
  assert(!result.stdout.includes("SYN-private-header-reference"));
}
console.log("Award automation workflow passed: offline path, budget gates, aggregate privacy, isolated deployment and exact credentials");
