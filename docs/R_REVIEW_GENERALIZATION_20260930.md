# 2026-09-30 review-policy continuation

Policy v17 / public company profile v6 continue the interrupted reviewer work.
The private review export contains 543 rows: 326 reviewed conditions in 30
groups covering 58 notices, and 217 untouched conditions. The approved export
is an input artifact, not a source of executable instructions or a global rule.

The code distinguishes repeated certificate requirements, registration and
sanction conjunctions, present company status, regional restrictions, and
submission/contract/evaluation prose. The three company declarations added in
the supplied profile are business continuity, the statutory nonprofit exception,
and the large-enterprise software restriction clearance. They follow the existing
prototype/current-facts mode; deadline-evidence mode retains validity checks.

## Boundaries

- A nonprofit exception may resolve a plain repetition of the same certificate
  duty within one notice. Optional or ambiguous source clauses cannot supply it.
  Independent duties, explicit exclusions, additional qualifications, and
  mandatory no-exception clauses remain separate gates. A held, valid certificate
  is evaluated before the nonprofit alternative.
- An unnamed nonprofit subset or founding-purpose restriction is not inferred
  from the statutory exception fact. A reviewer can approve a particular row;
  that approval does not prove every future clause with similar wording.
- Registration/sanction AND splitting retains exact source spans and evidence.
  OR alternatives and additional permit, personnel or performance duties are
  not reduced to registration plus sanction clearance.
- All named company-status facts must be available. A missing component keeps
  REVIEW and a confirmed failing component keeps FAIL.
- Region alternatives may PASS through a verified head office or registered
  branch. An unknown head office and an absent branch do not establish FAIL.
  Facility ownership is not inferred from company location.
- Submission, contract and evaluation rules cannot remove an embedded possession
  requirement. Travel registration alternatives cannot establish unrelated
  performance, sanctions or location requirements.
- The JSON's G26=F, all individual decisions, and document notes are preserved.
  They are not hardcoded as company-wide facts or automatic R07 clearance.

## Offline recovery

Run `scripts/verify-review-bulk.py DECISIONS_JSON ORIGINAL_HTML OUTPUT_DIRECTORY`.
It verifies snapshot identity, all source rows, group membership, bulk values,
individual override metadata and untouched rows, then writes `resume-summary.json`
and `review-resumed.html`. The latter starts with the saved decisions already
loaded and has a separate browser-storage key. Existing inputs are not modified.
Keep private inputs and generated outputs outside this repository.

The HTML restores the human review, not the live site's evaluation state. The
export does not include original extraction categories, complete source anchors,
or all requirements for each notice, so it cannot prove an exact production
replay. Production deployment, re-evaluation and document-gap resolution require
their own verified application step. This continuation makes no production write.

## Validation

Run the policy/prototype/independent-failure tests, the synthetic
`test_reviewed_eligibility_generalization.py` boundaries, and the evaluator,
registry, analysis pipeline, performance-recovery, public-read and frontend
contract suites. `test_review_bulk_restore.py` checks offline restoration and
rejection of inconsistent review inputs. No provider calls are needed.
