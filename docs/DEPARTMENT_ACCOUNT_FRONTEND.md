# Department account frontend

The static application uses `runtime-profile.department_accounts_enabled` to
select the opt-in department login flow. This change does not activate the flag,
create accounts, or deploy the application. The disabled mode keeps the existing
scoped operator PIN behavior.

In account mode, login and logout use same-origin cookies; mutations send the
session's `X-CSRF-Token`. The password is cleared after every login attempt and
when the dialog closes. Account lookup or login failures never fall back to a
PIN. Unrelated discovery, private performance editing, pre-specification analysis,
and company-award mutations show an explicit unavailable message instead of
attempting those operations with a department cookie.

An account change, session rotation, logout, or current-session 401 clears private
records and open forms. In-flight responses are checked against the session
generation before success or error handling, including `/accounts/me`. An old
401 cannot expire a newer login, and a delayed response cannot restore old
records. Result pagination and loading state are also reset for the new account.

Decision list hydration sends at most 200 notice keys per batch. Only complete,
successful reads enable the department's decision filter; missing or failed reads
remain unavailable rather than becoming undecided. Admins read department
histories in detail and have no own-department decision filter or write controls.

Decision and result forms use the authenticated department's highest
`department_revision`, independently of timestamps and other departments. Legacy
unassigned records never become an own record because their actor label matches.
Decision POST carries `expected_decision_id`. A new own result carries
`expected_outcome_id`, and editing an existing own manual result sends PATCH with
`expected_updated_at`. External result observations can seed a separate review.
A conflict reloads the current records for review; the UI never retries a write
automatically or adopts a newer version silently.

`tests/test_department_accounts_frontend.py` executes the actual JavaScript in a
Node VM with synthetic records and controlled fetch responses. It covers session
races, login cleanup, batch boundaries, own form payloads, conflict reloads, and
the separate paid-analysis permission. The existing frontend, operator-decision,
and result-editor tests remain part of the focused validation. These tests do not
verify a production deployment, browser cookie policy, or visual layout.
