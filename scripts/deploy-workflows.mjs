import fs from "node:fs/promises";
import path from "node:path";

const validateOnly = process.argv.includes("--validate-only");
const onlyArgument = process.argv.find((argument) => argument.startsWith("--only="));
const onlyKey = onlyArgument?.slice("--only=".length) || undefined;
const rootDirectory = process.cwd();

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
  for (const definition of definitions) {
    if (definition.key === daily.key || definition.key === continuation.key) continue;
    assert(
      definition.config.publish === false,
      `${definition.key}: only workflows 10 and 11 may be published`,
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
  assert(daily.workflow.settings?.timezone === "Asia/Seoul", "daily workflow timezone must be Asia/Seoul");
  const schedules = daily.workflow.nodes.filter(
    (node) => node.type === "n8n-nodes-base.scheduleTrigger",
  );
  assert(schedules.length === 1, "daily workflow must have exactly one schedule trigger");
  assert(
    schedules[0].parameters?.rule?.interval?.[0]?.expression === "0 9 * * *",
    "daily workflow schedule must be 09:00 every day",
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
  assert(httpNodes.length === 9, "daily live branch must expose exactly nine protected backend HTTP boundaries");
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
    serialised.includes("https://pai-loop-demo.onrender.com"),
    "daily workflow must contain the public Render origin fallback",
  );
  assert(serialised.includes("retentionDays: 7"), "daily workflow must declare seven-day retention");
  assert(
    serialised.includes("/api/v1/notices/analysis/batch"),
    "daily workflow must route PPS notice keys through the backend batch analysis endpoint",
  );
  assert(
    serialised.includes("maxAnalysisBatchNotices: 3")
      && serialised.includes("maxAttachmentsPerNotice: 1"),
    "daily batch analysis must remain bounded to three notices and one attachment each",
  );
  assert(
    serialised.includes("created_notice_keys")
      && serialised.includes("updated_notice_keys")
      && serialised.includes("refresh_notice_keys")
      && serialised.includes("retry_notice_keys")
      && serialised.includes("retry_epoch")
      && serialised.includes("request_token")
      && serialised.includes("$execution.id")
      && serialised.includes("execution_limit: 30")
      && serialised.includes("max_continuations: 128")
      && serialised.includes("segment_id")
      && serialised.includes("chunk_indices")
      && serialised.includes("refusing silent truncation")
      && serialised.includes("splitInBatches"),
    "daily analysis must durably lease exact created+updated keys without silent truncation",
  );
  assert(
    serialised.includes("useProfileKeywords: true")
      && serialised.includes("profileDepartmentIds: []")
      && serialised.includes("ppsPageSize: 999")
      && serialised.includes("ppsMaxPages: 3")
      && serialised.includes("page-limited PPS ingestion must be PARTIAL"),
    "daily ingestion must paginate the organization profile and fail closed on page caps",
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
        === JSON.stringify(["Fetch Ranked Seven-Day Briefing"])
      && JSON.stringify(targets("Record Batch Analysis Skipped"))
        === JSON.stringify(["Fetch Ranked Seven-Day Briefing"]),
    "analysis completion or explicit skip must immediately precede the final briefing",
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
    daily.config.contractVersion === "daily-briefing-1.5",
    "daily workflow manifest contractVersion must be daily-briefing-1.5",
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
      && continuationSerialised.includes("maxContinuations: 128")
      && continuationSerialised.includes("queueName: 'ANY'")
      && continuationSerialised.includes("resumeOnly: true")
      && continuationSerialised.includes("segment_id")
      && continuationSerialised.includes("chunk_indices"),
    "workflow 11 must use the resumable 30-notice durable segment contract",
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
      && continuationSchedules[0].parameters?.rule?.interval?.[0]?.expression === "*/15 * * * *",
    "workflow 11 continuation schedule must poll every 15 minutes",
  );

  const preservationProbe = preserveRemoteNodeCredentials(
    {
      nodes: [
        { name: "same", type: "n8n-nodes-base.httpRequest" },
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
      ],
    },
  );
  assert(
    preservationProbe.nodes[0].credentials?.httpHeaderAuth?.id === "opaque-probe",
    "exact node-name/type credential preservation failed",
  );
  assert(
    !preservationProbe.nodes[1].credentials && !preservationProbe.nodes[2].credentials,
    "credential preservation must reject type changes and new nodes",
  );
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

function preserveRemoteNodeCredentials(payload, remote) {
  const remoteByName = new Map((remote?.nodes ?? []).map((node) => [node.name, node]));
  return {
    ...payload,
    nodes: payload.nodes.map((node) => {
      const prior = remoteByName.get(node.name);
      if (!prior || prior.type !== node.type || !prior.credentials) return node;
      // Credentials are environment-owned.  Preserve only an exact node-name
      // and node-type match; never print or write the IDs back to the repo.
      return { ...node, credentials: prior.credentials };
    }),
  };
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

function inheritApprovedBackendCredential(payload, credential, approvedNodeNames) {
  if (!approvedNodeNames.size) return payload;
  assert(credential?.httpHeaderAuth?.id, "approved new backend HTTP nodes require an inherited credential");
  return {
    ...payload,
    nodes: payload.nodes.map((node) => (
      approvedNodeNames.has(node.name)
        ? { ...node, credentials: node.credentials ?? credential }
        : node
    )),
  };
}

const approvedCredentialInheritance = new Map([
  ["pai-loop-10-daily-opportunity-briefing", new Set([
    "Reserve or Resume Daily Analysis Operation",
    "Finalize Daily Analysis Segment",
  ])],
  ["pai-loop-11-analysis-backfill", new Set([
    "Reserve or Resume Backfill Plan",
    "Analyze One Bounded Chunk",
    "Finalize Backfill Audit",
  ])],
]);

const remoteWorkflows = await listAllWorkflows();
const remoteByName = new Map();
for (const workflow of remoteWorkflows) {
  const matches = remoteByName.get(workflow.name) ?? [];
  matches.push(workflow);
  remoteByName.set(workflow.name, matches);
}

let sharedBackendCredential;

for (const { key, config, workflow } of definitions.filter(
  (definition) => !onlyKey || definition.key === onlyKey,
)) {
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
    payload = preserveRemoteNodeCredentials(payload, remote);
    payload = inheritApprovedBackendCredential(
      payload,
      sharedBackendCredential ?? extractSingleBackendCredential(remote),
      approvedCredentialInheritance.get(key) ?? new Set(),
    );
    const updated = await request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    remote = updated.body;
    console.log(`Updated ${key} (${workflowId})`);
  } else {
    payload = inheritApprovedBackendCredential(
      payload,
      sharedBackendCredential,
      approvedCredentialInheritance.get(key) ?? new Set(),
    );
    const created = await request("/workflows", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    remote = created.body;
    workflowId = remote.id;
    remoteByName.set(workflow.name, [remote]);
    console.log(`Created ${key} (${workflowId}); future deploys will match it by exact name`);
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
