# Partial subtotal from an unresolved manifest — 2026-09-22

## Measured cause

A read-only production survey of the 919 notices that hold a stored
`quantitative.total` snapshot found **no notice with an AVAILABLE quantitative
profile**: all 918 evaluated profiles were `INCOMPLETE` and every activation was
`REVIEW_REQUIRED`. Seventeen notices already held source-validated candidates,
yet each produced zero criteria, no denominator and no points.

Those seventeen were not blocked by table validation. Their activation reasons
were manifest-level: `ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT` (12),
`EXTRACTION_DECLARED_INCOMPLETE` (9), `ATTACHMENT_INCOMPLETE` (8) and
`VALIDATED_RECORD_MISSING` (8). One attachment proving a scoring table did not
survive another attachment in the same manifest being unresolved.

The existing partial path could not apply: `_partial_profile_review_criteria`
requires `profile.status == "REVIEW"`, and it also requires a fully processed
manifest. Production profiles are `INCOMPLETE`, so that path was unreachable.

Resolving only the coverage inputs would not have helped: none of the seventeen
was blocked by coverage codes alone, and every one carried at least one further
reason.

## Change

`PARTIAL_SOURCE` is a new activation status for a profile that is incomplete as
a whole while some attachment's rules are source-validated. It reports the
validated rows as a subtotal and names the unresolved remainder.

The contract keeps the claim no stronger than the source:

- the overall status is always `REVIEW`; no path produces a confirmed total
- `rule_source_status` and `source_validation_status` stay `INCOMPLETE`
- `minimum_score` is dropped, so `meets_minimum` stays unknown: a minimum
  printed on one table cannot judge a subtotal
- the unresolved remainder must be stated as activation reasons, or the request
  is rejected
- review rows belong to `PARTIAL_ACTIVE` and are refused here
- the assumption text states that the subtotal is not the notice total

Verified company evidence resolves against these rows, because each row carries
the same per-criterion source binding as an active request. Only the notice
total stays unresolved, never an individual row. Inactive requests still never
read company inputs.

The switch is off by default. Notice scoring opts in; the reviewed-input
preview and company-evidence binding stay fail-closed on an incomplete source,
including their existing guarantee that an incomplete source never reads
company inputs.

## Measured effect

Re-running the same read-only survey after the change: eighteen notices (13 of
them open) produced criteria and a subtotal range where all had produced
nothing. One notice reached a confirmed item — a 부산광역시교육청 notice whose
credit-rating criterion resolved to 10 confirmed points, giving a 10–20 range
against a 20-point partial denominator with two performance rows unresolved.
Its overall status remained `REVIEW`.

The remaining items are unscored because the company credit certificate is bound
per notice and only one such binding exists. Registration and binding stay a
reviewed operator action; this change does not create or infer one.

## Boundary

This reports what the source already proved and what it did not. It does not
relax source validation, does not infer numbers, units, comparators or awards,
and does not persist a new record. Stored extraction records, contracts and
fingerprints are unchanged.
