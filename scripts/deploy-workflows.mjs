import fs from "node:fs/promises";

const baseUrl = process.env.N8N_BASE_URL?.replace(/\/$/, "");
const apiKey = process.env.N8N_API_KEY;
if (!baseUrl || !apiKey) throw new Error("N8N_BASE_URL and N8N_API_KEY are required");

const headers = {
  "X-N8N-API-KEY": apiKey,
  "Content-Type": "application/json",
  Accept: "application/json",
};

async function request(path, options = {}) {
  const response = await fetch(`${baseUrl}/api/v1${path}`, { headers, ...options });
  const body = await response.text();
  if (!response.ok) throw new Error(`${options.method ?? "GET"} ${path} failed (${response.status}): ${body}`);
  return body ? JSON.parse(body) : {};
}

const manifest = JSON.parse(await fs.readFile("manifest.json", "utf8"));
for (const [key, config] of Object.entries(manifest.workflows ?? {})) {
  const workflow = JSON.parse(await fs.readFile(config.file, "utf8"));
  const payload = {
    name: workflow.name,
    nodes: workflow.nodes,
    connections: workflow.connections ?? {},
    settings: workflow.settings ?? {},
    ...(workflow.staticData ? { staticData: workflow.staticData } : {}),
  };

  let workflowId = config.n8nWorkflowId;
  if (workflowId) {
    await request(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    console.log(`Updated ${key} (${workflowId})`);
  } else {
    const created = await request("/workflows", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    workflowId = created.id;
    console.log(`Created ${key} (${workflowId}); add this ID to manifest.json`);
  }

  if (config.publish === true) {
    await request(`/workflows/${encodeURIComponent(workflowId)}/activate`, { method: "POST" });
    console.log(`Activated ${key}`);
  }
}
