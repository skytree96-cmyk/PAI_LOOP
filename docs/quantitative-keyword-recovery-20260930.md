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
no-repeat behavior. Scheduled reads and accepted extraction records do not
activate this path. The separate LONG_OUTPUT_ONCE contract is unchanged.

A completed gateway JSON/schema normalization failure also permits the one XML
fallback. An ambiguous transport timeout, refusal, provider error or token stop
does not trigger an immediate second call. Failed quote output is not persisted
as unverified recovery scoring rows.

The recovery keeps the existing two-call attachment ceiling: one keyword text
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

## Image and provider behavior

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
