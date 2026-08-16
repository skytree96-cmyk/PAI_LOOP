import fs from "node:fs/promises";
import path from "node:path";

const validateOnly = process.argv.includes("--validate-only");
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
console.log(`Validated ${definitions.length} workflow definition(s)`);
if (validateOnly) process.exit(0);

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

const remoteWorkflows = await listAllWorkflows();
const remoteByName = new Map();
for (const workflow of remoteWorkflows) {
  const matches = remoteByName.get(workflow.name) ?? [];
  matches.push(workflow);
  remoteByName.set(workflow.name, matches);
}

for (const { key, config, workflow } of definitions) {
  const payload = deploymentPayload(workflow);
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
    const updated = await request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    remote = updated.body;
    console.log(`Updated ${key} (${workflowId})`);
  } else {
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
