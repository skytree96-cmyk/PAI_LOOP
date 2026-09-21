# Printed quantitative intervals — 2026-09-20

The reviewed 32k extraction completed at 28,133 output tokens, but quantitative
validation remained incomplete. Some printed two-sided intervals were represented
as a single GTE lower cutoff. An ascending disjoint interval table also cannot be
made into a supported descending CASE program merely by retaining its lower bounds.
This is a source-transcription problem; increasing the output cap alone does not fix it.

The extraction prompt now distinguishes BRACKET intervals from ordered CASE programs:

- Keep both printed endpoints and their exact inclusivity. For example, the
  synthetic row `20% 이상 40% 미만 2점` has bounds 20 inclusive and 40 exclusive.
- Preserve an explicit closed count interval, including personnel counts, on both
  sides: synthetic `2~4명 3점` retains 2 and 4 inclusive. Keep source row order and
  never borrow another row's or criterion's boundary to fill an absent endpoint.
- Preserve valid descending overlapping GTE CASE cutoffs, categorical and percentage
  awards, and the existing PERFORMANCE_COUNT BETWEEN/NOT_SUBMITTED program. A
  valid CASE program is not replaced just because one row has two endpoints.

The prompt does not expand the schema, convert units, reclassify any percent rule
as FINANCIAL_RATIO, or weaken deterministic checks. An unsupported metric/unit,
missing recognition condition, conflicting table, or ambiguous source still requires
review. Correct interval transcription does not guarantee quantitative activation or
a confirmed company score. Existing stored extractions are not rewritten by a prompt
change, and no production re-extraction is performed by these tests.

The validator now recognizes a complete, closed integer-count BRACKET row such as
`3~5명 4점` or its consecutive condition/award cells. Both endpoints must match,
both must be inclusive, the unit must belong to the criterion's registered count
dimension, and that row must contain its own matching award. Extra alternatives,
missing boundaries, mixed dimensions and changed awards remain blocked. The same
proof runs on initial validation and when loading a stored AVAILABLE candidate.
There is no automatic conversion of an existing lossy GTE result.

The current contract is prompt 0.5.8 / schema 0.4.2 / validator 0.6.21 / processing
0.5.2. The exact previously deployed extraction contract and its parser predecessor
remain readable under read policy v4, with their original fingerprints and review
states. Earlier supported CASE contracts also remain readable. A newer failed
attempt blocks fallback to an older successful proof. Unknown combinations are
rejected. Upgrading alone does not enqueue unchanged accepted attachments for paid
re-extraction, including accepted attachments whose quantitative rules need review.

A read-only, in-memory review of the observed source preserved both bounds in two
criteria and removed the four number mismatches, four comparator mismatches and two
CASE execution failures. The resulting reviewed draft still had incompleteness and
ambiguity issues. It was not a new model response and was not persisted as an
approved extraction or company score. All committed fixtures use synthetic sources.

Transport-mocked contract tests verify that the instructions reach ordinary, diagnostic
probe, and LONG_OUTPUT_ONCE requests without changing their schema or budgets. These
tests do not establish that a live model will follow the instructions. Deterministic
validator and boundary tests remain separate evidence of what the application accepts.

A 32k cap is not a completeness guarantee. Follow-up work remains separate: a compact
quantitative-only schema, shared exact evidence references to avoid repeating quotes,
and segmentation by a complete scoring program rather than arbitrary pages or rows.
Any segment must retain its full table, recognition conditions, footnotes, and applicable
references; recombination must prove coverage and preserve missing-source failures.
Multiple segments also require an attachment-wide call/token/cost cap and duplicate
dispatch protection. The existing quantitative probe is diagnostic-only and is not that
production segmentation pipeline.
