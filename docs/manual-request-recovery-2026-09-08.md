# Resume a manual analysis request without starting it again

A browser can lose the response containing its analysis request ID, or stop polling
after a network interruption. Repeating the same notice request now returns the
existing live `MANUAL_ANALYSIS` reservation through the normal status projection.
It does not create a callback, change the original request intent or actor, reset
quota, acquire its worker lease, or start another provider call.

The server reads the active reservation before attempting the execution lock and
checks again at the lock boundary. A running worker therefore does not block a
read of that same request. Another notice or a pre-specification request cannot
be substituted. Existing authorization and same-origin checks run before the
lookup; account POST requests still require CSRF. Reattaching to existing work is
a status read. Starting new paid work still requires its separate permission and
the existing limits, including the configured zero/unlimited hourly setting.

The existing orphan recovery remains authoritative: age alone cannot terminate a
worker. An old reservation is recovered only when its execution lock is free.
Recovery returns the terminal review result without scheduling replacement work.
A delayed callback cannot revive the recovered reservation because the worker
checks the same persisted request ID and RUNNING status before execution.

On the browser, the notice is marked busy before asynchronous state lookup and
confirmation, preventing overlapping preflight actions. During confirmation it
is labeled as checking the request, not as executing analysis. Cancellation,
preflight error and terminal response all release this state. Confirmation still
precedes every POST. Each operation has its own identity and account generation;
logout/account change clears it, stops old polling, and prevents an old finalizer
from clearing a newer operation. The quantitative diagnostic retry also stops on
account change and keeps polling the original request ID. A mismatched status
response is rejected, and unknown terminal status is never shown as completion.

No provider, attachment, continuation, lifecycle, deadline or timeout budget is
increased. The current 1,800-poll limit and three-second spacing remain unchanged;
network request time is additional, so those constants are not a measured total
completion time. FastAPI background callbacks remain process-local: persistence
of a reservation alone does not prove its callback survived a process restart.
This change makes such reservations discoverable/recoverable without silently
rerunning them; it does not introduce another worker or durable dispatch system.

Synthetic regressions cover busy-worker reattachment, delayed reservations beyond
the short duplicate-request cooldown, idle-only recovery, callback non-revival,
request scope/actor preservation, account CSRF and paid permissions, duplicate
preflight actions, account changes during requests/poll delays, confirmation
cancellation, status identity and busy-state cleanup. No live service or provider
was used for validation.

For a live incident, correlate the manual request ID with its child batch ID and
record safe status/timing/lease metadata. Distinguish a queued/running reservation
from a terminal REVIEW response. A `MODEL_HTTP_FAILED` marker alone does not
identify an HTTP status, gateway failure, timeout or rate limit. Establish that
from sanitized error class/status/timing information without exposing response
bodies, credentials, source documents or provider identifiers. Code regressions
are not proof of the cause of an operational incident.
