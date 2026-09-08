# Department account backend contract

This is an opt-in backend. `PAI_LOOP_DEPARTMENT_ACCOUNTS_ENABLED` defaults to false. No real accounts, passwords, registration, feature activation, or production settings are created by this change. PIN browser authentication is retired regardless of this flag. In production the flag off pauses human writes/paid browser actions; it never restores PIN access. Server-to-server API contracts remain available. The old token environment setting is accepted only as deprecated configuration compatibility and has no authentication effect. See [the cutover runbook](ACCOUNT_LOGIN_CUTOVER.md).

## Browser authentication

- `GET /api/v1/runtime-profile` adds `department_accounts_enabled`. A failed account lookup in enabled mode must never trigger a PIN fallback.
- `GET /api/v1/accounts/me` returns `enabled`, `authenticated`, nullable `account`, nullable `csrf_token`, and `capabilities`. No cookie returns an anonymous 200; an invalid, revoked, expired, or disabled-account cookie returns 401. GET needs no CSRF token.
- `POST /api/v1/accounts/login` accepts `{username,password}` and requires the application's exact `Origin`. Username is ASCII letter followed by letters/digits/underscore/hyphen, at most 40 characters; it is normalized to uppercase. Password is at most 256 characters. Validation and authentication errors never echo submitted input. Successful login returns the same payload as `me`.
- Session cookie `pai_department_session` is opaque, random, HttpOnly, SameSite=Strict, host-only, path `/`, with an eight-hour expiry. It is Secure on HTTPS and always in production. The server stores only a version-namespaced SHA-256 digest. Pre-cutover cookies require a fresh login; failed-login bucket hashes remain unchanged. The separate `pai_department_csrf` cookie is readable by the same-origin UI and its digest is bound to that session.
- Every account mutation requires exact same-origin `Origin` and `X-CSRF-Token` matching both the CSRF cookie and the server session digest. Cross-site/same-site origins are rejected. Authenticated GETs use the cookie and reject cross-origin fetch metadata; they need no CSRF header.
- `POST /api/v1/accounts/logout` revokes the session and clears both cookies. Expired/revoked cookies can also be cleared by same-origin logout. A live session still requires CSRF.
- Login rotates the session, caps live sessions at eight per account, and removes long-expired session rows. Persistent 15-minute failed-login budgets are 5 per normalized username, 20 per client address, and 100 global. Successful logins do not consume a failure budget or clear earlier failures or their expiry, allowing all 24 departments to sign in from a shared office address. Existing lockouts are checked before password verification and still reject correct credentials until expiry. Budget checks and failure recording remain inside the same serial transaction. The throttle table has a hard 1,024-row bound and removes expired buckets. Forwarded headers are not trusted by this module. Failed/unknown/disabled logins use the same generic message and perform scrypt verification.
- All account, decision, and result-learning responses are `Cache-Control: no-store`.

`account` contains `id`, `username`, `role` (`DEPARTMENT` or `ADMIN`), `department_id`, `department_name`, `actor_label`, and `paid_analysis_allowed`. The department ID/name comes from the packaged public 24-department catalog; the browser never supplies an authenticated label. The admin label is `개발자 관리자` and has no department.

Capabilities are `read_department_records`, `write_decisions`, `write_results`, `manage_accounts`, `request_paid_analysis`, `recompute_analysis`, and `private_evidence`. The last is always false: a department/admin cookie never satisfies private-evidence authentication. A shared account identifies a department, not the individual who clicked.

## Department decisions and results

All authenticated departments and admins may read other departments' decision/result records. Only a department may write its own records. Admin cannot impersonate a department or modify its results.

