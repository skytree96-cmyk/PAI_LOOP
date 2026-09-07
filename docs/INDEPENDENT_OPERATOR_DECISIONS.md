# Independent operator decisions

An authorized operator can record GO, HOLD, NO_GO, or CONDITIONAL_GO before a
current AI evaluation exists and after the notice deadline. Authoritative PPS
cancellation still blocks new decisions. Existing API-key, PIN, same-origin,
wrong-notice, and stale-evaluation validation remain in place.

Every API request requires a nonblank rationale; persistence repeats that check
for internal callers. The frontend requires the operator to write a reason when
analysis is missing or incomplete, including incomplete PPS attachment coverage.
It does not insert a generic default reason for those states.

The server captures an immutable analysis snapshot when writing each decision.
`analysis_state_snapshot` is `EVALUATED` only when a current evaluation and
complete source analysis exist, `INCOMPLETE` when a retained current evaluation
has incomplete source analysis, and `NOT_EVALUATED` when no current evaluation
exists. The snapshot records its completeness, analysis reason, effective and
stored notice status, deadline, capture time, and any linked evaluation's public
summary fields. Historical evaluations do not imply current analysis. Later
analysis never rebinds or rewrites earlier human decisions.

When a current evaluation exists, the operator route still requires its exact
identifier. A legacy evaluation hidden from the analysis display is retained
only as the decision request's concurrency token; this does not turn incomplete
analysis into a current qualification. Stale or foreign identifiers are rejected.
Database locking prevents the first or a replacement evaluation from crossing
the validated decision snapshot.

Migration `20260908_01_independent_operator_decisions` adds nullable snapshot
columns and makes `evaluation_id` nullable. PostgreSQL relaxes the constraint in
place. SQLite performs an atomic table copy using the original table definition,
changing only that constraint while preserving existing values, extension
columns, CHECK/foreign-key constraints, indexes, triggers, and referencing rows.
The migration checks copied data and foreign keys before commit, restores foreign
key enforcement on success or failure, and records the ledger only on success.
Existing SQLite databases upgrade without recreation; reruns are idempotent.

Once decisions with no evaluation have been stored, reverting to a schema that
requires an evaluation is incompatible with those records. Preserve the records
and nullable column when rolling back application code.
