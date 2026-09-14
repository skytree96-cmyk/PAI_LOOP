# XLS parser diagnostics

`xlrd` evaluates defined-name references while opening a BIFF workbook. A
self-referencing name reaches its recursion guard and remains a deterministic
`XLS_PARSE_FAILED` extraction failure. Before reaching that guard, the reader
can print source names and formula bytes even with its default verbosity.

The XLS reader now receives a per-workbook text sink that discards diagnostics
without storing them or redirecting process-wide stdout. Its ordinary exception
mapping and all extraction limits remain unchanged. This is a diagnostic privacy
fix; it does not skip name evaluation or make a rejected attachment acceptable.

A synthetic BIFF workbook with a self-referencing `SYN-CIRCULAR-NAME` reproduces
the reader failure. The focused regression checks both the original failure and
empty stdout/stderr. No real attachment or extracted source text is a fixture.

All successfully parsed binary XLS workbooks still return
`XLS_FORMULA_EXPRESSIONS_UNAVAILABLE` with `complete=False`: xlrd exposes cached
cell values but does not provide the formula-expression evidence required by the
existing extraction contract.
