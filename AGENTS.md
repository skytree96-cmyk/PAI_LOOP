# PAI_LOOP repository guidance

This file applies to the whole repository. Keep it concise and durable: put
temporary incident notes, one-off counts, and current production snapshots in a
dated runbook or PR instead of adding them here.

## Start every task here

- Confirm the Git root and run `git status --short` before changing anything.
- Preserve unrelated or pre-existing work. Never discard, reset, or overwrite
  user changes to make a task easier.
- Read the files that implement the requested behavior before trusting a README
  or historical handoff. Several runbooks intentionally retain migration history
  and may lag the runtime.
- Search with `rg`/`rg --files` first. Make the smallest coherent change and add
  or update focused tests for behavior changes.
- Cloud agents only have repository-tracked inputs. If a task depends on an
  untracked workbook, proposal, attachment, credential, or production record,
  report the missing source; do not reconstruct or invent it.

## Sources of truth

Use separate precedence rules for product intent and runtime facts.

Product and policy intent:

1. The user's latest explicit instruction.
2. An explicitly supplied and approved product source (the approved workbook v7
   for cell-level rules, then planning document v6 for product design).
3. This `AGENTS.md` and current executable tests.
4. Current repository documentation.
5. Older handoff notes and superseded runbooks.

Runtime and deployment facts:

1. Current code, tests, `manifest.json`, `render.yaml`, workflow JSON, and Git
   history define the intended state.
2. Read-only inspection of Render, n8n, and PostgreSQL defines what is actually
   deployed, active, and stored when the task concerns production.
3. README and runbook claims apply only after they agree with the sources above.

A green CI run or a `publish: true` manifest entry does not prove that production
is deployed or active. Report intended and observed state separately when they
differ.

Do not silently resolve a conflict between sources. State the conflict and its
impact. Never copy a time, limit, workflow state, model, version, or DB count from
an old handoff into code without rechecking the current implementation and, for
an operational claim, the live service.

## Repository map

- `src/pai_loop/`: FastAPI application, domain rules, integrations, migrations,
  and static frontend.
- `tests/`: Python regression and public-contract tests.
- `workflows/`: versioned n8n workflow definitions.
- `manifest.json`: authoritative workflow publication allowlist and contract
  metadata.
- `scripts/`: n8n validation, deployment, credential binding, and E2E helpers.
- `docs/`: product specifications, architecture, and dated operational runbooks.
- `render.yaml` and `Dockerfile`: declared production runtime and startup path.
- `.github/workflows/`: CI and n8n deployment automation.

## Local setup and validation

Use Python 3.12 when matching CI; the package supports Python 3.11 or newer.

```bash
python -m pip install -e ".[test]"
python -m compileall -q src tests tools
python -m pytest --cov=pai_loop --cov-report=term-missing --cov-report=xml --cov-fail-under=85
```

Run focused tests while iterating, then the relevant full gate before handoff.
For frontend behavior, include `tests/test_frontend_public_contract.py` and the
feature-specific API tests. Exercise PostgreSQL behavior only against an explicit,
disposable test database. Never point tests or migration checks at the Render
production database.

When `workflows/`, `manifest.json`, or n8n scripts change, also run:

```bash
node --check scripts/deploy-workflows.mjs
node --check scripts/bind-claude-gateway-credentials.mjs
node scripts/deploy-workflows.mjs --validate-only
node scripts/bind-claude-gateway-credentials.mjs --self-test
node scripts/test-daily-workflow.mjs
node scripts/test-teams-delivery-workflow.mjs
node scripts/test-claude-gateway-workflow.mjs
```

Do not weaken the CI coverage gate, public-release boundary, wheel-content check,
frontend asset check, workflow validation, or secret/source scan to make a change
pass. This repository has no separate Ruff, Black, Mypy, ESLint, or Prettier gate;
do not invent one and report it as required.

## Evidence-first product invariants

- LLM output is untrusted structured extraction, never the final decision.
  Deterministic policy code evaluates extracted requirements and stored company
  facts. A human remains responsible for the final `GO`, `CONDITIONAL_GO`, `HOLD`,
  or `NO_GO` bid decision and for approving genuine policy exceptions.
- Audit every attachment in the latest valid PPS attachment manifest for a
  notice. Superseded historical attachments remain audit history but are not a
  substitute for the current manifest. Do not mark analysis complete when the
  expected, audited, and accepted attachment sets differ.
