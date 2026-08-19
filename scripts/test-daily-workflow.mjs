import assert from "node:assert/strict";
import fs from "node:fs/promises";

const daily = JSON.parse(await fs.readFile("workflows/pai-loop-10-daily-opportunity-briefing.json", "utf8"));
const continuation = JSON.parse(await fs.readFile("workflows/pai-loop-11-analysis-backfill.json", "utf8"));
const manifest = JSON.parse(await fs.readFile("manifest.json", "utf8"));
const nodes = new Map(daily.nodes.map((item) => [item.name, item]));
const continuationNodes = new Map(continuation.nodes.map((item) => [item.name, item]));

function executeCode(map, name, input = {}, globals = {}) {
  const target = map.get(name);
  assert(target && target.type === "n8n-nodes-base.code", `missing Code node ${name}`);
  const blocked = new Proxy({}, { get(_target, key) { throw new Error(`blocked global ${String(key)}`); } });
  // eslint-disable-next-line no-new-func
  const run = new Function("$json", "$env", "$node", "$now", "$input", target.parameters.jsCode);
  const result = run(input, globals.env ?? blocked, globals.node ?? blocked, globals.now ?? blocked, globals.input ?? blocked);
  const rows = Array.isArray(result) ? result : [result];
  assert(rows.every((item) => item && typeof item === "object" && "json" in item));
  return rows.map((item) => item.json);
}

function one(map, name, input = {}, globals = {}) {
  const rows = executeCode(map, name, input, globals);
  assert.equal(rows.length, 1);
  return rows[0];
}

const runtime = one(nodes, "Scheduled Runtime Gates", {}, { env: {} }).runtime;
assert.equal(runtime.executionMode, "scheduled-live");
assert.equal(runtime.maxAnalysisBatchNotices, 3);
assert.equal(runtime.maxDailyNewNotices, 3000);
assert.equal(runtime.maxBacklogRetryNotices, 12);
assert.equal(runtime.maxAttachmentsPerNotice, 1);
assert.equal(runtime.ppsPageSize, 999);
assert.equal(runtime.ppsMaxPages, 3);
assert.equal(runtime.useProfileKeywords, true);
assert.equal(runtime.collectionWindowDays, 8);
assert.deepEqual(runtime.collectionKeywords, ["교육", "컨설팅", "연수", "포럼", "위탁 운영"]);
assert.match(
  nodes.get("Refresh PPS Notices Behind Gate").parameters.body,
  /collectionWindowDays - 1/,
);
assert.match(
  JSON.stringify(nodes.get("Refresh PPS Notices Behind Gate").parameters.headerParameters),
  /daily-v4-window8/,
);
assert.equal(one(nodes, "Scheduled Runtime Gates", {}, { env: { PAI_LOOP_EMERGENCY_DISABLE: "true" } }).runtime.dailyLiveEnabled, false);

