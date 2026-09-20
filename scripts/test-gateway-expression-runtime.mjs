import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import { gatewayFailureDetails } from "./gateway-failure-details.mjs";

// Native Function/vm execution skips n8n's AST transform. Use the exact
// Tournament version required by n8n 2.33.7's n8n-workflow 2.33.2 package.
const require = createRequire(new URL("./n8n-expression-test-runtime/package.json", import.meta.url));
const { Tournament } = require("@n8n/tournament");
assert.equal(require("@n8n/tournament/package.json").version, "1.9.0");
const evaluator = new Tournament(error => { throw error; });
const workflow = JSON.parse(fs.readFileSync("workflows/pai-loop-13-claude-extraction-gateway.json", "utf8"));
const fallback = { gateway_error: { version: "gateway-failure-v1", stage: "OUTPUT_NORMALIZATION",
  code: "OUTPUT_REJECTED", upstream_http_status: null, detail_code: "TERMINAL_GUARD_REJECTED" } };
const canary = "SYN-PRIVATE-EXPRESSION-INPUT";
const valid = { id: "pai_claude_SYN_runtime", status: "completed", model: "claude-sonnet-5",
  output_text: '{"summary":"SYN valid output"}', usage: { input_tokens: 1, output_tokens: 2, total_tokens: 3 } };
const failures = [
  { gateway_error: { version: "gateway-failure-v1", stage: "INPUT_VALIDATION", code: "REQUEST_REJECTED", upstream_http_status: null } },
  { gateway_error: { version: "gateway-failure-v1", stage: "MODEL_EXECUTION", code: "MODEL_EXECUTION_FAILED", upstream_http_status: 429 } },
  fallback,
];
const unsafe = [{ synthetic_probe: "SYN-GATEWAY-INVALID-0908" }, { text: canary },
  { system_prompt: canary, user_prompt: canary, max_output_tokens: 20000 },
  { error: { message: canary, status: 429 } },
  { ...valid, extra: canary }, { ...valid, output_text: "not JSON" },
  { ...valid, output_text: "[]" }, { ...valid, output_text: "x".repeat(500001) },
  { ...valid, usage: { input_tokens: true } }, { ...valid, usage: { input_tokens: -1 } },
  { ...valid, usage: { input_tokens: 10000001 } }, { ...valid, usage: { secret: canary } },
  { gateway_error: { ...failures[0].gateway_error, message: canary } }];
let checked = 0;
for (const [name, successAllowed] of [["Respond Gateway Success", true], ["Respond Gateway Failure", false]]) {
  const node = workflow.nodes.find(item => item.name === name);
  const expressions = { status: node.parameters.options.responseCode, body: node.parameters.responseBody };
  const evaluate = input => Object.fromEntries(Object.entries(expressions).map(([field, expression]) =>
    [field, evaluator.execute(expression.slice(1), { $json: input, Object, Array, Number, JSON })]));
  // This was valid native JavaScript but both live response parameters became
  // empty when n8n swallowed this non-SyntaxError from its expression engine.
  for (const expression of Object.values(expressions)) {
    assert.throws(() => evaluator.execute(expression.slice(1).replace("catch (ignored)", "catch"),
      { $json: failures[0], Object, Array, Number, JSON }), /null does not match type Pattern/);
  }
  for (const input of [...unsafe, ...failures, valid,
    ...[{}, { input_tokens: null, output_tokens: null, total_tokens: null }, { input_tokens: 0 }]
      .map(usage => ({ ...valid, usage }))]) {
    const result = evaluate(input);
    const isValid = input.status === "completed" && !unsafe.includes(input);
    const expected = successAllowed && isValid
      ? { status: 200, body: { ...input, usage: { input_tokens: input.usage.input_tokens ?? null,
        output_tokens: input.usage.output_tokens ?? null, total_tokens: input.usage.total_tokens ?? null } } }
      : { status: 500, body: failures.includes(input) && (!successAllowed || input.gateway_error.stage === "MODEL_EXECUTION") ? input : fallback };
    assert.deepEqual(result, expected, `${name} must preserve the bounded terminal response`);
    assert.notEqual(result.body, input);
    assert(!JSON.stringify(result).includes(canary));
    checked += 1;
  }
  for (const detail of ["OUTPUT_EMPTY", "OUTPUT_TYPE_INVALID", "OUTPUT_TOO_LARGE",
    "OUTPUT_FENCE_INVALID", "OUTPUT_JSON_INVALID", "OUTPUT_NOT_OBJECT",
    "EXECUTION_CONTEXT_INVALID", "NORMALIZER_EXCEPTION", "TERMINAL_GUARD_REJECTED"]) {
    const input = { gateway_error: { ...fallback.gateway_error, detail_code: detail } };
    const result = evaluate(input);
    assert.deepEqual(result, { status: 500, body: input });
    assert.notEqual(result.body.gateway_error, input.gateway_error);
    checked += 1;
  }
  for (const stage of ["INPUT_VALIDATION", "MODEL_EXECUTION"]) {
    for (const detail_code of gatewayFailureDetails()[stage]) {
      const input = { gateway_error: { version: "gateway-failure-v1", stage,
        code: stage === "INPUT_VALIDATION" ? "REQUEST_REJECTED" : "MODEL_EXECUTION_FAILED",
        upstream_http_status: detail_code === "MODEL_HTTP_ERROR" ? 429 : null, detail_code } };
      const result = evaluate(input);
      assert.deepEqual(result, { status: 500, body: successAllowed && stage === "INPUT_VALIDATION" ? fallback : input });
      assert(!JSON.stringify(result).includes(canary));
      checked += 1;
    }
  }
  for (const input of [
    { gateway_error: { ...fallback.gateway_error, detail_code: canary } },
    { gateway_error: { ...fallback.gateway_error, detail_code: 1 } },
    { gateway_error: { ...failures[1].gateway_error, detail_code: "OUTPUT_EMPTY" } },
    { gateway_error: { ...fallback.gateway_error, message: canary } },
  ]) {
    const result = evaluate(input);
    assert.deepEqual(result, { status: 500, body: fallback });
    assert(!JSON.stringify(result).includes(canary));
    checked += 1;
  }
  for (const [stop_reason, detail_code] of [["end_turn", "OUTPUT_JSON_INVALID"],
    ["max_tokens", "NATIVE_STOP_MAX_TOKENS"], ["refusal", "NATIVE_STOP_REFUSAL"], ["tool_use", "NATIVE_STOP_UNSUPPORTED"]]) {
    const input = { gateway_error: { ...fallback.gateway_error, stop_reason, detail_code,
      usage: { input_tokens: 12, output_tokens: 6, total_tokens: 18 } } };
    assert.deepEqual(evaluate(input), { status: 500, body: input });
    checked += 1;
    for (const patch of [{ stop_reason: canary }, { usage: { input_tokens: true, output_tokens: 6, total_tokens: 7 } },
      { usage: { input_tokens: 12, output_tokens: 6, total_tokens: 99 } }, { usage: { private: canary } },
      { stop_details: { explanation: canary } }, { detail_code: null }]) {
      const result = evaluate({ gateway_error: { ...input.gateway_error, ...patch } });
      assert.deepEqual(result, { status: 500, body: fallback });
      assert(!JSON.stringify(result).includes(canary)); checked += 1;
    }
  }
}
console.log(`Gateway Tournament 1.9.0: ${checked} terminal cases and all four legacy-expression failures verified`);