- Preserve exact evidence anchors, attachment identity, digest, page/section or
  cell location, and quoted support. Cross-attachment evidence may close a local
  gap only when the sibling evidence is explicit and traceable.
- Extraction failure, unsupported HWP/HWPX content, missing text, or poor
  document quality is a review/document-quality condition, not automatically an
  eligibility failure. Unsupported attachments still require an explicit,
  deterministic audit marker. Fail closed with a visible reason; never fabricate
  text.
- Distinguish bidder gates from descriptions, submission checklists, contract
  performance duties, staffing narratives, and evaluation prose. Do not promote
  descriptive language into a qualification requirement.
- Eligibility resolution is `applicable PASS path -> linked REVIEW exception ->
  DF-000 default FAIL`. Never replace it with a global severity ordering such as
  `FAIL > REVIEW > PASS`.
- Within an AND path, an explicit mandatory FAIL fails the path; otherwise retain
  a linked REVIEW when present. Within OR alternatives, a complete PASS path wins;
  otherwise use only an applicable linked REVIEW, then DF-000. REVIEW must never
  hide another explicit mandatory FAIL.
- Preserve review codes R01-R07 and R09. A DF-000 explanation must retain
  `failed_condition`, `required_value`, `current_value`,
  `unmatched_pass_paths`, and `review_not_matched_reason`.
- Evaluate time-sensitive company evidence as of the notice deadline. Do not
  apply today's qualification state retroactively to an older notice. A
  conditional PASS rule is not proof that the company possesses its condition.
- A notice-specific override or official answer does not become a global PASS
  rule. Oral information cannot satisfy a requirement when the notice excludes
  oral effect.

## Quantitative scoring boundaries

- Keep eligibility (`PASS` / `REVIEW` / `FAIL`), quantitative readiness, business
  risk, and the human bid decision as separate dimensions.
- Extract score tables, bands, totals, units, formulas, and their anchors from
  every attachment in the current valid PPS manifest. A rule may become
  `AUTO_ACTIVE` only after attachment coverage, anchors, table totals, bands,
  units, and formula checks are mechanically valid.
- When HWP table cells split one source row across consecutive paragraphs,
  rebind only a unique, bounded condition-to-score span inside the same table
  and criterion. Treat blank/section boundaries, another criterion or case
  anchor, repeated anchors, and overlapping row claims as hard failures; never
  alter the extracted operator, category, threshold, award, or maximum value.
- In a multi-column categorical HWP row where the extracted category anchor is
  repeated, an exact percent-award cell may seed repair only when that score is
  unique inside the owning criterion and every extracted category plus the
  award matches one bounded, ordered source span. A duplicate score cell or a
  missing category remains a hard failure. Keep parallel fact-type columns
  separate; for example, enterprise credit rating must not union company-bond
  or commercial-paper aliases.
- Successful extraction does not approve a score. Reject LLM-proposed company
  scores, `GO` values, or other decision fields; only deterministic application
  code may calculate and persist a score.
- Resolve an attachment-local missing score table only from a different
  attachment in the same current manifest whose mechanically validated table is
  `AVAILABLE` and whose filename has an exact, unambiguous required document
  role. Form/example files, ambiguous multi-role filenames, stale bindings, and
  same-role substitutes fail closed. An `AVAILABLE` table inside a `REVIEW`
  record may supply the table, but every remaining review issue must stay visible
  and must continue to block automatic activation.
- Persist a `CONFIRMED`/final company score only when every required input is bound
  to a verified, deadline-valid company fact. The deterministic engine may emit a
  clearly labeled, non-final `ESTIMATED` range from supported `ESTIMATED` facts;
  never present it as confirmed. When a required binding is absent or unsupported,
  return `UNSCORABLE` or `REVIEW_REQUIRED` with the missing inputs.
- Public performance or award similarity is candidate evidence only. A score may
  use only `VALIDATED` performance records that satisfy the notice's deadline,
  scope, VAT, completion, joint-share, and evidence rules.
- Never infer zero, full marks, a midpoint, or a convenient value for missing
  evidence. A quantitative `RED` state is not an eligibility `FAIL` and is not an
  automatic `NO_GO`.

## Notice lifecycle, search, queues, and history

- Preserve cancelled, closed, extended, and reissued notices plus prior analyses
  as audit history. Skip inactive notices before any paid/provider analysis.
  Newer authoritative extension or reissue data may reactivate only the current
  representative notice.
- Keep stored-notice search and PPS-wide discovery distinct. Search the local DB
  first; PPS-wide results must be visibly identified, saved explicitly, and
  analyzed only through a separate user action.
