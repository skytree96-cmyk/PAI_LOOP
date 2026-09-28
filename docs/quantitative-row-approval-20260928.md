# Reviewed Quantitative Row Approvals

This additive private operator path lets one notice row that failed only printed
literal checks contribute an `ESTIMATED` partial subtotal after a person has
read the source row. It does not change the global parser, the validator, the
extraction contract or `HUMAN_REVIEWED_NOTICE_CONDITIONS`, and it never grants a
`CONFIRMED` score.

## When it applies

- The notice's current dynamic profile is `INCOMPLETE` (an unresolved manifest).
  Rows of other profile states stay as they are.
- The row is a current `REVIEW` candidate whose raw extracted row can be read
  from the same attempt the profile used.
- Every current blocker on the row, its table and its attachment (unlocated
  issues) is individually reviewable. Only these printed-literal codes are:
  `CASE_NUMBER_MISMATCH`, `MAX_POINTS_LITERAL_MISMATCH`, `AMBIGUOUS_TABLE`,
  `SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION`. A model-declared ambiguous rule,
  a nondeterministic case table, missing evidence, an unknown metric, totals
  and minimums are never waivable here.
- Interpretation `HYPHEN_SEPARATED_POINTS`: each case literal must fully match
  one `N(명|건|개|회|년|%)(이상|이하|미만)-M점` token printed in the row's own
  quote, and the extracted operator, comparison and award must equal it. The
  top award must equal the row maximum. Any deduction wording (감점, 차감, 공제,
  마이너스, 벌점) keeps the row unapproved, so a real negative award is never
  read as positive.

## Operator steps

1. GET `/api/v1/operator-evidence/notices/{notice_key}/quantitative-rows/approval-context`.
   Each row shows the printed literal, quote, recognition conditions, the
   extracted program, the exact blocker codes and whether it is `approvable`.
2. Read the source row. If the program is the correct reading, POST
   `/api/v1/operator-evidence/quantitative-row-approvals` with
   `QuantitativeRowApproval` from `quantitative_row_approval.py`: attestation
   `HUMAN_REVIEWED_ROW_PROGRAM`, the bindings returned in step 1
   (`manifest_sha256`, `attachment_id`, `document_sha256`, `table_id`,
   `criterion_id`, `raw_candidate_sha256`), `waived_issue_codes` equal to the
   row's current codes, the `program` shown, `accepted_on` (not in the future)
   and an `effective_through` that covers the deadline.
3. Run the normal analysis (`enrich_missing=false` is enough) and verify the
   persisted public result. Registration itself runs no analysis.

Registration is idempotent for the identical approval; a different approval
for the same row is a 409 conflict. Both endpoints use private operator
authentication and `no-store`.

## Fail-closed rules

Every calculation rebuilds the approved row from the current source and
ignores the approval when the manifest, document digest, raw row, program,
blocker codes, deadline window, verification flag, source or effective range
differ, or when two approvals exist for one row. The row then stays `REVIEW`.
Approved rows join a `PARTIAL_SOURCE` subtotal; the notice total and minimum
remain unresolved. Company facts bound to an approved row are capped at
`ESTIMATED`, and the row rationale states that a person approved the reading.
The public view states that the subtotal includes human-approved rows and
exposes no approval identifiers or digests.

The approval is part of the private analysis input manifest, so registering
one invalidates analysis reuse. The quantitative engine version is unchanged:
results differ only for notices with a stored approval, whose input digest
changes.

No production registration, provider call, deployment or notification is
implied by this code. Tests use synthetic sources only.
