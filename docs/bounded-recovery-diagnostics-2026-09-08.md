# Bounded recovery diagnostics

`POST /api/v1/diagnostics/notice-recovery` reads stored PPS attachment and
quantitative-rule diagnostics for **1–25 explicit, unique notice keys**. It does
not download attachments, invoke a model, reserve a queue job, recover a stale
job, recompute an evaluation, or write business/account records. This endpoint
supports selecting a later retry; it does not authorize or execute that retry.

Use the existing n8n server header credential for `X-PAI-LOOP-API-KEY`. The key is
required even in development. Browser metadata (`Origin`, `Referer`, any
`Sec-Fetch-*` header), cookies, manual PIN, private-evidence token and CSRF headers
are explicitly rejected, even alongside a valid server key. Existing public,
PIN/BFF, account and private-evidence route permissions are unchanged.

Safe request body example; substitute only previously selected public notice
keys, never credential values or source text:

```json
{
  "notice_keys": ["PPS-SYN-NOTICE-001", "PPS-SYN-NOTICE-002"]
}
```

In the n8n HTTP Request node, use POST, JSON body, the existing server credential,
and no browser/cookie headers. Each response is `Cache-Control: no-store`.
Empty/over-limit/duplicate/malformed keys and additional body properties return
422 with a fixed message; missing keys return 404 before returning a partial
projection. Validation responses never echo submitted input. Do not call this
route from frontend JavaScript or copy the server credential into a browser.

## Response contract

`diagnostic_version` is `notice-recovery-diagnostics-v1`.
`notices[]` preserves request order. Each row contains:

- `notice_key`, `notice_status`, `provider_disposition`, `diagnostic_status`.
  Notice lifecycle and provider cancellation remain distinct. `diagnostic_status`
  is `OK` or `UNAVAILABLE`; a malformed-data/computation failure is explicit and
  cannot become zero successful counts. Other notices can still be classified.
- `metadata_schema_current` (nullable); `analysis_state` and
  `analysis_reason_code` from the existing PPS document-audit projection;
  `attachment_count`, `invalid_manifest_slot_count`, `audited_attachment_count`,
  `accepted_attachment_count`, `recorded_attempt_attachment_count`, and
  `attachment_coverage_complete`. Recorded processing history is distinct from
  attempts eligible for the current audit. These are document statuses, not a
  qualification, company score, or human decision.
- `attachments[]`: ordinal within the validated manifest, allowlisted extension
  (unknown extension becomes null), public state/reason, `safe_error_code`,
  `error_code_redacted`, at most 20 `processing_warning_codes`, and
  `processing_codes_redacted`. Invalid manifest slots are counted separately,
  never invented as real attachments.
- Attachment binding metadata: `manifest_bound_attempt`, `attempt_contract`
  (`CURRENT`, `LEGACY_CASE_V1`, or `NONE`), `stored_document_digest_matches`,
  `stored_download_complete`, `stored_source_read_complete`,
  `stored_analysis_input_complete`, and `stored_document_digest_basis`
  (`DOWNLOADED_BYTES`, `FAILED_DOWNLOAD_MARKER`, or null). Missing/malformed
  stored provenance is null, not an inferred success. Matching stored digests
  can describe a failed-download marker; equality never proves a fresh download.
- `quantitative`: profile status, expected/processed/bound attachment counts,
  table status counts, available/review candidate counts, aggregated
  `issues[]` and `review_candidate_issues[]` (`code`, `disposition`, `count`),
  `activation_reasons`, and `code_lists_may_be_truncated`. Existing code lists are
  capped at 100; reaching a cap is reported. Unknown table statuses are counted
  under `UNKNOWN`. No candidate shapes or company inputs are returned.

Only fixed code vocabularies cross this boundary. Unknown/new quantitative codes
are counted under `UNRECOGNIZED_DIAGNOSTIC_CODE`; arbitrary uppercase strings
and prefix matches are not accepted. Processing errors use a fixed parser/safety
vocabulary plus the finite HTTP status vocabulary emitted by the PPS downloader.
Raw provider error messages and HTTP response bodies are never examined for
details. The response contains no document names, attachment/internal IDs,
hashes, source quotes, company facts, provider identifiers, or raw logs.

## Interpretation and freshness limits

- `safe_error_code=XLS_PARSE_FAILED`, `XLS_CODEPAGE_UNVERIFIED`, or
  `XLS_FORMULA_EXPRESSIONS_UNAVAILABLE` separates parser refusal from unverifiable
  encoding and missing formula expressions. A stored old parser failure does
  not establish that the current parser still fails. Free stored-evidence
  recomputation does not reparse the original bytes.
- `ATTACHMENT_HTTP_403`, for example, is a stored **PPS attachment download**
  failure. It is not a model HTTP status. `public_reason_code=MODEL_HTTP_FAILED`
  is the existing public mapping of model `HTTP_ERROR`; model status/timeout/
  rate-limit/gateway details are deliberately not inferred from raw prose.
- A quantitative source validation or unsupported scoring-DSL/unit/fact-dimension
  code is not proof the company lacks evidence. This endpoint does not classify
  private company missing-input causes or return a recalculated company score.
  `MISSING` quantitative profile is not the same as explicit `NOT_APPLICABLE`.
- The reader selects the latest authoritative PPS metadata and loads only
  extraction generations bound to its exact whole-manifest hash. It retains all
  generations within that boundary so that rejected new attempts cannot revive
  an older legacy result. Shared current-manifest and accepted-record validators
  still make the final selection. Unknown contracts stay outside current audit;
  the explicitly supported predecessor contract remains labeled separately.
- `metadata_schema_current` validates a schema label only; it is not proof the
  manifest or every attachment is complete. Current selection depends on the
  shared hash/descriptor/contract/record validators and is visible in the other
  fields. No extraction version label is guessed from the deployed application.
- These reads do not verify an analysis run's pipeline/requirement policy or
  company-fact freshness. They intentionally do not load evaluations, score
  bases, company facts, evidence or job histories. Cross-check scoped dashboard
  counts when measuring a prior recompute; do not substitute public score
  fallback values for stored current scores.

The request is bounded by notice count and output size, and only one notice's
current-manifest extraction history is resident at a time. It is not a
transactional census of all notices; reads may observe concurrent ingestion
between statements. No history is truncated within the selected manifest merely
to fabricate a current success. Large individual stored histories can still take
time to validate. Keep batch HTTP failures/UNAVAILABLE rows separate from missing
evidence, and never automatically start paid retries from this response.

## Validation

Synthetic endpoint regressions cover server/PIN/account/browser isolation,
strict bounds and input redaction, exact parser and public model-code handling,
canaries in stored payloads, current/stale and replaced-manifest selection,
legacy generation barriers, nullable download provenance, issue-count
preservation, per-notice failure isolation, and SELECT-only repeat reads without
private table or queue/provider access. Existing public-read and manual
quantitative-diagnostic tests verify unchanged adjacent behavior.

No live data or provider performance claim follows from those tests. Deployment,
PostgreSQL execution and the actual remaining-file census require separate
authorized verification.