- Queue claims and retries must remain bounded, exact, idempotent, and safe under
  concurrency. Preserve stale/orphan recovery and prevent duplicate provider
  calls for the same notice, attachment, prompt, and generation.
- Backfills must be observable and resumable. Record the selected notices,
  outcome, retry/dead-letter state, and provider-call result; do not describe a
  queued item as completed.
- Analyses, snapshots, decisions, and source versions are append/versioned audit
  records. Use additive, idempotent migrations and transactions. Do not rewrite
  old evaluations or apply short operational-log retention to canonical business
  data.
- Result feedback supports human-reviewed rule improvement; do not claim that
  automatic machine learning is complete or promote an outcome directly into a
  global rule.

## LLM and workflow operations

- Production requirement extraction uses the n8n Claude gateway and the model
  declared by current production configuration. At the time this guidance was
  created, the enforced model is `claude-sonnet-5`.
- Direct OpenAI production calls are disabled. Preserve the startup guard that
  rejects `OPENAI_API_KEY` in production. Legacy filenames, fields, counters, or
  runbooks containing `openai` do not prove an OpenAI request occurred.
- `manifest.json` is the publication allowlist. Deploy or activate only workflows
  with `publish: true`; never interpret "activate all workflows" as permission to
  enable archived W00-W04, mock, migration, or smoke workflows.
- Read schedules and rate/batch limits from current workflow/config files, then
  verify live n8n/Render state before reporting that they are operational.
- A code change does not by itself authorize a production deployment, workflow
  activation, credential mutation, backfill, paid call, or DB write. Perform such
  actions only when the user's task includes that operation, and verify the
  resulting external state.

## Security and public-release boundary

- Never commit or print secret values, `.env`, `secrets.txt`, API keys, tokens,
  PINs, database URLs, webhook URLs, provider response identifiers, credential
  IDs, or credential exports.
- Keep `PAI_LOOP_API_KEY` server-to-server only and never expose it in browser
  JavaScript. A public operator PIN is a same-origin scoped demo control, not an
  embedded application secret.
- Never commit source proposals, workbooks, HWP/HWPX/PDF/PPTX files, raw
  attachments, private staging databases, internal notes, personal contact data,
  certificate bodies, or identifying registration numbers unless the identifier
  is explicitly approved by the public organization-profile contract. Use only
  approved, allowlisted, redacted seed assets.
- Do not place sensitive values in workflow JSON, browser responses, fixtures,
  screenshots, logs, PR descriptions, or Markdown examples. Use obvious
  placeholders.
- Synthetic fixtures use `SYN-` identifiers and must not copy real company or
  personal data. Contract tests may exercise an explicitly approved public
  organization-profile identifier; do not add another real identifier without
  contract review. Public APIs must not expose raw payloads, internal digests,
  provider metadata, or private evidence. Reviewed public evidence SHA-256 values
  are allowed only through the approved public-profile contract.
- Treat live credentials as deployment-platform or n8n credential-store state.
  Preserve remote n8n bindings only through the repository's exact node-name and
  node-type rules; do not copy credentials into Codex Cloud merely for
  convenience.

## Git, documentation, and handoff

- Prefer a task branch and a focused commit. Do not push directly to `main` unless
  the user explicitly requests it. Open a PR, wait for required CI, and merge only
  after the checks pass.
- Do not merge an old or conflicting branch wholesale. Port still-relevant
  behavior semantically onto current `main` with current tests.
- When behavior, contracts, schedules, limits, provider routing, or workflow
  publication changes, update the relevant current documentation in the same PR.
  Mark historical runbooks as historical rather than rewriting their evidence.
- In the final handoff, report files changed, tests run and their result, commit or
  PR, deployment state, and any unverified live assumptions.

## Code review rules

Flag as high priority any change that:

- can complete a notice without full current-manifest attachment coverage and
  anchors;
- changes eligibility precedence or mixes eligibility with scoring/risk/decision;
- guesses quantitative inputs or scores from missing company evidence;
- calls OpenAI directly in production or bypasses the n8n Claude contract;
- publishes a workflow outside the `manifest.json` allowlist;
- can duplicate provider calls, lose queue leases, or analyze inactive notices;
- exposes secrets, source documents, identifiers, private facts, or raw payloads;
- turns a notice-specific exception or human outcome into an unapproved global
  rule; or
- reports deployment, automation, learning, or analysis completion without
  verifiable runtime evidence.
