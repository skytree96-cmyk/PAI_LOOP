# Dashboard count diagnosis — 2026-09-13

## Observed state

A read-only production GET at approximately 13:07 KST returned HTTP 200 for
health, runtime configuration, and dashboard. Database health was `ok`.
The dashboard request took 24.938 seconds. No production writes, analysis
triggers, workflow changes, push, merge, or deployment were performed.

| Stored or current measure | Observed count |
| --- | ---: |
| Stored notices | 931 |
| Active notices | 288 |
| Stored evaluation rows, including repeated evaluations | 2,094 |
| Stored decisions | 8 |
| Qualified FAIL queue | 4 |
| Qualified review queue | 2 |
| Qualified urgent queue | 1 |
| Qualified missing-result queue | 1 |
| Qualified cancelled queue | 0 |
| All cancelled notices, regardless of qualification | 20 |
| All missing-result notices, regardless of qualification | 623 |

These observations disprove a complete loss of the stored notice/evaluation
dataset. They do not establish that every historical row is unchanged: no
previous database snapshot was compared. Queue counts are not database totals.
See [the queue contract](DASHBOARD_WORK_QUEUES.md) for each population.

## Why the screenshot contains numbers and dashes together

The board loads its scoped notice list before the whole-database dashboard.
While that aggregate is pending, or when it fails, `dashboardWithoutGlobalTotals`
can derive review, urgent, and GO values from the loaded list. It leaves FAIL,
cancelled, and missing-result totals unavailable. The screenshot's review 2,
urgent 1, GO 0, and three dashes match this state.

A dash means an unconfirmed aggregate, not zero or a deletion signal. The
screenshot alone cannot distinguish a request still loading from a request that
failed. The observed 25-second request shows a substantial loading interval; it
does not prove the screenshot's request timed out. The API's 60-second browser
timeout and bounded batch reads remain unchanged.

## Analysis is running, but current completion is limited

For the 288 current open PPS notices, the dashboard reported:

- Historical processing linked to the current attachment manifest: 175 notices,
  573 attachments. This is a narrower population than all stored evaluations.
- Processing recognized by current validation: 74 notices.
- Current attachment state: 5 analyzed, 69 review, 214 pending.
- Current qualification: PASS 0, FAIL 3, REVIEW 2, NOT_EVALUATED 283.
- Current score state: 4 review, 284 not evaluated; no confirmed scores or
  estimated score ranges.

Current qualification requires valid current source versions, deadlines, complete
attachment coverage, and applicable prompt/schema/validator checks. Historical
evaluations remain stored when those checks no longer supply a current result.
The gap between historical and current processing cannot all be attributed to
one cause from these aggregate values.

The ingestion audit records five analysis jobs from 07:31 through 07:49 KST on
September 13, all `PARTIAL`. Recent analysis warnings include incomplete attachment
coverage, continuation required, network/HTTP errors, unreadable source material,
and unresolved or unverified evidence. Warning categories may overlap within a
job and do not constitute a count of distinct failed notices.

Read-only workflow inspection found:

- W10 daily briefing: active, scheduled for 07:30 Asia/Seoul. Successful execution
  data is not retained, so absence of recent success entries is not evidence of
  inactivity.
- W11 analysis backfill continuation: inactive. Its configured minute schedule
  does not automatically resume backlog while the workflow is inactive. The
  reason for its inactive state was not established or changed in this task.
- W13 extraction gateway: active, with both successful and failed execution data
  retention disabled. Its empty execution listing does not demonstrate failure.

The daily jobs and inactive continuation scheduler are different paths. The
evidence supports both recent analysis activity and incomplete backlog progress;
it does not support saying that analysis has entirely stopped.

## Local presentation changes

The dashboard now distinguishes aggregate loading, failure, partial responses,
and confirmed values. It displays server-observed total notices and evaluation
history separately from qualified work queues, explains the dash, and offers an
aggregate-only retry. Previously confirmed totals can remain visible with their
observation time during refresh; account changes invalidate them. Missing server
fields remain unknown rather than being replaced by a filtered-list zero.

This work is isolated on `fix/dashboard-counts-0913`. The earlier result-entry UX
changes remain on `fix/result-entry-save-0913` for later integration. Production
behavior and workflow activation remain unchanged.

Validation: 164 frontend, dashboard sync, and lean-projection regression tests
passed, together with the focused queue/account/Teams contract checks. JavaScript
syntax, Python compilation, and diff whitespace checks passed. Synthetic Chrome
checks at 1440 × 1000 and 390 × 844 covered loading, failure, successful retry,
retained observations, and incomplete responses without horizontal overflow,
overlapping elements, or page errors. Retrying issued only a dashboard GET.