const httpNodes = daily.nodes.filter((item) => item.type === "n8n-nodes-base.httpRequest");
assert.equal(httpNodes.length, 9);
for (const item of [...httpNodes, ...continuation.nodes.filter((node) => node.type === "n8n-nodes-base.httpRequest")]) {
  assert.equal(item.parameters.authentication, "genericCredentialType");
  assert.equal(item.parameters.genericAuthType, "httpHeaderAuth");
  assert.equal(item.credentials, undefined);
}
assert.equal(nodes.get("Process Daily Chunks Serially").type, "n8n-nodes-base.splitInBatches");
assert.equal(nodes.get("Process Daily Chunks Serially").parameters.batchSize, 1);
assert.equal(continuationNodes.get("Process Continuation Chunks Serially").parameters.batchSize, 1);
assert.match(nodes.get("Analyze Evaluate and Snapshot PPS Notices").parameters.body, /operation_id:/);
assert.match(nodes.get("Analyze Evaluate and Snapshot PPS Notices").parameters.body, /segment_id:/);
assert.match(nodes.get("Analyze Evaluate and Snapshot PPS Notices").parameters.body, /chunk_index:/);
assert.match(nodes.get("Finalize Daily Analysis Segment").parameters.body, /segment_id:/);
assert.match(nodes.get("Reserve or Resume Daily Analysis Operation").parameters.body, /analysisBatch\.noticeKeys/);
assert.match(nodes.get("Reserve or Resume Daily Analysis Operation").parameters.body, /refresh_notice_keys: \$json\.analysisBatch\.updatedNoticeKeys/);
assert.match(nodes.get("Reserve or Resume Daily Analysis Operation").parameters.body, /retry_notice_keys: \$json\.analysisBatch\.retryableBacklogKeys/);
assert.match(nodes.get("Reserve or Resume Daily Analysis Operation").parameters.body, /retry_epoch:/);
assert.match(nodes.get("Reserve or Resume Daily Analysis Operation").parameters.body, /request_token: 'w10:' \+ \$execution\.id/);
assert.match(nodes.get("Reserve or Resume Daily Analysis Operation").parameters.body, /max_total: 3012/);
assert.match(nodes.get("Reserve or Resume Daily Analysis Operation").parameters.body, /max_continuations: 128/);
assert.match(continuationNodes.get("Analyze One Bounded Chunk").parameters.body, /segment_id:/);
assert.match(continuationNodes.get("Reserve or Resume Backfill Plan").parameters.body, /request_token: 'w11:' \+ \$execution\.id/);
assert.match(continuationNodes.get("Finalize Backfill Audit").parameters.body, /segment_id:/);
for (const chunkNode of [
  nodes.get("Analyze Evaluate and Snapshot PPS Notices"),
  continuationNodes.get("Analyze One Bounded Chunk"),
]) {
  assert.equal(chunkNode.retryOnFail, true);
  assert.equal(chunkNode.maxTries, 2);
  assert.ok(chunkNode.waitBetweenTries >= 1500 && chunkNode.waitBetweenTries <= 3000);
}

const targets = (workflow, name, lane = 0) => (workflow.connections?.[name]?.main?.[lane] ?? []).map((item) => item.node);
assert.deepEqual(targets(daily, "Expand Daily Three-Notice Chunks"), ["Process Daily Chunks Serially"]);
assert.deepEqual(targets(daily, "Process Daily Chunks Serially", 1), ["Analyze Evaluate and Snapshot PPS Notices"]);
assert.deepEqual(targets(daily, "Validate Batch Analysis Contract"), ["Process Daily Chunks Serially"]);
assert.deepEqual(targets(daily, "Process Daily Chunks Serially", 0), ["Verify Batch Analysis Aggregate Invariants"]);
assert.deepEqual(targets(continuation, "Expand Bounded Three-Notice Chunks"), ["Process Continuation Chunks Serially"]);
assert.deepEqual(targets(continuation, "Validate Chunk Result"), ["Process Continuation Chunks Serially"]);

const ppsResponse = {
  job_id: "pps-test", source: "PPS", mode: "live", status: "COMPLETED",
  window: { from: "2026-08-12", to: "2026-08-19" }, dry_run: false,
  keywords_used: ["교육", "컨설팅", "연수", "포럼", "위탁 운영", ...Array.from({ length: 24 }, (_, index) => `부서-${index}`)],
  provider_queries: 29, department_coverage_count: 24, api_calls: 29, fetched: 7,
  matched: 6, created: 2, updated: 2, duplicates: 2, quarantined: 0,
  manifests_created: 4, manifests_reused: 2, attachments_discovered: 4,
  notice_keys: ["new-a", "shared", "updated-a", "unchanged-duplicate"],
  created_notice_keys: ["new-a", "shared"],
  updated_notice_keys: ["updated-a", "shared"],
  warnings: [],
};
const ingestion = one(nodes, "Validate PPS Ingestion Contract", ppsResponse, {
  node: { "Scheduled Runtime Gates": { json: { runtime } } },
}).ingestion;
assert.deepEqual(ingestion.createdNoticeKeys, ["new-a", "shared"]);
assert.deepEqual(ingestion.updatedNoticeKeys, ["updated-a", "shared"]);
assert.throws(() => one(nodes, "Validate PPS Ingestion Contract", {
  ...ppsResponse,
  window: { from: "2026-08-13", to: "2026-08-19" },
}, {
  node: { "Scheduled Runtime Gates": { json: { runtime } } },
}), /exactly eight calendar days/);

