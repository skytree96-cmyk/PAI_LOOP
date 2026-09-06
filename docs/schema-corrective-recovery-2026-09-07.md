# Source-based schema failure recovery — 2026-09-07

A completed provider response can contain invalid CASE rows even when its JSON
schema was sent with the request. Previously, a schema failure stopped after the
first response. The client now makes at most one fresh extraction from the same
source when its existing two-request budget still has capacity.

## Acceptance and call boundaries

- The initial failure must be `SCHEMA_VALIDATION_ERROR`. Invalid output JSON,
  a non-object output, missing required fields, and nested schema violations use
  the same bounded recovery. Initial network/HTTP errors, refusals, incomplete
  responses, missing output, and unknown attachment IDs do not trigger it.
- The second request keeps the original system instruction, complete source
  prompt, allowed attachment IDs, output schema, model, token limit, and
  `store: false`. Feedback contains only locally generated field paths and fixed
  validation codes. The invalid raw response, unknown field names/values,
  provider prose, and credentials are not copied into the request or diagnostics.
- The second response passes the ordinary schema, attachment identity, and exact
  source-quote checks. Downstream quantitative source, table, total, formula, and
  evidence checks remain unchanged. Extraction `ACCEPTED` still does not mean a
  quantitative rule is `AVAILABLE`, nor does it approve a company score.
- No data are coerced, thresholds invented, percentages clamped, or missing
  evidence turned into zero. A schema/quote/identity/provider failure on the
  second response remains an honest REVIEW with its actual final reason.
- Actual HTTP transmissions share `max_total_api_calls <= 2`, including any
  transport retries. A one-call budget permits no recovery. The n8n gateway still
  has zero blind transport retries. Schema and quote correction cannot chain to
  a third call. Usage, missing usage, response receipt, and request latency are
  aggregated across both attempts.

## Separate correction contracts

The schema route is a fresh extraction because the initial response never became
an accepted structured payload. The quote route continues to repair only exact
anchors while preserving its existing structural comparison and prompt.

CASE shape diagnostics distinguish these fixed reasons without emitting values:

- `CASE_NUMERIC_SHAPE_INVALID`
- `CASE_CATEGORY_SHAPE_INVALID`
- `CASE_PERCENT_AWARD_OUT_OF_RANGE`

The outer error stays `SCHEMA_VALIDATION_ERROR`. Existing safe field diagnostics
and the public `MODEL_SCHEMA_INVALID` label remain compatible.

Schema attempts store `pai-loop-schema-correction-0.1.0` in the existing
`correction_prompt_version` field, with `corrective_retry_used: true`. The quote
version stays `pai-loop-quote-correction-0.6.1`. The extraction prompt/schema,
processing, quantitative validator, engine, accepted evidence, manifests, and
fingerprints are unchanged.

Existing accepted records remain reusable. This release does not invalidate
cached failures by version alone. The established explicit retry-ID snapshot is
required for an immediate retry of an existing REVIEW; the newly persisted
failure has a new ID and subsequent continuations reuse it during cooldown.
A schema correction that ends in `UNVERIFIED_QUOTE` is also a completed current
correction attempt: both reuse selectors recognize the exact released schema
correction version only with the current extraction header. This preserves its
cooldown and prevents ordinary continuations from repeating paid calls. Unknown
versions and a legacy extraction header carrying the new schema version are not
accepted as current correction contracts. Existing stale quote recovery remains.

## Verification

New synthetic tests cover the three CASE reasons, other schema failures, raw
output non-disclosure, source/request identity, one- and two-request budgets,
transport retry accounting, terminal second failures, unchanged quote correction,
boolean rejection, accepted first responses, downstream quantitative rejection,
SQLite persistence, exact retry IDs, and newly stored REVIEW reuse. Existing
extraction, PPS, and released-contract regression suites also run.

The existing single-response diagnostic helper explicitly requests a one-call
budget, preserving its purpose. Default two-call recovery and final failures are
covered separately rather than weakening those assertions.

All verification uses synthetic inputs and mock HTTP transports. Production
re-extraction, provider success rates, deployment, and source availability require
separate operational verification; local tests do not establish those outcomes.
