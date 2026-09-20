import assert from "node:assert/strict";
import fs from "node:fs";
import { gatewayFailureDetails } from "./gateway-failure-details.mjs";
import { guardGatewayResponse } from "./gateway-response-contract.mjs";

const workflow = JSON.parse(fs.readFileSync("workflows/pai-loop-13-claude-extraction-gateway.json", "utf8"));
const nodes = new Map(workflow.nodes.map(node => [node.name, node]));
const run = (name, json, count = 1) => new Function("$json", "$input", nodes.get(name).parameters.jsCode)(json, { all: () => Array(count).fill({}) });
const canary = "SYN-PRIVATE-EXCEPTION-CONTENT";
const body = { model: "claude-sonnet-5", service_tier: "default", store: false, max_output_tokens: 20000,
  input: [{ role: "system", content: [{ type: "input_text", text: "You extract procurement requirements as evidence only." }] },
    { role: "user", content: [{ type: "input_text", text: "Allowed attachment IDs: SYN-A\n\nSOURCE:\nSYN source" }] }],
  text: { format: { type: "json_schema", name: "pai_loop_requirements", strict: true,
    schema: { type: "object", additionalProperties: false, properties: { summary: { type: "string" } }, required: ["summary"] } } },
};
const checked = new Set();
function rejected(json, expected, count = 1) {
  let failure;
  try { run("Validate Gateway Request", json, count); } catch (error) { failure = error; }
  assert(failure, expected);
  assert.equal(failure.message, expected);
  checked.add(expected);
  // Exact n8n 2.33.7 ExecutionError wrappers; task-runner wrappers may retain
  // the original message. The sanitizer returns neither suffix nor stack.
  for (const message of [failure.message, `${failure.message} [line 42]`, `${failure.message} [line 42, for item 0]`]) {
    for (const error of [message, { message, stack: canary, context: canary }]) {
      const safe = run("Sanitize Gateway Input Failure", { error, body: canary })[0].json;
      assert.equal(safe.gateway_error.detail_code, expected);
      assert.deepEqual(guardGatewayResponse(safe, false), { status: 500, body: safe });
      assert(!JSON.stringify(safe).includes(canary));
    }
  }
}
rejected({ body }, "INPUT_ITEM_COUNT_INVALID", 2);
rejected({ body: [] }, "INPUT_BODY_INVALID");
const cases = [
  [b => b.budget_policy = "SYN-UNKNOWN", "INPUT_BUDGET_POLICY_INVALID"],
  [b => b.budget_policy = "QUANTITATIVE_PROBE_ONCE", "INPUT_PROBE_SCOPE_INVALID"],
  [b => b.extra = canary, "INPUT_FIELDS_INVALID"],
  [b => b.model = canary, "INPUT_MODEL_INVALID"],
  [b => b.store = true, "INPUT_OPTIONS_INVALID"],
  [b => b.max_output_tokens = 20001, "INPUT_TOKEN_LIMIT_INVALID"],
  [b => b.input = [], "INPUT_MESSAGES_INVALID"],
  [b => b.input[0].role = "user", "INPUT_MESSAGE_SHAPE_INVALID"],
  [b => b.input[0].content[0].type = "image", "INPUT_CONTENT_TYPE_INVALID"],
  [b => b.input[0].content[0].text = " ", "INPUT_CONTENT_EMPTY"],
  [b => b.input[0].content[0].text += "\0", "INPUT_CONTENT_NUL"],
  [b => b.input[0].content[0].text += "x".repeat(12000), "INPUT_SYSTEM_TOO_LARGE"],
  [b => b.input[1].content[0].text += "x".repeat(140000), "INPUT_SOURCE_TOO_LARGE"],
  [b => b.input[0].content[0].text = canary, "INPUT_SYSTEM_IDENTITY_INVALID"],
  [b => { b.budget_policy = "LONG_OUTPUT_ONCE"; b.max_output_tokens = 32000;
    b.input[1].content[0].text = "FINAL CORRECTIVE RETRY.\n" + b.input[1].content[0].text; }, "INPUT_CORRECTIVE_POLICY_INVALID"],
  [b => b.input[1].content[0].text = canary, "INPUT_USER_IDENTITY_INVALID"],
  [b => b.text.format.strict = false, "INPUT_FORMAT_INVALID"],
  [b => b.text.format.schema.additionalProperties = true, "INPUT_SCHEMA_INVALID"],
  [b => b.text.format.schema.description = "x".repeat(64000), "INPUT_SCHEMA_TOO_LARGE"],
  [b => b.text.format.schema.properties.summary.pattern = canary, "INPUT_SCHEMA_PROJECTION_REJECTED"],
  [b => b.input[0].content[0].text = b.input[0].content[0].text.padEnd(12000, "x"), "INPUT_PROVIDER_SYSTEM_TOO_LARGE"],
  [b => { b.input[1].content[0].text = b.input[1].content[0].text.padEnd(140000, "x");
    for (let i = 0; i < 3; i++) { b.text.format.schema.properties["syn" + i] = { type: "string", description: "x".repeat(12000) };
      b.text.format.schema.required.push("syn" + i); } }, "INPUT_REQUEST_TOO_LARGE"],
];
for (const [mutate, expected] of cases) { const value = structuredClone(body); mutate(value); rejected({ body: value }, expected); }
for (const error of [canary, { message: canary }, { message: "INPUT_SOURCE_TOO_LARGE " + canary },
  { message: "Error: INPUT_SOURCE_TOO_LARGE" }, { message: { private: canary } }, null,
  { message: `INPUT_SOURCE_TOO_LARGE [line 42] ${canary}` },
  { message: "INPUT_SOURCE_TOO_LARGE [line 42]\n" },
  { message: `INPUT_SOURCE_TOO_LARGE [line ${canary}]` },
  { message: "INPUT_SOURCE_TOO_LARGE [line 42, for item -1]" },
  { message: "INPUT_UNKNOWN [line 42]" }]) {
  const safe = run("Sanitize Gateway Input Failure", { error })[0].json;
  assert.equal(safe.gateway_error.detail_code, "INPUT_VALIDATOR_FAILED");
  assert(!JSON.stringify(safe).includes(canary));
}
checked.add("INPUT_VALIDATOR_FAILED");
assert.deepEqual([...checked].sort(), gatewayFailureDetails().INPUT_VALIDATION.slice().sort());

