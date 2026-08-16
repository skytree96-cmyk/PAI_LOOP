# PAI_LOOP

GitHub-managed n8n workflows for the PAI_LOOP automation project.

## Deployment

Changes under `workflows/` are validated and deployed to n8n by GitHub Actions after a push to `main`.

Required repository Actions secrets:

- `N8N_BASE_URL`
- `N8N_API_KEY`

Secrets and credential values must never be committed. GitHub is the source of truth; avoid editing deployed workflows directly in n8n without syncing the JSON back here.
