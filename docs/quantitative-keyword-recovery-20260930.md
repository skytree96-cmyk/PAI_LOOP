# Keyword-focused quantitative recovery (2026-09-30)

The mentor requested smaller scoring-table inputs, XML fallback, disabled
adaptive thinking, and image-free scoring input.

## Implemented boundary

`OpenAIExtractionClient.extract_quantitative_keywords` is a read-only diagnostic.
It selects complete keyword pages (with neighboring context) or bounded whole-line
windows containing scoring headings. It keeps original offsets and a canonical
source hash. Oversized selections fail without cutting rows. No keyword match is
an explicit selection failure, never zero points or proof of non-applicability.

The gateway receives text first. A schema, incomplete-response, or quote failure
permits one retry with XML-framed source excerpts. Each call has no transport
retry; at most two calls are made and their usage is aggregated. Explicit
single-call budget policies cannot use this two-step entry point. The XML is
escaped original text, not a binary HWP-to-HWPX conversion. Output remains the
strict JSON schema. Both attempts verify quotes against canonical original text.

When original HWPX bytes are supplied, the caller also gets bounded native table
XML on the fallback call. The existing archive security checks run first, and
the original HWPX must extract to the same canonical text before either provider
call. Only scoring table text and row/cell structure are included; images,
unrelated document text and metadata attributes are excluded. The XML context
is still untrusted and never replaces the canonical evidence verifier.

`tools/probe_quantitative_keywords.py --source <local.txt-or.hwpx>` checks the
selection without provider calls. Add `--execute` only for an authorized live
diagnostic with the existing server-to-server gateway environment configured.
The CLI prints summary fields only and never writes a score or raw source.

The diagnostic wrapper remains `QUANTITATIVE_PROBE_ONLY` with
`persistence_eligible=false` and `attachment_coverage_complete=false`. Production
does not promote or persist this diagnostic outcome.

## Production recovery

An explicit retry of the latest current-contract, exact-document/manifest-bound
failed attachment can use the separate `extract_quantitative_recovery` entry.
Schema, quote and incomplete-response failures are eligible. A strictly parsed
20k/32k output-limit or gateway timeout is eligible only when keyword selection
actually reduces the input; identical full-input stops retain their existing
no-repeat behavior. Accepted extraction records do not activate this path.

The ordinary production chain now continues automatically after its existing
32k escalation fails: normal extraction, bounded 32k escalation, keyword text,
then XML on an eligible output-validation failure. Success stops progression.
The recovery portion adds at most two calls; existing normal corrective-call
and 32k limits remain unchanged. No n8n edit is needed for this orchestration.
If the request cannot reserve time for both recovery calls, it records attachment
continuation and resumes from the failed 32k result on the next queue lease.
The separately requested single-call LONG_OUTPUT_ONCE execution remains one call.

Automatic recovery commits a unique QUANTITATIVE_RECOVERY_ONCE reservation before
provider I/O. Its identity binds the notice, attachment, document and manifest,
not the attempt ID. Concurrent attempts, ambiguous failures and new retry version
IDs cannot create another allowance. These reservations survive log retention.
Current status, deadline, cancellation, MANUAL_ONLY and latest source bindings
are checked before reserving. Failed recovery remains REVIEW and is terminal.

A completed gateway JSON/schema normalization failure also permits the one XML
fallback. An ambiguous transport timeout, refusal, provider error or token stop
does not trigger an immediate second call. Failed quote output is not persisted
as unverified recovery scoring rows.

The recovery stage has a two-call ceiling: one keyword text
call, then XML on an eligible validation failure, without transport retries.
Native HWPX tables are included when present; a paragraph-encoded HWPX table
uses source-text XML framing. Optional native XML that exceeds its size or
structure budget is omitted with an audit reason, without blocking bounded
source text or truncating table rows. Source mismatches still fail before paid calls.

Persistence stores the failed-version identity, canonical and selected hashes,
source ranges, XML usage and `QUANTITATIVE_KEYWORD_RECOVERY` scope. Requirements
must be empty, input coverage remains incomplete and the attachment result
remains REVIEW even when quantitative extraction is accepted. The existing
mechanical table validator and partial-source scoring path consume the verified
table. Failed quotes do not become recovery scoring rows. No whole-document
completion, eligibility approval or HUMAN_REVIEWED_NOTICE_CONDITIONS flag is
created. An accepted recovery still requires bound company evidence for a score.

Current-manifest reuse prevents a continuation from repeating a saved recovery.
Notice lifecycle, queue admission, MANUAL_ONLY and existing scoped-retry guards
continue to apply before this attachment path is reached.

Before moving to a sibling attachment, expired rows in the frozen source
history are refreshed inside an explicit read transaction. Source revalidation
and rollback can expire these rows even with `expire_on_commit=False`; allowing
an implicit refresh later would collide with the next claim/result transaction
and could discard the response's accumulated provider telemetry. This refresh
does not expand the retry scope or commit pending caller writes.

## Image and provider behavior

An attachment named `.hwpx` whose bytes have the HWP5 OLE signature uses the
already validated HWP text for keyword/XML excerpt recovery. It must not be
reopened by the native HWPX ZIP reader. Actual HWPX archives retain their native
table context and all archive/source-binding checks. This does not convert HWP
to HWPX, bypass extraction guards, or renew an already consumed recovery claim.

HWPX BinData inspection reads only 16 bytes instead of inflating an entire unused
image. Signature-verified PNG/JPEG/GIF members do not consume the XML/text
decompression budget. Disguised files still consume that budget; embedded
PDF/Office files, duplicate entries, unsafe paths, encryption, active content,
and external relationships remain rejected. Images are not OCR'd or scored.
The download limit is unchanged; prior uncommitted size/409 changes were not
found in this checkout and were not claimed as restored.

The W13 source now explicitly requests `thinking.type=disabled` and omits effort.
Structured output, model identity, call limits, authentication and credential
preservation contracts remain intact. Deployment must compare live W13 state
before replacing anything. Local workflow changes do not prove live activation.

## Validation and release gate

Run the focused keyword/HWPX/probe/PPS tests, native gateway and repository
workflow validations, then full coverage CI on the exact PR head. Preserve the
latest user-managed Teams settings; the earlier instruction to disable Teams
and lock W12 was superseded. Read the live values before any deployment and
verify they remain unchanged afterward. Do not perform a cohort backfill until
the operational score UI and all notice lifecycle gates have been reverified.

## First-call failures (2026-10-02)

Automatic recovery used to start only after the 32k stage failed. An ordinary
call that completed with malformed or unverifiable output (`SCHEMA_VALIDATION_ERROR`,
`UNVERIFIED_QUOTE`, `INCOMPLETE_RESPONSE`, or `HTTP_ERROR` with
`OUTPUT_NORMALIZATION` decode/JSON/fence details and `end_turn`) never reached it,
because a 32k budget does not help such output. On 2026-10-02 nine attachments of
six notices stopped there.

Now such a failure continues into keyword text, then XML, in the same request:

- Only a failure this request just paid for (`fresh_failure`). Stored failures do
  not spend after deployment; an explicit failed-attachment retry already enters
  recovery directly.
- A 20k stop or gateway timeout still takes the one-shot 32k stage first while it
  is available. With `PAI_AUTO_LONG_OUTPUT=0` it goes straight to keyword recovery,
  which still must shrink the input.
- Same once-per-source reservation, two-call cap, time reservation and
  status/deadline/MANUAL_ONLY/manifest checks. If the time reservation does not
  fit, the failure stays REVIEW until an explicit retry.
- Kill switch: `PAI_FIRST_FAILURE_RECOVERY=false`.
