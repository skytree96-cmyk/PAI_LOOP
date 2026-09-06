# Source-explicit lower count rows and persisted extraction compatibility

A numeric count row such as `1건 이하 1점` must remain a numeric comparison. The extraction schema now supports LTE and LT only for an explicit final lower count row after the deterministic descending GTE/EQ sequence. Source comparators, awards, ordering, integer boundaries and count units are validated before the table can supply a rule. Numeric comparisons cannot be encoded as category strings, and no historical IN row is converted into a new operator.

The existing CASE compiler accepts the final lower interval only for discrete counts; negative/fractional/missing facts remain unscorable, omitted intermediate count rows remain gaps, and zero earns a source-supported award only when the company input actually supplies zero. Amounts, ratios, categories, credit ratings, nonfinal/multiple lower rows and overlapping rows do not acquire these operators.

## Extraction and execution versions

New extraction writes use prompt `pai-loop-extraction-0.5.5`, schema `pai-loop-requirements-0.4.1`, validator `pai-loop-quantitative-attachment-validator-0.6.18`, and unchanged processing `pps-document-processing-0.5.0`. The candidate profile is `pai-loop-quantitative-candidate-profile-0.7.15`; executable results use engine `pai-loop-quantitative-engine-1.7.3`. This single release includes the numeric CASE input-unit and percent-award separation fix described in `numeric-case-percent-award-units-2026-09-07.md`.

The only predecessor read contract is the exact tuple prompt `pai-loop-extraction-0.5.4`, schema `pai-loop-requirements-0.4.0`, validator `pai-loop-quantitative-attachment-validator-0.6.17`, processing `pps-document-processing-0.5.0`. Separately mixing allowed version values is rejected. Legacy accepted evidence needs its original extraction result as well as the immutable quantitative record: original REVIEW candidates are included in the GTE/EQ/IN feature check, although their compact persisted review form does not carry CASE rows.

Compatibility preserves current manifest/attachment/document bindings, current record invariants, current targeted fingerprints, original table/criterion census, immutable CASE semantics and source anchors. A repaired quote may be wider than its original model anchor; compatibility does not change the repaired proof. This is a check of persisted proof integrity and feature compatibility, not a new comparison against absent document text.

An ACCEPTED document can retain a REVIEW/INCOMPLETE quantitative record, or a neutral NO_TABLE record. Existing issues remain visible and keep blocking activation. New current-contract REVIEW supersedes predecessor AVAILABLE in document selection, quantitative profiles, materialization, public publication, sibling closure and pricing. An invalid current-contract accepted attempt does not reopen an earlier legacy score.

New writes and downloaded-result deduplication remain current-contract only. Duplicate-content reuse cannot stamp the new prompt/schema on an old result. Normal continuation may return an already compatible stored row with its original identity and zero provider calls. Explicit reviewed retries retain their frozen old generation boundary and do not repeatedly skip the new result.

## Refresh and verification

The engine version change makes old aggregate score snapshots stale; it does not remove compatible attachment evidence. Existing calculation-version refresh selection can append current engine runs using stored sources with `enrich_missing=false`. Pipeline input hashes include the quantitative output and actual source versions, so one refresh creates one new run and replay is idempotent. Historical runs and source rows remain unchanged. New run basis records the consumer read policy and actual source prompt/schema combinations.

Tests cover exact/mixed/unknown version tuples, current and predecessor source proof, original REVIEW CASE feature checks, strict new numeric shape, comparator inversion, mixed-contract current manifests, newest-generation supersession, source/anchor/census mutation, public projection, immutable reuse, retry boundaries, duplicate-content rejection, and provider-free engine refresh. Existing lower-tail compiler tests preserve the four old compiled-program regression hashes.

Deploy the complete read compatibility and new schema/validator/engine together. Verify unaffected accepted evidence first, then perform bounded calculation-only refreshes. Re-extract source rows whose old unsupported count categories require the new contract. Source table totals, financial statements and missing company facts still need their own evidence; this release does not invent them.
