# Private credit certificate registration and notice application

The legacy notice-specific credit endpoint first derives one executable credit
criterion. Missing or incomplete rules can therefore reject both binding and
certificate registration. A company certificate must be registerable independently
of those rules, without turning an unbound value into a confirmed score.

## Source registration

`POST /api/v1/operator-evidence/credit-ratings` accepts the existing certificate
fields (`rating`, `document_sha256`, `evidence_reference`, `issued_on`,
`effective_on`, `valid_until`, `verification_attestation`) plus `company_name`,
`issuer_name`, `rating_kind: ENTERPRISE_CREDIT` and `purpose: PUBLIC_PROCUREMENT`.
The document-review attestation remains `HUMAN_REVIEWED_COMPLETE_DOCUMENT`.
It represents operator document review, not authentication by the rating agency.
Submit metadata only; never send the certificate body through this API.

Company identity must match the configured organization. Whitespace and the
explicit `(사단)` / `(사)` spelling variants of `사단법인` are normalized; names
are never fuzzy-matched. Issuer identity and rating applicability still require
notice-condition review before a scoreable projection is created.

The result contains an opaque `certificate_id`, registration status, company and
issuer names, grade, dates, and `notice_binding_required: true`. It deliberately
omits source locations and document hashes. `GET /api/v1/operator-evidence/credit-ratings/{certificate_id}`
returns the same restricted summary. Both require the existing private-evidence
authority; ordinary department sessions and anonymous browsers have no access.
Successful responses use `Cache-Control: no-store`; validation errors are redacted.

One immutable `Evidence` source row stores the reviewed metadata. Its type is
`COMPANY_CREDIT_CERTIFICATE`. Registration creates no scoreable `CompanyFact`,
does not need a notice, and does not call a model. Exact repeat registrations
return `UNCHANGED`, including concurrent submissions. Different metadata for the
same document conflicts rather than overwriting the prior registration. Other
documents are separate registrations. Historical expired certificates may be
registered; registration itself does not assert validity today or at any deadline.

## Notice-specific application

1. Register or retrieve the certificate once.
2. Review the notice's actual issuer, grade-kind, recognition and deadline rules.
3. Read `GET /api/v1/operator-evidence/notices/{notice_key}/credit-rating/binding`
   to obtain the current `fact_binding_sha256` and deadline. An unavailable or
   ambiguous executable credit criterion remains a 422; the certificate stays saved.
   This context endpoint does not certify issuer acceptance on the operator's behalf.
4. Call `POST /api/v1/operator-evidence/notices/{notice_key}/credit-rating/bind` with
   `certificate_id`, `expected_fact_binding_sha256`, and
   `verification_attestation: HUMAN_REVIEWED_NOTICE_CONDITIONS`.
5. Use the existing deterministic re-scoring path after rule and fact prerequisites
   are satisfied. Registration/binding never starts re-analysis or paid extraction.

The binding call reloads and validates registered metadata, checks the issue date
and both inclusive validity endpoints against the notice's Korean deadline date,
and derives the binding from current source-validated rules. Stale expected
bindings return 409. It does not accept a caller-supplied replacement rating.
Existing exact source, unit, recognition-condition and score validation remains
unchanged. A notice may still require review for unrelated rules or missing facts.

The scoreable result uses the existing `PRIVATE_DOCUMENT` CompanyFact and
`QUANTITATIVE_FACT` Evidence contract. One certificate can produce multiple
condition-specific projections; identical document-and-condition projections are
reused. Repeating the legacy endpoint with the same metadata also returns
`UNCHANGED`. Stored legacy proofs are not migrated or rewritten.

Source registrations and scoreable projections are immutable snapshots, not a
new certificate revocation service. Changed/disabled source rows cannot be used
for new bindings. Revoking an already projected fact must use the existing
fact/evidence controls; changing a source row alone does not invalidate earlier
projections. No silent cascade, choice between conflicting ratings, or retroactive
re-scoring is introduced here.

## Compatibility and rollout

- No DB migration, extraction prompt/schema version change, n8n change, or
  call/token-budget change is required.
- The existing `/notices/{notice_key}/credit-rating` endpoint retains its behavior.
- The sibling-document and ZIP/HWPX recovery changes in PR #185 remain intact.
- SYN-only tests cover source registration without rules, deduplication and
  concurrent registration, private authorization, metadata conflict/tampering,
  stale bindings, Korean date boundaries, and the real validated credit-rule
  compiler through deterministic scoring and legacy reuse.
- Real certificates and registration payloads belong in private operator storage,
  never repository seeds, tests, workflow JSON, PR text, or public profile assets.
- A paid re-extraction campaign is not a prerequisite for source registration.
  Whether individual rules need re-extraction is a separate diagnosis and budget
  decision; a new deployment must not automatically re-extract accepted records.
