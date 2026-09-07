# Public performance search

The read-only performance list supports search, contract year, division, inclusive
contract-date range (`date_from`/`date_to`) and inclusive KRW contract-amount range
(`min_amount`/`max_amount`). Filters combine before pagination; `total` is the
number matching all filters. The separate dataset summary retains the full count.

Unknown dates or amounts do not match an active bound on that field. A known zero
amount can match zero; missing amount is never converted to zero. Negative amounts,
invalid dates, fractional-won query bounds and reversed ranges return HTTP422.

Each card includes a disabled “실적증명서 · 연결 예정” preview. This is a mockup,
not a download endpoint. It exposes no certificate, private file path, or original
company evidence. A future authenticated evidence mapping must be reviewed before
enabling downloads. Public performance candidates do not certify eligibility or
quantitative scoring evidence.
