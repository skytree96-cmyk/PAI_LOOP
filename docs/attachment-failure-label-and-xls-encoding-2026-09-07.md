# Attachment failure labels and unverifiable XLS encoding

Three defects in the attachment read path, found by auditing the code against
the 2026-09-07 attachment census (282 open notices, 941 current attachments, 803
audited, 563 accepted, with PDF_EXTRACT_FAILED 14, DOCUMENT_EXTRACT_FAILED 8,
HWPX_EXTRACT_FAILED 7, UNSUPPORTED_ATTACHMENT 1). None of the three changes
relaxes an acceptance gate, and none of them raises the accepted count by
loosening validation.

## 1. Shared failure codes were reported as "the document was read"

`_public_attachment_failure_reason_code` recognized only format-prefixed error
codes. Every failure raised by the shared container, budget and safety layer
carries no format prefix — the `ARCHIVE_*` family, `DOCUMENT_EMPTY`, the input
and uncompressed-size limits, `XML_DTD_FORBIDDEN`, `UNSAFE_DOCUMENT_FILENAME`,
`UNSUPPORTED_ARCHIVE_MEMBER_TYPE`, `UNSUPPORTED_DOCUMENT_TYPE`,
`LEAF_EXTRACTION_FAILED` and `MEMBER_EXTRACTION_FAILED` — so all of them fell
through to `OPENAI_REVIEW`. That label's operator text states that the document
was read and only the LLM structuring or validation step failed, which is false
for a document the extractor never opened, and it hides the deterministic
extraction marker the audit requires.

An `.hwpx` attachment that carries HWP5 OLE bytes is also deliberately routed to
the HWP5 reader, so its failures arrive under an `HWP_` prefix and were
mislabelled the same way.

These codes now resolve to the extraction marker for the attachment's own format
(`PDF_EXTRACT_FAILED`, `HWPX_EXTRACT_FAILED`, `DOCUMENT_EXTRACT_FAILED`). The
codes are enumerated rather than matched by a broad prefix, so
`UNSUPPORTED_ATTACHMENT_TYPE` and the HWP-only codes keep their own earlier,
more specific labels, and the default stays `OPENAI_REVIEW` so a genuine
LLM-stage failure is not relabelled as an extraction failure.

This is a label-accuracy change. The attachment `state` is set to `REVIEW`
independently of the code, no attachment moves toward `ACCEPTED`, and all four
codes already belong to the same retryable set, so retry behaviour is unchanged.

## 2. An .xls whose text encoding cannot be established was accepted as evidence

`xlrd` falls back to `iso-8859-1` for a pre-BIFF8 workbook that carries no
`CODEPAGE` record. Korean cell bytes then decode into characters that are not in
the source document, and the resulting mojibake is semantic enough to pass the
extracted-text check, so it was returned as a successful extraction with no
warning and no member issue — an overclaim rather than a fail-closed result.

`_extract_xls` now refuses that workbook with a new deterministic
`XLS_CODEPAGE_UNVERIFIED` code, so the attachment lands in REVIEW under an
explicit extraction marker. No encoding is guessed: substituting a plausible
codepage would fabricate text. The check detects exactly the reader's own
fallback condition (a declared-absent codepage on a pre-BIFF8 workbook, or a
resolved `unknown_codepage_*` encoding); a reader that does not report those
fields is left alone rather than assumed to have guessed.

## 3. An OOXML workbook served under an .xls name could not be read

PPS metadata is not always consistent with the bytes served by the public
attachment endpoint. The module already recovers two instances of that: an
`.hwpx` carrying OLE bytes, and a `.hwp` carrying an HWPX package whose exact
media type is proven inside a bounded archive read. The same mislabel for
workbooks was not covered: a real OOXML package served as `.xls` went straight
to the BIFF reader, which can only reject it as `XLS_PARSE_FAILED`.

