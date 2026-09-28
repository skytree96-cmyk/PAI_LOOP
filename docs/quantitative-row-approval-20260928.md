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

## Sufficient-row approvals

A second private path, `QuantitativeSufficientRowApproval`, lets a person confirm
the single printed row the company falls into. Only that row is executed as a
one-row case program; the other rows of the table are not used, so their
ambiguity no longer blocks the estimate. The row must still exist verbatim in
the raw extracted row bound by digest, and the current company value is
retested on every read: when it no longer falls into the confirmed row the
criterion becomes unscorable (never 0 or full marks).

- Supported metrics: `CREDIT_RATING`, `PERSONNEL_COUNT`, `FINANCIAL_RATIO`,
  `PERFORMANCE_COUNT`. Supported rows: `N 이상` (GTE), integer `EQ`/`BETWEEN`
  ranges for counts, open-ended or integer-bounded count brackets, and credit
  grade lists. Two-sided numeric ratio brackets and lower tails (`미만`,
  `이하`) cannot stand alone under the engine's safety rules and are refused.
- Waivable codes are the printed-literal codes above plus other-row and table
  codes a reader settles by reading the one applicable row (for example
  `AMBIGUOUS_RULE`, `CASE_TABLE_NOT_DETERMINISTIC`,
  `EXTRACTION_DECLARED_INCOMPLETE`, `REQUIRED_EVIDENCE_INCOMPLETE`).
  `UNKNOWN_METRIC` is never waivable: without a metric there is no company
  value to test.
- Credit rows bind the registered certificate (id, registration digest), the
  row's recognition-condition digest and explicit assertions that the
  certificate is from a qualified issuer, issued before publication, valid
  through the deadline, and that no succession, joint, cooperative or startup
  exception applies. Dates are rechecked on every read. A validated
  (`AVAILABLE`) credit row whose scenario grammar is unsupported may use the
  same binding for its certificate fact.
- Performance rows list the exact register records a person selected, with a
  window, an optional minimum single-contract amount and a VAT requirement.
  Every read recounts only records that are still `VALIDATED`, completed and
  inside those bounds, under the reviewer-selected metric key
  `company.performance.count.reviewer_selected`; keywords never re-derive it.
- Personnel and financial rows use the existing roster and statement
  resolvers.

Operator steps: GET
`/api/v1/operator-evidence/notices/{notice_key}/quantitative-rows/sufficient-context`,
then POST `/api/v1/operator-evidence/quantitative-sufficient-rows`. Registration
runs a probe calculation and is refused unless the confirmed row scores the
stated award right now. A row may carry only one approval of either kind.
Results are always `ESTIMATED`, disclose the confirmed-row assumption, and join
the notice's `PARTIAL_SOURCE` subtotal.
