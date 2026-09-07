import assert from "node:assert/strict";
import fs from "node:fs/promises";

const workflow = JSON.parse(await fs.readFile("workflows/pai-loop-10-daily-opportunity-briefing.json", "utf8"));
const code = workflow.nodes.find((node) => node.name === "Validate Award Refresh Batch").parameters.jsCode;
const run = new Function("$input", "$node", code);
const context = { "Build Bounded Award Refresh Plan": { json: {
  runtime: { synthetic: true }, awardRefresh: { limit: 2, dryRun: false },
} } };
const validate = (...responses) => run({ all: () => responses.map((json) => ({ json })) }, context)[0].json.awardRefresh;
const base = {
  job_id: "SYN-job", notice_key: "PPS-SYN-award", status: "COMPLETED", keyword: "SYN",
  window: { from: "2025-09-08", to: "2026-09-08" }, api_calls: 2, fetched: 0,
  created: 0, updated: 0, duplicates: 0, records: 0, dry_run: false, warnings: [],
  window_error_counts: [],
};
const diagnostic = { phase: "FALLBACK", error_type: "HTTP_ERROR", http_status: 503, provider_code: null, count: 1 };
const partial = { ...base, status: "PARTIAL", warnings: ["SYN incomplete window"], window_error_counts: [diagnostic] };
assert.equal(validate(base).status, "COMPLETED");
assert.equal(validate({ body: base }).status, "COMPLETED");
assert.equal(validate(partial).status, "PARTIAL");
assert.equal(validate(base, partial).completed, 1);
assert.equal(validate(base, partial).status, "PARTIAL");
assert.equal(validate({ ...base, window_error_counts: [{ ...diagnostic, phase: "PRIMARY" }] }).status, "COMPLETED",
  "recovered primary errors do not turn complete coverage into PARTIAL");
const legacy = { ...base }; delete legacy.window_error_counts;
assert.equal(validate(legacy).status, "COMPLETED");
for (const error_type of ["UNKNOWN", "NETWORK_ERROR", "INVALID_JSON", "INVALID_PAYLOAD", "MISSING_RESPONSE",
  "MISSING_HEADER", "MISSING_BODY", "MISSING_TOTAL_COUNT", "INVALID_TOTAL_COUNT", "INVALID_ITEMS", "AWARD_PAGE_INVALID"]) {
  validate({ ...partial, window_error_counts: [{ ...diagnostic, error_type, http_status: null }] });
}
for (const error_type of ["SERVICE_ERROR", "PROVIDER_RESULT_ERROR"]) {
  for (const provider_code of ["0", "00", "10", "99", null]) {
    validate({ ...partial, window_error_counts: [{ ...diagnostic, error_type, http_status: null, provider_code }] });
  }
}
const canary = "SYN-PRIVATE-DO-NOT-EMIT";
const invalid = [
  null, {}, "SYN", [null], [[]], [true], Array(2049).fill(diagnostic),
  [{ ...diagnostic, phase: canary }], [{ ...diagnostic, error_type: canary }],
  [{ ...diagnostic, count: 0 }], [{ ...diagnostic, count: -1 }], [{ ...diagnostic, count: 1.5 }],
  [{ ...diagnostic, count: "1" }], [{ ...diagnostic, count: true }], [{ ...diagnostic, count: Infinity }],
  [{ ...diagnostic, count: Number.MAX_SAFE_INTEGER + 1 }],
  [{ ...diagnostic, http_status: "503" }], [{ ...diagnostic, http_status: 99 }],
  [{ ...diagnostic, http_status: 600 }], [{ ...diagnostic, http_status: 503.1 }],
  [{ ...diagnostic, error_type: "NETWORK_ERROR" }], [{ ...diagnostic, provider_code: "30" }],
  [{ ...diagnostic, error_type: "SERVICE_ERROR", http_status: null, provider_code: canary }],
  [{ ...diagnostic, error_type: "SERVICE_ERROR", http_status: null, provider_code: 30 }],
  [{ ...diagnostic, error_type: "SERVICE_ERROR", http_status: null, provider_code: "100" }],
  [{ ...diagnostic, raw_message: canary }], [{ ...diagnostic, headers: { Authorization: canary } }],
];
for (const value of invalid) {
  assert.throws(() => validate({ ...partial, window_error_counts: value }),
    (error) => /award window error/.test(error.message) && !error.message.includes(canary));
}
for (const key of Object.keys(diagnostic)) {
  const value = { ...diagnostic }; delete value[key];
  assert.throws(() => validate({ ...partial, window_error_counts: [value] }), /award window error/);
}
assert.throws(() => validate({ ...partial, warnings: [] }), /warnings/);
assert.throws(() => validate({ ...base, status: "FAILED" }), /status/);
assert.throws(() => validate({ ...base, dry_run: true }), /dry-run/);
assert.throws(() => validate({ ...base, raw_payload: canary }), /unexpected fields/);
assert.throws(() => validate({ ...base, window: { from: "SYN-invalid-date", to: "2026-09-08" } }), /window invalid/);
assert.equal(JSON.stringify(validate(partial)).includes("window_error_counts"), false,
  "validated diagnostics do not widen the existing emitted summary");
