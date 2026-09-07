import assert from "node:assert/strict";
import fs from "node:fs";

const workflow = JSON.parse(
  fs.readFileSync("workflows/pai-loop-13-claude-extraction-gateway.json", "utf8"),
);
const nodes = new Map(workflow.nodes.map((node) => [node.name, node]));
const webhook = nodes.get("Claude Extraction Webhook");
const validation = nodes.get("Validate Gateway Request");
const chain = nodes.get("Claude JSON Extraction");
const model = nodes.get("Claude Sonnet 5");
const normalizer = nodes.get("Normalize Gateway Response");

assert.equal(workflow.nodes.length, 10);
assert.equal(webhook.type, "n8n-nodes-base.webhook");
assert.equal(webhook.parameters.httpMethod, "POST");
assert.equal(webhook.parameters.path, "pai-loop-claude/responses");
assert.equal(webhook.parameters.authentication, "headerAuth");
assert.equal(webhook.parameters.responseMode, "responseNode");
assert.equal(chain.type, "@n8n/n8n-nodes-langchain.chainLlm");
assert.equal(chain.typeVersion, 1.9);
assert.equal(model.type, "@n8n/n8n-nodes-langchain.lmChatAnthropic");
assert.equal(model.typeVersion, 1.5);
assert.equal(model.parameters.model.value, "claude-sonnet-5");
assert.equal(model.parameters.options.maxTokensToSample, "={{ $json.max_output_tokens }}");
assert.equal(model.parameters.options.thinkingMode, "adaptive");
assert.equal(model.parameters.options.effort, "medium");
for (const unsupported of ["temperature", "topP", "topK", "thinkingBudget"]) {
  assert.equal(unsupported in model.parameters.options, false);
}
assert.equal(workflow.settings.saveDataSuccessExecution, "none");
assert.equal(workflow.settings.saveDataErrorExecution, "none");
for (const node of workflow.nodes) assert.equal(node.credentials, undefined);

const executeValidation = new Function("$json", validation.parameters.jsCode);
const validBody = {
  model: "claude-sonnet-5",
  service_tier: "default",
  store: false,
  max_output_tokens: 20000,
  input: [
    {
      role: "system",
      content: [
        {
          type: "input_text",
          text: "You extract procurement requirements as evidence only. Never decide a bid.",
        },
      ],
    },
    {
      role: "user",
      content: [
        {
          type: "input_text",
          text: "Allowed attachment IDs: [\"a-1\"]\n\nSOURCE:\npublic source text",
        },
      ],
    },
  ],
  text: {
    format: {
      type: "json_schema",
      name: "pai_loop_requirements",
      strict: true,
      schema: {
        type: "object",
        additionalProperties: false,
        properties: { summary: { type: "string" } },
        required: ["summary"],
      },
    },
  },
};

const validated = executeValidation({ body: validBody });
assert.equal(validated.length, 1);
assert.equal(validated[0].json.system_prompt, validBody.input[0].content[0].text);
assert.match(validated[0].json.user_prompt, /RESPONSE JSON SCHEMA/);
assert.equal(validated[0].json.max_output_tokens, 20000);

const correctiveBody = structuredClone(validBody);
correctiveBody.input[1].content[0].text =
  `FINAL CORRECTIVE RETRY. Regenerate the JSON.\n\n${validBody.input[1].content[0].text}`;
assert.equal(executeValidation({ body: correctiveBody }).length, 1);

assert.throws(
  () => executeValidation({ body: { ...validBody, model: "gpt-5.6-luna" } }),
  /model must be claude-sonnet-5/,
);
assert.throws(
  () => executeValidation({ body: { ...validBody, max_output_tokens: 20001 } }),
  /max_output_tokens is outside/,
);
assert.throws(
  () => executeValidation({ body: { ...validBody, arbitrary_prompt: true } }),
  /request fields do not match/,
);
assert.throws(
  () => executeValidation({
    body: {
      ...validBody,
      text: { format: { ...validBody.text.format, strict: false } },
    },
  }),
  /strict response format is invalid/,
);

const executeNormalizer = new Function(
  "$json",
  "$execution",
  "$",
  normalizer.parameters.jsCode,
);
const executeNormaliserText = (text) => executeNormalizer(
  { text },
  { id: "fixture-123" },
  () => ({
    all: () => [{
      json: {
        tokenUsage: { promptTokens: 12, completionTokens: 7, totalTokens: 19 },
      },
    }],
  }),
);
const normalised = executeNormaliserText(' { "summary" : "ok" } ');
assert.deepEqual(normalised[0].json, {
  id: "pai_claude_fixture-123",
  status: "completed",
  model: "claude-sonnet-5",
  output_text: "{\"summary\":\"ok\"}",
  usage: { input_tokens: 12, output_tokens: 7, total_tokens: 19 },
});

