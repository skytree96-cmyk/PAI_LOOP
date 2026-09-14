"""Non-persistent parsing review of a supplied performance-rule literal.

SOURCE_VERIFIED is the draft workflow's requested status name. Here it means
SUPPLIED_LITERAL_PARSE_ONLY: the supplied SHA is syntax-checked, but no native
bytes, complete source, quote ownership, or attachment coverage are available.
This result is not SourceVerificationProof, an engine input, or a company fact.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pai_loop.quantitative_performance import (
    PerformanceRecognitionScope,
    parse_performance_recognition_scope,
)


class _DraftModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
    )


class VerifiedRuleDraft(_DraftModel):
    """Only a claimed attachment identity and literal, never a supplied scope."""

    attachment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    literal: str = Field(max_length=2_000)
    metric_key: Literal["company.performance.amount", "company.performance.count"]


DraftStatus = Literal["SOURCE_VERIFIED", "ATTESTED_ONLY"]
DraftReason = Literal["LITERAL_PARSE_UNSUPPORTED", "MANUAL_CONDITIONS_REMAIN"]


def _classification(
    scope: PerformanceRecognitionScope | None,
) -> tuple[DraftStatus, DraftReason | None]:
    if scope is None:
        return "ATTESTED_ONLY", "LITERAL_PARSE_UNSUPPORTED"
    if scope.manual_verification_conditions:
        return "ATTESTED_ONLY", "MANUAL_CONDITIONS_REMAIN"
    return "SOURCE_VERIFIED", None


class VerifiedRuleDraftResult(_DraftModel):
    """Literal parsing only; even SOURCE_VERIFIED grants no source authority.

    ``draft.literal`` preserves the exact supplied text. ``parsed_scope`` is
    produced by the current parser and contains its normalized source_literal.
    ATTESTED_ONLY describes insufficient verification, not a verified human
    identity or proof that an attestation occurred.
    """

    draft: VerifiedRuleDraft
    parsed_scope: PerformanceRecognitionScope | None
    status: DraftStatus
    reason_code: DraftReason | None
    verification_scope: Literal["SUPPLIED_LITERAL_PARSE_ONLY"] = "SUPPLIED_LITERAL_PARSE_ONLY"
    source_content_verified: Literal[False] = False
    coverage_verified: Literal[False] = False
    engine_eligible: Literal[False] = False
    persistence_eligible: Literal[False] = False

    @field_validator(
        "source_content_verified", "coverage_verified", "engine_eligible", "persistence_eligible",
        mode="before",
    )
    @classmethod
    def require_false_flag(cls, value: object) -> bool:
        # Literal[False] alone also accepts numeric zero in Pydantic. These
        # authority flags are strictly boolean and cannot be caller-enabled.
        if value is not False:
            raise ValueError("literal parsing cannot grant source or execution authority")
        return False

    @model_validator(mode="after")
    def require_current_parser_result(self) -> "VerifiedRuleDraftResult":
        # A deserialized result is still checked against its supplied literal;
        # matching a status or injecting a plausible scope is not sufficient.
        expected = parse_performance_recognition_scope(
            self.draft.literal, metric_key=self.draft.metric_key,
        )
        if self.parsed_scope != expected or (self.status, self.reason_code) != _classification(expected):
            raise ValueError("draft result does not match current literal parsing")
        return self


def verify_rule_draft(draft: VerifiedRuleDraft | dict[str, object]) -> VerifiedRuleDraftResult:
    """Parse one bounded draft without I/O, scoring, persistence, or activation.

    An unavailable literal remains ATTESTED_ONLY with no parsed scope. No zero
    count, non-submission fact, score, or existing engine binding is generated.
    """
    checked = VerifiedRuleDraft.model_validate(draft)
    scope = parse_performance_recognition_scope(checked.literal, metric_key=checked.metric_key)
    status, reason = _classification(scope)
    return VerifiedRuleDraftResult(
        draft=checked, parsed_scope=scope, status=status, reason_code=reason,
    )
