# Award window recovery and safe failure counts

Two synthetic regressions established avoidable behavior in `iter_awards`:

- A failed interval of seven days or fewer was requested again unchanged as its
  own fallback. It now records missing coverage once. The existing configured
  transport retry count is preserved; this fix only removes the duplicate
  interval request after those retries are exhausted.
- Successful fallback rows accumulated before the total deadline were dropped
  when the next fallback's deadline check returned. They are now yielded once
  before stopping, without another request or an extended deadline.

Longer failed windows still use the existing seven-day subdivision policy.
Strict callers still receive the exception; callers that explicitly continue
on failed windows retain missing-coverage flags. The history-refresh route still
uses a 480-second total budget, a 12-second request timeout and zero transport
retries. None of these limits, opening-result fetching, award normalization or
stored record identities changes here.

## Backward compatible diagnostics

`POST /api/v1/notices/{notice_key}/award-history/refresh` adds
`window_error_counts` to its response and stores the same list in the ingestion
job's `request_json`. The list is empty if no window raises a PPS exception:

```json
{
  "window_error_counts": [
    {
      "phase": "PRIMARY",
      "error_type": "HTTP_ERROR",
      "http_status": 429,
      "provider_code": null,
      "count": 1
    }
  ]
}
```

`phase` is `PRIMARY` or `FALLBACK`. `error_type` uses the fixed vocabulary in
`integrations/common.py`: network, HTTP, JSON/payload/envelope/header/body,
provider service/result, total-count/items and award-page structure failures,
plus `UNKNOWN` for an existing unclassified exception. HTTP status is an integer
only for HTTP errors. Provider codes are exact finite numeric protocol labels
(`0` or `00`–`99`) only for provider service/result errors; no interpretation of
their operational meaning is added. Unknown codes become null.

Counts describe **terminal failed window attempts**, not every retry, page,
failed record or unresolved interval. A primary failure successfully covered by
fallbacks remains diagnostic history and does not itself force `PARTIAL`.
Unresolved windows, time/page limits and incomplete response flags continue to
force `PARTIAL`. Zero fetched records under `PARTIAL` is unknown coverage, never
proof of no award history, and existing stored award records are preserved.
Header-declared provider errors are classified before looking for a success
body, so a bodyless error cannot masquerade as an empty successful page.

Only fixed type/status/code/count facets are added. Exception prose, provider
bodies, request URLs, keys and identifiers are not copied into these counts.
Existing exception positional arguments/messages remain compatible for other
callers. The new fields do not retroactively classify old ingestion jobs.

## Validation and limits

MockTransport and synthetic API tests cover both pre-fix counterexamples,
one/seven/eight-day and final-tail boundaries, existing retry budgets, strict
failure behavior, partial result retention, counter reset, exact error facets,
canary exclusion and preservation of stored history after an empty partial run.

These tests do not identify the cause of an observed production `PARTIAL` run or
prove that an upstream response will succeed. A repeated month-to-seven-day
fallback pattern alone cannot distinguish HTTP, provider, parser or network
failure. Use newly captured fixed counts on a separately authorized run; no
production or provider calls were made for this change.
