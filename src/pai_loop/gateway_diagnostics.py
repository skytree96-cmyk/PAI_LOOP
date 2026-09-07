"""Strict, non-prose gateway failure contract shared by writes and reads."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class GatewayFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal["gateway-failure-v1"]
    stage: Literal["INPUT_VALIDATION", "MODEL_EXECUTION", "OUTPUT_NORMALIZATION"]
    code: Literal["REQUEST_REJECTED", "MODEL_EXECUTION_FAILED", "OUTPUT_REJECTED"]
    upstream_http_status: int | None = Field(default=None, ge=400, le=599)

    @model_validator(mode="after")
    def consistent_stage(self) -> "GatewayFailure":
        codes = {"INPUT_VALIDATION": "REQUEST_REJECTED", "MODEL_EXECUTION": "MODEL_EXECUTION_FAILED",
                 "OUTPUT_NORMALIZATION": "OUTPUT_REJECTED"}
        if self.code != codes[self.stage] or (self.stage != "MODEL_EXECUTION" and self.upstream_http_status is not None):
            raise ValueError("invalid gateway failure facets")
        return self


def safe_gateway_failure(value: object) -> GatewayFailure | None:
    if not isinstance(value, dict):
        return None
    try:
        return GatewayFailure.model_validate(value)
    except ValidationError:
        return None
