# Previous quantitative display after failed reprocessing

The detail endpoint keeps the latest score, readiness and automation gate
unchanged. When the current result has no scored rows, it may additionally show
the most recent valid stored public item snapshot from the last 20 completed
analysis runs. The original values are read from immutable score snapshots;
there is no model call, company-fact substitution or mutation of prior results.

The UI labels this separate section **previous saved score / current application
on hold**, with its calculation time and whether the attachment manifest still
matches. An unchanged manifest does not prove unchanged remote bytes. A changed
or unverified source is marked explicitly. Historical values do not populate
current criteria, totals, eligibility, readiness or automatic-analysis inputs.

The existing public snapshot validator checks aggregate/item consistency,
engine and input bindings. The historical response allowlists only generic
public labels, points, maxima and timestamp, including for authenticated users.
Raw source text, evidence identifiers, source hashes and company facts are not
returned. Missing or invalid history stays missing.

Operational retries must separately freeze failed attachment/version identities.
Selecting notices that contain a failure and enabling broad review retry can
also select accepted extractions with incomplete quantitative validation; it is
not a failed-attachment-only operation. Do not resume that broader incident
campaign as a failed-only retry.