for (const [error, detail, status] of [
  [{ status: 429, message: canary }, "MODEL_HTTP_ERROR", 429],
  [{ statusCode: 503, httpCode: 503, code: "ETIMEDOUT" }, "MODEL_HTTP_ERROR", 503],
  [{ code: "ETIMEDOUT", message: canary }, "MODEL_TRANSPORT_TIMEOUT", null],
  [{ cause: { code: "ESOCKETTIMEDOUT", message: canary } }, "MODEL_TRANSPORT_TIMEOUT", null],
  [{ code: "ECONNRESET" }, "MODEL_CONNECTION_RESET", null],
  [{ code: "ECONNREFUSED" }, "MODEL_CONNECTION_REFUSED", null],
  [{ code: "ENOTFOUND" }, "MODEL_DNS_FAILURE", null],
  [{ code: "EAI_AGAIN" }, "MODEL_DNS_FAILURE", null],
  [{ status: 429, statusCode: 502 }, "MODEL_TRANSPORT_UNKNOWN", null],
  [{ status: "429", code: "ETIMEDOUT" }, "MODEL_TRANSPORT_UNKNOWN", null],
  [{ code: "ECONNRESET", cause: { code: "ETIMEDOUT" } }, "MODEL_TRANSPORT_UNKNOWN", null],
  [{ message: "ETIMEDOUT " + canary }, "MODEL_TRANSPORT_UNKNOWN", null],
  [{ code: "ECONNABORTED" }, "MODEL_TRANSPORT_UNKNOWN", null],
  [{ code: canary, status: true }, "MODEL_TRANSPORT_UNKNOWN", null],
]) {
  const safe = run("Sanitize Gateway Model Failure", { error, body: canary })[0].json;
  assert.equal(safe.gateway_error.detail_code, detail);
  assert.equal(safe.gateway_error.upstream_http_status, status);
  for (const lane of [true, false]) assert.deepEqual(guardGatewayResponse(safe, lane), { status: 500, body: safe });
  assert(!JSON.stringify(safe).includes(canary));
}
console.log("Gateway input rejection details and observed model failure codes survive without exception prose");
