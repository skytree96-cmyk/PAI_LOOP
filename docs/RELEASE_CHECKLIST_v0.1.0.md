# PAI_LOOP Release Checklist v0.1.0

Date: 2026-08-16  
Release target: competition demo foundation / vertical slice  
Decision: **PASS for a local/access-restricted synthetic demo; HOLD for production**

This checklist applies only to repository outputs. The authorized planning,
notice and business-data corpus remains read-only outside the repository.

## 1. Current verification record

| Gate | Status | Evidence |
|---|---|---|
| Original files remain outside Git | PASS | the Git candidate set contains no DOCX, HWP/HWPX, PDF, spreadsheet, presentation, archive or database source artifact; the local synthetic SQLite file is ignored |
| Exact configured secret values absent | PASS | local equality scan compared configured values without printing them; no match found |
| Generic secret/PII patterns absent | PASS | OpenAI/GitHub/AWS/JWT/private-key/credential-URL/PPS/n8n and contact/identifier patterns returned no finding |
| Python import and bytecode compile | PASS | `python -m compileall -q src tests tools` |
| Python unit/API/integration tests | PASS | 44 passed |
| Python branch coverage | PASS | 87.88% total; CI minimum is 85% |
| Installed dependency consistency | PASS | `python -m pip check` reported no broken requirements |
| PPS client live compatibility | PASS | configured encoded key, current direct-array response and a `+09:00` deadline were parsed end to end without logging key or notice content |
| n8n manifest/workflow static validation | PASS | three definitions validated; JSON, node IDs/names, connections and Code-node syntax checked |
| Deployment script JavaScript syntax | PASS | `node --check scripts/deploy-workflows.mjs` |
| Release wheel contents | PASS | built wheel contains `index.html`, CSS, JavaScript, favicon and the versioned rule registry |
| Frontend referenced assets | PASS | all local link/script references resolve and JavaScript syntax checks pass |
| Browser contract/viewport smoke test | PASS | live FastAPI + Chrome at 1440px and 390px Teams context; 3 notices and PASS/REVIEW/FAIL rendered, detail and HOLD save passed, no duplicate IDs, horizontal overflow, page errors or 4xx responses |
| Container build and health check | NOT RUN | Docker is not installed in the current workstation environment; CI/runtime build remains required |
| Live GitHub Actions run | PENDING | must pass on the exact release commit |
| n8n replay contract E2E | PASS | built-in dry run and live local API replay → 3 keys → detail fetch → one REVIEW → final validator passed |
| Live n8n deployment | PASS | Architecture `B5kG7yj8SnL4af4f`, Replay `vpHuBXswJyOCStEZ`, Smoke `JDy3nFbWEkV2Jrhl`; names and node/connection counts match the local candidate and all three are inactive |

One third-party deprecation warning is currently suppressed by the normal test
configuration: Starlette's TestClient reports that its `httpx` path is
deprecated in favor of `httpx2`. It does not fail current tests, but dependency
upgrades should be planned and tested independently.

## 2. Closed candidate defect

### B-01 — n8n/backend replay contract mismatch — CLOSED

The workflow now calls the canonical `POST /api/v1/ingestion/replay`, validates
the replay envelope, expands its notice keys, requests
`GET /api/v1/notices/{notice_key}`, selects the single REVIEW fixture and maps
`latest_evaluation` into the common assessment contract. Eligibility is
`PASS | REVIEW | FAIL`; `DF-000` remains a reason code rather than a fourth enum.

Acceptance evidence:

- static workflow validation passed;
- the built-in dry-run path passed;
- the real local API contract path passed end to end;
- API tests prove replay idempotency;
- the deployed workflow remains inactive until production authentication is
  connected.

## 3. Remaining production blocker

### B-02 — end-user authentication and authorization incomplete

