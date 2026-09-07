# W10 award refresh diagnostics contract

`Validate Award Refresh Batch` accepts the additive `window_error_counts` field
from `AwardHistoryRefreshOut`. Its absence remains compatible with older server
responses. When present, it must be an array of at most 2,048 records, each with
exactly `phase`, `error_type`, `http_status`, `provider_code`, and `count`.

- Phase is `PRIMARY` or `FALLBACK`; error type must be a current fixed PPS error
  classification from `integrations/common.py`.
- Count is a positive safe integer. HTTP status is null or an integer from 100
  through 599 and is allowed only with `HTTP_ERROR`.
- Provider code is null or the fixed numeric protocol form `0` or two digits,
  allowed only with `SERVICE_ERROR` or `PROVIDER_RESULT_ERROR`.
- Unknown fields, free text, raw responses, headers, and malformed values fail
  validation with a fixed error. These diagnostics are validated and omitted
  from the existing downstream summaries.

The response's `PARTIAL` state and required warnings remain authoritative.
Recovered primary errors may coexist with `COMPLETED`; diagnostic counts alone
neither promote a partial result nor downgrade completed coverage. Existing
batch, dry-run, identity, window and aggregate checks remain in place.

The daily workflow self-test includes the award response contract. A focused
Python test also passes responses produced by the actual synthetic backend
route directly to the workflow JavaScript, so additive API fields cannot bypass
the integration check. Applying the change requires replacing only the existing
validator node's JavaScript; node identity, connections, credentials, workflow
settings, analysis gates and human decision handling remain unchanged.
