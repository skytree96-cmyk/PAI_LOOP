from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import AtomicOperator, DecisionChoice, Eligibility, EvidenceStatus, ReadinessStatus, RiskBand


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class NoticeCreate(ApiModel):
    notice_key: str | None = Field(default=None, max_length=160)
    bid_notice_no: str = Field(min_length=1, max_length=80)
    revision_no: str = Field(default="00", max_length=20)
    title: str = Field(min_length=1, max_length=500)
    agency: str = Field(default="", max_length=255)
    published_at: datetime | None = None
    deadline: datetime
    status: str = Field(default="OPEN", max_length=32)
    category: str | None = Field(default=None, max_length=120)
    estimated_amount: float | None = Field(default=None, ge=0)
    source_url: str | None = None
    risk_dimensions: dict[str, float] | None = None

    @field_validator("risk_dimensions")
    @classmethod
    def validate_risk(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is not None and any(score < 0 or score > 100 for score in value.values()):
            raise ValueError("risk dimension values must be between 0 and 100")
        return value


class NoticeVersionCreate(ApiModel):
    version_no: int = Field(ge=1)
    file_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    document_complete: bool = True
    extraction_status: str = "COMPLETE"
    extraction_confidence: float = Field(default=1.0, ge=0, le=1)
    source_payload: dict[str, Any] | None = None


class EvidenceCreate(ApiModel):
    evidence_key: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=255)
    evidence_type: str = Field(min_length=1, max_length=80)
    status: EvidenceStatus = EvidenceStatus.VERIFIED
    issued_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    source_location: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    metadata_json: dict[str, Any] | None = None


class CompanyFactCreate(ApiModel):
    fact_key: str = Field(min_length=1, max_length=120)
    value: Any
    value_label: str | None = Field(default=None, max_length=500)
    effective_from: datetime
    effective_to: datetime | None = None
    evidence_key: str | None = None
    verified: bool = False
    source: str = Field(default="MANUAL", max_length=80)


class AtomicRequirementCreate(ApiModel):
    requirement_key: str = Field(min_length=1, max_length=120)
    group_key: str = Field(min_length=1, max_length=120)
    path_key: str = Field(default="default", min_length=1, max_length=120)
    sequence: int = Field(default=0, ge=0)
    label: str = Field(min_length=1, max_length=500)
    fact_key: str = Field(min_length=1, max_length=120)
    operator: AtomicOperator = AtomicOperator.EQ
    required_value: Any
    evidence_required: bool = True
    mandatory: bool = True
    pass_rule_id: str = Field(min_length=1, max_length=80)
    linked_review_code: str | None = Field(default=None, pattern=r"^R(0[1-7]|09)$")
    review_trigger_value: Any | None = None
    parse_confidence: float = Field(default=1.0, ge=0, le=1)
    source_excerpt: str | None = None
    source_location: str | None = Field(default=None, max_length=255)
    active: bool = True


class EvaluateRequest(ApiModel):
    version_no: int | None = Field(default=None, ge=1)
    ruleset_version: str = Field(default="2026.08-v1", max_length=32)


class DecisionCreate(ApiModel):
    evaluation_id: str | None = None
    choice: DecisionChoice
    actor_label: str = Field(default="담당자", min_length=1, max_length=120)
    rationale: str = Field(min_length=1, max_length=4000)
    conditions: list[str] | None = None


class NoticeVersionOut(ApiModel):
    id: str
    version_no: int
    file_sha256: str
    document_complete: bool
    extraction_status: str
    extraction_confidence: float
    created_at: datetime


class EvaluationOut(ApiModel):
    id: str
    evaluated_at: datetime
    deadline_snapshot_at: datetime
    eligibility: Eligibility
    reason_code: str
    readiness_score: float
    readiness_status: ReadinessStatus
    evidence_coverage: float
    risk_score: float | None
    risk_band: RiskBand
    ruleset_version: str
    atomic_results: list[dict[str, Any]]
    explanation: dict[str, Any]


class DecisionOut(ApiModel):
    id: str
    evaluation_id: str
    choice: DecisionChoice
    actor_label: str
    rationale: str
    conditions: list[str] | None
    created_at: datetime


class NoticeSummary(ApiModel):
    notice_key: str
    bid_notice_no: str
    revision_no: str
    title: str
    agency: str
    deadline: datetime
    status: str
    estimated_amount: float | None
    latest_evaluation: EvaluationOut | None = None


class NoticeDetail(NoticeSummary):
    id: str
    published_at: datetime | None
    category: str | None
    source_url: str | None
    risk_dimensions: dict[str, float] | None
    versions: list[NoticeVersionOut]
    requirements: list[dict[str, Any]]
    decisions: list[DecisionOut]


class ReplayResponse(ApiModel):
    fixture_version: str
    created: int
    existing: int
    notice_keys: list[str]
    note: str


class HealthResponse(ApiModel):
    status: str
    service: str
    version: str
    database: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

