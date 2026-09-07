# Safe W13 failure stages

Gateway contract 1.2 keeps successful extraction, strict output JSON handling,
model parameters, request/input/output bounds, credentials, retention and retry
budgets unchanged. Existing five node IDs, names, types and versions are retained.
The webhook now uses explicit response nodes: success returns the existing JSON
envelope with HTTP 200; handled failures return HTTP 500 with only:

```json
{
  "gateway_error": {
    "version": "gateway-failure-v1",
    "stage": "MODEL_EXECUTION",
    "code": "MODEL_EXECUTION_FAILED",
    "upstream_http_status": null
  }
}
```

The only other stage/code pairs are INPUT_VALIDATION / REQUEST_REJECTED and
OUTPUT_NORMALIZATION / OUTPUT_REJECTED. All errors remain failed HTTP responses;
none becomes an accepted extraction or starts another model call. Three isolated
error outputs construct fresh objects and reach only the failure response node.
Both responses use `Cache-Control: no-store`.

`upstream_http_status` is available only at MODEL_EXECUTION and only from a
structured error object's numeric `status`, `statusCode` or `httpCode`, in
400–599. Conflicting valid fields produce null. No string parsing, nested body
inspection, status-name inference, or message copying is performed. An SDK or
workflow configuration failure can also occur at MODEL_EXECUTION; that stage
does not prove an Anthropic call occurred.

The official Basic LLM Chain implementation (node version 1.9 included) can
reduce a continued error to `{error: executionError.message}`. In that case
upstream status is deliberately null. Deployed n8n behavior still requires a
separate version check and authorized verification; local mocks do not prove it.

The Python client accepts this diagnostic only from its `n8n_claude` provider,
an HTTP 500 response and the exact envelope shape. Unknown/extra fields or
inconsistent stage/code/status are discarded as a whole. The outcome remains
HTTP_ERROR / REVIEW with the existing fixed HTTP message and no automatic retry.
Persistence stores the sanitized object. Server-only recovery diagnostics adds
`attachments[].gateway_failure`, validated again and exposed only for the selected
current attempt with exact HTTP_ERROR / HTTP 500 provenance. Old records and
malformed/unselected attempts have null. No migration or backfill is needed.

Failure responses and diagnostic objects contain no message, document text,
headers, keys, execution ID, source ID, stack or provider body. The pre-existing
successful envelope's response ID behavior is unchanged and remains outside the
recovery diagnostic projection. Success/error execution data retention remains
`none`, with manual execution saving disabled; never enable raw logging to debug
this path.

## Later authorized rollout

1. Deploy the Python consumer first; the old generic HTTP 500 remains compatible.
2. Validate installed n8n support for `continueErrorOutput` and Respond to Webhook
   1.4. Stage only W13 using the existing deployment/credential-preservation
   process. Keep the exact webhook and Anthropic credential node bindings.
3. Before live model work, verify the active JSON has all three isolated error
   edges, explicit 200/500 response nodes and retention `none`. A separately
   authorized invalid synthetic request can verify INPUT_VALIDATION without
   reaching Claude; do not use a company document or loosen input validation.
4. On the next already-authorized bounded analysis, collect only status, fixed
   stage/code and nullable structured status. If the envelope is absent, report
   unclassified gateway/infrastructure failure, not a guessed provider cause.
5. Rollback can restore W13 1.1 without changing the Python consumer. Its new
   nullable field remains backward compatible. No existing failure is rewritten.

No production call, deployment, credential operation or model call was performed
for this patch. It does not establish why the previously observed 90 HTTP 500
attachment failures occurred. Existing manifest publication/promotion fields are
preserved; they do not certify this changed contract's live verification.

## Official references and local verification

- [Respond to Webhook](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.respondtowebhook/)
  documents explicit response mode, response status and unhandled-error behavior.
- [n8n workflow execution engine](https://github.com/n8n-io/n8n/blob/master/packages/core/src/execution-engine/workflow-execute.ts)
  recognizes `continueErrorOutput` and routes error items separately.
- [Basic LLM Chain implementation](https://github.com/n8n-io/n8n/blob/master/packages/%40n8n/nodes-langchain/nodes/chains/ChainLLM/ChainLlm.node.ts)
  establishes the continued-error string limitation above.

Local tests exercise strict successful output unchanged, validation and invalid
JSON failures through isolated edges, canary redaction, numeric/conflicting status
handling, safe Python envelope validation, no transport/corrective retry, current
attachment persistence/reuse and authenticated diagnostic reads. These are code
and synthetic contracts, not execution on the deployed n8n runtime.
