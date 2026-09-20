// Embedded directly in Respond-to-Webhook expressions. A separate Code node
// cannot be the final guard: a runner failure may forward that node's input.
// Keep an explicit catch binding: n8n's AST scope transform rejects `catch {}`.
import { gatewayFailureDetails } from "./gateway-failure-details.mjs";

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
      const optional = ["detail_code", "stop_reason", "usage"];
      if (!object(item) || fields.some(key => !Object.hasOwn(item, key))
        || Object.keys(item).some(key => !fields.includes(key) && !optional.includes(key))) return failure();
      const pairs = { INPUT_VALIDATION: "REQUEST_REJECTED", MODEL_EXECUTION: "MODEL_EXECUTION_FAILED",
        OUTPUT_NORMALIZATION: "OUTPUT_REJECTED" };
      if (item.version !== "gateway-failure-v1" || !Object.hasOwn(pairs, item.stage)
        || pairs[item.stage] !== item.code) return failure();
      const status = item.upstream_http_status;
      if (status !== null && (item.stage !== "MODEL_EXECUTION"
        || !Number.isSafeInteger(status) || status < 400 || status > 599)) return failure();
      const detail = item.detail_code;
      const details = gatewayFailureDetails()[item.stage];
      if (detail !== undefined && detail !== null
        && !details.includes(detail)) return failure();
      if (item.stage === "MODEL_EXECUTION" && detail != null
        && ((detail === "MODEL_HTTP_ERROR") !== (status !== null))) return failure();
      if (successAllowed && !(item.stage === "OUTPUT_NORMALIZATION" && details.includes(detail))
        && !(item.stage === "MODEL_EXECUTION" && item.stop_reason == null && item.usage == null)) return failure();
      // A native HTTP response may also reach main[0] with a safe model failure.
      const stop = item.stop_reason;
      const usage = item.usage;
      const stops = ["end_turn", "max_tokens", "refusal", "stop_sequence", "tool_use", "pause_turn", "model_context_window_exceeded"];
      if ((stop !== undefined && stop !== null) || (usage !== undefined && usage !== null)) {
        if (item.stage !== "OUTPUT_NORMALIZATION" || !details.includes(detail) || !stops.includes(stop)
          || !keys(usage, ["input_tokens", "output_tokens", "total_tokens"])
          || !Object.values(usage).every(counter)) return failure();
        const requiredDetail = stop === "max_tokens" ? "NATIVE_STOP_MAX_TOKENS"
          : stop === "refusal" ? "NATIVE_STOP_REFUSAL" : stop === "end_turn" ? null : "NATIVE_STOP_UNSUPPORTED";
        if ((requiredDetail && detail !== requiredDetail) || (!requiredDetail && detail?.startsWith("NATIVE_STOP_"))) return failure();
        if (usage.input_tokens != null && usage.output_tokens != null && usage.total_tokens != null
          && usage.total_tokens !== usage.input_tokens + usage.output_tokens) return failure();
      } else if (detail?.startsWith("NATIVE_STOP_")) return failure();
      return { status: 500, body: { gateway_error: { version: "gateway-failure-v1",
        stage: item.stage, code: item.code, upstream_http_status: status,
        ...(detail === undefined || detail === null ? {} : { detail_code: detail }),
        ...(stop === undefined || stop === null ? {} : { stop_reason: stop, usage: {
          input_tokens: usage.input_tokens ?? null, output_tokens: usage.output_tokens ?? null,
          total_tokens: usage.total_tokens ?? null,
        } }) } } };
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
  const source = [gatewayFailureDetails, guardGatewayResponse]
    .map(fn => fn.toString().replace(/\r\n/g, "\n")).join("\n");
  return `={{ (() => { ${source}\nreturn guardGatewayResponse($json, ${successAllowed}).${field}; })() }}`;
}
