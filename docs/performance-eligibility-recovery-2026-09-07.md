# Preserve explicit performance bidder requirements

An accepted requirement that explicitly limited participation to bidders with a
minimum performance history fell through to INFORMATION. It was omitted from
materialized eligibility conditions; an otherwise complete extraction could then
produce PASS with NO_BLOCKING_REQUIREMENTS even with no qualifying evidence.

The policy now recognizes a bounded set of explicit performance-possession or
absence clauses linked to bid participation. It does not promote the PERFORMANCE
category, scoring rows, bonus criteria, certificate submission, optional clauses,
or post-award performance reports into bidder requirements. Direct possession
and complete clause endings prevent borrowing a registration requirement from
adjacent scoring prose or treating a negated/historical condition as current.

The resulting requirement remains ELIGIBILITY/REVIEW with an explicit pending
recognition-scope marker. The evaluation key is derived from normalized source
condition text, not a provider-chosen requirement ID. That key is a pending audit
identity, never proof of scope completeness. The pipeline excludes only those
pending keys from both eligibility evaluator calls. Generic totals, Boolean facts,
and even a fabricated True value stored under the exact pending key cannot make
this unbound condition PASS. Stored company facts are preserved. Other facts and
mandatory failures continue through the unchanged linked-review, default-fail,
AND/OR evaluator contracts. Requirement snapshots preserve the pending marker.

A source-preserving AND expansion separates directly stated bidder registration,
direct-production or business-certificate possession from the mandatory
performance condition. It finds a top-level grammatical boundary, validates the
preceding clause with the existing qualification helper and a directly owned
positive predicate, then verifies the following mandatory performance gate.
Conjunctions, a completed duty followed by an explicit addition, and an actor
qualified by another duty are recognized. Parenthetical OR and the established
SME certificate name are distinguished from a top-level alternative.
Scoring/submission descriptions, negated or historical duties, mismatched
parentheses and possession borrowed from another object cannot create an AND. The two normalized conditions are exact source
substrings, retain the original anchors, and use deterministic distinct IDs.
Expansion is idempotent at the policy and pipeline entry points; the pipeline
also rejects count, order or ID-uniqueness mismatches before materialization.
The existing fact remains eligible for comparison: FALSE produces DF-000/FAIL,
while TRUE leaves the separate performance condition at linked REVIEW.

Existing mapped policy is classified before applying the performance fallback.
A general eligibility heading alone is not a registration condition. For a
performance-only heading that has no remaining registration or statutory basis,
the old generic bidder-registration mapping is replaced by pending performance;
a registration Boolean cannot satisfy that standalone performance requirement.
If an unexpanded combined condition still has an existing evaluation fact key,
that mapping, comparison and result are preserved, including when FALSE exists
only in database facts. A performance_relation_unresolved marker and message
record the remaining unverified relation. Complete OR alternatives retain their
existing valid PASS path. Unknown relationships are not silently rewritten as
AND, and their marker is not proof that the performance condition was satisfied.
In particular, confirmed mandatory AND omissions cannot be accepted as a
completed recovery under this limitation: the five independently reproduced
conjunctive forms now have separate pending performance atoms, with TRUE giving
REVIEW and FALSE preserving DF-000/FAIL.

This change does not compare a workbook's total record count with a bidder gate.
The existing performance register powers quantitative score estimates, while
eligibility requires a separately verified source scope, operator, threshold,
time basis and evidence binding. Until that bridge exists, the new message says
that recognition conditions and validated register evidence are not yet linked.
It does not assert that the underlying company data is absent.

POLICY_VERSION advances to pai-loop-requirement-policy-2026.09.07-v11. That field
already participates in the pipeline input digest and stored basis versions.
(Superseded: v11 was later advanced to
`pai-loop-requirement-policy-2026.09.07-v12` by
`docs/nonprofit-small-business-or-recovery-2026-09-07.md`. The evidence recorded
in this runbook is unchanged and remains the record for the v11 change.)
The PR108 accepted-gap analysis-pipeline-0.6.6 baseline is preserved; extraction
prompt/schema/validator and quantitative engine versions are unchanged. Existing
accepted extraction can be reused for a provider-free recalculation. Old audit
snapshots remain immutable and are not silently rewritten.

Synthetic regression coverage includes explicit and descriptive clauses, category
independence, source-scoped key stability and provider-ID isolation, missing and
present validated records, forged generic/exact facts, source-quality gates,
unrelated titles, exact AND expansion/re-entry, known fact PASS/FAIL preservation,
unsupported relation baseline parity, and pairing-corruption rejection.
The existing evaluator AND/OR and performance recognition tests are also run.
No production occurrence count, company document, private record, or source
attachment is included in this change.
