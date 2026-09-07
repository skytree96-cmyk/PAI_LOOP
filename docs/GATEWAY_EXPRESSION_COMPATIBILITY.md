# Gateway response expression compatibility

The response guard uses an explicit `catch (ignored)` binding. Native JavaScript
accepts `catch {}`, but the deployed expression parser throws
`null does not match type Pattern` while transforming that form in this guard.
This affects both body and status expressions on both terminal response nodes.

The official package dependency chain is
[`n8n@2.33.7`](https://registry.npmjs.org/n8n/2.33.7) →
[`n8n-workflow@2.33.2`](https://registry.npmjs.org/n8n-workflow/2.33.2) →
[`@n8n/tournament@1.9.0`](https://registry.npmjs.org/@n8n/tournament/1.9.0).
The workflow package also pins ast-types 0.16.1, esprima-next 5.8.4 and recast
0.22.0. Test dependencies and their transitive versions are locked under
`scripts/n8n-expression-test-runtime`; they are not application dependencies.

In n8n-workflow 2.33.2's published `dist/cjs/expression.js`, the legacy
`renderExpression` catches this ordinary Error and returns null. The
[Respond to Webhook implementation](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/RespondToWebhook/RespondToWebhook.node.ts)
uses `options.responseCode || 200` and leaves the JSON body unset for a falsy
body parameter. This explains the observed HTTP 200 empty response; the local
regression independently reproduces the parser failure, not the entire hosted
n8n execution.

CI installs these test-only dependencies with lifecycle scripts disabled and
runs `node scripts/test-gateway-expression-runtime.mjs`. The test executes all
four actual workflow expressions through Tournament, checks valid output and
optional/null usage, fixed failures, malformed data and raw prompt canaries,
and verifies that the former binding-free expressions fail in that engine.
The existing native terminal guard suite remains in place.

The production change is only the catch binding and the four regenerated
expressions. Response schemas, status decisions, model selection, call limits,
node identities, credential references, edges and execution retention settings
remain unchanged. Hosted behavior still requires a separate authenticated
synthetic invalid-request check before paid work resumes.
