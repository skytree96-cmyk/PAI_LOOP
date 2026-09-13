# Quantitative document diagnostic, 2026-09-13

This branch preserves the earlier quantitative source-rule fixes on the deployed
UX baseline. The new work prepares a bounded extraction experiment. It does not
establish that production scoring is fixed or authorize a deployment.

## Measured failure and change

PDF text extraction can concatenate a credit grade, percent award and points.
Reading order alone does not prove which characters belong to each cell. Local
comparison of three source PDFs found missing dotted rulings and omitted outer
borders. The geometry helper now binds the complete ordered glyph stream to the
unchanged canonical source, then assigns only unambiguous glyphs to cells.

`pdf_source_structure.py` preserves painted line fragments. It joins a segmented
line only with a regular dash pattern and derives an open outer envelope only
from consistent connected ruling endpoints. Overlaps, rotated or unassigned
glyphs and unmatched source streams cannot authorize a cell transition. This is
partial geometry evidence; it does not invent an operator, row meaning, subtotal
relationship, company fact or missing scoring rule.

`quantitative_review_input.py` requires explicitly reviewed complete pages and
retains every supplied original quotation. Referenced conditions, forms and
footnotes remain in scope even when they occur outside an evaluation table.
The displayed source may add newlines between proven cells, with the PDF and
canonical hashes checked. Characters and original order remain unchanged.
Cross-page omission markers cannot become source evidence. A physical table is
not required for a quantitative rule.

`extract_quantitative_probe()` uses a separate, unsupported persistence contract,
one gateway call and no retries. It asks for quantitative rules only, rejects
qualification requirements in the response and validates quotations against the
full canonical text. Geometry context is separate untrusted data. Its wrapper
always records incomplete attachment coverage and ineligibility for persistence.
The ordinary full-document extraction path keeps its existing source and prompt
contract. No automatic page filter or production analysis switch was added.

## Time and cost controls

The default client I/O wait is 200 seconds, leaving response-return time beyond
the gateway's declared 180-second provider wait. Shared attachment admission
accounting now reserves 441 seconds: three 12-second download attempts, two
200-second model attempts and a 5-second guard. The 550-second enrichment budget
and 600-second outer HTTP setting are unchanged. Admission accounting and HTTPX
phase timeouts are not hard wall-clock cancellation guarantees.

Transport diagnostics store only fixed exception-type codes. A missing response
still has unknown usage; it is never interpreted as zero cost. Existing retry
and cooldown rules remain in force. The probe permits only one initial call,
without corrective or transport retries.

The reviewed experiment includes the full input prompt and optional geometry
context in its measured request size. Reduced source characters alone do not
prove equal token or cost savings. Frozen paid-run markers from the earlier
experiment must not be reset. Any new paid run requires fresh scope/cost approval.

## Conservative performance policy

The user requested that unverified performance be excluded from the calculated
score. Existing aggregate lower bounds already withhold those unverified points
while preserving other supported components. An unknown contract count is not an
actual zero count, and missing recognition evidence is not non-submission.

A separately labeled, non-persistent count scenario may use only records whose
recognition conditions were verified. A zero-record scenario can receive a score
only where the validated source rule covers zero. Otherwise its award remains
undetermined; the existing conservative partial total can still be shown. No
general evidence guard, company registration or eligibility decision is relaxed.

## Validation and rollout boundary

Synthetic regressions cover repeated text, adjacent tables, merged and open
cells, dotted rulings, rotation, ownership ambiguity, source/PDF mismatch,
omitted references, number-internal boundary tampering and the one-call probe
contract. Existing extraction, source-rule, scoring and retry tests remain gates.
Private real-document measurements and request plans stay under `.local/`.

Next gate: independently approved one-call-per-document extraction of the three
reviewed PDFs, followed by source-rule validation and comparison with the manual
reference criteria. Only then assess production integration, current-manifest
company evidence registration and broader re-extraction. No production DB write,
quantitative deployment or workflow activation is part of this diagnostic.

The geometry dependency is [pdfplumber](https://github.com/jsvine/pdfplumber),
under its [MIT license](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt).
PDF byte/page/decoded-stream limits are bounded locally; post-decode checks do
not replace process-level isolation for hostile files.
