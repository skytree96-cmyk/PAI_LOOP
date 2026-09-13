import fs from "node:fs";
import { nativeGatewaySchema } from "./native-gateway-schema.mjs";
import { validateNativeGatewayRequest } from "./native-gateway-request.mjs";
import { normalizeNativeGatewayResponse } from "./native-gateway-response.mjs";
import { gatewayResponseExpression } from "./gateway-response-contract.mjs";
import { nativeTimeoutExpression } from "./native-gateway-contract.mjs";

const path = "workflows/pai-loop-13-claude-extraction-gateway.json";
const workflow = JSON.parse(fs.readFileSync(path, "utf8"));
workflow.settings.saveExecutionProgress = false;
const nativeName = "Claude Sonnet 5 Native JSON";
const source = fn => fn.toString().replace(/\r\n/g, "\n");
workflow.nodes = workflow.nodes.filter(node => !["Claude JSON Extraction", "Claude Sonnet 5"].includes(node.name));
if (!workflow.nodes.some(node => node.name === nativeName)) workflow.nodes.splice(2, 0, {
  parameters: {}, id: "d086528f-11bc-46b0-94d5-a872e329ddf3", name: nativeName,
  type: "n8n-nodes-base.httpRequest", typeVersion: 4.2, position: [20, 0],
});
for (const node of workflow.nodes) {
  if (node.name === "Validate Gateway Request") node.parameters.jsCode = `${source(nativeGatewaySchema)}\n${source(validateNativeGatewayRequest)}\nreturn validateNativeGatewayRequest($json, $input.all().length, nativeGatewaySchema);`;
  if (node.name === nativeName) {
    node.position = [20, 0];
    node.onError = "continueErrorOutput";
    node.retryOnFail = false;
    node.parameters = {
      method: "POST", url: "https://api.anthropic.com/v1/messages",
      authentication: "predefinedCredentialType", nodeCredentialType: "anthropicApi",
      sendHeaders: true, headerParameters: { parameters: [
        { name: "anthropic-version", value: "2023-06-01" }, { name: "Content-Type", value: "application/json" },
      ] }, sendBody: true, contentType: "json", specifyBody: "json", jsonBody: "={{ $json.provider_request }}",
      options: { timeout: nativeTimeoutExpression, response: { response: { fullResponse: true, neverError: true, responseFormat: "json" } },
        redirect: { redirect: { followRedirects: false } } },
    };
  }
  if (node.name === "Normalize Gateway Response") node.parameters.jsCode = `${source(nativeGatewaySchema)}\n${source(normalizeNativeGatewayResponse)}\nreturn normalizeNativeGatewayResponse($json, $execution, $('Validate Gateway Request').first().json.original_schema, nativeGatewaySchema);`;
  for (const [suffix, allowed] of [["Success", true], ["Failure", false]]) if (node.name === `Respond Gateway ${suffix}`) {
    node.parameters.responseBody = gatewayResponseExpression(allowed, "body");
    node.parameters.options.responseCode = gatewayResponseExpression(allowed, "status");
  }
}
delete workflow.connections["Claude Sonnet 5"];
delete workflow.connections["Claude JSON Extraction"];
workflow.connections["Validate Gateway Request"].main[0][0].node = nativeName;
workflow.connections[nativeName] = { main: [
  [{ node: "Normalize Gateway Response", type: "main", index: 0 }],
  [{ node: "Sanitize Gateway Model Failure", type: "main", index: 0 }],
] };
if (workflow.nodes.some(node => node.credentials)) throw new Error("credential-free source required");
fs.writeFileSync(path, JSON.stringify(workflow, null, 2) + "\n");
if (process.argv.includes("--emit-ui-fragment")) {
  fs.mkdirSync(".local", { recursive: true });
  fs.writeFileSync(".local/native-gateway-ui-fragment.json", JSON.stringify({
    nodes: workflow.nodes, connections: workflow.connections, settings: workflow.settings,
  }, null, 2) + "\n");
}
