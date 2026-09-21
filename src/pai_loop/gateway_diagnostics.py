"""Strict, non-prose gateway failure contract shared by writes and reads."""
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer, model_validator


GatewayOutputDetail = Literal[
    "OUTPUT_EMPTY", "OUTPUT_TYPE_INVALID", "OUTPUT_TOO_LARGE", "OUTPUT_FENCE_INVALID",
    "OUTPUT_JSON_INVALID", "OUTPUT_NOT_OBJECT", "EXECUTION_CONTEXT_INVALID",
    "NORMALIZER_EXCEPTION", "TERMINAL_GUARD_REJECTED",
    "NATIVE_RESPONSE_INVALID", "NATIVE_CONTENT_INVALID", "NATIVE_SCHEMA_DECODE_INVALID",
    "NATIVE_STOP_MAX_TOKENS", "NATIVE_STOP_REFUSAL", "NATIVE_STOP_UNSUPPORTED",
]
GatewayInputDetail = Literal[
    "INPUT_ITEM_COUNT_INVALID", "INPUT_BODY_INVALID", "INPUT_BUDGET_POLICY_INVALID",
    "INPUT_PROBE_SCOPE_INVALID", "INPUT_FIELDS_INVALID", "INPUT_MODEL_INVALID",
    "INPUT_OPTIONS_INVALID", "INPUT_TOKEN_LIMIT_INVALID", "INPUT_MESSAGES_INVALID",
    "INPUT_MESSAGE_SHAPE_INVALID", "INPUT_CONTENT_TYPE_INVALID", "INPUT_CONTENT_EMPTY",
    "INPUT_CONTENT_NUL", "INPUT_SYSTEM_TOO_LARGE", "INPUT_SOURCE_TOO_LARGE",
    "INPUT_SYSTEM_IDENTITY_INVALID", "INPUT_CORRECTIVE_POLICY_INVALID",
    "INPUT_USER_IDENTITY_INVALID", "INPUT_FORMAT_INVALID", "INPUT_SCHEMA_INVALID",
    "INPUT_SCHEMA_TOO_LARGE", "INPUT_SCHEMA_PROJECTION_REJECTED",
    "INPUT_PROVIDER_SYSTEM_TOO_LARGE", "INPUT_REQUEST_TOO_LARGE", "INPUT_VALIDATOR_FAILED",
]
GatewayModelDetail = Literal[
    "MODEL_HTTP_ERROR", "MODEL_TRANSPORT_TIMEOUT", "MODEL_CONNECTION_RESET",
    "MODEL_CONNECTION_REFUSED", "MODEL_DNS_FAILURE", "MODEL_TRANSPORT_UNKNOWN",
    "MODEL_RESPONSE_INVALID",
]
_DETAILS_BY_STAGE = {
    "INPUT_VALIDATION": frozenset(get_args(GatewayInputDetail)),
    "MODEL_EXECUTION": frozenset(get_args(GatewayModelDetail)),
    "OUTPUT_NORMALIZATION": frozenset(get_args(GatewayOutputDetail)),
}


class GatewayUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input_tokens: int | None = Field(ge=0, le=10_000_000)
    output_tokens: int | None = Field(ge=0, le=10_000_000)
    total_tokens: int | None = Field(ge=0, le=10_000_000)

    @model_validator(mode="after")
    def consistent_total(self):
        if all(value is not None for value in (self.input_tokens, self.output_tokens, self.total_tokens)):
            if self.total_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("invalid gateway usage total")
        return self


class GatewayFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal["gateway-failure-v1"]
    stage: Literal["INPUT_VALIDATION", "MODEL_EXECUTION", "OUTPUT_NORMALIZATION"]
    code: Literal["REQUEST_REJECTED", "MODEL_EXECUTION_FAILED", "OUTPUT_REJECTED"]
    upstream_http_status: int | None = Field(default=None, ge=400, le=599)
    detail_code: GatewayOutputDetail | GatewayInputDetail | GatewayModelDetail | None = None
    stop_reason: Literal["end_turn", "max_tokens", "refusal", "stop_sequence", "tool_use",
                         "pause_turn", "model_context_window_exceeded"] | None = None
    usage: GatewayUsage | None = None

    @model_validator(mode="after")
    def consistent_stage(self) -> "GatewayFailure":
        codes = {"INPUT_VALIDATION": "REQUEST_REJECTED", "MODEL_EXECUTION": "MODEL_EXECUTION_FAILED",
                 "OUTPUT_NORMALIZATION": "OUTPUT_REJECTED"}
        if self.code != codes[self.stage] or (self.stage != "MODEL_EXECUTION" and self.upstream_http_status is not None):
            raise ValueError("invalid gateway failure facets")
        if self.detail_code is not None and self.detail_code not in _DETAILS_BY_STAGE[self.stage]:
            raise ValueError("invalid gateway detail stage")
        if self.stage == "MODEL_EXECUTION" and self.detail_code is not None:
            if (self.detail_code == "MODEL_HTTP_ERROR") != (self.upstream_http_status is not None):
                raise ValueError("invalid gateway model detail status")
        if self.stop_reason is not None or self.usage is not None:
            if self.stage != "OUTPUT_NORMALIZATION" or self.detail_code is None or self.stop_reason is None or self.usage is None:
                raise ValueError("invalid native gateway diagnostics")
            required = {"max_tokens": "NATIVE_STOP_MAX_TOKENS", "refusal": "NATIVE_STOP_REFUSAL"}.get(self.stop_reason)
            if self.stop_reason not in {"end_turn", "max_tokens", "refusal"}:
                required = "NATIVE_STOP_UNSUPPORTED"
            if ((required is not None and self.detail_code != required)
                    or (required is None and (self.detail_code or "").startswith("NATIVE_STOP_"))):
                raise ValueError("invalid native stop detail")
        elif (self.detail_code or "").startswith("NATIVE_STOP_"):
            raise ValueError("native stop reason missing")
        return self

    @model_serializer(mode="wrap")
    def optional_detail(self, handler):
        result = handler(self)
        for field in ("detail_code", "stop_reason", "usage"):
            if getattr(self, field) is None:
                result.pop(field, None)
        return result


def safe_gateway_failure(value: object) -> GatewayFailure | None:
    if not isinstance(value, dict):
        return None
    try:
        return GatewayFailure.model_validate(value)
    except ValidationError:
        return None
