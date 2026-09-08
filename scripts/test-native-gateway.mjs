import assert from "node:assert/strict";
import fs from "node:fs";
import { nativeGatewaySchema } from "./native-gateway-schema.mjs";
import { normalizeNativeGatewayResponse } from "./native-gateway-response.mjs";
import { assertNativeGatewayWorkflow, nativeNodeName } from "./native-gateway-contract.mjs";
import { guardGatewayResponse } from "./gateway-response-contract.mjs";

const workflow = JSON.parse(fs.readFileSync("workflows/pai-loop-13-claude-extraction-gateway.json", "utf8"));
assertNativeGatewayWorkflow(workflow);
for (const node of workflow.nodes) assert.equal(node.credentials, undefined);
const nodes = new Map(workflow.nodes.map(node => [node.name, node]));
const schema = { type: "object", additionalProperties: false, properties: {
  summary: { type: "string", maxLength: 1000 }, evidence: { type: "array", items: { $ref: "#/$defs/EvidenceAnchor" } },
  ambiguity: { anyOf: [{ type: "string" }, { type: "null" }] },
}, required: ["summary", "evidence", "ambiguity"], $defs: { EvidenceAnchor: {
  type: "object", additionalProperties: false, properties: {
    attachment_id: { type: "string" }, page: { anyOf: [{ type: "integer", minimum: 1 }, { type: "null" }] },
    section: { anyOf: [{ type: "string" }, { type: "null" }] }, quote: { type: "string", maxLength: 500 },
    confidence: { type: "number", minimum: 0, maximum: 1 },
  }, required: ["attachment_id", "page", "section", "quote", "confidence"],
} } };
const originalBytes = JSON.stringify(schema);
const projection = nativeGatewaySchema(schema, "project");
assert.deepEqual(projection.counts, { unique: { unions: 1, optional: 2 }, expanded: { unions: 1, optional: 2 } });
assert.deepEqual(projection.schema.$defs.EvidenceAnchor.required, ["attachment_id", "quote", "confidence"]);
assert.match(projection.schema.$defs.EvidenceAnchor.properties.page.description, /minimum=1/);
assert.deepEqual(projection.schema.properties.ambiguity, schema.properties.ambiguity);
assert.equal(JSON.stringify(schema), originalBytes);
const body = { model: "claude-sonnet-5", service_tier: "default", store: false, max_output_tokens: 20000,
  input: [{ role: "system", content: [{ type: "input_text", text: "You extract procurement requirements as evidence only. Never decide a bid." }] },
    { role: "user", content: [{ type: "input_text", text: 'Allowed attachment IDs: ["SYN-A1"]\n\nSOURCE:\nSYN source only.' }] }],
  text: { format: { type: "json_schema", name: "pai_loop_requirements", strict: true, schema } },
};
const executeValidation = new Function("$json", "$input", nodes.get("Validate Gateway Request").parameters.jsCode);
const validate = value => executeValidation({ body: value }, { all: () => [{}] });
const prepared = validate(body)[0].json;
assert.deepEqual(prepared.original_schema, schema);
const request = prepared.provider_request;
assert.deepEqual(Object.keys(request).sort(), ["model", "max_tokens", "system", "messages", "thinking", "output_config", "stream"].sort());
assert.equal(request.model, "claude-sonnet-5"); assert.equal(request.max_tokens, 20000);
assert.deepEqual(request.thinking, { type: "adaptive" }); assert.equal(request.output_config.effort, "medium");
assert.equal(request.output_config.format.type, "json_schema"); assert.equal(request.stream, false);
assert.equal(request.messages.length, 1); assert(request.messages[0].content.includes(JSON.stringify(schema)));
assert.match(request.messages[0].content, /Only EvidenceAnchor.page and EvidenceAnchor.section/);
assert(!("temperature" in request) && !("store" in request) && !("service_tier" in request));
for (const patch of [{ model: "SYN-invalid" }, { max_output_tokens: 20001 }, { store: true },
  { extra: "SYN-private" }, { input: [] }, { text: { format: { ...body.text.format, strict: false } } }]) assert.throws(() => validate({ ...body, ...patch }));
