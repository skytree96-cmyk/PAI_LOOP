# Three-column credit projection order — 2026-09-22

## Reproduced cause

A real notice (한국과학기술연구원, prompt 0.5.8) stored a 회사채/기업어음/기업신용평가
credit table whose every row literal appears verbatim exactly once in the
attachment's own canonical text. Re-downloading the attachment reproduced the
stored `document_sha256` and the stored canonical text exactly. The stored
record was nevertheless `INCOMPLETE` with `CASE_NUMBER_MISMATCH` three times and
`CASE_TABLE_NOT_DETERMINISTIC` once.

The rejection is structural, not numeric. An `IN` row must have its declared
categories exhaust the quoted condition, and a three-column row leaves the two
neighbouring instrument columns behind. The enterprise-column projection exists
for exactly that, and it did not run.

Two gates stopped it, in order:

1. `_rebind_split_table_cell_literals` runs first and narrowed one row — the row
   whose evidence quote covered only the enterprise column — from seven lines to
   three. `_enterprise_credit_projection` requires at least four lines, so that
   row projected to `None` and the whole table was refused.
2. Relaxing the line floor was not enough. The projection proves its table by
   rebuilding one contiguous source region from the header cluster followed by
   the concatenated row literals. With one row already narrowed, that region can
   no longer be reconstructed and the span lookup returns nothing.

On the original, unnarrowed literals the span proof passes and every other gate
is satisfied. Only that one row's quote scope differs.

## Change

The projection now runs once before split-cell rebinding, on the original row
literals, and the existing pass after rebinding is unchanged. Reapplying the
binding to an already projected payload is a no-op, which the binder's module
contract already guarantees.

A row may be quoted as the whole three-column row or as its enterprise column
alone. The narrower quote is accepted only when it equals the projection this
function derived independently from the source columns. The quote therefore
never widens or replaces what the source established: two independent
derivations — the model's quote and the column proof — must agree.

Header order, the terminal footnote, region uniqueness, single-occurrence
anchoring, row order, operators, categories and awards are all unchanged. The
candidate profile version becomes 0.7.19 to identify the revised binding.

## Measured effect

With the same stored extraction and the same attachment bytes, that notice moved
from `INCOMPLETE` with no candidates to `AVAILABLE` with one candidate and no
issues.

Across a read-only survey of 40 notices and 139 attachments revalidated against
their own re-downloaded sources, without any model call, five notices recovered
candidates they did not have, spanning both the current contract and the
0.5.7 predecessor. Seven more that already had candidates kept them. Native
parsing reproduced the stored canonical text for 132 of the 139 attachments.

## What this does not fix

Twenty-seven surveyed notices remain blocked. Every one of them declares a
material source gap: the scoring table is absent from the documents we hold, or
is unreadable in them. Those declarations were checked against the existing gap
classifiers and none is merely a qualitative-form gap.

Among the remaining credit tables that still fail, the single failing gate is
column projection, and the rows involved have lost their cell boundaries — the
three columns are joined on one line, or split inconsistently across rows.
Recovering those would mean inferring where one column ends and the next begins,
which this codebase does not do. That is an extraction-contract question, not a
validation-rule question.