An `.xls` whose bytes start with the ZIP signature is now routed to the audited
XLSX leaf, but only after a bounded archive read proves both parts
`_extract_xlsx` itself requires (`[content_types].xml` and `xl/workbook.xml`).
A ZIP signature alone diverts nothing, and the filename, bytes and digest
identity are untouched. Extracted text passes exactly the same gates as any
other workbook.

## Initial verification (head 991af39)

- `tests/test_document_extraction.py`: 33 passed, including a BIFF5 stream built
  with and without a `CODEPAGE` record from byte-identical cp949 cells, an OOXML
  package read identically under `.xls` and `.xlsx`, and a non-workbook ZIP that
  keeps `XLS_PARSE_FAILED`.
- `tests/test_pps_enrichment.py`: 85 passed, with 14 new label cases covering the
  shared failure family per format, the HWP-code-from-`.hwpx` path, the retained
  priority of the more specific labels, and two LLM-stage codes that must stay
  `OPENAI_REVIEW`.
- `tests/test_api.py`, `test_analysis_pipeline.py`, `test_daily_operations.py`,
  `test_manual_analysis.py`, `test_openai_extraction.py`,
  `test_quantitative_rule_extraction.py`,
  `test_extraction_contract_compatibility.py`: 656 passed.

## What this does not establish

The census counts cannot be attributed to these defects from repository inputs.
Doing that needs the persisted `error_code` for each failing attachment attempt,
read only, joined against the current PPS manifest per `attachment_id`. In
particular it is not known how many of the 14/8/7 recorded extraction failures
were being shown to operators as `OPENAI_REVIEW` before this change, nor how
many `.xls` attachments are OOXML packages or pre-BIFF8 workbooks without a
codepage. No fixture here is a real attachment; all are synthetic.

Two further findings were examined and deliberately not changed. An HWPX with
unread active content or an unread embedded object discards its parsed section
text instead of returning it with `complete=False`, unlike the DOCX/XLSX/PPTX
and HWP5 paths; that asymmetry is asserted by existing tests, and returning the
partial text would let an attachment with unread content anchor an evidence
quote. `PdfReader` is opened with `strict=True`, which rejects PDFs whose text
the reader could recover after xref repair; that strictness is an integrity
precondition for page-anchored evidence, and the leaf contract offers no channel
to record a deterministic "recovered" marker, so relaxing it would remove a
fail-closed check without leaving an audit trace. Both need a product decision
and a coverage channel, not a quiet loosening.

## Follow-up: charge the format probe once

A synthetic boundary check found that the initial XLS probe charged archive
entries and declared uncompressed size, then the XLSX reader charged the same
package again. A four-entry package succeeded as `.xlsx` but failed as `.xls`
at the identical configured four-entry limit. This is a reproduced code defect;
the number of affected production attachments is still unknown.

The probe now checks a copy of the current shared budget, including earlier
sibling usage. A matching workbook is charged once by its actual parser.
False and exceptional probes retain their consumed counters because no later
archive parser charges those attempts. Repeated non-workbook `.xls` siblings
therefore cannot evade cumulative limits. Archive safety and budget exceptions
propagate with their original codes; only valid non-workbook packages keep the
existing BIFF fallback. No size, entry, compression, or content-validation limit
was raised or removed.

Follow-up validation: the two document/enrichment modules passed 126 cases after
the first boundary fix. After adding failed-probe budget preservation and four
more cases, the changed document module passed all 45 cases; `git diff --check`
passes. Required CI on the final PR head is recorded in the discussion. The
12 new synthetic cases cover entry/size limits, nested sibling usage, repeated
failed probes, and unsafe/duplicate archive entries. Intermediate browser-upload
commits are assembled without CI; the final commit runs the required gate.

The internal browser could read the production UI and saved queue progress,
but did not provide the raw `error_code` / current manifest / `attachment_id`
join. The free Render instance does not provide Shell access. The historical
14/8/7 counts above are not current counts and are not attributed to these fixes.
No production merge, deployment, workflow change, new campaign, or paid analysis
was performed as part of this review.
