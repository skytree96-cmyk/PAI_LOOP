# Bounded continuation throughput

W11 dispatches at most two existing one-notice chunks at a time. PostgreSQL
permits two queue executions for different notices while retaining the existing
parent, segment, exact chunk claim, extraction contract, and retry limits.
The workflow retains all chunk outcomes before finalizing the segment and rejects
missing, substituted or duplicate notice keys and duplicate child job IDs.
Out-of-order successful responses are valid.

Queue requests acquire a shared lock on the existing global execution key, an
exclusive notice lock, and one of two exclusive execution lanes. The lane uses
the durable chunk index modulo two; two adjacent chunk indices can run together.
A same-notice request remains serialized even if it has a different lane.
Manual and generic batches retain the global exclusive lock and cannot overlap
queue execution. Older servers taking the original exclusive lock also exclude
new shared executions. Failed lock cleanup invalidates the dedicated connection
instead of returning a locked PostgreSQL session to the pool.
The dedicated connection uses autocommit. Lock acquisition has a total ten-second
budget; a busy slot returns HTTP 503 with Retry-After 15 before any child claim or
provider work. Partial or uncertain acquisition invalidates its connection. The
original lock_timeout is restored before executing work.

SQLite retains the existing process-wide serialization. CI uses a disposable
PostgreSQL service for the actual advisory-lock concurrency tests; a local run
without that explicit test database reports those tests as skipped.

The 450-second extraction budget, 401-second complete attachment reservation,
600-second HTTP boundary, attachment call limits, paid retry accounting, current
source validation, and human decisions are unchanged. This change neither
creates a campaign nor changes stored parent planning limits. W10 remains serial.
Existing parents must offer at least two chunks for parallelism to take effect.
Nonconsecutive chunks can share a lane and therefore serialize; two is a maximum,
not a promised speed multiplier.

## Rollout and measurement

1. Pass focused workflow tests and the full CI gate, including PostgreSQL locks.
2. Prevent automatic workflow publication during the coordinated transition.
   Unpublish only W11 and let its current saved execution finish; do not stop an
   in-flight provider request or create a replacement campaign.
3. Merge the reviewed change and wait for the exact commit to become Render Live.
4. Apply W11's reviewed node changes with its existing credentials and node IDs,
   then publish and restore the deployment workflow's original enabled state.
5. Confirm the same parent resumes, both different-notice chunks overlap, and
   the final output has no duplicate or missing results. Observe service errors
   and completed-notice throughput before revising the completion estimate.

If the service regresses, return W11's loop batch size to one at a drained segment
boundary. This serializes W11 dispatch, not every possible caller. To restore the
server-wide exclusive gate, roll back the server to the prior verified release
after draining work. Server guards remain compatible with serial dispatch. Do not clear
leases, increase paid retry limits, drop validation, or run a new campaign.

The theoretical throughput ceiling is twice the previous single-lane rate.
Real improvement depends on document duration, lane balance, shared CPU/memory,
database work, and provider latency. CI success alone is not live throughput proof.
