# Bounded dashboard counter projection

Dashboard counters now load only the evaluation and analysis-run fields needed
for current-result selection. Full NoticeVersion source payloads and the existing
manifest, prompt, schema, validator and fingerprint checks remain unchanged.
Historical evaluation explanations and atomic results, analysis input manifests,
and output summaries are not loaded for counter-only batches.

The initial batch that supplies the ten recent notice summaries retains the full
summary loader. Public redaction, authenticated evaluation fields, HOLD conditions,
and evidence counts continue through that existing serializer. Later batches use
an explicit projection whose excluded fields and relationships reject lazy loads.

After selecting the latest current run through the existing source-freshness
rules, the dashboard fetches only that run's quantitative total and system bid
recommendation. Missing or invalid current output never falls back to an older
success. The complete quantitative total basis, including public criteria, still
passes through the shared public snapshot validator before it is counted.

Database graphs remain bounded to 25 notices and snapshot lookups to their current
run IDs. Every batch is detached before the next is materialized. No partial JSON
is assigned to stored source payloads, no child relationship is replaced with a
filtered list, and these read projections perform no database writes. The PR99
attachment validation cache remains local to one read-only notice projection.

Synthetic regression coverage compares the lean and full graph responses in both
public and authenticated modes, checks current-board parity, stale prompts,
missing latest outputs and malformed total bindings, and verifies that unused
historical payloads raise on access. Tests also verify that selected snapshot
queries stay at two per non-empty bounded batch despite many historical runs,
that only selected snapshots enter the identity map, and that complete source
payloads and clean read-session state are preserved.

This change does not trim NoticeVersion JSON or raise the UI timeout. Production
latency must be measured with a single request and then with normal browser use;
a smaller synthetic query graph is not proof that an operational timeout is fixed.
