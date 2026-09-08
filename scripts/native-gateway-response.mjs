// Embedded together with nativeGatewaySchema in the response Code node.
export function normalizeNativeGatewayResponse(response, execution, originalSchema, decodeSchema) {
  const object = item => item !== null && typeof item === "object" && !Array.isArray(item);
  const counter = item => Number.isSafeInteger(item) && item >= 0 && item <= 10000000 ? item : null;
  let stop = null, usage = null;
  const reject = detail => [{ json: { gateway_error: { version: "gateway-failure-v1",
    stage: "OUTPUT_NORMALIZATION", code: "OUTPUT_REJECTED", upstream_http_status: null,
    detail_code: detail, ...(stop === null ? {} : { stop_reason: stop, usage }) } } }];
  if (!object(response) || response.statusCode !== 200) {
    const status = response?.statusCode;
    return [{ json: { gateway_error: { version: "gateway-failure-v1", stage: "MODEL_EXECUTION",
      code: "MODEL_EXECUTION_FAILED", upstream_http_status: Number.isSafeInteger(status) && status >= 400 && status <= 599 ? status : null } } }];
  }
  const message = response.body;
  if (!object(message) || message.type !== "message" || message.role !== "assistant"
    || message.model !== "claude-sonnet-5") return reject("NATIVE_RESPONSE_INVALID");
  const stops = ["end_turn", "max_tokens", "refusal", "stop_sequence", "tool_use", "pause_turn", "model_context_window_exceeded"];
  if (!stops.includes(message.stop_reason)) return reject("NATIVE_RESPONSE_INVALID");
  stop = message.stop_reason;
  const supplied = object(message.usage) ? message.usage : {};
  const base = counter(supplied.input_tokens), created = counter(supplied.cache_creation_input_tokens), cached = counter(supplied.cache_read_input_tokens);
  const inputTokens = base === null || created === null || cached === null ? null : counter(base + created + cached);
  // output_tokens already includes thinking. Never add a thinking counter again.
  const outputTokens = counter(supplied.output_tokens);
  const totalTokens = inputTokens === null || outputTokens === null ? null : counter(inputTokens + outputTokens);
  usage = { input_tokens: inputTokens, output_tokens: outputTokens, total_tokens: totalTokens };
  if (stop === "max_tokens") return reject("NATIVE_STOP_MAX_TOKENS");
  if (stop === "refusal") return reject("NATIVE_STOP_REFUSAL");
  if (stop !== "end_turn") return reject("NATIVE_STOP_UNSUPPORTED");
  if (!Array.isArray(message.content) || !message.content.length || message.content.length > 32
    || message.content.some(block => !object(block) || !["text", "thinking", "redacted_thinking"].includes(block.type))) return reject("NATIVE_CONTENT_INVALID");
  const texts = message.content.filter(block => block.type === "text");
  if (texts.length !== 1) return reject("NATIVE_CONTENT_INVALID");
  const rawOutput = texts[0].text;
  if (typeof rawOutput !== "string") return reject("OUTPUT_TYPE_INVALID");
  if (rawOutput.length > 500000) return reject("OUTPUT_TOO_LARGE");
  if (!rawOutput.trim()) return reject("OUTPUT_EMPTY");
  let parsed;
  try { parsed = JSON.parse(rawOutput); } catch (ignored) { return reject("OUTPUT_JSON_INVALID"); }
  if (!object(parsed)) return reject("OUTPUT_NOT_OBJECT");
  // No Markdown stripping, substring extraction, coercion, or second model call.
  try { parsed = decodeSchema(originalSchema, "decode", parsed); }
  catch (ignored) { return reject("NATIVE_SCHEMA_DECODE_INVALID"); }
  const outputText = JSON.stringify(parsed);
  if (outputText.length > 500000) return reject("OUTPUT_TOO_LARGE");
  const executionId = String(execution?.id ?? "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 80);
  if (!executionId) return reject("EXECUTION_CONTEXT_INVALID");
  return [{ json: { id: `pai_claude_${executionId}`, status: "completed", model: "claude-sonnet-5",
    output_text: outputText, usage } }];
}