const partialIngestion = one(nodes, "Validate PPS Ingestion Contract", {
  ...ppsResponse,
  status: "PARTIAL",
  warnings: ["max_pages 제한에서 수집을 중단했습니다. 다음 실행에서 기간을 더 좁히세요."],
}, {
  node: { "Scheduled Runtime Gates": { json: { runtime } } },
}).ingestion;
assert.equal(partialIngestion.status, "PARTIAL");
assert.throws(() => one(nodes, "Validate PPS Ingestion Contract", {
  ...ppsResponse,
  status: "COMPLETED",
  warnings: ["max_pages 제한에서 수집을 중단했습니다. 다음 실행에서 기간을 더 좁히세요."],
}, {
  node: { "Scheduled Runtime Gates": { json: { runtime } } },
}), /page-limited PPS ingestion must be PARTIAL/);

const backlogNoticeKeys = Array.from({ length: 14 }, (_, index) => `backlog-${index + 1}`);
const neverAttemptedNoticeKeys = backlogNoticeKeys.slice(0, 7);
const retryableNoticeKeys = backlogNoticeKeys.slice(7);
const rootPlan = one(nodes, "Build Bounded Batch Analysis Plan", { awardRefresh: { status: "COMPLETED" } }, {
  node: {
    "Scheduled Runtime Gates": { json: { runtime } },
    "Validate PPS Ingestion Contract": { json: { ingestion } },
    "Fetch Award Candidates from Seven-Day Briefing": { json: { analysis_queue: { policy: "NEVER_ATTEMPTED_THEN_OLDEST_RETRY", pending_total: 14, notice_keys: backlogNoticeKeys, never_attempted_notice_keys: neverAttemptedNoticeKeys, retryable_notice_keys: retryableNoticeKeys } } },
  },
});
assert.deepEqual(rootPlan.analysisBatch.newNoticeKeys, ["new-a", "shared", "updated-a"]);
assert.deepEqual(rootPlan.analysisBatch.createdNoticeKeys, ["new-a", "shared"]);
assert.deepEqual(rootPlan.analysisBatch.updatedNoticeKeys, ["updated-a", "shared"]);
assert.deepEqual(rootPlan.analysisBatch.backlogKeys, backlogNoticeKeys.slice(0, 12));
assert.deepEqual(rootPlan.analysisBatch.neverAttemptedBacklogKeys, neverAttemptedNoticeKeys);
assert.deepEqual(rootPlan.analysisBatch.retryableBacklogKeys, retryableNoticeKeys.slice(0, 5));
assert.equal(rootPlan.analysisBatch.retryEpoch, "2026-08-19");
assert.equal(rootPlan.analysisBatch.noticeKeys.includes("unchanged-duplicate"), false);
assert.deepEqual(rootPlan.analysisBatch.noticeKeys.slice(0, 3), ["new-a", "shared", "updated-a"]);
assert.deepEqual(rootPlan.analysisBatch.noticeKeys.slice(-12), backlogNoticeKeys.slice(0, 12));

assert.throws(() => one(nodes, "Build Bounded Batch Analysis Plan", {}, {
  node: {
    "Scheduled Runtime Gates": { json: { runtime } },
    "Validate PPS Ingestion Contract": { json: { ingestion } },
    "Fetch Award Candidates from Seven-Day Briefing": { json: { analysis_queue: { policy: "NEVER_ATTEMPTED_THEN_OLDEST_RETRY", pending_total: 2, notice_keys: ["backlog-1", "backlog-2"], never_attempted_notice_keys: ["backlog-2"], retryable_notice_keys: ["backlog-1"] } } },
  },
}), /ordered, disjoint, and exactly cover/);

assert.throws(() => one(nodes, "Build Bounded Batch Analysis Plan", {}, {
  node: {
    "Scheduled Runtime Gates": { json: { runtime } },
    "Validate PPS Ingestion Contract": { json: { ingestion: { ...ingestion, createdNoticeKeys: Array.from({ length: 3001 }, (_, index) => `daily-${index}`), updatedNoticeKeys: [] } } },
    "Fetch Award Candidates from Seven-Day Briefing": { json: {} },
  },
}), /refusing silent truncation/);

