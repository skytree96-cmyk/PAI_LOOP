# Whole-proposal minimum anchor equivalence

Date: 2026-09-06

A complete objective table could remain blocked by MINIMUM_SCORE_EXCEEDS_TOTAL when the model quoted a whole-proposal cutoff from the document overview. The same cutoff quoted from the evaluation section already passed. Local real-source reconstruction reproduced both paths; it is not a copy of the current production model response.

The existing source-wide proof now looks for the same cutoff between the objective subtotal and detailed table independently of which verified overview sentence the model quoted. The original quote must still be source-bound and have whole-proposal ownership. Every source-wide cutoff, competing score, table boundary, row census, subtotal and attachment constraint remains checked. Missing, cross-section and conflicting cutoff cases continue to fail closed. No numeric threshold, award, category or company score is changed.

Bounded operator diagnostics now expose the table subtotal, minimum and minimum-anchor length/point tokens alongside candidate metadata. They never return the source quote or private evidence.

Only records carrying MINIMUM_SCORE_EXCEEDS_TOTAL receive the new targeted fingerprint revision. Unaffected records retain their current fingerprints; global validator and prompt versions stay unchanged. Existing current-prompt extraction can be revalidated through the established duplicate-content path after downloading and checking the same source digest, without another model call. Historical records remain append-only. The extraction prompt version stays unchanged.

Validate the updated full CI before merge, then revalidate current attachments and inspect live table diagnostics. Remaining source gaps or missing company evidence must remain visible and continue blocking unsupported scores.
