# Security and Privacy Baseline v0.1.0

## Repository

- Never commit `.env`, `secrets.txt`, API keys, Teams webhook URLs, credentials,
  source attachments, extracted full text or internal databases.
- Run CI secret scanning and dependency checks.
- Prefer a private repository before adding internal deployment configuration.
- Keep synthetic fixtures clearly labeled and free of real contact information.

## Runtime secrets

- Store n8n, PPS, OpenAI and Teams values in the platform credential store.
- Inject secrets server-side and redact query strings and headers from logs.
- Rotate a credential immediately if it appears in a workflow export, issue,
  execution payload or browser bundle.

## Data classification

- public: published tender metadata and public award/contract facts;
- internal: company qualifications, performance candidates and user decisions;
- restricted: evidence documents, employee details, contact information and
  credentials.

Restricted data is encrypted at rest, excluded from public demos and protected
by role-based access. Public competition demos use synthetic company facts.

## AI boundary

- send only the minimum relevant page/segment;
- remove unrelated names, emails, phone numbers and identifiers;
- wrap source text as untrusted data and reject document-borne instructions;
- validate structured output against a strict schema;
- discard claims without a valid evidence anchor;
- prohibit the model from granting final PASS or GO authority.

## Auditability

Every evaluation stores rule version, input hashes, evidence IDs, timestamps and
the human override trail. Overrides are scoped to one notice/requirement unless
an approved rule-version change is created.
