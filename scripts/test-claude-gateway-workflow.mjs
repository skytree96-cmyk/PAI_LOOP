import assert from "node:assert/strict";
import fs from "node:fs";

const workflow = JSON.parse(
  fs.readFileSync("workflows/pai-loop-13-claude-extraction-gateway.json", "utf8"),
);
const nodes = new Map(workflow.nodes.map((node) => [node.name, node]));
const webhook = nodes.get("Claude Extraction Webhook");
const validation = nodes.get("Validate Gateway Request");
const chain = nodes.get("Claude JSON Extraction");
const model = nodes.get("Claude Sonnet 5");
const normalizer = nodes.get("Normalize Gateway Response");

assert.equal(workflow.nodes.length, 5);
assert.equal(webhook.type, "n8n-nodes-base.webhook");
assert.equal(webhook.parameters.httpMethod, "POST");
assert.equal(webhook.parameters.path, "pai-loop-claude/responses");
assert.equal(webhook.parameters.authentication, "headerAuth");
assert.equal(webhook.parameters.responseMode, "lastNode");
assert.equal(chain.type, "@n8n/n8n-nodes-langchain.chainLlm");
assert.equal(chain.typeVersion, 1.9);
assert.equal(model.type, "@n8n/n8n-nodes-langchain.lmChatAnthropic");
assert.equal(model.typeVersion, 1.5);
assert.equal(model.parameters.model.value, "claude-sonnet-5");
assert.equal(model.parameters.options.maxTokensToSample, "={{ $json.max_output_tokens }}");
assert.equal(model.parameters.options.thinkingMode, "adaptive");
assert.equal(model.parameters.options.effort, "medium");
for (const unsupported of ["temperature", "topP", "topK", "thinkingBudget"]) {
  assert.equal(unsupported in model.parameters.options, false);
}
assert.equal(workflow.settings.saveDataSuccessExecution, "none");
assert.equal(workflow.settings.saveDataErrorExecution, "none");
for (const node of workflow.nodes) assert.equal(node.credentials, undefined);

const executeValidation = new Function("$json", validation.parameters.jsCode);
const validBody = {
  model: "claude-sonnet-5",
  service_tier: "default",
  store: false,
  max_output_tokens: 20000,
  input: [
    {
      role: "system",
      content: [
        {
          type: "input_text",
          text: "You extract procurement requirements as evidence only. Never decide a bid.",
        },
      ],
    },
    {
      role: "user",
      content: [
        {
          type: "input_text",
          text: "Allowed attachment IDs: [\"a-1\"]\n\nSOURCE:\npublic source text",
        },
      ],
    },
  ],
  text: {
    format: {
      type: "json_schema",
      name: "pai_loop_requirements",
      strict: true,
      schema: {
        type: "object",
        additionalProperties: false,
        properties: { summary: { type: "string" } },
        required: ["summary"],
      },
    },
  },
};

const validated = executeValidation({ body: validBody });
assert.equal(validated.length, 1);
assert.equal(validated[0].json.system_prompt, validBody.input[0].content[0].text);
assert.match(validated[0].json.user_prompt, /RESPONSE JSON SCHEMA/);
assert.equal(validated[0].json.max_output_tokens, 20000);

const correctiveBody = structuredClone(validBody);
correctiveBody.input[1].content[0].text =
  `FINAL CORRECTIVE RETRY. Regenerate the JSON.\n\n${validBody.input[1].content[0].text}`;
assert.equal(executeValidation({ body: correctiveBody }).length, 1);

assert.throws(
  () => executeValidation({ body: { ...validBody, model: "gpt-5.6-luna" } }),
  /model must be claude-sonnet-5/,
);
assert.throws(
  () => executeValidation({ body: { ...validBody, max_output_tokens: 20001 } }),
  /max_output_tokens is outside/,
);
assert.throws(
  () => executeValidation({ body: { ...validBody, arbitrary_prompt: true } }),
  /request fields do not match/,
);
assert.throws(
  () => executeValidation({
    body: {
      ...validBody,
      text: { format: { ...validBody.text.format, strict: false } },
    },
  }),
  /strict response format is invalid/,
);

const executeNormalizer = new Function(
  "$json",
  "$execution",
  "$",
  normalizer.parameters.jsCode,
);
const normalised = executeNormalizer(
  { text: "{\"summary\":\"ok\"}" },
  { id: "fixture-123" },
  () => ({
    all: () => [{
      json: {
        tokenUsage: { promptTokens: 12, completionTokens: 7, totalTokens: 19 },
      },
    }],
  }),
);
assert.deepEqual(normalised[0].json, {
  id: "pai_claude_fixture-123",
  status: "completed",
  model: "claude-sonnet-5",
  output_text: "{\"summary\":\"ok\"}",
  usage: { input_tokens: 12, output_tokens: 7, total_tokens: 19 },
});

console.log("Claude extraction gateway workflow tests passed");
