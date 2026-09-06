# CASE award evidence boundary

A numeric condition previously doubled as evidence for a company score when its
number equaled the extracted award. A synthetic row containing only a condition,
or containing an explicit zero-point award, could therefore produce an AVAILABLE
record and AUTO_ACTIVE CASE table with a different positive award.

The current check requires a separate terminal award: an explicit points/percent
expression (optionally parenthesized), an explicit award label, or an independently
bounded last table cell. The remaining source text must match the declared
comparison or categories. Another condition number, a different point award,
multiple award alternatives, a reversed row, or a missing award cannot supply this
proof. Exact source/quote, criterion ownership, unique repair windows, table totals,
category/metric, and unit checks remain in place. No extracted numbers or source
text are rewritten to make this check pass.

The same rule protects fresh validation, source-backed header support, serialized
AVAILABLE record invariants, and the execution boundary for already materialized
profiles. Thus a historical record with a valid old fingerprint still fails when
its retained literal does not prove the award. The check uses retained literal and
quote evidence; it does not claim to re-read an absent original document. A missing
award requires an authorized fresh extraction or source review.

The extraction prompt/schema/validator tuple remains unchanged. Normal historical
proofs with explicit distinct condition and award evidence stay usable. Engine
1.7.4 invalidates stale quantitative snapshots so provider-free recalculation uses
the current execution gate. It does not itself trigger global provider calls.

Synthetic regression coverage includes missing/wrong/same-valued awards, alternate
condition numbers, signs, award units, inline and separate cells, correctly repeated
numbers, serialized old proofs, and already materialized profiles. Existing CASE,
credit, Korean currency, percent-award, source-binding and contract compatibility
regressions must remain green before deployment. No production prevalence is
inferred from the synthetic defect reproduction.

Two prior negative fixtures now report INCOMPLETE rather than REVIEW: a borrowed
commercial-paper column, and a repeated row with an extra institution clause.
Both retain their original collision/non-determinism reason, add CASE_NUMBER_MISMATCH,
and cannot activate. Tests assert the stricter classification explicitly.
An incompatible count unit remains a downstream unit error; recognizing its
lexical cell boundary does not make it valid for another metric.
