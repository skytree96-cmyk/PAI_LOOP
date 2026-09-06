# Schema failure diagnostics and registration-document submissions

Failed structured extraction now records a bounded diagnostic containing only known schema field names and allowlisted validation error types. Array positions are replaced with `[]`; unknown field names are replaced with `*`. At most eight distinct diagnostics are retained. Raw input, rejected values, error context, provider text, and arbitrary exception messages are never copied. Invalid JSON and non-object output have fixed codes. Extraction status, strict validation, API-call bounds, and retry behavior remain unchanged.

A request to submit or attach a copy of a bidder registration certificate is treated as a document submission, rather than evidence that the bidder has completed registration. The registration matcher removes only the certificate name in such clauses and can still recognize a separate explicit registration obligation. Existing company fact and deadline checks remain required. Policy version v10 invalidates stale eligibility snapshots for recomputation.

Production schema failures must be retried or newly observed to obtain the new diagnostics; historical generic failures cannot retrospectively reveal the missing field. A diagnostic is not successful extraction or a score.
