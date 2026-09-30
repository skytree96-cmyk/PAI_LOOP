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

The result wrapper remains `QUANTITATIVE_PROBE_ONLY` with
`persistence_eligible=false` and `attachment_coverage_complete=false`. Existing
eligibility extraction and score persistence do not consume this diagnostic.
Production integration and source-bound approval remain separate pending work;
an accepted probe is not a persisted score or evidence that every condition was
included. No HUMAN_REVIEWED_NOTICE_CONDITIONS flag is changed.

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
workflow validations, then full coverage CI on the exact PR head. Keep Teams
flags false, W12 emergency-disabled, and do not perform a cohort backfill until
the operational score UI and all notice lifecycle gates have been reverified.
