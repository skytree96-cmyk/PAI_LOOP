# Validate count brackets on their nonnegative integer domain

The source-independent scoring engine treated every BRACKET program as a
continuous real-number partition. A count program beginning with `[0, 0]`
therefore failed the mandatory unbounded-lower-tail check. Extending that first
band below zero for diagnosis merely exposed a second failure: `[0, 0]` followed
by `[1, 5)` was considered an undefined real-number gap. Scalar scoring at the
valid integer values was already correct. `_points_for_numeric_range` itself
was not the blocker; `_rule_error` ran first.

CASE_TABLE already identifies five canonical count metrics as DISCRETE:
performance count, personnel count, certification count, facility/equipment
count, and award count. Their registered units all have scale one. This change
uses the same explicit metric set and corresponding registered fact keys for
BRACKET programs. Amounts, ratios, years, unknown keys, other DSLs and source
extraction validation retain their existing contracts.

Each supported bracket is projected onto the nonnegative integers it contains.
Finite bounds must be integer-valued; unsupported fractional bounds and empty
integer rows remain blocked. Included/excluded endpoints determine the first
and last covered integers. Sorted intervals must start at zero, meet at the
next integer without missing or duplicating one, and end with an open upper
tail. No source bound or score is changed.

Exact values and estimated range endpoints must be finite nonnegative integers.
Count bounds, exact values, and estimated endpoints also have an absolute safe
integer limit of `2**53 - 1`. Values at or beyond `2**53` are rejected before
float conversion, including values already rounded by the fact/bound models.
This avoids assigning different scalar and range scores when adjacent large
integers collapse to the same float. The limit is confined to count BRACKET
guards; generic schemas and continuous metrics are unchanged.
Range scoring intersects the finite list of bracket intervals and returns all
possible score extrema, including interior nonmonotonic bands. Runtime depends
on the number of brackets, not the size of a count range. Invalid scalar inputs
also cannot prove maximum-score saturation. Missing facts and mismatched fact
bindings remain unscorable.

The twelve initial positive expectations failed against main `c981294`. The
candidate passes 40 new synthetic regressions and the 12 existing singleton
regressions. Checks cover all five scale-one metrics, exact/estimated scoring,
large ranges, omitted and duplicated integers, exclusive endpoints, unsupported
bounds, invalid facts, continuous controls, and evidence-binding preservation.
The scoring engine version advances to 1.7.5; extraction contract versions and
persisted attachment proofs are unchanged.

A subsequent precision review added eight focused boundary regressions. Six
failed before the safe-integer guard was added; all eight pass after the fix.
They check raw and model-coerced unsafe values, exact and estimated scoring,
bracket endpoints, the valid `2**53 - 1` boundary, and rejection of a Python
integer too large to convert to float without raising.

This is a local engine candidate, not proof that an observed attachment table
is ready to score. Literal/number/comparator mismatches, overlapping source
rows, unsupported price formulas, source completeness, and company evidence
must still independently pass their existing checks. No source document or
company evidence was loaded or added for these regressions.
