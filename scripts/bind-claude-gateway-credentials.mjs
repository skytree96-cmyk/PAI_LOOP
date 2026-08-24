const dryRun = process.argv.includes("--dry-run");
const baseUrl = process.env.N8N_BASE_URL?.trim().replace(/\/$/, "");
const apiKey = process.env.N8N_API_KEY?.trim();
const anthropicCredentialName = process.env.PAI_LOOP_N8N_CLAUDE_CREDENTIAL_NAME?.trim();

const gatewayWorkflowName = "PAI_LOOP 13 - Claude Extraction Gateway";
const dailyWorkflowName = "PAI_LOOP 10 - Daily Opportunity Briefing";
const webhookNodeName = "Claude Extraction Webhook";
const modelNodeName = "Claude Sonnet 4.6";
const anthropicNodeType = "@n8n/n8n-nodes-langchain.lmChatAnthropic";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

assert(baseUrl && /^https:\/\/[^\s/@]+(?:\/[^\s]*)?$/i.test(baseUrl), "N8N_BASE_URL must be a safe HTTPS URL");
assert(apiKey, "N8N_API_KEY is required");
assert(
  anthropicCredentialName && anthropicCredentialName.length <= 128,
  "PAI_LOOP_N8N_CLAUDE_CREDENTIAL_NAME is required and must exactly match the approved n8n credential name",
);

const headers = {
  "X-N8N-API-KEY": apiKey,
  "Content-Type": "application/json",
  Accept: "application/json",
};

async function request(apiPath, options = {}) {
  const response = await fetch(`${baseUrl}/api/v1${apiPath}`, { headers, ...options });
  const text = await response.text();
  let body = {};
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { message: text.slice(0, 500) };
    }
  }
  if (!response.ok) {
    throw new Error(`${options.method ?? "GET"} ${apiPath} failed with HTTP ${response.status}`);
  }
  return body;
}

async function listAllWorkflows() {
  const workflows = [];
  let cursor;
  for (let page = 0; page < 100; page += 1) {
    const query = new URLSearchParams({ limit: "100" });
    if (cursor) query.set("cursor", cursor);
    const response = await request(`/workflows?${query}`);
    assert(Array.isArray(response.data), "n8n workflow list did not contain data[]");
    workflows.push(...response.data);
    cursor = response.nextCursor;
    if (!cursor) return workflows;
  }
  throw new Error("n8n workflow pagination did not terminate");
}

function exactWorkflow(workflows, name) {
  const matches = workflows.filter((workflow) => workflow.name === name);
  assert(matches.length === 1, `expected exactly one remote workflow named ${name}`);
  return matches[0];
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

const summaries = await listAllWorkflows();
const gatewaySummary = exactWorkflow(summaries, gatewayWorkflowName);
const dailySummary = exactWorkflow(summaries, dailyWorkflowName);
const gateway = await request(`/workflows/${encodeURIComponent(gatewaySummary.id)}`);
const daily = await request(`/workflows/${encodeURIComponent(dailySummary.id)}`);

assert(gateway.active === false, "gateway must remain inactive while credentials are first bound");
const webhookNode = gateway.nodes?.find((node) => node.name === webhookNodeName);
const modelNode = gateway.nodes?.find((node) => node.name === modelNodeName);
assert(
  webhookNode?.type === "n8n-nodes-base.webhook"
    && webhookNode.parameters?.authentication === "headerAuth"
    && webhookNode.parameters?.path === "pai-loop-claude/responses",
  "remote gateway webhook contract is invalid",
);
assert(
  modelNode?.type === anthropicNodeType
    && modelNode.parameters?.model?.value === "claude-sonnet-4-6",
  "remote gateway Claude model contract is invalid",
);
assert(
  !gateway.nodes.some((node) => (
    node.type === "n8n-nodes-base.scheduleTrigger"
    || node.type === "n8n-nodes-base.httpRequest"
    || node.type.includes("agent")
    || node.type.includes("memory")
    || node.type.includes("Tool")
  )),
  "remote gateway contains an unapproved schedule, HTTP, agent, memory, or tool node",
);

const backendCredentials = daily.nodes
  .filter((node) => node.type === "n8n-nodes-base.httpRequest")
  .map((node) => node.credentials?.httpHeaderAuth)
  .filter(Boolean);
const backendCredentialIds = new Set(backendCredentials.map((credential) => String(credential.id ?? "")));
assert(
  backendCredentials.length > 0 && backendCredentialIds.size === 1 && !backendCredentialIds.has(""),
  "daily workflow does not expose exactly one approved Generic Header credential",
);
const backendCredential = backendCredentials[0];

const anthropicCredentialsById = new Map();
for (const summary of summaries) {
  const workflow = summary.nodes
    ? summary
    : await request(`/workflows/${encodeURIComponent(summary.id)}`);
  for (const node of workflow.nodes ?? []) {
    if (node.type !== anthropicNodeType) continue;
    const credential = node.credentials?.anthropicApi;
    if (!credential || credential.name !== anthropicCredentialName) continue;
    const id = String(credential.id ?? "");
    assert(id, "matched Anthropic credential is missing its remote ID");
    anthropicCredentialsById.set(id, credential);
  }
}
assert(
  anthropicCredentialsById.size === 1,
  "the selected Anthropic credential name must resolve to exactly one remote credential ID",
);
const anthropicCredential = [...anthropicCredentialsById.values()][0];

const payload = deploymentPayload({
  ...gateway,
  nodes: gateway.nodes.map((node) => {
    if (node.name === webhookNodeName) {
      return { ...node, credentials: { httpHeaderAuth: backendCredential } };
    }
    if (node.name === modelNodeName) {
      return { ...node, credentials: { anthropicApi: anthropicCredential } };
    }
    return node;
  }),
});

if (dryRun) {
  console.log(`Validated credential binding for ${gatewayWorkflowName}; no remote changes made`);
  process.exit(0);
}

await request(`/workflows/${encodeURIComponent(gateway.id)}`, {
  method: "PUT",
  body: JSON.stringify(payload),
});
console.log(
  `Bound the approved backend Header Auth and Anthropic credential "${anthropicCredentialName}" to ${gatewayWorkflowName}; workflow remains inactive`,
);