for (const fenced of [
  '```json\n{ "summary": "fenced" }\n```',
  '```\r\n{ "summary": "fenced" }\r\n```',
]) {
  assert.equal(
    executeNormaliserText(fenced)[0].json.output_text,
    '{"summary":"fenced"}',
  );
}
assert.throws(
  () => executeNormaliserText('Here is the JSON:\n```json\n{"summary":"no"}\n```'),
  /without prose/,
);
assert.throws(
  () => executeNormaliserText('```json\n{"summary":"one"}\n```\n```json\n{"summary":"two"}\n```'),
  /multiple Markdown fences/,
);
assert.throws(
  () => executeNormaliserText('```json\n{"summary":}\n```'),
  /not valid JSON/,
);
assert.throws(
  () => executeNormaliserText('[{"summary":"array"}]'),
  /plain JSON object/,
);
assert.equal(
  normalizer.parameters.jsCode.includes("Object.getPrototypeOf(parsed)"),
  false,
  "n8n Code-node values can cross a sandbox realm, so prototype identity must not be used",
);
assert.throws(
  () => executeNormaliserText(" ".repeat(500001)),
  /empty or oversized/,
);

const canary = "SYN-PRIVATE-GATEWAY-CANARY";
const stages = [
  ["Validate Gateway Request", "Input Failure", "INPUT_VALIDATION", "REQUEST_REJECTED", "Claude JSON Extraction"],
  ["Claude JSON Extraction", "Model Failure", "MODEL_EXECUTION", "MODEL_EXECUTION_FAILED", "Normalize Gateway Response"],
  ["Normalize Gateway Response", "Output Failure", "OUTPUT_NORMALIZATION", "OUTPUT_REJECTED", "Respond Gateway Success"],
];
for (const [source, suffix, stage, code, success] of stages) {
  const safeName = `Sanitize Gateway ${suffix}`;
  const safeNode = nodes.get(safeName);
  const sanitize = new Function("$json", safeNode.parameters.jsCode);
  assert.equal(nodes.get(source).onError, "continueErrorOutput");
  assert.deepEqual(workflow.connections[source].main, [
    [{ node: success, type: "main", index: 0 }], [{ node: safeName, type: "main", index: 0 }],
  ]);
  assert.deepEqual(workflow.connections[safeName].main, [[{ node: "Respond Gateway Failure", type: "main", index: 0 }]]);
  for (const error of [canary, null, { message: canary, stack: canary, headers: { authorization: canary }, status: 429 },
    { status: "429 " + canary }, { status: true }, { status: 399 }, { status: 600 },
    { status: 429, statusCode: 500 }]) {
    const rows = sanitize({ error, input: canary, body: canary, executionId: canary });
    assert.equal(rows.length, 1);
    const expectedStatus = stage === "MODEL_EXECUTION" && error?.status === 429 && !error.statusCode ? 429 : null;
    assert.deepEqual(rows[0].json, { gateway_error: { version: "gateway-failure-v1", stage, code, upstream_http_status: expectedStatus } });
    assert(!JSON.stringify(rows).includes(canary));
  }
  for (const field of ["status", "statusCode", "httpCode"]) {
    for (const status of [403, 429, 502]) {
      const diagnostic = sanitize({ error: { [field]: status, message: canary } })[0].json.gateway_error;
      assert.equal(diagnostic.upstream_http_status, stage === "MODEL_EXECUTION" ? status : null);
    }
  }
}
for (const [suffix, code] of [["Success", 200], ["Failure", 500]]) {
  const response = nodes.get(`Respond Gateway ${suffix}`);
  assert.equal(response.type, "n8n-nodes-base.respondToWebhook");
  assert.equal(response.parameters.responseBody, "={{ $json }}");
  assert.equal(response.parameters.options.responseCode, code);
  assert(response.parameters.options.responseHeaders.entries.some(row => row.name === "Cache-Control" && row.value === "no-store"));
}
// Execute both former generic-500 counterexamples through their error edges.
// Neither path can reach/retry the provider after the failure.
for (const [source, failingCall] of [
  ["Validate Gateway Request", () => executeValidation({ body: { ...validBody, arbitrary_prompt: canary } })],
  ["Normalize Gateway Response", () => executeNormaliserText(canary)],
]) {
  let failed = false;
  try { failingCall(); } catch (error) {
    failed = true;
    const edge = workflow.connections[source].main[1][0].node;
    const safe = new Function("$json", nodes.get(edge).parameters.jsCode)({ error: error.message, body: canary });
    assert(!JSON.stringify(safe).includes(canary));
    assert.equal(workflow.connections[edge].main[0][0].node, "Respond Gateway Failure");
  }
  assert(failed);
}
console.log("Claude extraction gateway workflow success, failure isolation and redaction tests passed");
