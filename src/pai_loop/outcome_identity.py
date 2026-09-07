"""Explicit PPS opening identity shared by participation and final outcomes."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class PpsOpeningIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bid_notice_no: str = Field(min_length=1, max_length=80)
    revision_no: str = Field(min_length=1, max_length=20)
    classification_no: str = Field(min_length=1, max_length=20)
    rebid_no: str = Field(min_length=1, max_length=20)

    @field_validator("bid_notice_no")
    @classmethod
    def notice_number(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("명시적인 공고번호가 필요합니다.")
        return value

    @field_validator("revision_no", "classification_no", "rebid_no")
    @classmethod
    def ordinal(cls, value: str) -> str:
        value = value.strip()
        if not value or not value.isascii() or not value.isdigit():
            raise ValueError("차수·분류번호·재입찰번호는 명시적인 숫자여야 합니다.")
        return str(int(value))


def normalise_opening_identity(value: object) -> dict[str, str] | None:
    """Missing/invalid components remain unknown; never default them to zero."""
    if not isinstance(value, dict):
        return None
    try:
        return PpsOpeningIdentity.model_validate({
            field: value.get(field) for field in PpsOpeningIdentity.model_fields
        }).model_dump()
    except ValidationError:
        return None
