# Frozen-source quantitative revalidation

The current extraction writer intentionally rejects earlier prompt/schema
contracts. Stored-input score recomputation therefore cannot establish whether
an older extraction would pass today's validator with the actual original text.
Changing the saved prompt, schema or validator version would erase provenance.

`quantitative_source_revalidation.revalidate_quantitative_source()` supplies a
separate, offline attachment diagnostic for the exact previous contract:
prompt 0.5.6, schema 0.4.1, validator 0.6.18 and processing 0.5.0. It does not
change current extraction, persistence or legacy read compatibility.

## Inputs and proof

The caller supplies an independently frozen source attempt, source version ID,
native bytes, canonical text and the complete current attachment manifest,
with their expected SHA-256 digests. The adapter checks attachment ownership,
the original processing text digest, both manifest bindings and predecessor
CASE vocabulary, including raw rows omitted from old REVIEW records.

The application parser then reads the native bytes again. Only a complete read
that exactly reproduces the supplied canonical text can produce a new candidate
profile. HWP5 and HWPX use the current bounded readers; a PDF conversion cannot
substitute for an original HWP attachment. Unsupported, incomplete, empty or
different text returns diagnostics without a profile.

The result preserves original extraction/record hashes and contract separately
from current parser, validator and adapter versions. It fingerprints the entire
result and leaves the input unchanged. A caller-supplied hash is an integrity
binding, not independent authentication of a database or a publisher.

## Interpretation

Every result has `persistence_eligible=false` and
`attachment_coverage_complete=false`. A VERIFIED native/canonical relationship
does not mean that extracted rules are correct. The current validator still
checks the unchanged raw conditions, awards, totals, quotes and source gaps.
Its profile is attachment-local and has no manifest document bindings.

Do not turn this output into a current extraction record, a company fact or a
complete-notice score. It has a distinct model and cannot pass the ordinary
extraction persistence contract. Rule repairs, whole-manifest validation and
company evidence bindings remain separate steps. A changed rule cannot reuse
an old criterion-bound company fact.

This API has no provider, download, database, lease or retry operation. It lets
an operator measure whether original-source revalidation is useful before
authorizing another model extraction. Never invoke ordinary enrichment as a
substitute for this offline check: failed reuse there may trigger paid fallback.

## Validation

Focused tests cover exact previous contracts, mixed and unknown versions,
independent native/raw/canonical/manifest tampering, forged processing digests,
new CASE vocabulary in old records, missing or cross-attachment evidence,
partial parser results, immutability, result tampering and persistence rejection.
Existing compatibility tests remain unchanged. Real source material and frozen
measurements stay in private `.local` artifacts; they are not regression fixtures.

## Separate reviewed-PDF experiment

The three approved 0.1 probes were executed once each. Two returned HTTP 500
at the model-execution stage after approximately 180 seconds without reported
usage. The live gateway had the same ordinary 180-second provider wait as the
repository. This timing strongly suggests that limit, but execution retention
was disabled, so the exact upstream error could not be confirmed.

The third response arrived after 110 seconds and was rejected for two short
quotes. Its printed count rows had fewer than eight non-whitespace characters;
the PDF canonical text joined cells while the model inserted newlines. The
existing exact-anchor floor correctly rejected an otherwise coincidental short
match. Reattaching the full source parent and marked footnote in a separate
diagnostic copy allowed the existing context binder to verify both criteria
without changing any case value, operator or award. A source-gap declaration
and missing recognition details still prevented a score. No rejected response
was promoted or rewritten.

Probe instruction 0.2 now asks for the complete parent/program evidence for
short rows, original marked footnotes, and recognition conditions found in
referenced forms (including VAT, own share, subcontract and proof requirements).
It distinguishes page-selection metadata from a specific missing source target.
The deterministic short-anchor and missing-source checks remain unchanged.
These instructions have not yet been tested with a new model response.

The explicit `QUANTITATIVE_PROBE_ONCE` execution policy adds a diagnostic-only
300-second gateway wait and 320-second client I/O wait while keeping the 20,000
output-token ceiling. It requires `request_scope=QUANTITATIVE_PROBE_ONLY`, one
dispatch and an operator-owned reservation callback. Ordinary extraction and
the existing 32,000-token `LONG_OUTPUT_ONCE` contract retain their limits. This
policy does not enable a job or authorize a paid request. Client I/O waits are
not a hard total-duration guarantee.

The new gateway policy must be published and independently verified before it
is used. No workflow was updated or activated for this change. Historical frozen
request plans and spent dispatch markers remain immutable; new code must never
silently regenerate and replay a spent plan. Any subsequent paid experiment or
production source/evidence write requires its own authorized scope.