const operationResponse = {
  job_id: "11111111-1111-4111-8111-111111111111", status: "RUNNING", queue_name: "DAILY",
  segment_id: "33333333-3333-4333-8333-333333333333",
  dry_run: false, policy: "OPEN_NOT_SELECTED_THEN_COOLED_RETRY", chunk_size: 3,
  planned: 35, attempted: 0, remaining: 35, in_flight: 0, offered: 4,
  continuation_required: true, continuation_round: 1, max_continuations: 128,
  completed: 0, partial: 0, failed: 0, child_jobs: 0,
  notice_keys: ["new-a", "shared", "updated-a", "backlog-1"],
  chunks: [["new-a", "shared", "updated-a"], ["backlog-1"]], chunk_indices: [7, 9], warnings: [], note: "bounded",
};
const operationPlan = one(nodes, "Validate Daily Analysis Operation", operationResponse, {
  node: { "Build Bounded Batch Analysis Plan": { json: rootPlan } },
});
assert.equal(operationPlan.analysisBatch.requested, 4);
assert.equal(operationPlan.analysisOperation.remainingBefore, 35);
const expanded = executeCode(nodes, "Expand Daily Three-Notice Chunks", operationPlan);
assert.deepEqual(expanded.map((item) => item.analysisChunk.chunkIndex), [7, 9]);
assert.deepEqual(expanded.map((item) => item.analysisChunk.segmentId), [operationResponse.segment_id, operationResponse.segment_id]);
assert.deepEqual(expanded.map((item) => item.analysisChunk.noticeKeys.length), [3, 1]);

function responseFor(keys, jobSuffix) {
  return {
    job_id: `22222222-2222-4222-8222-${jobSuffix.padStart(12, "0")}`,
    status: "COMPLETED", dry_run: false, requested: keys.length, processed: keys.length,
    completed: keys.length, skipped: 0, failed: 0, document_materialized: keys.length,
    evaluations_created: keys.length, snapshots_refreshed: keys.length, openai_calls: keys.length,
    enrichment: { requested: keys.length, attempted: keys.length, completed: keys.length, skipped: 0, failed: 0, attachments_discovered: keys.length, attachments_processed: keys.length, openai_calls: keys.length, warnings: [] },
    results: keys.map((key) => ({ notice_key: key, status: "COMPLETED", analysis_state: "ANALYZED", analysis_reason_code: "ANALYZED", analysis_reason: "검증된 분석 snapshot이 생성되었습니다.", warnings: [] })),
    warnings: [],
  };
}
const validatedChunks = operationResponse.chunks.map((keys, index) => one(nodes, "Validate Batch Analysis Contract", responseFor(keys, String(index + 1))).chunkResult);
const correctiveResponse = responseFor(operationResponse.chunks[0], "99");
correctiveResponse.openai_calls = correctiveResponse.requested * 2;
correctiveResponse.enrichment.openai_calls = correctiveResponse.requested * 2;
assert.equal(
  one(nodes, "Validate Batch Analysis Contract", correctiveResponse).chunkResult.openaiCalls,
  correctiveResponse.requested * 2,
);
assert.throws(() => one(nodes, "Validate Batch Analysis Contract", {
  ...correctiveResponse,
  openai_calls: correctiveResponse.requested * 2 + 1,
}), /bounds\/counts invalid/);
const aggregated = one(nodes, "Verify Batch Analysis Aggregate Invariants", {}, {
  node: { "Validate Daily Analysis Operation": { json: operationPlan } },
  input: { all: () => validatedChunks.map((chunkResult) => ({ json: { chunkResult } })) },
});
assert.equal(aggregated.analysisBatch.processed, 4);
assert.equal(aggregated.analysisBatch.chunksExecuted, 2);

const correctiveFinalBatch = structuredClone(aggregated.analysisBatch);
correctiveFinalBatch.openaiCalls = correctiveFinalBatch.requested * 2;
correctiveFinalBatch.enrichment.openaiCalls = correctiveFinalBatch.requested * 2;
assert.equal(
  one(nodes, "Validate End-to-End Analysis Boundary", {
    runtime,
    analysis_batch: correctiveFinalBatch,
  }).analysis_batch.openaiCalls,
  correctiveFinalBatch.requested * 2,
);
assert.throws(() => one(nodes, "Validate End-to-End Analysis Boundary", {
  runtime,
  analysis_batch: {
    ...correctiveFinalBatch,
    openaiCalls: correctiveFinalBatch.requested * 2 + 1,
  },
}), /OpenAI calls exceed/);

const continuationState = one(nodes, "Validate Daily Continuation State", {
  ...operationResponse, segment_id: null, status: "PARTIAL", attempted: 4, remaining: 31, offered: 0, in_flight: 0,
  notice_keys: [], chunks: [], chunk_indices: [],
  continuation_round: 2, child_jobs: 2, completed: 4,
}, { node: { "Verify Batch Analysis Aggregate Invariants": { json: aggregated } } });
assert.equal(continuationState.analysisOperation.remaining, 31);
assert.equal(continuationState.analysisOperation.continuationRequired, true);

