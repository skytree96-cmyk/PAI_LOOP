// Self-contained functions: the generator embeds these in n8n Code nodes and
// terminal expressions. No exception prose or request data crosses this boundary.
export function gatewayFailureDetails() {
  return {
    INPUT_VALIDATION: [
      "INPUT_ITEM_COUNT_INVALID", "INPUT_BODY_INVALID", "INPUT_BUDGET_POLICY_INVALID",
      "INPUT_PROBE_SCOPE_INVALID", "INPUT_FIELDS_INVALID", "INPUT_MODEL_INVALID",
      "INPUT_OPTIONS_INVALID", "INPUT_TOKEN_LIMIT_INVALID", "INPUT_MESSAGES_INVALID",
      "INPUT_MESSAGE_SHAPE_INVALID", "INPUT_CONTENT_TYPE_INVALID", "INPUT_CONTENT_EMPTY",
      "INPUT_CONTENT_NUL", "INPUT_SYSTEM_TOO_LARGE", "INPUT_SOURCE_TOO_LARGE",
      "INPUT_SYSTEM_IDENTITY_INVALID", "INPUT_CORRECTIVE_POLICY_INVALID",
      "INPUT_USER_IDENTITY_INVALID", "INPUT_FORMAT_INVALID", "INPUT_SCHEMA_INVALID",
      "INPUT_SCHEMA_TOO_LARGE", "INPUT_SCHEMA_PROJECTION_REJECTED",
      "INPUT_PROVIDER_SYSTEM_TOO_LARGE", "INPUT_REQUEST_TOO_LARGE", "INPUT_VALIDATOR_FAILED",
    ],
    MODEL_EXECUTION: [
      "MODEL_HTTP_ERROR", "MODEL_TRANSPORT_TIMEOUT", "MODEL_CONNECTION_RESET",
      "MODEL_CONNECTION_REFUSED", "MODEL_DNS_FAILURE", "MODEL_TRANSPORT_UNKNOWN",
      "MODEL_RESPONSE_INVALID",
    ],
    OUTPUT_NORMALIZATION: [
      "OUTPUT_EMPTY", "OUTPUT_TYPE_INVALID", "OUTPUT_TOO_LARGE", "OUTPUT_FENCE_INVALID",
      "OUTPUT_JSON_INVALID", "OUTPUT_NOT_OBJECT", "EXECUTION_CONTEXT_INVALID",
      "NORMALIZER_EXCEPTION", "TERMINAL_GUARD_REJECTED", "NATIVE_RESPONSE_INVALID",
      "NATIVE_CONTENT_INVALID", "NATIVE_SCHEMA_DECODE_INVALID", "NATIVE_STOP_MAX_TOKENS",
      "NATIVE_STOP_REFUSAL", "NATIVE_STOP_UNSUPPORTED",
    ],
  };
}

export function sanitizeGatewayInputFailure(json) {
  const error = json?.error;
  // n8n 2.33.7 Code/ExecutionError adds only these line/item suffixes. Accept
  // that complete shape or the exact validator code, never arbitrary prose.
  const message = typeof error === "string" ? error : error?.message;
  const allowed = gatewayFailureDetails().INPUT_VALIDATION;
  const match = typeof message === "string" && message.length <= 200
    ? /^(INPUT_[A-Z_]+)(?: \[line \d+(?:, for item \d+)?\])?$/.exec(message) : null;
  const detail = match && match[0] === message && allowed.includes(match[1])
    ? match[1] : "INPUT_VALIDATOR_FAILED";
  return [{ json: { gateway_error: { version: "gateway-failure-v1",
    stage: "INPUT_VALIDATION", code: "REQUEST_REJECTED", upstream_http_status: null,
    detail_code: detail } } }];
}

export function sanitizeGatewayModelFailure(json) {
  const error = json?.error;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  let upstream = null, detail = "MODEL_TRANSPORT_UNKNOWN";
  if (object(error)) {
    const candidates = [error.status, error.statusCode, error.httpCode].filter(value => value != null);
    const validStatus = value => Number.isSafeInteger(value) && value >= 400 && value <= 599;
    if (candidates.length && candidates.every(value => validStatus(value) && value === candidates[0])) {
      upstream = candidates[0];
      detail = "MODEL_HTTP_ERROR";
    } else if (!candidates.length) {
      const codes = [error.code, object(error.cause) ? error.cause.code : undefined].filter(value => value != null);
      const transport = { ETIMEDOUT: "MODEL_TRANSPORT_TIMEOUT", ESOCKETTIMEDOUT: "MODEL_TRANSPORT_TIMEOUT",
        ECONNRESET: "MODEL_CONNECTION_RESET", ECONNREFUSED: "MODEL_CONNECTION_REFUSED",
        ENOTFOUND: "MODEL_DNS_FAILURE", EAI_AGAIN: "MODEL_DNS_FAILURE" };
      if (codes.length && codes.every(code => typeof code === "string" && code === codes[0])
        && Object.hasOwn(transport, codes[0])) detail = transport[codes[0]];
    }
  }
  return [{ json: { gateway_error: { version: "gateway-failure-v1",
    stage: "MODEL_EXECUTION", code: "MODEL_EXECUTION_FAILED", upstream_http_status: upstream,
    detail_code: detail } } }];
}
