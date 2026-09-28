# Quantitative proof preservation

Quantitative reads and automatic analysis now share a conservative rule for a
same-input extraction retry that loses an already validated score table. This is
a source-selection repair, not approval of a company score or a source rewrite.

## Preservation Boundary

A prior proof may be selected only when all of these checks pass:

- Every intervening attempt is accepted and complete, with complete source and
  analysis input processing metadata.
- Attachment identity, descriptor and full-manifest hashes, native document
  hashes, canonical source hash, analysis-input hash, and the exact released
  extraction/validation/processing contract agree.
- The prior attachment record is fully `AVAILABLE` and passes its existing
  immutable-record validation and binding checks.
- The newer attempt has no available candidate. Its only validation failures are
  `CASE_NUMBER_MISMATCH` and optional accompanying `CASE_POINTS_EXCEED_MAX` or
  `CASE_TABLE_NOT_DETERMINISTIC`, with unchanged table and criterion outlines.
  A standalone nondeterministic-table error is not eligible. An identical record
  whose fingerprint alone is damaged is also eligible.
- Both original extraction payloads must parse. For numeric losses, their entire
  contents must agree except for CASE award and comparison numbers. Recognition
  conditions, required evidence, source quotations, units, other requirements,
  and summaries cannot change. Fingerprint-only recovery requires identical raw
  extraction content as well as the identical validated record body.
- No source-gap warning, additional criterion, missing provenance, unsupported
  generation, failed extraction, or other validation issue is crossed.

`NO_TABLE` and `NOT_APPLICABLE` results are not treated as numeric extraction
losses. A newer usable candidate remains authoritative. A changed source never
borrows the previous source's proof. Unsupported newer contracts also block
automatic selection of older current-contract rows.
Ordinary analysis fallback also cannot cross a changed native document digest,
even when the newer record is invalid.

The default attachment-attempt selector remains unchanged for diagnostics and
retry decisions. The quantitative profile explicitly opts into proof selection;
analysis uses the same preservation predicate. Explicit historical source
selection remains an audit operation. No attempt, extraction payload, immutable
record, fingerprint, or manifest is modified.

`PAI_LOOP_EXTRACTION_DEMOTION_GUARD=off` disables proof preservation. It does not
disable current-manifest or unsupported-generation safety checks.

## Scores And Caches

Engine version `1.8.8` invalidates earlier cached quantitative results. It does
not change the extraction contract or require another provider call. Existing
coverage, criterion compilation, company-evidence binding, and public redaction
checks still apply. A preserved source rule is not a confirmed company score;
missing facts and incomplete sources remain visibly unscorable or under review.

Regression tests use synthetic records only. No company roster, source document,
production mutation, paid extraction, or deployment is part of this change.
