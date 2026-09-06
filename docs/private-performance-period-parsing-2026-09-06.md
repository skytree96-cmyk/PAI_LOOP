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
