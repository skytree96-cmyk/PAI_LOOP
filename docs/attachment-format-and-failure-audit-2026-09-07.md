# Exact HWPX format recovery and failed-download audit — 2026-09-07

A supported HWPX ZIP served under a `.hwp` filename was sent to the OLE reader and failed as `HWP_CONTAINER_INVALID`. The reverse case (`.hwpx` containing OLE HWP5) was already handled. The parser now uses the existing HWPX reader only when the bytes have the ZIP signature, bounded archive validation passes, and the `mimetype` member exactly equals `application/hwp+zip`. The original manifest filename, attachment identity and downloaded bytes remain unchanged. An ordinary ZIP, missing/wrong media type, unsafe path, duplicate member or archive limit violation cannot enable this recovery. The HWPX reader still rejects missing sections, active/embedded/external content and malformed XML.

A second defect replaced the successfully downloaded file hash with a manifest/error marker whenever parsing failed. Such failures now retain the real SHA-256 and downloaded byte count, with explicit `download_complete` and `document_digest_basis` fields in the internal failure audit. A download that never succeeds still uses the previous synthetic marker, explicitly identified as `FAILED_DOWNLOAD_MARKER`. Old rows remain immutable; no historical marker is relabeled as a source hash, and no failed parse becomes ACCEPTED.

The actual leaf failure `DOCUMENT_TEXT_EMPTY` is also classified as a file extraction failure for supported document formats. Previously only `DOCUMENT_TEXT_EMPTY_OR_SHORT` was in that mapping, causing an empty text result with zero model calls to appear as a generic model REVIEW. Fixed model transport/schema/response failures retain their existing labels.

## Retry and compatibility boundary

No prompt, schema, validator or processing version changes are required. Current `DETERMINISTIC_REVIEW_CODES` contains only the three unsupported-format markers; `HWP_CONTAINER_INVALID` is already eligible for `current_retryable_review_version_ids`. A recent failure is still reused by an ordinary request. An explicitly authorized reviewed campaign snapshots its current bound old IDs and permits one retry under the same current manifest. A new REVIEW gets a new ID and is reused on later continuations using that frozen snapshot. A successful retry must pass the existing source, model-output, quantitative-record and manifest validation before becoming usable.

Create a new reviewed campaign only through the existing authorized recovery procedure after deployment and prior-parent completion. Do not change an already frozen campaign, widen its scope, reset successful attachments or bump the processing contract to force a retry. There is no new source-format override, automatic provider retry or fabricated text. Existing accepted proof and current manifest checks are unchanged.

## Verification

Synthetic tests cover exact-byte format dispatch, inexact/absent media type, archive traversal/duplicates/size/ratio limits, missing sections and embedded documents, explicit old-failure retry, immutable old audit, successful and failed continuation reuse, distinct hashes for distinct failed source bytes, failed-download markers and zero provider calls. Blank PDF/HWPX tests verify file-error labels and no model request; existing genuine model failure labels remain intact.

The local candidate is based on the cumulative PR105 repair source. Runtime changes are limited to `document_extraction.py` and the failure audit/label sections of `pps_enrichment.py`. The latest-manifest selector patch is developed separately and can be combined without changing these boundaries. No operational download, account work or production mutation was performed while preparing this change.