Production startup now fails closed unless a server API key is configured, and
`/api/v1` routes use a constant-time header check. This is a useful n8n-to-API
boundary, but it is not end-user identity or authorization. The browser SPA
must not receive the server key, so an external or Teams-embedded dashboard
still needs SSO plus a trusted backend/session boundary before it can safely
read or change notice data, company facts, evidence metadata or decisions.

Acceptance evidence for any non-local deployment:

- organization SSO or a server-side identity proxy protects the web/API;
- service-to-service calls use a separate scoped identity and rotateable key;
- roles distinguish viewer, reviewer, decision maker and administrator;
- the authenticated subject, not a caller-supplied display label, owns audit
  records;
- unauthorized read and write API tests return 401/403;
- rate limiting, CSRF strategy and CORS origins are reviewed for the chosen
  hosting topology.

For a competition demonstration before B-02 is implemented, keep browser and
API access local or behind a short-lived access-restricted proxy, and use only
synthetic fixtures. Do not place the server API key in HTML/JavaScript or load
company facts, evidence files, personal information or live user decisions.

## 4. CI enforcement

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests and manual
dispatch. It enforces:

- Python 3.12 installation and application/test dependency resolution;
- bytecode compilation;
- pytest with coverage and an 85% minimum;
- a real release-wheel build and required package-data inspection;
- deployment-script syntax and n8n manifest/workflow validation;
- existence of every local CSS/JavaScript asset referenced by `index.html`;
- scanning of Git-tracked text for common credentials and direct contact or
  Korean personal/business identifier patterns;
- rejection of office documents, HWP/HWPX, PDFs, archives and database files.

The static scanner reports only rule labels and file paths. It never prints a
matched credential value.

## 5. Demo release procedure

- [ ] Freeze a candidate commit and record its SHA.
- [ ] Confirm the repository visibility matches the data classification.
- [x] Run the exact-secret comparison locally with output redaction.
- [ ] Confirm the CI workflow passes on the candidate SHA.
- [x] Confirm all dashboard assets load with no browser/page errors.
- [x] Run the synthetic replay twice and verify the second run creates no
      duplicate notices or evaluations.
- [x] Verify PASS, REVIEW and FAIL examples display distinct explanations and
      evidence states.
- [x] Confirm no UI text presents an LLM recommendation as a final eligibility
      or bid decision.
- [x] Confirm n8n workflows are deployed inactive unless their trigger,
      authentication, retry, rate limit and alerting controls are approved.
- [ ] Inspect a failed n8n execution and confirm secrets and document bodies are
      not retained in logs.
- [ ] Test health and API behavior from the actual hosting boundary.
- [ ] Record rollback instructions and the prior n8n workflow export/commit.

## 6. Production-only gates

- [ ] Close B-02 with SSO/service authentication and RBAC.
- [ ] Use PostgreSQL with encrypted backups and tested restore procedures.
- [ ] Place source attachments in private encrypted object storage.
- [ ] Add database migrations; do not rely on `create_all` for upgrades.
- [ ] Configure correlation IDs, structured redaction and audit-log retention.
- [ ] Add bounded request sizes, timeouts, retry budgets and dead-letter review.
- [ ] Validate PPS pagination, watermarking, schema drift and attachment hashes.
- [ ] Validate OpenAI strict structured output, `store: false`, evidence-anchor
      verification and refusal/timeout handling using non-sensitive fixtures.
- [ ] Complete threat modeling, dependency/vulnerability scanning and recovery
      rehearsal.
- [ ] Obtain business-owner approval for scoring thresholds and every rule-set
      version.

## 7. Rollback

1. Disable affected n8n triggers before changing data or credentials.
2. Redeploy the last known-good workflow JSON from its Git commit.
3. Restore the prior application image and verify `/healthz` plus a read-only
   synthetic fixture.
4. Quarantine partial ingestion/evaluation runs by correlation or run ID; never
   rewrite historic evaluations in place.
5. Rotate any credential that may have entered an export, log or execution
   payload.
