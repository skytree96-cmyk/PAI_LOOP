# Gateway output rejection details

This records the PR129 prompt-only gateway behavior retained for rollback. The
pending native transport accepts strict JSON without fence removal and extends
the bounded failure diagnostics; see [Native Anthropic gateway](NATIVE_ANTHROPIC_GATEWAY.md).

The optional `detail_code` field extends the existing `gateway-failure-v1`
failure object. Its stage must be `OUTPUT_NORMALIZATION`, its code must be
`OUTPUT_REJECTED`, and `upstream_http_status` must be null. The HTTP response
remains 500. Old four-field failures remain valid; absent/null details serialize
without the new field. Other stages cannot carry a non-null detail.

| Fixed detail | Meaning |
| --- | --- |
| `OUTPUT_EMPTY` | The chain text is missing, null, empty, or whitespace. |
| `OUTPUT_TYPE_INVALID` | The chain text is present but is not a string. |
| `OUTPUT_TOO_LARGE` | The text exceeds the existing character bound. |
| `OUTPUT_FENCE_INVALID` | Non-JSON text with fence markers is not one complete allowed wrapper. |
| `OUTPUT_JSON_INVALID` | Bare text, or the complete wrapper's contents, cannot be parsed as JSON. |
| `OUTPUT_NOT_OBJECT` | Parsed JSON is null, an array, or a primitive. |
| `EXECUTION_CONTEXT_INVALID` | A usable local execution context is unavailable. |
| `NORMALIZER_EXCEPTION` | The normalizer's dedicated error lane ran; it does not identify an upstream cause. |
| `TERMINAL_GUARD_REJECTED` | The final response guard rejected its input, including runtime input passthrough. |

The details contain no text excerpts, error messages, lengths, token counts,
identifiers, headers or provider response bodies. They identify a local rejection
boundary, not provider success, refusal, truncation, or the cause of an earlier
production failure. Legacy responses and failures before a guarded response may
still lack a detail. No raw output retention is introduced.

The normalizer first parses the intact text as JSON. A JSON string may contain
backticks, including inside the existing optional whole-output Markdown wrapper.
Only after intact JSON parsing fails does it try one anchored `json` or unlabeled
wrapper. Prose, arbitrary embedded blocks, multiple JSON objects/blocks and
non-object JSON remain rejected. Multiple wrapped blocks fail JSON parsing;
the new detail describes that parsing boundary, not an inferred model behavior.

Expected rejection returns a fixed envelope through the existing main connection.
Both terminal responders validate its exact keys, stage/code pairing and optional
enum, construct fresh response objects, and return HTTP 500. They never forward
the original input. Code-node error continuation still uses the dedicated sanitizer;
runtime input passthrough is rejected by the terminal guard.

Python extraction and bounded server-key-only diagnostics share the same strict
`GatewayFailure` model. Unknown details, extra fields and invalid stage/status
combinations are rejected as a whole. Current-manifest binding, auth, retry and
attachment reuse policies are unchanged. The successful extraction envelope and
optional/null usage contract are unchanged. No schema parser or model options
are added.

Deploy the compatible Python consumer first and confirm its Live version. Then,
through the approved browser operation, update only the normalizer code, output
failure sanitizer code, and the body/status expressions on the two responders.
Preserve node identities, credentials, edges, limits and execution-retention settings.
Run only separately authorized synthetic checks before resuming paid work.
