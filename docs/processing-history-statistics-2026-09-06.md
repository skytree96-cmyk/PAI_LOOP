# Processing history and current analysis statistics

A prompt or quantitative validator revision can invalidate a stored extraction
without erasing the fact that the current attachment was processed. The dashboard
now reports distinct current-manifest files and notices with recorded processing
history alongside the existing current-contract audit and accepted counts.

Historical counts are observational only. They cannot activate rules, reuse stale
extraction, expose old scores, or satisfy attachment coverage. Changed manifests,
unbound attachment digests, unsupported metadata schemas, and records from other
source kinds do not contribute. Multiple retries count once per current file.
Deterministic document failures count as processing; this is not an API-call count.

Progress percentages no longer inherit the count-unit suffix from generic cards.
Regression coverage checks stale prompt/processing/validator contracts, binding
changes, deduplication, pending versus failed records, the API, and rendered text.

When the dashboard request is delayed or fails, filtered board rows no longer
stand in for total stored notices, cancellations, or missing outcomes. These
aggregates stay unavailable until observed, or retain their last observed value
and timestamp after a successful mutation whose metadata refresh failed.
