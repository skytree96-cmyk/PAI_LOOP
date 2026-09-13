# Dashboard qualification queues

The dashboard replaces the stored-total card with a clickable **FAIL 공고** card.
Existing DOM IDs remain stable. Review, urgent, result-entry-needed, and cancelled
cards and their lists include only explicitly qualified `PASS` or `REVIEW` notices.
Missing evaluations stay `NOT_EVALUATED`; they are not converted to `REVIEW`.

| Card | Scope |
| --- | --- |
| FAIL 공고 | Non-cancelled stored notices with valid current qualification `FAIL` |
| 검토 대기 | Open `PASS`/`REVIEW` notices that satisfy the existing analysis/review predicate |
| 마감 임박 (5일) | Open `PASS`/`REVIEW`, with a KST deadline date from today through D+5 inclusive |
| 결과 입력 필요 공고 | Ended, non-cancelled `PASS`/`REVIEW`, without a stored bid outcome |
| 취소공고 | Authoritatively cancelled notices with valid retained `PASS`/`REVIEW` qualification |

`/dashboard.work_queue_counts` reports these scopes independently of existing
global totals. Stored notice/evaluation/decision totals, general cancellation and
result-missing totals, analysis statistics, and the broad analysis backlog remain
available with their existing denominators. `deadline_soon` now matches the
qualified D+5 urgent queue. `/fail` and `/cancelled` open their respective lists;
the existing `/result-missing` route remains stable.

The board shows a separate aggregate status and server-observed stored notice and
evaluation totals. A dash is an unconfirmed value, not zero. While the aggregate
loads or fails, scoped list counts can still be available; an aggregate-only retry
does not reload the notice list or trigger analysis. Previously observed totals
are explicitly dated when retained during refresh. An incomplete API aggregate
must not turn missing whole-database values into filtered-list counts. See the
[September 13 diagnosis](dashboard-counts-diagnosis-2026-09-13.md) for measured
production evidence and its limits.

Summary/detail `qualification_status` requires a stored evaluation selected by
`latest_current_evaluation`: the evaluation must match current PPS material and
the deadline snapshot. PPS qualification additionally requires complete current
attachment coverage and the `ANALYZED` public audit, which validates current
manifest, prompt, schema, processing, source completeness, and quantitative
validator records. Legacy empty manifests, incomplete coverage, stale basis, or
missing evaluations do not supply a qualification for these queues.

Cancelled notices always have current `qualification_status=NOT_EVALUATED`, no
current evaluation/recommendation projection, and disabled recommendation UI.
Only a retained evaluation that passes the same source/audit checks supplies
`historical_qualification` (eligibility, evaluation timestamp, and explicit
`LAST_VALID_STORED_EVALUATION` scope). The list labels it **당시 PASS/REVIEW/FAIL**.
When that history cannot be validated, the field stays null and the notice is
excluded from the qualified cancellation card; its stored history is preserved.
No historical qualification is promoted into a current decision or recommendation.

Summary deadlines explicitly include UTC so browser-local time zones cannot
change lifecycle or KST calendar-day queue membership. The frontend cache key is
`20260913-dashboard-counts-v1`.
