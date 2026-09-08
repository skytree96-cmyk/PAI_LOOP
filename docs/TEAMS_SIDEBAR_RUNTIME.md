# Teams sidebar destination

The existing sidebar button reads `paiLoopRuntimeConfig.paiBotTeamsUrl`.
The backend fills that JSON value from the `PAI_BOT_TEAMS_URL` environment variable
when serving the application index and all registered frontend entry routes,
including `/index.html`. No new API endpoint or browser credential is needed.

Set `PAI_BOT_TEAMS_URL` to the user-approved exact Teams HTTPS destination through
the deployment platform's authorized environment UI. Apply/restart the service
and verify its Live version, then reload the application. Do not put the actual
channel/group/tenant URL in source, workflow JSON, examples, logs or screenshots.
The destination is intentionally delivered to users of the public sidebar; it
must not contain a secret access token. Opening it does not grant Teams membership.

Only HTTPS on the exact `teams.microsoft.com` host is accepted, without userinfo,
non-default ports, control characters or backslashes. Missing/invalid configuration
leaves the button disabled with “채널 연결 준비 중”; it does not open a generic home
page. Valid configuration opens the supplied destination in a new tab with
`noopener,noreferrer`. Query and fragment data are preserved by safe JSON encoding.

The inert JSON script escapes HTML delimiters. Dynamic index responses use
`Cache-Control: no-store` and retain the existing CSP/security middleware. Other
static assets retain their existing serving behavior. Environment changes require
a process restart; no configuration write endpoint is added.

The provided Teams PNG already exists unchanged in the static directory. That
directory is mounted at `/`, so the image is served from `/teams-icon.png`.
The former `/static/teams-icon.png` path returned 404. The fix changes the reference,
without recreating the brand image or changing button IDs, events or accessible
labels. Frontend asset version: `20260908-teams-sidebar-v1`.
