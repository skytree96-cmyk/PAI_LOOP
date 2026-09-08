# Required login for the Render work app

The Render application requires an active department or administrator account
before returning business pages or data. The separate Cloudflare introduction
site is unchanged. This supersedes the older anonymous read-only demo contract.

An anonymous visit to `/`, `/index.html`, or an application deep link receives
only the standalone login document. It has no application markup, notice data,
runtime Teams destination, or application bootstrap script. Its only requests
are session discovery and explicit account login. A successful login reloads the
same URL, preserving the notice link without accepting an external redirect.
The authenticated document stays hidden until its initial session check passes.

The common application boundary protects API routes, schema/docs, and other
static paths before their individual dependencies run. Necessary static assets,
account login/session/logout endpoints, and the existing minimal `/healthz`
operational response remain reachable. Session endpoints retain their own
origin, CSRF, rate-limit, and authentication requirements. Application HTML and
API responses use `Cache-Control: no-store`.

The former `public_read_allowed` selector now chooses the sanitized department
read representation only after account authentication; it grants no anonymous
access. Valid server-to-server API keys retain internal access. Browser requests
cannot substitute a server key for account authority. The separate private
evidence token still applies only to its existing evidence routes. Department
roles, paid-analysis grants, CSRF, ownership, and human decision policy are
unchanged. Disabling department accounts closes browser access; the retired PIN
cannot reopen it.

Logout, a current-session 401, or back/forward restoration hides the application,
clears private state and DOM, and returns to the same URL's login page. Late
responses from an earlier account epoch cannot repopulate the new session. The
login page clears passwords after submission, stores no credentials, and checks
session state after an ambiguous login response rather than replaying the POST.
If the authenticated page cannot confirm its session because of a network,
server, or malformed-response error, it clears the work DOM and stops on a fixed
error with an explicit retry button. It does not repeatedly reload the page.

Validation uses synthetic accounts and a disposable local SQLite database.
API tests cover anonymous closure, sanitized account reads, revoked/disabled
sessions, disabled account mode, server authentication, and private boundaries.
Frontend tests execute the login script and session lifecycle. A local 390px
browser check covers login, deep-link return, logout, expiry, and overflow.
These checks do not establish deployment or live account availability. Verify
an actual administrator browser login and the enabled account setting before
deploying the gate, so account administration remains reachable. An active DB
record alone is insufficient. The health probe and server-key workflow contract
are preserved.
