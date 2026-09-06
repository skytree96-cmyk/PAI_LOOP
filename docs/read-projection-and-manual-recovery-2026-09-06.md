# Current notice read performance and interrupted manual work

Current attachment selection now scans newest-first and stops validating older
retries once the same valid, current-manifest attachment attempt has been found.
A newer invalid record still falls through according to the existing contract;
a newer bound review still replaces an older accepted result.

Dashboard and list serialization reuse an exact quantitative record binding
check only within one read-only notice projection. The scope is discarded on
completion or error, isolated between execution contexts, and never wraps a
worker or write transaction. Source, prompt, schema, validator, fingerprint and
attachment coverage gates remain in force on each subsequent read. Database
batches remain bounded to preserve the small production worker memory limit.

Manual status polling now detects a reservation stranded by process replacement.
Recovery requires both the existing batch timeout/grace period to have elapsed
and the manual worker's execution lock to be free. An active long-running worker
is never cancelled based on age alone. The interrupted request is recorded as
failed with incomplete usage accounting; it is not falsely marked complete or
automatically submitted for a second paid extraction. A delayed callback cannot
revive a terminal reservation. A subsequent explicit retry uses the normal
current-source and idempotent analysis gates.

Validation: 1,391 tests passed with 87.79% coverage. Synthetic regression cases
cover superseded accepted/review/invalid attempts, source changes between reads,
exception cleanup, context isolation, active workers, fresh reservations,
interrupted reservations, callback fencing and request authorization. A local
microbenchmark with 80 retry records and 40 repeated projections reduced full
binding checks from 12,880 to 40 (about 11.6 times faster for that synthetic
projection). This is not a production end-to-end latency measurement.
