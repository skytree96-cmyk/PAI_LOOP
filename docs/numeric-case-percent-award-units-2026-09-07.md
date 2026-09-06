# Numeric CASE input units and percentage awards

A source row such as `5명 이상 배점의 100%` describes an input measured in people and an award measured as a percentage of the criterion maximum. The previous activation check collected both units from the entire row and reported `BOUND_UNIT_INCONSISTENT`. A mechanically valid table could therefore remain `REVIEW_REQUIRED` solely because it expressed an award as a percentage.

The calculation boundary now isolates the numeric condition only when the complete row literal is contained in its persisted evidence, the row ends in the exact declared percentage award, and the preceding condition contains one comparison value matching the extracted operator and value. Inline and split award cells are supported. The shared extraction comparison and score-cell validators establish the separation; arbitrary percent tokens are never discarded.

Input unit validation uses that verified condition in both the source-unit and unit-consistency checks. The award percentage cannot establish a missing financial-ratio input unit. Explicit mismatched input units, wrong or alternative awards, inverted comparisons, competing numeric cells and partial evidence remain blocked. Categorical credit rows and the existing source-equivalent currency conversion path retain their existing behavior. Source literals, extracted numbers, company facts and compiled CASE programs are unchanged.

## Version and deployment boundary

This calculation fix follows the lower-tail CASE contract at the implementation boundary and ships in the same release, using quantitative engine `pai-loop-quantitative-engine-1.7.3`. The release uses extraction prompt 0.5.5, schema 0.4.1 and validator 0.6.18 from the CASE contract; this input-unit fix adds no further extraction-version or fingerprint change. Existing attachment proof remains subject to that contract's exact compatibility policy.

Stored aggregate snapshots from the previous engine become stale and must be recalculated with existing verified attachments (`enrich_missing=false`). Recalculation must preserve provider-call accounting and append current snapshots; no source re-extraction is required solely for this calculation change. Missing company facts still cannot be treated as zero or as confirmed evidence.

## Verification

Synthetic tests cover equivalent POINTS and PERCENT_OF_MAX tables, final lower-count rows, inline and split source cells, Korean percent words, separate financial input percentages, missing input units, real input-unit conflicts, wrong values and operators, conflicting awards and partial anchors. They compare exact scores and verify that the original extraction remains unchanged. Related CASE, source validation, activation, logical-program and extraction-compatibility regression tests are included in the focused gate.

This demonstrates the code defect and its correction on synthetic input. No production notice count attributable to this issue has been measured. The latest local per-notice issue census predates this calculation change and does not include activation-reason frequencies.
