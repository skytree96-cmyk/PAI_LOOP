# Claude Sonnet 5 W13 migration runbook

This is a one-time, fail-closed migration for the production extraction path.
The repository keeps Workflow 13 at `publish:false` until a live Sonnet 5 E2E
has passed. Do not combine this migration with a normal all-workflow deploy.

## Preconditions

1. Run the offline contracts:

   ```powershell
   node scripts/deploy-workflows.mjs --validate-only
   node scripts/bind-claude-gateway-credentials.mjs --self-test
   node scripts/test-claude-gateway-workflow.mjs
   ```

2. In n8n, deactivate these three exact workflows before any PUT:

   - `PAI_LOOP 10 - Daily Opportunity Briefing`
   - `PAI_LOOP 11 - Analysis Backfill Queue`
   - `PAI_LOOP 13 - Claude Extraction Gateway`

   Workflow 12 may remain active because it only delivers an already stored
   briefing. W10 and W11 must remain inactive so no analysis job reaches W13
   while its model contract changes.

## Migrate the stored W13 definition

With `N8N_BASE_URL` and `N8N_API_KEY` set locally, run only W13:

```powershell
node scripts/deploy-workflows.mjs --only=pai-loop-13-claude-extraction-gateway
```

The deployer refuses the migration before any write unless W10, W11, and W13
are all inactive and `--only` selects W13. It permits exactly one credential
migration: the `anthropicApi` reference on the exact `Claude Sonnet 4.6` node
may move to the exact `Claude Sonnet 5` replacement in Workflow 13. It does not
match by position, partial name, or node type alone. Immediately after PUT it
GETs W13 again and verifies both the webhook Header Auth and Anthropic
credential references without printing their IDs.

## Rebind and verify

Set `PAI_LOOP_N8N_CLAUDE_CREDENTIAL_NAME` to the exact approved n8n credential
name. Validate first, then bind:

```powershell
node scripts/bind-claude-gateway-credentials.mjs --dry-run
node scripts/bind-claude-gateway-credentials.mjs
```

The binder leaves W13 inactive and performs a second GET after PUT to verify
the exact credential references. Keep W10 and W11 inactive. Activate only W13
for one bounded backend extraction E2E, confirm a `claude-sonnet-5` response and
valid structured extraction, then deactivate W13 again if promotion is not
immediate.

Only after that live E2E may a separate reviewed change set W13 to
`publish:true` and `promotionState:"verified-live-e2e"`. The subsequent normal
deployment can reactivate W10, W11, and W13. If any check fails, leave all three
inactive and restore the previous W13 export; do not bypass the assertions.

## CI behaviour during the staged migration

A push-time all-workflow deployment fails before its first PUT while W13 is
`publish:false`, because it would otherwise publish W10/W11 against an inactive
gateway. A deployment that still sees a remote Sonnet 4.6 W13 also requires the
bounded `--only` migration command and all three workflows inactive. Both
failures are intentional. Do not rerun the normal deployment until the separate
live-E2E promotion changes W13 to `publish:true`.
