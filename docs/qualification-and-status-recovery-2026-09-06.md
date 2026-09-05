# Qualification and analysis status recovery — 2026-09-06

## Statutory qualification conjunction

A complete clause requiring both statutory bidder qualification and no current bidder sanction previously fell through to unmapped ENTITY review. A narrow full-clause grammar now derives two independent mandatory gates using the existing bidder_registration and sanction_clear facts. This applies to the recognized statutory wording across notices and has no notice-name or notice-key binding. Exact original evidence anchors and document identities remain attached to both gates; no source quote is rewritten.

Unknown extra clauses, OR alternatives, ambiguous extractions and future penalties do not take the split path. Missing or stale facts still block the corresponding gate. A combined sanction/insolvency/financial-credit condition cannot pass solely on the sanction fact; financial evidence remains explicitly required. Policy version advances to 2026.09.06-v8; extraction prompt and quantitative record versions are unchanged.

## Public scoring labels

The weighted confirmed-score percentage is labeled company evidence confirmation, independently of source-table validation. A stored public score snapshot hides source/evidence details; the UI now labels this privacy boundary instead of calling the source missing. A computed range containing an unscorable criterion is labeled partially unscored, while the criterion itself stays UNSCORABLE.

## Deployment pause visibility

W11 preserves a fixed ANALYSIS_DEPLOYMENT_GRACE reason when the planner pauses immediately after deployment. An actual no-active response is distinguished as NO_ACTIVE_OPERATION. Arbitrary warnings and notes are not forwarded by this path. The one-minute schedule, queue leases, time budgets, provider-call bounds and deployment stabilization period are unchanged.

Validation includes focused policy/pipeline/public API/frontend tests, a source-preserving two-gate materialization check and end-to-end pipeline checks requiring both facts. All seven required workflow validation commands passed. Full CI and live deployment/workflow state must be verified before declaring completion.
