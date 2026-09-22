# Null-unit credit table recovery — 2026-09-21

## Reproduced cause and change

An exact three-column credit table may have valid conditions and awards while
the model leaves the criterion's unit null. The score compiler already supports
a source-proven implicit rating unit, but the enterprise-column binder rejected
the null before that compiler ran. A four-row synthetic example reproduces
`CASE_NUMBER_MISMATCH` four times plus `CASE_TABLE_NOT_DETERMINISTIC`.

The binder now permits null under the same source-column proof required for an
explicit rating unit. It does not fill the raw unit or change categories,
operators, awards, company facts or complete-manifest requirements. The source
profile version is 0.7.18; existing extraction contracts and fingerprints remain
unchanged. This is a source-binding correction, not a new scoring formula.

Regression cases cover points-only and ratio-plus-points columns, missing
company facts, explicit wrong units, substituted columns, incomplete rows,
changed awards, duplicate sources and full/partial notice scoring. With a
verified, deadline-valid SYN company fact, the synthetic credit table yields
9 confirmed points. A second unsupported performance criterion remains REVIEW,
giving a 9–15 range with no confirmed total. These are synthetic results, not
measurements of recovered production notices.

## Existing stored extractions

Ordinary score recomputation continues to read saved validation proofs. It does
not silently rewrite rejected historical tables or invoke a model. To measure
recoverability without another model call, source-revalidation adapter version
2 now accepts these exact released tuples:

| Prompt | Schema | Original validator | Original processing |
| --- | --- | --- | --- |
| 0.5.8 | 0.4.2 | 0.6.21 | 0.5.2 |
| 0.5.7 | 0.4.2 | 0.6.20 | 0.5.2 |
| 0.5.7 | 0.4.2 | 0.6.20 | 0.5.1 |
| 0.5.6 | 0.4.1 | 0.6.18 | 0.5.0 |

Mixed/unknown tuples are rejected. The 0.5.6 vocabulary restriction stays in
place; newer contracts retain their bounded-count and non-submission operators.
Original attempt, raw result, record and fingerprint provenance stay separate
from the new parser/validator/profile proof.

Supply a private frozen notice/company snapshot and exact local native files
and canonical text to the existing CLI:

```bash
.venv/bin/python scripts/replay-quantitative-sources.py \
  --snapshot /PRIVATE/frozen-notices.json \
  --company-snapshot /PRIVATE/frozen-company.json \
  --source-map /PRIVATE/source-map.json \
  --output /PRIVATE/new-recovery-report.json
```

The paths are placeholders. The snapshot and source-map contracts are described
in the CLI module docstring. The output path must not already exist. Never add
these inputs or reports to the public repository. The CLI blocks provider,
network, database and subprocess operations.

For supported contracts, `source_attempts[].native_diagnostic` now has a common
shape: `status`, `contract`, `profile`, `proof`, `diagnostic_codes` and the
explicit persistence/coverage flags. The current-contract path no longer emits
the earlier `CURRENT_RAW_REVALIDATED_DIAGNOSTIC_ONLY` record summary. Consumers
should read the common `profile` summary. `VERIFIED` means only that native
parsing reproduced the canonical text; inspect `profile.status` and issues to
determine whether rule validation passed.

Every native result remains attachment-local, `persistence_eligible=false` and
`attachment_coverage_complete=false`. The runtime score in the report still
uses saved records; recovered diagnostic candidates are not counted as runtime
scores. Incomplete parsing, changed bytes/canonical text, foreign anchors and
unsupported contracts cannot become verified profiles. A source unavailable in
this checkout cannot be reconstructed from model quotations.

## Operational boundary

The current task folder is a source archive without Git metadata or private
production source snapshots. The production 67/8-notice cohorts, deployed
revision and actual recovery count cannot be verified from this checkout.
No live records, provider calls, deployments or workflow states were changed.

After deployment, new source validation can use the corrected binder. Existing
records still require a separately authorized backfill design that persists
new validation provenance, validates the complete current manifest, rebinds
company facts and creates new score snapshots. This change supplies the free
diagnostic comparison, not a production backfill writer. The older
`QUANTITATIVE_SOURCE_REVALIDATION_20260913.md` records the original limited
adapter and remains historical.
