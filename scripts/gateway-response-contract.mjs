// Embedded directly in Respond-to-Webhook expressions. A separate Code node
// cannot be the final guard: a runner failure may forward that node's input.
// Keep an explicit catch binding: n8n's AST scope transform rejects `catch {}`.
export function guardGatewayResponse(value, successAllowed) {
  const failure = () => ({ status: 500, body: { gateway_error: {
    version: "gateway-failure-v1", stage: "OUTPUT_NORMALIZATION",
    code: "OUTPUT_REJECTED", upstream_http_status: null, detail_code: "TERMINAL_GUARD_REJECTED",
  } } });
  const object = (item) => item !== null && typeof item === "object" && !Array.isArray(item);
  const keys = (item, expected) => object(item)
    && Object.keys(item).sort().join(",") === [...expected].sort().join(",");
  const counter = (item) => item === null || item === undefined
    || (Number.isSafeInteger(item) && item >= 0 && item <= 10000000);
  try {
    // Expected normalization failures travel through main[0] as a safe envelope.
    // Both terminal lanes still rebuild it and can only return HTTP 500.
    if (!successAllowed || (object(value) && Object.hasOwn(value, "gateway_error"))) {
      if (!keys(value, ["gateway_error"])) return failure();
      const item = value.gateway_error;
      const fields = ["version", "stage", "code", "upstream_http_status"];
      if (!keys(item, fields) && !keys(item, [...fields, "detail_code"])) return failure();
      const pairs = { INPUT_VALIDATION: "REQUEST_REJECTED", MODEL_EXECUTION: "MODEL_EXECUTION_FAILED",
        OUTPUT_NORMALIZATION: "OUTPUT_REJECTED" };
      if (item.version !== "gateway-failure-v1" || !Object.hasOwn(pairs, item.stage)
        || pairs[item.stage] !== item.code) return failure();
      const status = item.upstream_http_status;
      if (status !== null && (item.stage !== "MODEL_EXECUTION"
        || !Number.isSafeInteger(status) || status < 400 || status > 599)) return failure();
      const detail = item.detail_code;
      const details = ["OUTPUT_EMPTY", "OUTPUT_TYPE_INVALID", "OUTPUT_TOO_LARGE",
        "OUTPUT_FENCE_INVALID", "OUTPUT_JSON_INVALID", "OUTPUT_NOT_OBJECT",
        "EXECUTION_CONTEXT_INVALID", "NORMALIZER_EXCEPTION", "TERMINAL_GUARD_REJECTED"];
      if (detail !== undefined && detail !== null
        && (item.stage !== "OUTPUT_NORMALIZATION" || !details.includes(detail))) return failure();
      if (successAllowed && (item.stage !== "OUTPUT_NORMALIZATION" || !details.includes(detail))) return failure();
      return { status: 500, body: { gateway_error: { version: "gateway-failure-v1",
        stage: item.stage, code: item.code, upstream_http_status: status,
        ...(detail === undefined || detail === null ? {} : { detail_code: detail }) } } };
    }
    if (!keys(value, ["id", "status", "model", "output_text", "usage"])
      || value.status !== "completed" || value.model !== "claude-sonnet-5"
      || typeof value.id !== "string" || !/^pai_claude_[A-Za-z0-9_-]{1,80}$/.test(value.id)
      || typeof value.output_text !== "string" || !value.output_text.trim()
      || value.output_text.length > 500000) return failure();
    const parsed = JSON.parse(value.output_text);
    if (!object(parsed)) return failure();
    const usage = value.usage;
    const usageKeys = ["input_tokens", "output_tokens", "total_tokens"];
    if (!object(usage) || Object.keys(usage).some(key => !usageKeys.includes(key))
      || !usageKeys.every(key => counter(usage[key]))) return failure();
    return { status: 200, body: { id: value.id, status: "completed", model: "claude-sonnet-5",
      output_text: JSON.stringify(parsed), usage: { input_tokens: usage.input_tokens ?? null,
        output_tokens: usage.output_tokens ?? null, total_tokens: usage.total_tokens ?? null } } };
  } catch (ignored) {
    return failure();
  }
}

export function gatewayResponseExpression(successAllowed, field) {
  if (typeof successAllowed !== "boolean" || !["body", "status"].includes(field)) {
    throw new Error("invalid gateway expression contract");
  }
  // Git checkouts may use CRLF; workflow JSON stores the reviewed LF expression.
  const source = guardGatewayResponse.toString().replace(/\r\n/g, "\n");
  return `={{ (${source})($json, ${successAllowed}).${field} }}`;
}