- Existing `GET /api/v1/operator-decisions/notices/{notice_key}` returns all decisions, preserving the array contract.
- `POST /api/v1/operator-decisions/batch-read` accepts `{notice_keys:[...]}` with 1–200 keys, each at most 160 characters. It returns `{decisions_by_notice:{key:[...]},missing_notice_keys:[...]}`. This is a read capability, including for admins, but POST still needs same-origin/CSRF. It writes nothing. Missing notices are explicit; existing notices with no decisions have empty arrays.
- Existing decision POST requires `expected_decision_id` in account mode: null before the department's first decision; otherwise the ID of its latest decision. The server replaces `actor_label`, adds authenticated identity, appends history, and rejects stale department state with 409. Existing required rationale, current-evaluation ID validation, cancelled notice restriction, and server analysis snapshot remain intact. Analysis can be absent or incomplete; a later analysis never rebinds the recorded decision.
- Existing `/api/v1/result-learning` GET adds `outcomes` per notice, while preserving `latest_outcome`. Each row has nullable `account_id`, `department_id`, `department_name`, and `department_revision`. Existing public notice/detail stays redacted even with an account cookie.
- Result POST additionally requires `expected_outcome_id` in account mode: null for the first own result; otherwise the latest own result's ID. Idempotency keys are namespaced by authenticated department. Result PATCH keeps `expected_updated_at` and atomically verifies ownership plus the existing version. Account writes to cancelled notices are blocked. External/source observations remain immutable; a department may create its own review based on a readable observation.
- Decision and result creation carry a monotonically increasing `department_revision` per notice/department. Choose the highest own revision for the next `expected_*_id`; do not infer latest own revision from another department or from wall-clock timestamp ordering. Result PATCH retains its creation `department_revision` and increments the existing result workflow `revision`.

SQLite uses `BEGIN IMMEDIATE`; PostgreSQL uses transaction advisory locks for department creation and the existing evaluation insert barrier for decisions. Result updates retain a conditional SQL update. Different departments never share an optimistic version.

Legacy records retain their original IDs, labels, evaluation references, snapshots, and outcome links. New identity/revision columns are nullable and receive no backfill. Null means **unassigned**, regardless of an old actor label that happens to match a department name. A department cannot claim or edit an unassigned record.

## Paid work and private boundaries

Paid extraction requires the separate `paid_analysis_allowed` grant; admin does not receive it automatically. Account authentication happens before work, and the paid grant is checked again after the server determines that extraction is needed. Free stored-evidence recompute is available to authenticated accounts only when existing current evidence/audit checks prove an evaluation-only path. Forged `recompute_current` cannot fall through to paid extraction. Diagnostics and request polling require an account but do not require a paid grant.

The existing manual-analysis hourly quota is unchanged, including zero meaning no aggregate hourly quota. Existing queue, execution slot, cooldown, attachment, idempotency, and call budgets remain. Reservations record server account/department IDs and a free/paid audit event; they contain no session or password.

Bounded company-award search, PPS/pre-spec search and selected save, and explicit pre-spec analysis now accept same-origin cookie + CSRF with the separate paid grant (including external API reads). They preserve existing provider limits and do not imply automatic extraction. Pre-spec request polling needs a logged-in account but no paid grant. Sanitized `GET /performance-records` is also available to accounts and continues to exclude private imports. Performance mutations/imports, generic analysis/ingestion APIs, and private evidence are not granted by department cookies. Strong private-evidence credentials remain separate. Existing server-to-server API-key routes still work without browser Origin/fetch metadata/session cookies. A server key presented through a browser cannot replace department authorization. Even a server-key result update cannot overwrite an account-owned result.

## Bootstrap and administration

`POST /api/v1/accounts/bootstrap` is available before flag activation, but requires an explicit valid server API key and rejects browser context. Admin cookies, PIN, and development's no-key convenience never authorize it.

Input is `{dry_run:true, accounts:[{username,password,role,department_id?,active:false,paid_analysis_allowed:false}]}`, at most 25 entries. Passwords require 12–256 characters and are salted with independent 16-byte salts, using standard-library scrypt N=32768, r=8, p=1 and a 32-byte result. There is no credential in responses.

