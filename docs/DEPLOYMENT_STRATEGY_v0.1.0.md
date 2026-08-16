# Deployment strategy v0.1.0

## Decision

The first contest and internal pilot release uses one responsive web application
as the canonical product, with n8n orchestration and Teams as the delivery
surface:

```text
PPS + documents -> n8n -> PAI_LOOP API/DB -> web dashboard
                                      \-> Teams Workflows card -> detail URL
```

This avoids an executable download, keeps the full evidence and three-year
history experience in a layout suited to tables, and lets staff receive alerts
inside their existing Teams workflow. The same hosted web app can later be
packaged as a Teams personal or channel tab without rebuilding the product.

## Why not a Teams-only bot for v0.1

Teams is technically capable of tabs, agents/bots, proactive notifications,
Adaptive Cards, message extensions and single sign-on. The material risk for a
contest is not Teams capability; it is tenant administration, Entra registration
and app-approval lead time. External judges may also be unable to install an app
inside the company's tenant.

A full conversational agent is therefore phase two. It should answer bounded
queries such as “today's notices above 80”, “show notice number …”, and “items
awaiting review”, while the evidence table and human decision form stay in the
web/tab experience.

Microsoft references:

- Teams platform overview: <https://learn.microsoft.com/en-us/microsoftteams/platform/overview>
- Teams tabs: <https://learn.microsoft.com/en-us/microsoftteams/platform/tabs/what-are-tabs>
- tab SSO: <https://learn.microsoft.com/en-us/microsoftteams/platform/tabs/how-to/authentication/tab-sso-overview>
- proactive bot messages: <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages>
- custom-app upload and publishing: <https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/apps-upload>

## Teams notification path

Use the Teams **Workflows** webhook trigger, not the retired Office 365 connector
or legacy Incoming Webhook connector. n8n posts a compact Adaptive Card containing
only:

- eligibility and readiness;
- the three strongest reasons and any exclusion condition;
- deadline and owner status;
- a “view evidence / record decision” URL.

Detailed score breakdowns, three-year award history and attachment evidence open
in the web app. The webhook URL is a secret and belongs in the n8n credential
store. For a production flow, use an approved service identity or authenticated
tenant-only trigger; do not rely on an anonymously callable URL simply because
the URL is hard to guess.

Microsoft documents the Workflows webhook option here:
<https://support.microsoft.com/en-us/office/send-messages-in-teams-using-incoming-webhooks-8ae491c7-0394-4861-ba59-055e33f75498>.
The legacy connector retirement is documented here:
<https://devblogs.microsoft.com/microsoft365dev/retirement-of-office-365-connectors-within-microsoft-teams/>.

## Release topology

### Contest/demo

- one HTTPS container hosting FastAPI and the bundled responsive static app;
- managed PostgreSQL for shared/demo state, or an ephemeral SQLite database only
  for a single-user replay demonstration;
- current n8n Community instance for orchestration;
- object storage for attachments only after access control and retention are set;
- synthetic replay fixture available even if an external API is unavailable;
- Teams Workflows card optional; browser link/QR remains the judge-safe entry.

### Internal pilot

- private network or access proxy in front of API/web;
- PostgreSQL plus private object storage with encryption and lifecycle rules;
- company SSO and role-based access (`viewer`, `reviewer`, `approver`, `admin`);
- n8n service ownership/co-owner rather than a single employee account;
- Teams custom tab uploaded to the organization catalog and pinned by policy;
- centralized logs with correlation IDs and secret/personal-data redaction.

## Deployment gates

1. All automated tests, workflow validation and secret scans pass.
2. No source attachment, internal row or extracted corpus is inside the public
   repository, image, container layer or browser bundle.
3. Production starts with an empty/private data store; synthetic data is visibly
   labeled and never mixed into business metrics.
4. HTTPS, authentication, authorization, rate limits and CSRF strategy are
   enabled before accepting external writes.
5. PPS, OpenAI, n8n and Teams secrets are supplied by the deployment platform,
   never by a client or committed file.
6. A fixed replay scenario is demonstrated before the live-data path, followed
   by one live health/API result to prove integration.
7. Rollback means deploying the previous immutable image and rule version; it
   never rewrites historical evaluation runs.

## Phase sequence

| Phase | Deliverable | Exit signal |
|---|---|---|
| 0.1 foundation | deterministic API, synthetic replay, responsive dashboard, n8n architecture/replay | CI green and reproducible local demo |
| 0.2 ingestion | live PPS incremental collector, attachment hashes, extraction queue | duplicate-safe daily run and dead-letter evidence |
| 0.3 decision | strict OpenAI extraction, anchor validation, quantitative/risk policies | labeled regression set and human review audit |
| 0.4 delivery | Teams Workflows cards and hosted web | internal users complete review without local install |
| 0.5 Teams app | tab SSO and bounded agent/search extension | tenant approval and role enforcement verified |
| 1.0 governed pilot | results feedback, monitoring, backup/restore, runbooks | agreed accuracy, latency and operational SLOs |

