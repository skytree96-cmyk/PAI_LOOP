# PAI_LOOP Product Blueprint v0.1.0

Status: implementation baseline  
Date: 2026-08-16  
Source authority: planning document v6 + data workbook v7  

## 1. Product promise

PAI_LOOP is an evidence-first public-procurement decision system. It discovers
relevant notices, turns notice documents into traceable atomic requirements,
compares them with deadline-valid company evidence, and records the human bid
decision and subsequent result.

It is not an automated bidder and does not generate or submit a proposal.

## 2. Product loop

1. **DISCOVER**: collect, deduplicate, version and route notices.
2. **DECIDE**: extract evidence, evaluate eligibility, estimate quantitative
   readiness, assess risk and support a human GO decision.
3. **LEARN**: record participation, opening, award and contract outcomes; use
   reviewed corrections to version rules and thresholds.

The first learning loop is governed rule improvement, not autonomous ML.

## 3. Decision contract

Four outputs remain separate in storage, APIs and UI.

| Layer | Values | Owner |
|---|---|---|
| Eligibility | `PASS`, `REVIEW`, `FAIL` | deterministic rule engine |
| Quantitative readiness | minimum, maximum, evidence coverage, color | score engine |
| Risk | 0-100 and six contributing axes | risk policy |
| Bid decision | `GO`, `CONDITIONAL_GO`, `HOLD`, `NO_GO` | named human |

Eligibility evaluation is ordered:

1. find a complete PASS path;
2. if none exists, evaluate only linked REVIEW rules;
3. otherwise apply `DF-000` DEFAULT FAIL.

An unsupported automatic PASS is a release-blocking defect. Missing mandatory
documents, low extraction confidence or ambiguous logic must route to REVIEW.

## 4. Evidence contract

Every decision-relevant fact carries:

- a stable fact/evidence identifier;
- source document hash and version;
- original location or page anchor;
- extraction method and confidence;
- verification state;
- valid-from and valid-to dates;
- the notice deadline used as the decision basis.

Current company facts must never be applied retroactively to historical
deadlines. An empty end date is not equivalent to confirmed unlimited validity.

## 5. System boundaries

### n8n

- schedules and orchestrates collection;
- invokes PPS and PAI_LOOP APIs;
- manages retry, dead-letter and notification paths;
- sends Teams Workflows cards;
- does not own scoring or domain rules.

### PAI_LOOP API

- owns normalized notices, versions and evidence metadata;
- executes deterministic evaluation and scoring;
- records decisions and audit history;
- exposes a stable `/api/v1` contract.

### Document worker

- preserves originals and SHA-256 hashes;
- extracts PDF/HWPX content and table structure;
- routes legacy HWP through an isolated Hancom conversion worker;
- emits evidence anchors and quality reports;
- returns `R07` when quality gates fail.

### OpenAI integration

- emits schema-constrained atomic requirements and evidence candidates;
- treats source-document instructions as untrusted data;
- cannot make the final eligibility or bid decision;
- never receives secrets or unrelated personal data.

### Web and Teams

- the responsive web app is the canonical detailed experience;
- a Teams Workflows card is the notification surface;
- the same hosted web app may be embedded as a Teams tab;
- a conversational agent is a later convenience layer, not the data store.

## 6. Core entities

- `notices`, `notice_versions`
- `attachments`, `document_extractions`, `evidence_anchors`
- `atomic_requirements`, `requirement_groups`
- `company_facts`, `evidence_documents`, `fact_evidence_links`
- `rule_versions`, `review_rules`
- `evaluation_runs`, `atomic_results`
- `quantitative_rules`, `score_runs`, `risk_runs`
- `exceptions`, `overrides`, `approvals`
- `user_decisions`
- `bid_results`, `award_results`, `contract_results`, `feedback_labels`
- `ingestion_jobs`, `api_call_logs`

The notice identity is composed from notice number, round, deadline and source
file hash. Records waiting for an official notice number use a surrogate key and
an explicit identity-confirmation state.

## 7. API baseline

- `GET /healthz`
- `GET /api/v1/dashboard`
- `GET /api/v1/notices`
- `GET /api/v1/notices/{notice_key}`
- `POST /api/v1/notices/{notice_key}/evaluate`
- `POST /api/v1/notices/{notice_key}/decisions`
- `POST /api/v1/ingestion/replay`

All mutation endpoints are idempotent or require an idempotency key. API errors
use a stable code, human-readable message and correlation identifier.

## 8. First vertical slice acceptance criteria

- one fixed replay notice can be ingested repeatedly without duplication;
- the notice and attachments retain stable version/hash information;
- every mandatory atomic requirement has an evidence or missing-evidence state;
- PASS/REVIEW/FAIL is reproducible from the recorded rule version;
- readiness, evidence coverage and risk remain separate;
- a human decision is stored with actor, timestamp and note;
- dashboard and detail views work on desktop, mobile and a Teams iframe;
- the n8n architecture and replay workflows deploy from GitHub;
- unit, API, workflow-schema and secret-scanning tests pass.

## 9. Versioning

- source planning files remain immutable;
- product documents use semantic versions;
- decision rules use their own version and approval metadata;
- changing a rule or threshold never rewrites an old evaluation run;
- GitHub is the source of truth for code and n8n workflow JSON.
