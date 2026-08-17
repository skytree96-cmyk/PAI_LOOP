# Security and privacy delta v0.2.0

This file extends, rather than replaces, the v0.1.0 baseline.

## Public-release data boundary

- The repository and release wheel contain only public procurement data,
  synthetic regression fixtures, and reviewed `PUBLIC_DERIVED` seed assets.
- Company source formats, source adapters, source files, row-level import tests,
  and internal-data operating procedures are outside the public release.
- The public web runtime has no endpoint for importing, inspecting, or matching
  local company-source data.
- Packaged seed assets use strict field allowlists and canonical SHA-256
  validation. Unexpected fields, digest drift, or identifier-pattern findings
  fail closed.
- Public performance records are search candidates. They never become accepted
  performance, eligibility, quantitative score, or a GO decision automatically.

CI enforces this boundary before testing or packaging. It rejects known
local-only adapter paths, credential/source artifacts, direct identifiers, and
release wheels that omit required public assets or contain excluded modules.

## Public document and AI boundary

- Only published procurement text selected for analysis is sent to OpenAI.
- `store:false`, strict JSON schema, untrusted-source instructions, attachment
  allow-listing, exact quote verification, and bounded input/output are enforced.
- Source text is hashed in memory and is not stored in the application database.
- Refusal, incomplete output, network failure, schema drift, unknown attachment,
  or unverifiable quote becomes `R07 REVIEW`; it can never become PASS.
- A successful extraction is an evidence candidate. Only the deterministic
  evaluator may calculate eligibility from approved public-profile facts.

## Public award-history boundary

- Award history is collected from the public PPS service in bounded windows;
  partial provider failures are surfaced as warnings.
- The normalisation allowlist retains only notice/revision, title, agency,
  winning organization name, participant count, amount/rate, opening/award
  dates, source, and title-similarity score.
- Provider registration number, representative name, address, phone, and
  procurement contact fields are discarded before persistence. Raw provider
  payloads and service keys are not written to storage or returned by the API.
- Similarity produces a review candidate, not a same-project assertion. It can
  never alter eligibility, readiness, bid score, or a human decision.

## External delivery

- The Teams path is mock-only. Adaptive Card JSON is produced and recorded, but
  no Teams webhook, connector, bot, or Graph request exists.
- n8n workflows are deployed inactive and contain no credential block or real
  URL/key value. Live collection additionally requires an explicit enable flag.

## Deployment gate

The public competition slice is read-oriented. Company-wide write access remains
blocked until the company approves:

1. Entra SSO and role-based authorization for browser users;
2. an encrypted managed database/object store with backup and retention policy;
3. HTTPS, rate limiting, CSRF/session controls, and immutable audit logging;
4. a service identity/credential for n8n and an approved Teams delivery method;
5. reviewer workflows for evidence promotion and performance recognition.

`PAI_LOOP_API_KEY` is a server-to-server fallback only. It must never be placed
in the browser bundle. Production mode fails closed when this fallback is absent.
