import assert from "node:assert/strict";
import fs from "node:fs";
import { gatewayResponseExpression } from "./gateway-response-contract.mjs";
import "./test-gateway-terminal-response.mjs";

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
for (const [text, detail] of [
  ['Here is the JSON:\n```json\n{"summary":"no"}\n```', 'OUTPUT_FENCE_INVALID'],
  ['```json\n{"summary":"one"}\n```\n```json\n{"summary":"two"}\n```', 'OUTPUT_JSON_INVALID'],
  ['```json\n{"summary":}\n```', 'OUTPUT_JSON_INVALID'],
  ['[{"summary":"array"}]', 'OUTPUT_NOT_OBJECT'],
  [' '.repeat(500001), 'OUTPUT_TOO_LARGE'],
]) {
  assert.deepEqual(executeNormaliserText(text)[0].json, { gateway_error: {
    version: 'gateway-failure-v1', stage: 'OUTPUT_NORMALIZATION', code: 'OUTPUT_REJECTED',
    upstream_http_status: null, detail_code: detail,
  } });
}
// An intact JSON object's string values are evidence, not Markdown delimiters.
for (const summary of ['```SYN```', 'SYN ``` one marker', '```json\nSYN\n```']) {
  const text = JSON.stringify({ summary });
  assert.equal(executeNormaliserText(text)[0].json.output_text, text);
  assert.equal(executeNormaliserText('```json\n' + text + '\n```')[0].json.output_text, text);
}
for (const [input, execution, detail] of [
  [{}, { id: 'SYN' }, 'OUTPUT_EMPTY'],
  [{ text: ' ' }, { id: 'SYN' }, 'OUTPUT_EMPTY'],
  [{ text: { private: 'SYN-PRIVATE-NORMALIZER' } }, { id: 'SYN' }, 'OUTPUT_TYPE_INVALID'],
  [{ text: 'null' }, { id: 'SYN' }, 'OUTPUT_NOT_OBJECT'],
  [{ text: '"SYN-PRIVATE-NORMALIZER"' }, { id: 'SYN' }, 'OUTPUT_NOT_OBJECT'],
  [{ text: '```JSON\n{}\n```' }, { id: 'SYN' }, 'OUTPUT_FENCE_INVALID'],
  [{ text: '{"SYN-PRIVATE-NORMALIZER":' }, { id: 'SYN' }, 'OUTPUT_JSON_INVALID'],
  [{ text: '{}' }, {}, 'EXECUTION_CONTEXT_INVALID'],
]) {
  const output = executeNormalizer(input, execution, () => ({ all: () => [] }))[0].json;
  assert.deepEqual(output, { gateway_error: { version: 'gateway-failure-v1',
    stage: 'OUTPUT_NORMALIZATION', code: 'OUTPUT_REJECTED', upstream_http_status: null, detail_code: detail } });
  const terminal = nodes.get('Respond Gateway Success');
  const evaluate = expression => new Function('$json', `return (${expression.slice(3, -2)});`)(output);
  assert.equal(evaluate(terminal.parameters.options.responseCode), 500);
  assert.deepEqual(evaluate(terminal.parameters.responseBody), output);
  assert(!JSON.stringify(output).includes('SYN-PRIVATE-NORMALIZER'));
}
assert.equal(
  normalizer.parameters.jsCode.includes("Object.getPrototypeOf(parsed)"),
  false,
  "n8n Code-node values can cross a sandbox realm, so prototype identity must not be used",
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
    assert.deepEqual(rows[0].json, { gateway_error: { version: "gateway-failure-v1", stage, code,
      upstream_http_status: expectedStatus,
      ...(stage === "OUTPUT_NORMALIZATION" ? { detail_code: "NORMALIZER_EXCEPTION" } : {}),
    } });
    assert(!JSON.stringify(rows).includes(canary));
  }
  for (const field of ["status", "statusCode", "httpCode"]) {
    for (const status of [403, 429, 502]) {
      const diagnostic = sanitize({ error: { [field]: status, message: canary } })[0].json.gateway_error;
      assert.equal(diagnostic.upstream_http_status, stage === "MODEL_EXECUTION" ? status : null);
    }
  }
}
for (const [suffix, allowed] of [["Success", true], ["Failure", false]]) {
  const response = nodes.get(`Respond Gateway ${suffix}`);
  assert.equal(response.type, "n8n-nodes-base.respondToWebhook");
  assert.equal(response.parameters.responseBody, gatewayResponseExpression(allowed, "body"));
  assert.equal(response.parameters.options.responseCode, gatewayResponseExpression(allowed, "status"));
  assert(response.parameters.options.responseHeaders.entries.some(row => row.name === "Cache-Control" && row.value === "no-store"));
}
// Execute both former generic-500 counterexamples through their error edges.
// Neither path can reach/retry the provider after the failure.
for (const [source, failingCall] of [
  ["Validate Gateway Request", () => executeValidation({ body: { ...validBody, arbitrary_prompt: canary } })],
  ["Normalize Gateway Response", () => executeNormalizer({ get text() { throw new Error(canary); } }, {}, () => {})],
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
