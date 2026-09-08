# Native structured JSON gateway (local implementation; provider canary pending)

The previous W13 used a prompt-only LangChain extraction node. This version sends
one Messages HTTP request with `output_config.format.type=json_schema`. It keeps
Sonnet 5, adaptive thinking, medium effort, the existing 20,000 output-token cap,
the authenticated webhook, and the original backend Pydantic/evidence checks.
It adds no model retry, repair chain, fallback model, tool, or second HTTP call.

`manifest.json` retains the already approved W13 `publish:true` allowlist.
`promotionState:awaiting-native-live-e2e` and `nativeCanaryState` explicitly record that
full gateway live E2E and promotion remain pending. Merging this code is
not evidence of n8n publication, provider schema acceptance, or extraction recovery.
Deployment remains the root operator's separately controlled manual operation.

## Why exactly two top-level fields use JSON string transport

The original `EXTRACTION_SCHEMA` has 18 nullable `anyOf` parameters. The first
native projection reduced this to 16 by making two anchor fields optional. A
second local candidate used required zero-or-one-item anchor arrays instead,
removing optional parameters. Root-operated full-schema SYN probes rejected
both with HTTP 400 `compiled grammar is too large`. These observations establish
grammar compilation failure for those requests, not a document failure or the
provider's internal complexity formula. The unsuccessful anchor candidate is
preserved in local history; its representation is no longer used.

Only top-level `quantitative_tables` and `quantitative_table_not_applicable` are
now required JSON strings encoding their complete original values. The strings
`"[]"` and `"null"` encode the corresponding original empty array and null. Every
other field retains its original type, including required nullable primitive
`EvidenceAnchor.page/section`. The provider grammar keeps only reachable
`EvidenceAnchor` and `ExtractedRequirement` definitions:

| Provider structure | Count |
| --- | ---: |
| Nullable unions, unique and reference-expanded | 4 |
| Optional parameters, both interpretations | 0 |
| Definitions / objects / properties | 2 / 3 / 19 |
| Enum sites / choices | 3 / 20 |
| Compact schema UTF-8 bytes | 2,971 |

All original quantitative definitions, fields, enums, evidence anchors and
numeric types remain in the complete original schema supplied with the prompt
and in the unchanged backend validator. They are removed only from the provider's
grammar graph because those two values are transported as strings. This does
not make arbitrary prose an acceptable quantitative value or replace structured
rules with free text. Provider-level grammar no longer constrains the inner
quantitative JSON; the gateway and backend must reject invalid inner values.

A trusted system note overrides exactly the two top-level wire types. The
decoder requires strings at those exact paths and strictly parses each complete
JSON document. Missing fields, Markdown, trailing tokens, duplicate object keys
(including escaped-equivalent keys), prototype keys, invalid numbers and wrong
original types fail closed. The outer JSON is also checked for duplicate keys.
No missing value is filled, substring salvaged, primitive coerced, or extra model
request made. Same-named fields elsewhere and ordinary strings/arrays are not
recursively interpreted as transport values. Parsing is bounded to 500,000
characters, 12,000 nodes and depth 60 per document; the existing decode budget
is separate so parsing does not consume its allowance.

Decoded values then traverse the entire original schema, including the original
quantitative references, required fields and enums. The backend's original
Pydantic range/length checks and source-evidence validation remain authoritative.
For example, page 0 or negative maximum points remain their supplied values and
fail original validation; they are never replaced with a convenient value.

Unsupported provider keywords present in the original schema (`minimum`,
`maximum`, `exclusiveMinimum`, `minLength`, `maxLength`, `maxItems`) remain
server-validated constraints. No unsupported `maxItems` keyword is added.
Schema limits stay 64,000 characters, source 140,000, final provider system
including the trusted note 12,000, and combined request 210,000. Output remains
20,000 tokens. Root's subsequent same-source/full-schema direct HTTP SYN probe
returned HTTP 200 with `end_turn` (reported input 9,115/output 208 tokens),
including the two string values `"[]"`/`"null"`. This establishes provider
acceptance for that synthetic request. Promotion metadata stays pending until
native full gateway live E2E checks. Even a passing empty SYN output is not evidence of
real quantitative extraction quality.

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
The global pending-native metadata blocks producer-only and all-workflow scripted
deployments even when the remote draft already contains native nodes. W13 must
be selected alone, and W10/W11/W12/W13 must all be confirmed inactive before any
CLI write while this canary remains pending. Progress persistence is explicitly
disabled with `saveExecutionProgress:false`, alongside the existing no-save settings.

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
source/output. The Node tests cover topology, limits, strict quantitative JSON string transport,
missing/invalid fields, cycles/references, HTTP and stop failures, thinking/usage,
credential boundaries, and both native and pinned Tournament terminal evaluation.
The observed synthetic provider payload is also a local regression: strict
decoding produces the original empty array/null and passes original Pydantic
validation. This local replay is not a deployed normalizer test. The direct HTTP
provider probe passed; full deployed gateway E2E and real attachment quality
remain unverified until root's separately authorized checks.

- [Anthropic structured outputs and complexity limits](https://platform.claude.com/docs/en/build-with-claude/structured-outputs#schema-complexity-limits)
- [Messages request API](https://platform.claude.com/docs/en/api/messages/create)
- [Official Message response type](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/message.py)
- [Official usage type](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/usage.py)

The credential/HTTP options were checked against n8n 2.33.7's published base 2.33.2
and LangChain 2.33.3 sources. HTTP Request 4.2 supports `predefinedCredentialType`
with `nodeCredentialType:anthropicApi`; the credential injects its key internally.
