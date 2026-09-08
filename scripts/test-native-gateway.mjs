import assert from "node:assert/strict";
import fs from "node:fs";
import { nativeGatewaySchema } from "./native-gateway-schema.mjs";
import { normalizeNativeGatewayResponse } from "./native-gateway-response.mjs";
import { assertNativeGatewayWorkflow, nativeNodeName, nativeCanaryWorkflowKeys,
  assertPendingNativeSelection, assertPendingNativeInactive } from "./native-gateway-contract.mjs";
import { guardGatewayResponse } from "./gateway-response-contract.mjs";

const workflow = JSON.parse(fs.readFileSync("workflows/pai-loop-13-claude-extraction-gateway.json", "utf8"));
assertNativeGatewayWorkflow(workflow);
const pendingConfig = { publish: true, contractVersion: "claude-extraction-gateway-2.0-native-json",
  promotionState: "awaiting-native-live-e2e", nativeCanaryState: "awaiting-root-synthetic-schema-probe" };
// The live draft already has nine native nodes: legacy detection cannot protect
// a producer-only deployment. The global pending state must stop every write.
const remoteDrafts = new Map(nativeCanaryWorkflowKeys.map(key => [key, { active: false, nodes: workflow.nodes }]));
for (const selected of [undefined, ...nativeCanaryWorkflowKeys.slice(0, 3)]) {
  let writes = 0;
  assert.throws(() => { assertPendingNativeSelection(pendingConfig, selected); assertPendingNativeInactive(remoteDrafts); writes++; });
  assert.equal(writes, 0);
}
assert.equal(assertPendingNativeSelection(pendingConfig, nativeCanaryWorkflowKeys[3]), true);
assertPendingNativeInactive(remoteDrafts);
for (const key of nativeCanaryWorkflowKeys) for (const active of [true, undefined]) {
  let writes = 0;
  const blocked = new Map(remoteDrafts); blocked.set(key, { active, nodes: workflow.nodes });
  assert.throws(() => { assertPendingNativeSelection(pendingConfig, nativeCanaryWorkflowKeys[3]); assertPendingNativeInactive(blocked); writes++; });
  assert.equal(writes, 0);
}
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
assert.deepEqual(projection.counts, { unique: { unions: 1, optional: 0 }, expanded: { unions: 1, optional: 0 } });
assert.deepEqual(projection.schema.$defs.EvidenceAnchor.required, schema.$defs.EvidenceAnchor.required);
assert.match(projection.schema.$defs.EvidenceAnchor.properties.page.items.description, /minimum=1/);
for (const field of ["page", "section"]) {
  const projected = projection.schema.$defs.EvidenceAnchor.properties[field];
  assert.equal(projected.type, "array"); assert(!Object.hasOwn(projected, "maxItems"));
  assert.equal(projected.items.type, field === "page" ? "integer" : "string");
}
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
assert.match(request.system, /TRUSTED NATIVE TRANSPORT CONVENTION: Only EvidenceAnchor.page and EvidenceAnchor.section/);
assert(request.system.startsWith(body.input[0].content[0].text));
assert.match(request.system, /return \[\] for an explicit null or \[value\]/);
assert(!request.messages[0].content.includes("may be omitted"));
assert(!("temperature" in request) && !("store" in request) && !("service_tier" in request));
for (const patch of [{ model: "SYN-invalid" }, { max_output_tokens: 20001 }, { store: true },
  { extra: "SYN-private" }, { input: [] }, { text: { format: { ...body.text.format, strict: false } } }]) assert.throws(() => validate({ ...body, ...patch }));
assert.throws(() => executeValidation({ body }, { all: () => [{}, {}] }));
const corrective = structuredClone(body);
corrective.input[1].content[0].text = "FINAL CORRECTIVE RETRY.\n" + corrective.input[1].content[0].text;
assert.equal(validate(corrective).length, 1);
for (const [index, cap] of [[0, 12000], [1, 140000]]) {
  const oversized = structuredClone(body);
  oversized.input[index].content[0].text += "x".repeat(cap);
  assert.throws(() => validate(oversized), /oversized/);
}
const systemBoundary = structuredClone(body);
systemBoundary.input[0].content[0].text += "x".repeat(12000 - request.system.length);
assert.equal(validate(systemBoundary)[0].json.provider_request.system.length, 12000);
systemBoundary.input[0].content[0].text += "x";
assert.throws(() => validate(systemBoundary), /combined system prompt/);
const combinedOverflow = structuredClone(body);
combinedOverflow.input[1].content[0].text += "x".repeat(139000);
for (const target of [combinedOverflow.text.format.schema.properties.summary,
  combinedOverflow.text.format.schema.properties.ambiguity,
  combinedOverflow.text.format.schema.$defs.EvidenceAnchor.properties.confidence]) target.description = "S".repeat(12000);
assert.throws(() => validate(combinedOverflow), /combined Claude request/);
const canary = "SYN-PRIVATE-PROVIDER-THINKING-OR-ID";
const extracted = { summary: "SYN ``` literal", evidence: [{ attachment_id: "SYN-A1", page: [], section: [], quote: "SYN source only.", confidence: 1 }], ambiguity: null };
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
const explicit = structuredClone(extracted); explicit.evidence[0].page = [2]; explicit.evidence[0].section = ["SYN section"];
assert.deepEqual(JSON.parse(normalize(response(JSON.stringify(explicit))).output_text),
  { ...explicit, evidence: [{ ...explicit.evidence[0], page: 2, section: "SYN section" }] });
for (const field of ["page", "section"]) {
  for (const invalid of [null, false, 1, "SYN", [null], [[]], [{}], [true], [1, 2],
    ...(field === "page" ? [[1.5], ["2"], [Number.MAX_SAFE_INTEGER + 1]] : [[1], [1.5]])]) {
    const value = structuredClone(extracted); value.evidence[0][field] = invalid;
    const failure = normalize(response(JSON.stringify(value)));
    assert.equal(failure.gateway_error.detail_code, "NATIVE_SCHEMA_DECODE_INVALID");
    assert.equal(guardGatewayResponse(failure, true).status, 500);
  }
  const missing = structuredClone(extracted); delete missing.evidence[0][field];
  assert.equal(normalize(response(JSON.stringify(missing))).gateway_error.detail_code, "NATIVE_SCHEMA_DECODE_INVALID");
}
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
const otherPath = structuredClone(schema);
otherPath.$defs.OtherAnchor = structuredClone(schema.$defs.EvidenceAnchor);
otherPath.properties.other = { $ref: "#/$defs/OtherAnchor" }; otherPath.required.push("other");
const otherOutput = { ...extracted, other: { attachment_id: "SYN-A1", page: 3,
  section: null, quote: "SYN source only.", confidence: 1 } };
assert.deepEqual(nativeGatewaySchema(otherPath, "decode", otherOutput).other, otherOutput.other);
const invalidOther = structuredClone(otherOutput); invalidOther.other.page = [3];
assert.throws(() => nativeGatewaySchema(otherPath, "decode", invalidOther));
const omittedInSchema = structuredClone(schema);
omittedInSchema.$defs.EvidenceAnchor.required = omittedInSchema.$defs.EvidenceAnchor.required.filter(key => key !== "page");
assert.throws(() => nativeGatewaySchema(omittedInSchema, "project"));
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
