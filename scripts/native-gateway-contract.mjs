import assert from "node:assert/strict";
import { nativeGatewaySchema } from "./native-gateway-schema.mjs";
import { validateNativeGatewayRequest } from "./native-gateway-request.mjs";
import { normalizeNativeGatewayResponse } from "./native-gateway-response.mjs";
import { gatewayResponseExpression } from "./gateway-response-contract.mjs";

export const nativeNodeName = "Claude Sonnet 5 Native JSON";
export const nativeTimeoutExpression = "={{ $json.provider_request.max_tokens === 32000 ? 300000 : 180000 }}";
export const nativeCanaryWorkflowKeys = ["pai-loop-10-daily-opportunity-briefing", "pai-loop-11-analysis-backfill",
  "pai-loop-12-teams-daily-delivery", "pai-loop-13-claude-extraction-gateway"];
export function assertPendingNativeSelection(config, onlyKey) {
  if (config.promotionState !== "awaiting-native-live-e2e"
    && config.nativeCanaryState !== "awaiting-root-synthetic-schema-probe") return false;
  assert(config.publish === true && config.contractVersion === "claude-extraction-gateway-2.0-native-json"
    && config.promotionState === "awaiting-native-live-e2e"
    && config.nativeCanaryState === "awaiting-root-synthetic-schema-probe", "native canary metadata is inconsistent");
  assert.equal(onlyKey, "pai-loop-13-claude-extraction-gateway", "pending native canary permits only W13; producers cannot be deployed or activated");
  return true;
}
export function assertPendingNativeInactive(remotes) {
  for (const key of nativeCanaryWorkflowKeys) assert.equal(remotes.get(key)?.active, false, `${key} must be inactive before pending native deployment`);
}
export function isNativeAnthropicNode(node) {
  return node?.name === nativeNodeName && node.type === "n8n-nodes-base.httpRequest"
    && node.typeVersion === 4.2 && node.parameters?.method === "POST"
    && node.parameters.url === "https://api.anthropic.com/v1/messages"
    && node.parameters.authentication === "predefinedCredentialType"
    && node.parameters.nodeCredentialType === "anthropicApi"
    && node.parameters.jsonBody === "={{ $json.provider_request }}";
}
export function assertNativeGatewayWorkflow(workflow) {
  const nodes = new Map(workflow.nodes.map(node => [node.name, node]));
  assert.equal(nodes.size, 9); assert.equal(workflow.nodes.length, 9);
  const provider = nodes.get(nativeNodeName);
  assert(isNativeAnthropicNode(provider));
  assert.deepEqual(Object.keys(provider.parameters).sort(), ["method", "url", "authentication", "nodeCredentialType", "sendHeaders", "headerParameters", "sendBody", "contentType", "specifyBody", "jsonBody", "options"].sort());
  assert.equal(provider.parameters.sendHeaders, true); assert.equal(provider.parameters.sendBody, true);
  assert.equal(provider.parameters.contentType, "json"); assert.equal(provider.parameters.specifyBody, "json");
  assert.deepEqual(provider.parameters.headerParameters, { parameters: [
    { name: "anthropic-version", value: "2023-06-01" }, { name: "Content-Type", value: "application/json" },
  ] });
  assert.deepEqual(provider.parameters.options, { timeout: nativeTimeoutExpression,
    response: { response: { fullResponse: true, neverError: true, responseFormat: "json" } },
    redirect: { redirect: { followRedirects: false } } });
  assert.equal(provider.retryOnFail, false);
  const source = fn => fn.toString().replace(/\r\n/g, "\n");
  assert.equal(nodes.get("Validate Gateway Request").parameters.jsCode,
    `${source(nativeGatewaySchema)}\n${source(validateNativeGatewayRequest)}\nreturn validateNativeGatewayRequest($json, $input.all().length, nativeGatewaySchema);`);
  assert.equal(nodes.get("Normalize Gateway Response").parameters.jsCode,
    `${source(nativeGatewaySchema)}\n${source(normalizeNativeGatewayResponse)}\nreturn normalizeNativeGatewayResponse($json, $execution, $('Validate Gateway Request').first().json.original_schema, nativeGatewaySchema);`);
  const webhook = nodes.get("Claude Extraction Webhook");
  assert.equal(webhook.type, "n8n-nodes-base.webhook");
  assert.equal(webhook.parameters.httpMethod, "POST");
  assert.equal(webhook.parameters.path, "pai-loop-claude/responses");
  assert.equal(webhook.parameters.authentication, "headerAuth");
  assert.equal(webhook.parameters.responseMode, "responseNode");
  assert.equal(webhook.parameters.options.responseData, "firstEntryJson");
  for (const [name, suffix, destination] of [
    ["Validate Gateway Request", "Input Failure", nativeNodeName],
    [nativeNodeName, "Model Failure", "Normalize Gateway Response"],
    ["Normalize Gateway Response", "Output Failure", "Respond Gateway Success"],
  ]) {
    const sanitizer = `Sanitize Gateway ${suffix}`;
    assert.equal(nodes.get(name).onError, "continueErrorOutput"); assert(!nodes.get(name).retryOnFail);
    assert.deepEqual(workflow.connections[name], { main: [
      [{ node: destination, type: "main", index: 0 }], [{ node: sanitizer, type: "main", index: 0 }],
    ] });
    assert.equal(nodes.get(sanitizer).type, "n8n-nodes-base.code");
    assert.deepEqual(workflow.connections[sanitizer], { main: [[{ node: "Respond Gateway Failure", type: "main", index: 0 }]] });
  }
  assert.deepEqual(workflow.connections[webhook.name], { main: [[{ node: "Validate Gateway Request", type: "main", index: 0 }]] });
  assert.equal(Object.keys(workflow.connections).length, 7);
  for (const [suffix, allowed] of [["Success", true], ["Failure", false]]) {
    const node = nodes.get(`Respond Gateway ${suffix}`);
    assert.equal(node.type, "n8n-nodes-base.respondToWebhook");
    assert.equal(node.parameters.responseBody, gatewayResponseExpression(allowed, "body"));
    assert.equal(node.parameters.options.responseCode, gatewayResponseExpression(allowed, "status"));
    assert(node.parameters.options.responseHeaders.entries.some(item => item.name === "Cache-Control" && item.value === "no-store"));
  }
  assert.equal(workflow.settings.saveDataSuccessExecution, "none");
  assert.equal(workflow.settings.saveDataErrorExecution, "none");
  assert.equal(workflow.settings.saveManualExecutions, false);
  assert.equal(workflow.settings.saveExecutionProgress, false);
  for (const node of workflow.nodes) assert(!node.retryOnFail && !node.executeOnce && !node.alwaysOutputData);
}
