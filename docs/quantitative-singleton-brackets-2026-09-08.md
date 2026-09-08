# Align extracted singleton bracket bounds with the existing scoring engine

The extraction validator and persisted AVAILABLE-record invariant rejected
every bracket with equal lower and upper bounds. The scoring engine already
accepts an equal-bounds interval when both endpoints are included and matches
exactly that one value. This mismatch incorrectly adds `INVALID_BRACKET_BOUNDS`
to a closed singleton such as `[0, 0]`.

Both validation paths now use the same nonempty-interval rule: lower must be
less than upper, or they must be equal with both endpoints included. Reversed
bounds and equal bounds with either endpoint excluded remain invalid. Numeric,
literal, comparator, evidence, overlap, and table validation are unchanged.

The synthetic regression initially produced two expected failures. After the
change, 12 focused cases pass. Validation and persisted-record loading accept a
fully anchored singleton; both reject exclusive singletons. The unchanged
scoring engine validates a complete negative/singleton/positive partition and
returns the expected values on either side and at the singleton. Overlapping
neighbors, reversed bounds, missing criterion literals, and independently
unsupported number/comparator text stay blocked.

This change does not establish that an observed performance-count table is
usable. A literal such as `실적없음` still lacks the existing numeric/comparator
proof for a structured `[0, 0]` bracket, and `[0, 0]` followed by `[1, 5)` still
requires separate handling of the scoring engine's continuous-range coverage
contract. No missing numeric data is synthesized. Existing persisted review
records are not rewritten or upgraded by this local candidate, and the
extraction contract/fingerprint version remains unchanged.
