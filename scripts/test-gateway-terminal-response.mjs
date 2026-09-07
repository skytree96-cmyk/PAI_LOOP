import assert from "node:assert/strict";
import fs from "node:fs";
import { gatewayResponseExpression } from "./gateway-response-contract.mjs";

const workflow = JSON.parse(fs.readFileSync("workflows/pai-loop-13-claude-extraction-gateway.json", "utf8"));
const nodes = new Map(workflow.nodes.map(node => [node.name, node]));
const canary = "SYN-PRIVATE-RUNNER-INPUT";
const fallback = { gateway_error: { version: "gateway-failure-v1", stage: "OUTPUT_NORMALIZATION",
  code: "OUTPUT_REJECTED", upstream_http_status: null } };
const evaluate = (expression, input) => typeof expression === "number" ? expression
  : new Function("$json", `return (${expression.slice(3, -2)});`)(input);
const respond = (name, input) => {
  const node = nodes.get(name);
  return { status: evaluate(node.parameters.options.responseCode, input),
    body: evaluate(node.parameters.responseBody, input) };
};
// Model the official engine's runtime-exception continuation: it forwards the
// original input to main[0], without going through the dedicated error edge.
const destination = workflow.connections["Normalize Gateway Response"].main[0][0].node;
for (const input of [{ text: canary }, { system_prompt: canary, user_prompt: canary, max_output_tokens: 20000 },
  { error: { message: canary, status: 429 } }]) {
  const result = respond(destination, input);
  assert.equal(result.status, 500, "runtime passthrough must never return HTTP200");
  assert.deepEqual(result.body, fallback);
  assert(!JSON.stringify(result).includes(canary));
  assert.notEqual(result.body, input);
}
const valid = { id: "pai_claude_SYN_123", status: "completed", model: "claude-sonnet-5",
  output_text: '{"summary":"SYN valid output"}', usage: { input_tokens: 1, output_tokens: 2, total_tokens: 3 } };
assert.deepEqual(respond(destination, valid), { status: 200, body: valid });
assert.notEqual(respond(destination, valid).body, valid);
assert.notEqual(respond(destination, valid).body.usage, valid.usage);
for (const usage of [{}, { input_tokens: null, output_tokens: null, total_tokens: null }, { input_tokens: 0 }]) {
  const result = respond(destination, { ...valid, usage });
  assert.equal(result.status, 200);
  assert.equal(result.body.usage.output_tokens, null);
}
for (const patch of [{ model: canary }, { status: "failed" }, { id: canary }, { output_text: canary },
  { output_text: "[]" }, { output_text: "x".repeat(500001) }, { extra: canary },
  { usage: { input_tokens: canary } }, { usage: { input_tokens: true } },
  { usage: { input_tokens: -1 } }, { usage: { input_tokens: 10000001 } }, { usage: { secret: canary } }]) {
  const result = respond(destination, { ...valid, ...patch });
  assert.deepEqual(result, { status: 500, body: fallback });
  assert(!JSON.stringify(result).includes(canary));
}
const modelFailure = { gateway_error: { version: "gateway-failure-v1", stage: "MODEL_EXECUTION",
  code: "MODEL_EXECUTION_FAILED", upstream_http_status: 429 } };
assert.deepEqual(respond("Respond Gateway Failure", modelFailure), { status: 500, body: modelFailure });
assert.notEqual(respond("Respond Gateway Failure", modelFailure).body.gateway_error, modelFailure.gateway_error);
for (const input of [{ text: canary }, valid, { ...modelFailure, input: canary },
  { gateway_error: { ...modelFailure.gateway_error, message: canary } },
  { gateway_error: { ...modelFailure.gateway_error, stage: "INPUT_VALIDATION" } },
  { gateway_error: { ...modelFailure.gateway_error, upstream_http_status: "429" } }]) {
  assert.deepEqual(respond("Respond Gateway Failure", input), { status: 500, body: fallback });
}
for (const [suffix, allowed] of [["Success", true], ["Failure", false]]) {
  const node = nodes.get(`Respond Gateway ${suffix}`);
  assert.equal(node.parameters.responseBody, gatewayResponseExpression(allowed, "body"));
  assert.equal(node.parameters.options.responseCode, gatewayResponseExpression(allowed, "status"));
}
console.log("Gateway terminal response guards reject runtime passthrough and preserve bounded success");
