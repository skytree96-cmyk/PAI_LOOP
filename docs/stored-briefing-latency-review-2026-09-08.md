# Stored briefing ranking cost and measurement limits

The stored daily briefing now calls the existing
`rank_notice_department_views` once per notice. The top business, review and
regional views retain their existing selectors and limits. Every selected notice
is still ranked before the response limit is applied. Eligibility, attachment
coverage, queue limits and provider routing are unchanged.

Normalization reuses an LRU of at most 8,192 short strings. The entry path retains
only plain strings of at most 128 characters after the existing
`str(value or "")` conversion. Larger text and unusual string subclasses are
normalized in full without caching; this is a retention policy, not an input
validation limit. Unicode normalization may expand the output, but both the
number of retained entries and their input lengths are bounded. Cached values are
strings, not notice, evaluation or ORM objects. Each process has its own cache.

## What the measurements establish

The original local report for commit `23be58d` recorded a SQLite synthetic fixture
with 50–200 notices, four versions and six award records per notice. For 200 notices
it reported 2.514 seconds before and 0.562 seconds after the change, with 69 SQL
statements in both cases. Its profiler identified repeated department ranking and
Unicode folding as substantial costs in that fixture. These figures were reported
by the original implementation task; the independent review did not rerun that
benchmark. The later input-length retention change has no new timing measurement.

The observed reduction supports eliminating duplicate CPU work. It does not
establish the cause of the reported operational 30-second timeout, an operational
speedup factor, or a safe number of notices per request. Linear statement counts
do not establish query latency or exclude database bottlenecks. PostgreSQL network
latency, result sizes, notice history, company/performance data, worker load and
concurrent requests can change the cost. Extrapolating the 200-notice fixture to
thousands of notices is not a measured capacity result.

No production service, database or provider was contacted for this review. The
response's fixed `source_calls` values describe its contract; checking those
values alone is not an independent provider-call measurement. Determining the
operational cause still needs a measured breakdown of database work, ranking,
other projection work and serialization for a representative workload, with cache
state, data volumes and concurrency recorded.

## Additional regression coverage

`test_department_normalization_cache.py` checks the prior object-conversion
behavior, oversized-input bypass, Unicode expansion without truncation, cache
hits and eviction, coercion failures and rejected oversized keywords. It compares
the combined views against the separate helpers using an independent uncached
normalizer for every public catalog department, with cold and warm caches. The
cases include weak matches, exclusions, regional routing, field boundaries and
long notice text. No timing threshold is used as a correctness assertion.
