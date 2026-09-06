# Explicit performance period parsing

The private workbook normalizer now recognizes exact start/end dates when the
source mixes compact and delimited dates, separates dates by a line break, or
omits an end year that has an unambiguous same-year interpretation. Parsing the
exact interval before removing wrapped digit whitespace prevents two adjacent
dates from being joined into one invalid number.

Single dates, Excel date serials without a second boundary, durations, month-only
ranges, malformed years, impossible dates and ambiguous year crossings remain
unresolved. The importer does not invent an end date or attest completion,
certificates, VAT, recognized amounts, or human review. The source workbook and
row identity remain unchanged. This change only normalizes local input. Verified operational imports are immutable:
replaying changed dates for the same verified source is rejected by the current
API. Operational correction requires a separately audited revision or replacement
workflow; this parser change does not update existing records or bypass that gate.


A bounded extension also recognizes one comma in place of the year/month dot,
only when the entire source contains exactly two complete four-digit-year dates
with month/day dots. Both calendar dates must be valid and the end cannot precede
the start. The comma may occur in either boundary, but there must be exactly one.
Comma-bearing prose, missing days, abbreviated end years, extra dates, a comma
between month/day, or multiple commas remain unresolved without token fallback.
No workbook value or row identity is rewritten by this parsing step.

The operational audited correction uses the explicitly requested
`explicit-performance-period-v2` algorithm. The original v1 parser and receipts
retain their original meaning; changed-date import replay and private-record
PATCH remain forbidden. A parser repair on its own does not update production.
