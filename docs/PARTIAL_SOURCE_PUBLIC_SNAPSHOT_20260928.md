# Partial Source Public Snapshots

When a current attachment manifest is incomplete, source-validated criteria can
still produce item scores and a subtotal under `PARTIAL_SOURCE`. The snapshot
writer and public reader now preserve those rows even when every retained item
is `CONFIRMED` or `ESTIMATED` and the notice-level result must remain `REVIEW`.

The public contract requires incomplete source and validation states, explicit
unresolved reasons, nonempty public item rows, and exact agreement between the
rows and the subtotal. The subtotal denominator covers only the retained rows.
No score or maximum is invented for unread attachments. The result never has a
final total, a minimum-score claim, or a confirmed notice-level status.

Public responses retain the existing allowlist: labels, item points, ranges,
statuses, and aggregate values. Company values, source quotations, evidence IDs,
and internal hashes remain private. Both direct and stored public projections
state that the partial subtotal is not the full notice total.

The detail overview calls this a subtotal of the known criteria, labels coverage
against only those criteria, and withholds a whole-notice readiness conclusion.
Complete-manifest displays keep their existing labels.

Regression coverage runs stored synthetic extraction through the analysis API,
persists the score snapshot, and reads it through the public API with fresh
calculation disabled. Cases include confirmed items, unresolved company facts,
and a known criterion awaiting review. Corrupted or stronger partial-source
claims remain rejected. Derived financial ratios also retain their estimated
item points through storage and the public API. These checks use local test databases and make no
provider calls or production changes.
