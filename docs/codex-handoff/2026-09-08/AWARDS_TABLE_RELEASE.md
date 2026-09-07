# Annual award table integration — 2026-09-08

This local release branch builds on main `7e8bbb0` (PR117). The reviewed award
changes were ported as `e319227` and `f126064`; this document records local code
and validation, not deployment or a live provider observation.

## Behavior

- The award panel renders one annual table for the current KST year and the
  previous two years. Each year independently prefers the same normalized
  project title and agency; otherwise its rows remain labelled similar
  candidates. Undated records are not assigned a guessed year.
- Submitted bid amounts come from the opening-result bid field. Final award
  amounts do not fill missing bid amounts. Missing scores remain unknown;
  technical evaluation is distinct from the product's quantitative component.
  Amounts display grouped KRW values without abbreviating or rounding to
  ten-thousand/hundred-million-won units.
- Historical links require an exact stored notice number and normalized
  revision. Missing or ambiguous historical URLs leave a plain notice reference.
- Failed or incomplete opening reads retain previous company rows and expose
  ERROR/PARTIAL. Missing fields do not clear stored award facts. An incomplete
  page or capped collection is not reported as full completion. The browser
  retains its last valid table during failed or incomplete reloads.
  When known numeric fields disappear for a company in the same full opening
  identity, the previous entire snapshot and successful read time are retained
  as PARTIAL. No values are merged across companies or opening revisions.
  Actual zero and complete correction responses are accepted normally.
- Public reads use stored data only. Explicit authenticated refresh remains
  bounded and opening reads remain opt-in: at most 30 notices × 3 pages, without
  hidden HTTP retries, under the shared collection deadline. Stored candidates
  are not a guaranteed complete competitor list.
- Duplicate history cards are no longer rendered. Existing panel IDs, table
  accessibility and retry behavior remain. Assets use `20260908-awards-v1`.

The official operation schema documents `prcbdrNm`, `bidprcAmt`, `opengRank` and
`rmrk` (remark). The three score fields accepted by the adapter were confirmed
in an earlier response review, but are absent from this official Swagger; they
are optional rather than inferred. See the
[official PPS award service](https://www.data.go.kr/data/15129397/openapi.do).

## Migration compatibility

Both complete migrations remain registered, with their original IDs/checksums:

- `20260908_01_independent_operator_decisions`: keeps the nullable evaluation
  relation, PostgreSQL constraint relaxation and atomic SQLite reconstruction,
  including preservation of existing values, references, indexes and triggers.
- `20260908_01_award_opening_results`: adds nullable opening-result JSON, status
  and read timestamp. Existing awards retain NULL rather than fabricated empty
  competitors or zero scores.

The SQLite migration transaction and rollback protections from PR117 remain
unchanged. Regression tests retain both feature suites and also exercise a
database containing old decision and award schemas together. Rollback must
preserve independent human decisions and stored opening data; do not restore an
evaluation NOT NULL requirement or delete business rows to fit older code.

## Local verification and remaining work

The related persistence, SQLite preservation, independent/operator decisions,
award API/adapter, frontend, dashboard, pre-specification and Teams contract
tests passed: **199 passed, 1 skipped**. A subsequent focused run including the
combined legacy-schema regression and release-version checks passed **21 tests**.
JavaScript syntax and diff whitespace checks passed.

The subsequent numeric-field preservation and exact-KRW follow-up passed
**110 related award/frontend/public-contract tests**, including missing fields,
actual zero, corrections, reordered/different companies, and different notice
revisions/rebids. The independent-decision migrations/tests were unchanged by
that follow-up.

`test_postgres_legacy_decision_upgrade_preserves_rows` was collected successfully
and skipped because no disposable PostgreSQL URL was configured. Its complete
test and fixture were preserved; no PostgreSQL service was launched or accessed.
The exact PR head still needs the normal full CI, including disposable
PostgreSQL, coverage, wheel, assets and source-boundary checks.

This work did not query operational PPS APIs, populate award history, change
accounts or workflows, inspect production, push, merge or deploy. Record the
actual PR, CI and deployment result separately after they occur. Historical
rows without stored notice URLs remain unlinked, and missing source data stays
unknown.