assert.equal(continuationNodes.get("Every 15 Minutes Continue Active Queue").parameters.rule.interval[0].expression, "*/15 * * * *");
assert.match(continuationNodes.get("Build Scheduled Continuation Runtime").parameters.jsCode, /queueName: 'ANY'/);
assert.match(continuationNodes.get("Build Scheduled Continuation Runtime").parameters.jsCode, /resumeOnly: true/);
assert.match(continuationNodes.get("Reserve or Resume Backfill Plan").parameters.body, /resume_only:/);
assert.deepEqual(targets(continuation, "Backfill Has Remaining Chunks?", 1), ["No Active or Claimable Continuation"]);
const continuationRuntime = one(continuationNodes, "Build Scheduled Continuation Runtime", {}, { env: {} }).runtime;
const recoveryRuntime = one(continuationNodes, "Build Fail-Closed Backfill Runtime", {}, { env: {} }).runtime;
assert.equal(recoveryRuntime.includeRetryable, true);
assert.equal(recoveryRuntime.executionLimit, 30);
assert.equal(recoveryRuntime.maxTotal, 3000);
const continuationPlan = one(continuationNodes, "Validate Backfill Plan", {
  ...operationResponse, queue_name: "DAILY", offered: 3,
  notice_keys: ["new-a", "shared", "updated-a"],
  chunks: [["new-a", "shared", "updated-a"]], chunk_indices: [11],
}, { node: { "Build Scheduled Continuation Runtime": { json: { runtime: continuationRuntime } } } });
assert.equal(continuationPlan.operation.segmentId, operationResponse.segment_id);
assert.deepEqual(continuationPlan.operation.chunkIndices, [11]);
const leasedNoOffer = one(continuationNodes, "Validate Backfill Plan", {
  ...operationResponse, offered: 0, notice_keys: [], chunks: [], chunk_indices: [],
}, { node: { "Build Scheduled Continuation Runtime": { json: { runtime: continuationRuntime } } } });
assert.equal(leasedNoOffer.operation.offered, 0);
assert.deepEqual(leasedNoOffer.operation.chunks, []);
const noActive = one(continuationNodes, "Validate Backfill Plan", {
  ...operationResponse, job_id: null, segment_id: null, status: "NO_ACTIVE", queue_name: "ANY",
  planned: 0, attempted: 0, remaining: 0, in_flight: 0, offered: 0,
  notice_keys: [], chunks: [], chunk_indices: [], continuation_round: 0,
}, { node: { "Build Scheduled Continuation Runtime": { json: { runtime: continuationRuntime } } } });
assert.equal(noActive.operation.status, "NO_ACTIVE");
const correctiveChunkResponse = responseFor(["backlog-1", "backlog-2", "backlog-3"], "98");
correctiveChunkResponse.openai_calls = 6;
assert.equal(
  one(continuationNodes, "Validate Chunk Result", correctiveChunkResponse).openaiCalls,
  6,
);
assert.throws(() => one(continuationNodes, "Validate Chunk Result", {
  ...correctiveChunkResponse,
  openai_calls: 7,
}), /bounds\/counts invalid/);
const finalContinuation = one(continuationNodes, "Backfill Complete for Operator Review", {
  ...operationResponse, segment_id: null, status: "PARTIAL", attempted: 4, remaining: 31,
  in_flight: 0, offered: 0, notice_keys: [], chunks: [], chunk_indices: [], openai_calls: 4,
  child_jobs: 2,
});
assert.equal(finalContinuation.openaiCalls, 4);
assert.equal(finalContinuation.notificationEvent, "ANALYSIS_OPERATION_PROGRESS");
const continuationManifest = manifest.workflows["pai-loop-11-analysis-backfill"];
const validContinuationPromotion = (
  continuationManifest.publish === false
  && continuationManifest.promotionState === "awaiting-live-e2e"
) || (
  continuationManifest.publish === true
  && continuationManifest.promotionState === "verified-live-e2e"
);
assert.equal(validContinuationPromotion, true);

console.log("Daily created+updated priority, serial chunking, continuation, and no-active contracts passed.");
