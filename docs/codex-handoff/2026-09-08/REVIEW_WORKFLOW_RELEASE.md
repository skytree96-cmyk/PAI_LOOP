# Review workflow release — 2026-09-08

This change builds on deployed main `2bd6d21` (PR116, including PR111/112).

## Behavior

- Human participation, hold, nonparticipation and conditional participation can
  be recorded before analysis is complete and after the deadline. A written
  reason is required, and the server preserves the state observed at saving.
  Current/stale evaluation checks, cancellation restrictions, and existing
  authentication remain. Reanalysis does not rewrite human records.
- The total-stored dashboard card becomes FAIL. Review, urgency (through D+5
  KST), result-entry-needed, and cancellation cards/list scopes include only
  validated PASS/REVIEW. Unknown remains distinct. Full dataset statistics stay
  available separately; stale cancellation history is not fabricated.
- Ten submission confirmation categories show explicit source quotes or
  “원문 확인 필요”. The full qualification engine and evidence remain separate.
- Performance search supports inclusive contract date and amount ranges before
  pagination. The certificate download control is a disabled preview only.
- Assets use `20260908-review-v2`.

## Migration

`20260908_01_independent_operator_decisions` makes the evaluation relation nullable
and adds server snapshots. PostgreSQL relaxes the constraint in place. SQLite uses
an atomic copy with value/FK/index/trigger verification and rollback tests.
Preserve any new unevaluated human records during rollback; do not reinstate a
NOT NULL constraint or delete those records to make old code fit.

## Validation and remaining work

Focused tests and local desktop/390px mobile checks cover filtering, empty and
invalid ranges, disabled certificate preview, decision snapshots/concurrency,
SQLite preservation, and card/list scope. The exact PR head must pass the full
GitHub CI, including disposable PostgreSQL, coverage, wheel, assets and source
boundary scans, before merge.

This release does not create department accounts, populate award history, run a
new analysis campaign, or change public intro media. Independent account and
award-table work is in separate local branches. Stored-evidence rejudgment and
targeted failed-attachment reprocessing remain operational follow-ups; deployment
alone does not recalculate stored qualification or scores.

Record the actual PR, CI and Render verification separately after they complete.
