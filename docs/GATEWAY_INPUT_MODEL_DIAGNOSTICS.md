# Gateway input and model failure details

The native W13 contract retains `gateway-failure-v1` and HTTP 500. It adds optional,
stage-specific fixed `detail_code` values to input and model failures. Existing
four-field records remain readable. The shared Python model preserves valid new
details in the attachment attempt and the server-only current-manifest diagnostic
response. Public pages gain no exception text or provider data.

Input validation now distinguishes the request shape, model/options, one-shot
policy, message identity, empty/NUL content, system/source/schema/combined length
limits, and schema projection failure. The full allowlist is
`scripts/gateway-failure-details.mjs`; it is checked against the Python contract.
For example, `INPUT_SOURCE_TOO_LARGE` is the user-message bound, whereas
`INPUT_PROVIDER_SYSTEM_TOO_LARGE` is the system message after adding the trusted
transport convention. Bounds count JavaScript string length, not model tokens.

The input sanitizer recognizes only the fixed validator error message in the
n8n error string or `error.message`, optionally followed by the exact numeric
` [line N]` or ` [line N, for item N]` suffix from
[n8n 2.33.7's Code ExecutionError](https://github.com/n8n-io/n8n/blob/n8n%402.33.7/packages/nodes-base/nodes/Code/ExecutionError.ts).
The task-runner [WrappedExecutionError](https://github.com/n8n-io/n8n/blob/n8n%402.33.7/packages/nodes-base/nodes/Code/errors/WrappedExecutionError.ts)
preserves the message. Suffixes are discarded. Matching is anchored to the whole
message; trailing prose or newlines are rejected. Unexpected runtime errors,
other message shapes, and missing error data become `INPUT_VALIDATOR_FAILED`. It never
returns an exception, stack, offending value, request field value, or text excerpt.
This fallback identifies the boundary only; it does not prove invalid user input.

Model failures describe only observed transport metadata:

| Detail | Required evidence |
| --- | --- |
| `MODEL_HTTP_ERROR` | An explicit integer HTTP status from 400 through 599. Conflicting status fields are not accepted. |
| `MODEL_TRANSPORT_TIMEOUT` | Exact `ETIMEDOUT` or `ESOCKETTIMEDOUT` code, without an HTTP status. |
| `MODEL_CONNECTION_RESET` | Exact `ECONNRESET`, without an HTTP status. |
| `MODEL_CONNECTION_REFUSED` | Exact `ECONNREFUSED`, without an HTTP status. |
| `MODEL_DNS_FAILURE` | Exact `ENOTFOUND` or `EAI_AGAIN`, without an HTTP status. |
| `MODEL_TRANSPORT_UNKNOWN` | Error-lane metadata is absent, contradictory, malformed, or unrecognized. |
| `MODEL_RESPONSE_INVALID` | The normalizer received neither a valid HTTP 200 response nor an explicit 400–599 status. |

Only `error.code` and `error.cause.code` are considered for transport mapping.
Messages are never searched for a status, timeout, credential problem, or other
cause. `ECONNABORTED` is intentionally unknown: it does not establish a timeout.
Neither a 429 nor a timeout detail identifies the provider's underlying cause.

Both terminal expressions validate the allowlist and stage/status pairing and
rebuild the response. A model HTTP detail requires a status; transport details
cannot carry one. Input/model details cannot carry output stop/usage fields.
Unknown fields, cross-stage codes, and private extras fail closed. Native output
diagnostics, including `NATIVE_STOP_MAX_TOKENS`, keep their existing behavior.

No request limits, token budgets, retries, credentials, workflow publication
allowlist, or execution retention settings change. Older input failures cannot
be retrospectively assigned a detail. Publish/activation and paid probes remain
separate operations; this change performs none. A rollout must put the compatible
Python consumer in service before publishing the generated W13 changes. The
pending native-canary metadata remains pending until separately verified.
