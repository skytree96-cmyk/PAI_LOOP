# Audited normalization of private performance periods

The private evidence API now provides a bounded date normalization operation at
`POST /api/v1/operator-evidence/performance-period-normalizations`. It requires
server or private evidence authentication; the public operator PIN is insufficient.
The existing verified import replay and private-record PATCH restrictions remain.

Only an active `PRIVATE_IMPORT` DRAFT whose start and end dates are both empty can
be normalized. Each request binds the current VERIFIED workbook SHA, sheet and
row, stored record identity, expected revision and canonical before-state hash.
The caller supplies the source period, not replacement dates or other record
fields. A narrow server parser accepts exactly two date boundaries, including
explicit mixed formats and same-year abbreviated ends. Durations, single dates,
invalid dates, additional prose and ambiguous year crossings remain unresolved.
Two-digit years use the private register normalizer's 2000-based convention.

The workbook SHA and cell reference do not independently prove that a supplied
period occurs in that workbook. The authenticated operator must first verify the
same local source bytes and exact source cell, and compare the old and new
normalizations. `AUTHENTICATED_LOCAL_SOURCE_MATCH` records that machine source
binding; it is not a new claim of human review. The existing workbook's completion,
VAT, certificate and recognized-share attestation is checked and preserved.

A request contains an opaque UUID idempotency key, source SHA, sheet name,
`explicit-performance-period-v1` algorithm version and at most 100 distinct rows.
Each row supplies its record UUID, full row key, source row, expected revision,
`expected_state_sha256` and bounded `source_period`. The canonical hash helper
`performance_normalization_state_sha256` accepts either a database record or its
authenticated API representation. Resolve this before-state immediately before
submission; never derive a current revision from historical logs.

The normalization shares the private import/replacement lock and checks every row
before writing. One transaction updates the same records and appends a batch
receipt plus immutable before/after revision snapshots. Record IDs, source SHA,
row references and other business fields remain unchanged. Raw workbook content
and raw period strings are not stored by this operation; the audit stores the
source cell, period hash, algorithm version and private before/after state.
No public endpoint exposes these audit tables. Responses report counts only,
validation failures are redacted, and responses use `Cache-Control: no-store`.

Dates alone do not establish validation. The server rechecks all required record
fields and the existing attested semantics before changing DRAFT to VALIDATED.
A zero share remains DRAFT because historical normalization also used zero for an
unknown share, whose original reason is not stored. Other incomplete records keep
DRAFT status even when their dates can be normalized. No attestation, monetary
value, share, agency, completion flag or certificate status can be supplied as a
correction. A record with already populated dates needs a different reviewed
correction process.

Exact receipt replay returns an unchanged result. Reusing a request UUID with
different content or submitting a stale source/record binding fails. A replay
never revives a source that a later workbook replacement superseded. The additive
migration creates only the normalization batch and revision tables; previous
analysis and score snapshots are untouched. Recompute affected notices through
the existing analysis workflow to create new snapshots. Remaining DRAFT evidence
can still make performance scores estimated or require review.

Synthetic tests cover parser boundaries, source and revision conflicts, atomic
rollback, concurrent replay, private access and redaction, preservation of prior
scores, continued import/PATCH immutability, and unresolved data after correction.
