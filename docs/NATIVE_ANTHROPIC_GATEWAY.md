# Native structured JSON gateway (local implementation; provider canary pending)

The previous W13 used a prompt-only LangChain extraction node. This version sends
one Messages HTTP request with `output_config.format.type=json_schema`. It keeps
Sonnet 5, adaptive thinking, medium effort, the existing 20,000 output-token cap,
the authenticated webhook, and the original backend Pydantic/evidence checks.
It adds no model retry, repair chain, fallback model, tool, or second HTTP call.

`manifest.json` retains the already approved W13 `publish:true` allowlist and
existing workflow promotion state. `nativeCanaryState` explicitly records that
this transport has not yet passed a real provider request. Merging this code is
not evidence of n8n publication, provider schema acceptance, or extraction recovery.
Deployment remains the root operator's separately controlled manual operation.

## Why exactly two fields use omission

The actual `EXTRACTION_SCHEMA` has 18 nullable `anyOf` parameters. Anthropic's
documented union limit is 16 and includes both `anyOf` and type arrays. Replacing
`anyOf` with `type:[...,null]` cannot avoid that limit. Omitting nullability from
all 18 parameters would create 34 optional locations if references are expanded,
exceeding the documented optional-parameter limit of 24.

Only `$defs.EvidenceAnchor.page` and `.section` become optional, non-null provider
properties. Every other nullable remains unchanged. The resulting schema has:

| Count interpretation | Union parameters | Optional parameters |
| --- | ---: | ---: |
| Unique definitions | 16 | 2 |
| References expanded per use | 16 | 18 |

The prompt explains that omission of these two fields means unknown. The decoder
walks the exact original schema, including referenced objects and arrays, and
restores only these two absent fields to `null`. It preserves explicit values,
including explicit nulls. It never fills missing text, evidence, scores, arrays,
or other required fields. Missing non-nullable/other required fields, unknown
properties, wrong structural types, invalid enums, cycles, external references,
unknown schema keywords, oversized schemas, or cap violations fail closed.

The adapter moves only the unsupported keywords present in the production schema
(`minimum`, `maximum`, `exclusiveMinimum`, `minLength`, `maxLength`, `maxItems`)
into descriptions. It keeps the complete original schema in the prompt and
passes the decoded object to the unchanged backend validation. For example,
explicit `page:0` is preserved by the transport and rejected by Pydantic's
original `page >= 1` rule. No constraint is converted into a factual assertion.

Schema size remains bounded at 64,000 characters; source/system limits remain
140,000/12,000 characters. The 210,000 combined-character cap now also counts the
native schema and transport convention, so native formatting cannot silently
increase the request budget. Internal provider grammar/compilation limits can
still reject a schema inside these public caps. A full-schema synthetic probe
is required before any real document is retried.

## Request and response contract

The HTTP node is exactly `Claude Sonnet 5 Native JSON`, type
`n8n-nodes-base.httpRequest`, version 4.2. It posts to the fixed URL
`https://api.anthropic.com/v1/messages`, using n8n's predefined `anthropicApi`
credential and explicit `anthropic-version:2023-06-01`/JSON headers. Existing
credentials are selected in the n8n UI; no API key or credential ID belongs in
workflow JSON, Git, UI fragments, prompts, logs, or this document.

The body contains only `model`, `max_tokens`, `system`, one user `messages` item,
`thinking:{type:adaptive}`, `output_config:{effort:medium,format:...}`, and
`stream:false`. OpenAI-style `store`, `input`, `text`, and `service_tier` do not
reach Anthropic. The webhook still accepts its existing backend request contract.
The HTTP node has no retry, pagination, redirects, or tools. Its 180-second n8n
timeout is a response-start timeout; the existing bounded backend/operation
deadlines also remain authoritative. Existing backend corrective-request policy
is unchanged: it is a separate caller request under its existing attachment
budget, not an additional call created by this gateway. The first native probe
must invoke the gateway exactly once.

A successful response requires HTTP 200, matching model, message/assistant type,
`stop_reason:end_turn`, and exactly one text block containing one JSON object.
Thinking/redacted-thinking blocks are ignored; their text, signatures, raw
provider message IDs, headers, and errors are never returned. Other block types,
multiple/empty text blocks, Markdown fences, malformed JSON, truncation, refusal,
and other stop reasons fail with the existing explicit HTTP 500 envelope. The
decoder performs no fence removal, JSON substring extraction, coercion, or repair.

Input usage is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.
The authoritative output count already includes thinking and is never incremented
again by a separate thinking counter. The neutral three-key usage envelope stays
bounded. Known output stops retain only fixed `detail_code`, allowlisted
`stop_reason`, and those numeric/null usage counters, including on failure.
The Python consumer accepts them only inside the exact gateway HTTP 500 envelope;
both n8n terminal expressions independently validate/reconstruct them. Legacy
four-field errors and previously added output details remain compatible. No
provider prose or reasoning is copied into telemetry or diagnostic responses.
If a contributing cache/input counter is absent, null, or invalid, the combined
input/total remains null; an unreported count is not inferred to be zero.

## Credential and rollout boundary

The binder now targets only the exact native HTTP node contract. Normal deployment
preserves credentials only for an exact name/type match. It deliberately does
not transfer credentials from the retired LangChain node to a different node
type. The root operator selects the existing Anthropic credential in the new
node through the UI and preserves the existing webhook Header Auth. A scripted
PUT fails before mutation if the native binding is missing. The older exact
Sonnet 4.6-to-5 migration remains limited to its original LangChain contract.

Before root stages/publishes the native draft: keep W11 paused, drain paid work,
confirm no in-flight requests, preserve the approved PR129 node version and
credential bindings, and review the credential-free fragment. A legacy-to-native
scripted migration additionally requires W13-only selection and inactive producer
workflows before any write. No workflow is activated by local generation/tests.

Generate the reviewed source and local UI fragment with:

```text
node scripts/build-native-gateway.mjs --emit-ui-fragment
```

The fragment in `.local/native-gateway-ui-fragment.json` contains only nodes,
connections, and non-persistence settings. Importing it does not supply credentials.
The root operator must verify the new native node and both credential selections,
publish the draft manually, then send one short synthetic source with the **full
production schema**, never a tiny substitute schema. A small output with empty
requirements/tables tests compilation and transport; it does not prove real
attachment extraction quality. On failure, preserve the bounded diagnostic and
restore the PR129 node version; do not automatically retry a paid request.

## Local evidence and primary references

`tests/test_native_gateway_schema.py` runs the actual Python schema through the
JavaScript adapter/decoder and then original Pydantic validation using only SYN
source/output. The Node tests cover topology, limits, nullable preservation,
missing/explicit fields, cycles/references, HTTP and stop failures, thinking/usage,
credential boundaries, and both native and pinned Tournament terminal evaluation.
These are local tests; a real Anthropic request and deployed n8n execution remain
unverified until root's separately authorized synthetic probe.

- [Anthropic structured outputs and complexity limits](https://platform.claude.com/docs/en/build-with-claude/structured-outputs#schema-complexity-limits)
- [Messages request API](https://platform.claude.com/docs/en/api/messages/create)
- [Official Message response type](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/message.py)
- [Official usage type](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/usage.py)

The credential/HTTP options were checked against n8n 2.33.7's published base 2.33.2
and LangChain 2.33.3 sources. HTTP Request 4.2 supports `predefinedCredentialType`
with `nodeCredentialType:anthropicApi`; the credential injects its key internally.