The dry run creates only a short-lived preview, returning `preview_id` and `WOULD_CREATE`/`EXISTS_UNCHANGED` statuses. Apply the exact same account plan with `dry_run:false,preview_id`. The preview is bound to all plan fields including the password through server-key HMAC, expires after 15 minutes, and is single-use. Existing usernames are always `EXISTS_UNCHANGED`: password, role, department, active state, and grant are never overwritten by bootstrap. Duplicate department identities are refused. No password reset occurs during import.

`POST /api/v1/accounts/initial-admin-activation` resolves the inactive-first-admin deadlock using the same server-only boundary. It accepts `{username,dry_run:true}` and returns `WOULD_ACTIVATE`, metadata and a preview. Apply with `{username,dry_run:false,preview_id}` only for an existing ADMIN with no department, inactive, revision 1, paid false, and no active administrator. The HMAC uses a distinct action namespace and binds the saved identity/revision/state. Fifteen-minute expiry, single use, 100-preview bound and the existing account transaction lock apply. Successful activation changes only active/revision, revokes all stored sessions and records `INITIAL_ADMIN_ACTIVATED`. No password or grant changes. A fresh dry run on an active target returns `ALREADY_ACTIVE`; replayed apply or a changed state is 409. Deliberately disabled/revised admins cannot use this initial-only path.

Admin-cookie endpoints:

- `GET /api/v1/accounts`: account metadata, no hashes.
- `PATCH /api/v1/accounts/{id}`: `{expected_revision,active?,paid_analysis_allowed?,password?}`. Role/department are immutable. Any accepted update increments revision, revokes target sessions, and adds an audit event.
- `GET /api/v1/accounts/sessions/list`: up to 200 session metadata rows, no session/CSRF values or hashes.
- `POST /api/v1/accounts/sessions/{id}/revoke`: explicit revocation.
- `GET /api/v1/accounts/audit/list`: latest 200 bounded metadata events, no credentials, evidence, reasons, or raw request bodies.

The account dialog provides ADMIN-only metadata refresh, per-department activation, and external-query/paid-analysis permission buttons for both department and administrator accounts. Activation sends only `expected_revision` and `active:true`; permission changes send only `expected_revision` and the requested `paid_analysis_allowed` boolean. Neither changes passwords, roles, department ownership, bootstrap defaults, or the existing pre-analysis cost confirmation. Pending changes disable all account actions. A conflict or ambiguous response clears the list until an explicit refresh and is never retried automatically. Every accepted change revokes the target's sessions; changing the current administrator's permission clears private content and shows an explicit return-to-login button. Administrator paid permission never grants proxy department decisions/results.

There is no public signup, account deletion, role reassignment, or impersonation endpoint. Bootstrap and account activation are separate operator actions. Existing supported SQLite databases receive additive account tables and nullable identity columns under the migration transaction; migration checks remain idempotent and fail closed on incompatible physical schema. PostgreSQL uses the same additive DDL, but this local validation does not activate or connect to a production database.

## PostgreSQL CI integration gate

`tests/test_postgres_department_accounts.py` runs against the existing disposable PostgreSQL 16 service through `PAI_LOOP_TEST_POSTGRES_URL` in the normal CI test/coverage step. It adds no duplicate workflow execution or service. Missing configuration skips PostgreSQL integration cases locally and fails them when `CI=true`.

The fixture accepts only the repository's local host allowlist and `pai_loop_test` database prefix, rejects URL/service/host-address overrides, and creates a generated `syn_accounts_<uuid>` schema per test. Only that schema is eligible for teardown. It never creates, drops, or migrates the public schema or a production database.

The real PostgreSQL cases cover an existing pre-account schema upgrade twice with unassigned decision IDs/labels/snapshots and linked outcomes preserved; two simultaneous first decisions/results for one department; independent writes from different departments; cross-department overwrite rejection; and two updates using the same result version. Create tests observe actual ungranted PostgreSQL advisory locks before release; the update test synchronizes two real SQL updates after both handlers read the original version. A local skipped case is not PostgreSQL validation: these six integration cases must pass in CI before rollout.
