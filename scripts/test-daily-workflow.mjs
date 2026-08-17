import assert from "node:assert/strict";
import fs from "node:fs/promises";


const workflow = JSON.parse(
  await fs.readFile("workflows/pai-loop-10-daily-opportunity-briefing.json", "utf8"),
);
const nodes = new Map(workflow.nodes.map((node) => [node.name, node]));

function executeCodeNode(name, input = {}) {
  const node = nodes.get(name);
  assert(node, `Missing Code node: ${name}`);
  assert.equal(node.type, "n8n-nodes-base.code", `${name} must be a Code node`);
  // This harness executes only the network-free manual path.  n8n globals not
  // used by that path are fail-closed stubs so an accidental dependency is
  // detected instead of silently mocked.
  const blocked = new Proxy({}, {
    get(_target, property) {
      throw new Error(`Offline harness forbids runtime global access: ${String(property)}`);
    },
  });
  // eslint-disable-next-line no-new-func
  const run = new Function("$json", "$env", "$node", "$now", node.parameters.jsCode);
  const result = run(input, blocked, blocked, blocked);
  assert(Array.isArray(result) && result.length === 1, `${name} must return one item`);
  return result[0].json;
}

let item = executeCodeNode("Force Zero-Call Manual Context");
item = executeCodeNode("Build Seven-Day Offline Fixture", item);
item = executeCodeNode("Normalize Optional Quant and Pricing", item);
item.notices[0].quantitative_estimate = {
  total_max_points: 100,
  estimated_points: null,
  lower_points: 76,
  upper_points: 86,
  evidence_coverage_pct: 90,
  readiness_band: "GREEN",
};
item.notices[0].pricing_intelligence = {
  record_count: 4,
  concentration: {
    top_winner: { winner_name: "가상 수행기관 A", count: 3, share: 0.75 },
    hhi_interpretation: "HIGH",
  },
  prediction: {
    award_rate: {
      status: "MODEL_ESTIMATE",
      center: 87.35,
      range_low: 86.9,
      range_high: 87.8,
      confidence: "LOW",
    },
  },
};
item = executeCodeNode("Build Consolidated Teams Adaptive Card", item);
item = executeCodeNode("Render Score Range and Sample Concentration", item);
item = executeCodeNode("Record One Push Mock Locally", item);
item = executeCodeNode("Validate Complete Dry-Run Contract", item);

assert.equal(item.status, "DRY_RUN_PASSED");
assert.equal(item.executionMode, "manual-fixture");
assert.deepEqual(item.schedule, {
  cron: "0 9 * * *",
  timezone: "Asia/Seoul",
  workflowActive: false,
});
assert.equal(item.retention.days, 7);
assert.equal(item.actualTeamsRequestSent, false);
assert.equal(item.actualPushSent, false);
assert.equal(item.adaptiveCard.type, "AdaptiveCard");
assert.equal(item.adaptiveCard.version, "1.5");
assert.equal(item.briefing.noticeKeys.length, 2);
const facts = item.adaptiveCard.body
  .filter((block) => block.type === "Container")
  .flatMap((block) => block.items.find((child) => child.type === "FactSet")?.facts ?? []);
assert.equal(
  facts.find((fact) => fact.title === "정량 예상")?.value,
  "76~86 / 100점",
);
const concentration = facts.find((fact) => fact.title === "표본 집중도")?.value ?? "";
assert.match(concentration, /3건 \/ 75\.0%/);
assert.match(concentration, /독점 확정 아님/);
console.log("Daily workflow offline E2E passed: 0 HTTP, 0 PPS, 0 OpenAI, 0 Teams calls");