assert.throws(() => executeValidation({ body }, { all: () => [{}, {}] }));
const corrective = structuredClone(body);
corrective.input[1].content[0].text = "FINAL CORRECTIVE RETRY.\n" + corrective.input[1].content[0].text;
assert.equal(validate(corrective).length, 1);
const canary = "SYN-PRIVATE-PROVIDER-THINKING-OR-ID";
const extracted = { summary: "SYN ``` literal", evidence: [{ attachment_id: "SYN-A1", quote: "SYN source only.", confidence: 1 }], ambiguity: null };
const response = text => ({ statusCode: 200, body: { type: "message", id: canary, role: "assistant", model: "claude-sonnet-5",
  stop_reason: "end_turn", usage: { input_tokens: 3, cache_creation_input_tokens: 4, cache_read_input_tokens: 5, output_tokens: 6, thinking_tokens: 999 },
  content: [{ type: "thinking", thinking: canary, signature: canary }, { type: "text", text }],
} });
const executeNormalizer = new Function("$json", "$execution", "$", nodes.get("Normalize Gateway Response").parameters.jsCode);
const normalize = value => executeNormalizer(value, { id: "SYN-execution" }, name => {
  assert.equal(name, "Validate Gateway Request"); return { first: () => ({ json: prepared }) };
})[0].json;
const complete = normalize(response(JSON.stringify(extracted)));
assert.deepEqual(JSON.parse(complete.output_text), { ...extracted, evidence: [{ ...extracted.evidence[0], page: null, section: null }] });
assert.deepEqual(complete.usage, { input_tokens: 12, output_tokens: 6, total_tokens: 18 });
const unreportedCache = response(JSON.stringify(extracted));
delete unreportedCache.body.usage.cache_read_input_tokens;
assert.deepEqual(normalize(unreportedCache).usage, { input_tokens: null, output_tokens: 6, total_tokens: null });
assert(!JSON.stringify(complete).includes(canary));
assert.equal(guardGatewayResponse(complete, true).status, 200);
const explicit = structuredClone(extracted); explicit.evidence[0].page = 2; explicit.evidence[0].section = "SYN section";
assert.deepEqual(JSON.parse(normalize(response(JSON.stringify(explicit))).output_text), explicit);
for (const patch of [value => delete value.summary, value => delete value.ambiguity,
  value => delete value.evidence[0].quote, value => value.evidence[0].page = "2",
  value => value.evidence[0].extra = canary]) {
  const value = structuredClone(extracted); patch(value);
  assert.equal(normalize(response(JSON.stringify(value))).gateway_error.detail_code, "NATIVE_SCHEMA_DECODE_INVALID");
}
for (const [text, detail] of [[" ", "OUTPUT_EMPTY"], ["{}", "NATIVE_SCHEMA_DECODE_INVALID"],
  ["[]", "OUTPUT_NOT_OBJECT"], ["```json\n{}\n```", "OUTPUT_JSON_INVALID"],
  ["{" + canary, "OUTPUT_JSON_INVALID"], ["x".repeat(500001), "OUTPUT_TOO_LARGE"]]) {
  const failure = normalize(response(text)); assert.equal(failure.gateway_error.detail_code, detail);
  for (const lane of [true, false]) assert.deepEqual(guardGatewayResponse(failure, lane), { status: 500, body: failure });
  assert(!JSON.stringify(failure).includes(canary));
}
for (const [stop, detail] of [["max_tokens", "NATIVE_STOP_MAX_TOKENS"], ["refusal", "NATIVE_STOP_REFUSAL"],
  ["tool_use", "NATIVE_STOP_UNSUPPORTED"], ["pause_turn", "NATIVE_STOP_UNSUPPORTED"],
  ["stop_sequence", "NATIVE_STOP_UNSUPPORTED"], ["model_context_window_exceeded", "NATIVE_STOP_UNSUPPORTED"]]) {
  const input = response(canary); input.body.stop_reason = stop;
  const failure = normalize(input); assert.equal(failure.gateway_error.detail_code, detail);
  assert.equal(failure.gateway_error.stop_reason, stop); assert.deepEqual(failure.gateway_error.usage, complete.usage);
  for (const lane of [true, false]) assert.deepEqual(guardGatewayResponse(failure, lane), { status: 500, body: failure });
  assert(!JSON.stringify(failure).includes(canary));
}
for (const patch of [r => r.body.content = [], r => r.body.content.push({ type: "text", text: "{}" }),
  r => r.body.content.push({ type: "tool_use", input: canary })]) {
  const input = response(JSON.stringify(extracted)); patch(input);
  assert.equal(normalize(input).gateway_error.detail_code, "NATIVE_CONTENT_INVALID");
}
for (const statusCode of [400, 429, 500, 529, 302]) {
  const failure = normalize({ statusCode, body: { error: canary } });
  assert.equal(failure.gateway_error.stage, "MODEL_EXECUTION");
  assert.equal(failure.gateway_error.upstream_http_status, statusCode >= 400 ? statusCode : null);
  assert.deepEqual(guardGatewayResponse(failure, true), { status: 500, body: failure });
}
for (const mutate of [s => s.properties.summary.pattern = ".*", s => s.properties.summary = {},
  s => s.properties.summary = { $ref: "https://invalid.example/schema" },
  s => s.properties.summary = { type: ["string", "null"] },
  s => s.$defs.Unused = { $ref: "#/$defs/Unused" },
  s => s.properties.ambiguity.anyOf.push({ type: "boolean" })]) {
  const invalid = structuredClone(schema); mutate(invalid); assert.throws(() => nativeGatewaySchema(invalid, "project"));
}
const optionalOverflow = structuredClone(schema);
for (let i = 0; i < 25; i++) optionalOverflow.properties[`syn_${i}`] = { type: "string" };
assert.throws(() => nativeGatewaySchema(optionalOverflow, "project"));
const prototype = JSON.parse('{"summary":"SYN","evidence":[],"ambiguity":null,"__proto__":{"x":1}}');
assert.throws(() => nativeGatewaySchema(schema, "decode", prototype));
for (const name of ["Input", "Model", "Output"]) {
  const node = nodes.get(`Sanitize Gateway ${name} Failure`);
  const sanitize = new Function("$json", node.parameters.jsCode);
  for (const error of [canary, null, { message: canary, stack: canary, headers: { authorization: canary }, status: 429 },
    { status: "429 " + canary }, { status: true }, { status: 399 }, { status: 600 }, { status: 429, statusCode: 500 }]) {
    const safe = sanitize({ error, input: canary, body: canary })[0].json;
    assert(!JSON.stringify(safe).includes(canary)); assert.equal(guardGatewayResponse(safe, false).status, 500);
    assert.equal(safe.gateway_error.upstream_http_status, name === "Model" && error?.status === 429 && !error.statusCode ? 429 : null);
  }
  for (const field of ["status", "statusCode", "httpCode"]) for (const status of [403, 429, 502]) {
    const safe = sanitize({ error: { [field]: status, message: canary } })[0].json;
    assert.equal(safe.gateway_error.upstream_http_status, name === "Model" ? status : null);
  }
}
assert.equal(guardGatewayResponse(response(canary), true).status, 500);
assert(!JSON.stringify(guardGatewayResponse(response(canary), true)).includes(canary));
assert.equal(workflow.nodes.filter(node => node.type === "n8n-nodes-base.httpRequest").length, 1);
assert.equal(workflow.connections[nativeNodeName].main[0][0].node, "Normalize Gateway Response");
if (process.argv.includes("--schema-stdin")) {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const result = nativeGatewaySchema(input.schema, "project");
  const decoded = nativeGatewaySchema(input.schema, "decode", input.output);
  process.stdout.write(JSON.stringify({ counts: result.counts, decoded, schema: result.schema }));
} else console.log("Native gateway: single-call request, two-field projection, strict response, stop/usage and failure isolation verified");
