"""Strict, non-prose gateway failure contract shared by writes and reads."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer, model_validator


GatewayOutputDetail = Literal[
    "OUTPUT_EMPTY", "OUTPUT_TYPE_INVALID", "OUTPUT_TOO_LARGE", "OUTPUT_FENCE_INVALID",
    "OUTPUT_JSON_INVALID", "OUTPUT_NOT_OBJECT", "EXECUTION_CONTEXT_INVALID",
    "NORMALIZER_EXCEPTION", "TERMINAL_GUARD_REJECTED",
]


class GatewayFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal["gateway-failure-v1"]
    stage: Literal["INPUT_VALIDATION", "MODEL_EXECUTION", "OUTPUT_NORMALIZATION"]
    code: Literal["REQUEST_REJECTED", "MODEL_EXECUTION_FAILED", "OUTPUT_REJECTED"]
    upstream_http_status: int | None = Field(default=None, ge=400, le=599)
    detail_code: GatewayOutputDetail | None = None

    @model_validator(mode="after")
    def consistent_stage(self) -> "GatewayFailure":
        codes = {"INPUT_VALIDATION": "REQUEST_REJECTED", "MODEL_EXECUTION": "MODEL_EXECUTION_FAILED",
                 "OUTPUT_NORMALIZATION": "OUTPUT_REJECTED"}
        if self.code != codes[self.stage] or (self.stage != "MODEL_EXECUTION" and self.upstream_http_status is not None):
            raise ValueError("invalid gateway failure facets")
        if self.detail_code is not None and self.stage != "OUTPUT_NORMALIZATION":
            raise ValueError("invalid gateway detail stage")
        return self

    @model_serializer(mode="wrap")
    def optional_detail(self, handler):
        result = handler(self)
        if self.detail_code is None:
            result.pop("detail_code", None)
        return result


def safe_gateway_failure(value: object) -> GatewayFailure | None:
    if not isinstance(value, dict):
        return None
    try:
        return GatewayFailure.model_validate(value)
    except ValidationError:
        return None
