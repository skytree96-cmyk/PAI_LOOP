from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class DepartmentRankingBreakdownOut(ApiModel):
    source: Literal[
        "USER",
        "BASELINE_STRONG",
        "BASELINE_SUPPORTING",
        "DEPARTMENT_STRONG",
        "DEPARTMENT_SUPPORTING",
        "REGION",
        "EXCLUSION",
    ]
    keyword: str
    weight: float


class DepartmentRankingOut(ApiModel):
    profile_version: str
    department_id: str
    department_name: str
    group: str
    ranking_scope: Literal["BUSINESS", "REGION"]
    score: float
    raw_score: float
    department_score: float
    priority: Literal["HIGH", "MEDIUM", "WATCH", "LOW"]
    priority_label: str
    matched_user_keywords: list[str] = Field(default_factory=list)
    matched_baseline_keywords: list[str] = Field(default_factory=list)
    matched_department_keywords: list[str] = Field(default_factory=list)
    matched_regions: list[str] = Field(default_factory=list)
    matched_exclusions: list[str] = Field(default_factory=list)
    score_breakdown: list[DepartmentRankingBreakdownOut] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class NoticeSummary(ApiModel):
    notice_key: str
    bid_notice_no: str
    revision_no: str
    title: str
    agency: str
    deadline: datetime
    status: str
    estimated_amount: float | None
    source_kind: Literal["SYNTHETIC", "PPS", "MANUAL"]
    ingestion_state: Literal["COLLECTED", "VERSIONED", "EVALUATED"]
    analysis_updated_at: datetime | None = None
    latest_evaluation: EvaluationOut | None = None
    department_ranking: DepartmentRankingOut | None = None
    top_department_rankings: list[DepartmentRankingOut] = Field(default_factory=list)


class AwardHistoryItemOut(ApiModel):
    id: str
    bid_notice_no: str
    revision_no: str
    title: str
    agency: str
    winner_name: str
    participant_count: int | None
    award_amount: float | None
    award_rate: float | None
    opened_at: datetime | None
    awarded_at: datetime | None
    similarity_score: float
    source: str


class AwardIntelligenceRecordOut(ApiModel):
    id: str
    bid_notice_no: str
    title: str
    agency: str
    winner_name: str | None
    participant_count: int | None
    award_amount: float | None
    estimated_price: float | None
    submitted_bid_price: float | None
    award_rate: float | None
    award_rate_basis: str
    submitted_bid_rate: float | None
    submitted_bid_rate_basis: str
    technical_score: float | None
    price_score: float | None
    opened_at: datetime | None
    awarded_at: datetime | None
    similarity_score: float | None
    source: str


class CompetitionRiskComponentOut(ApiModel):
    value: float | None
    unit: str
    risk_score: float | None
    weight: float
    source_status: Literal["DERIVED_FROM_STORED_FACTS", "UNAVAILABLE"]
    facts: dict[str, int | float | None]
    rationale: str


class CompetitionRiskCoverageFieldOut(ApiModel):
    available: int
    total: int
    pct: float
    minimum_available: int
    minimum_pct: float
    sufficient: bool


class CompetitionRiskCoverageOut(ApiModel):
    records_total: int
    minimum_records: int
    winner: CompetitionRiskCoverageFieldOut
    participant_count: CompetitionRiskCoverageFieldOut
    event_date: CompetitionRiskCoverageFieldOut
    similarity_score: CompetitionRiskCoverageFieldOut
    duplicate_record_keys: int
    sufficient: bool


class CompetitionRiskOut(ApiModel):
    method_version: str
    scope: Literal["STORED_3Y_SIMILARITY_CANDIDATES"]
    confidence_basis: Literal["INPUT_COMPLETENESS_AND_CANDIDATE_RELEVANCE_ONLY"]
    market_claim: Literal["NOT_DETERMINED"]
    status: Literal["MODEL_ESTIMATE", "UNKNOWN"]
    score: float | None
    band: Literal["LOW", "MODERATE", "HIGH", "VERY_HIGH", "UNKNOWN"]
    confidence: Literal["HIGH", "MEDIUM", "LOW", "INSUFFICIENT"]
    sample_count: int
    components: dict[str, CompetitionRiskComponentOut]
    coverage: CompetitionRiskCoverageOut
    method: str
    rationale: str
    warnings: list[str]
    separation_notice: str


class AwardCandidateWindowOut(ApiModel):
    from_: date = Field(alias="from")
    to: date
    years: Literal[3]
    undated_policy: Literal["KEPT_BUT_COVERAGE_GATED"]


class AwardIntelligenceOut(ApiModel):
    analytics_version: str
    boundary: Literal["STORED_HISTORY_ONLY"]
    generated_as_of: datetime
    candidate_window: AwardCandidateWindowOut
    notice_key: str
    period: dict[str, Any]
    record_count: int
    records: list[AwardIntelligenceRecordOut]
    field_coverage: dict[str, dict[str, int]]
    concentration: dict[str, Any]
    competition_risk: CompetitionRiskOut
    award_rate_distribution: dict[str, Any]
    submitted_bid_rate_distribution: dict[str, Any]
    prediction: dict[str, Any]
    target_amount_basis: dict[str, Any]
    pricing_method: dict[str, Any] | None
    warnings: list[str]


class NoticeDetail(NoticeSummary):
    id: str
    published_at: datetime | None
    category: str | None
    source_url: str | None
    risk_dimensions: dict[str, float] | None
    versions: list[NoticeVersionOut]
    requirements: list[dict[str, Any]]
    decisions: list[DecisionOut]
    document_analyses: list[dict[str, Any]] = Field(default_factory=list)
    award_history: list[AwardHistoryItemOut] = Field(default_factory=list)


class ReplayResponse(ApiModel):
    fixture_version: str
    created: int
    existing: int
    notice_keys: list[str]
    note: str


class PpsIngestionRequest(ApiModel):
    from_date: date
    to_date: date
    keyword: str | None = Field(default=None, min_length=1, max_length=100)
    page_size: int = Field(default=100, ge=1, le=999)
    max_pages: int = Field(default=5, ge=1, le=20)
    dry_run: bool = False

    @model_validator(mode="after")
    def validate_window(self) -> "PpsIngestionRequest":
        if self.to_date < self.from_date:
            raise ValueError("to_date must be on or after from_date")
        if (self.to_date - self.from_date).days > 30:
            raise ValueError("a single ingestion run is limited to 31 days")
        if self.keyword is not None:
            self.keyword = " ".join(self.keyword.split())
        return self


class PpsIngestionResponse(ApiModel):
    job_id: str
    source: Literal["PPS"] = "PPS"
    mode: Literal["live"] = "live"
    status: Literal["COMPLETED"] = "COMPLETED"
    window: dict[str, str]
    api_calls: int
    fetched: int
    matched: int
    created: int
    updated: int
    duplicates: int
    quarantined: int
    notice_keys: list[str]
    next_watermark: str
    warnings: list[str]
    dry_run: bool


class IngestionJobOut(ApiModel):
    id: str
    source: str
    mode: str
    status: str
    window_json: dict[str, str]
    keyword: str | None
    api_calls: int
    fetched: int
    matched: int
    created_count: int
    updated_count: int
    duplicate_count: int
    quarantined_count: int
    notice_keys: list[str]
    warnings: list[str]
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


class TeamsMockNotificationCreate(ApiModel):
    card: dict[str, Any] | None = None
    channel: Literal["teams"] = "teams"
    delivery_mode: Literal["mock"] = "mock"
    correlation_id: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("card")
    @classmethod
    def validate_card(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if value.get("type") != "AdaptiveCard" or not isinstance(value.get("body"), list):
            raise ValueError("card must be a Teams AdaptiveCard with a body array")
        return value


class TeamsMockNotificationOut(ApiModel):
    id: str
    notice_key: str
    channel: Literal["teams"]
    delivery_mode: Literal["mock"]
    status: str
    correlation_id: str | None
    card: dict[str, Any]
    created_at: datetime


class OpenAIExtractionRunRequest(ApiModel):
    attachment_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    source_label: str = Field(min_length=1, max_length=255)
    document_text: str = Field(min_length=20, max_length=120_000)
    document_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    force: bool = False


class OpenAIExtractionRunOut(ApiModel):
    notice_key: str
    version_id: str
    version_no: int
    file_sha256: str
    status: Literal["ACCEPTED", "REVIEW"]
    review_code: Literal["R07"] | None = None
    error_code: str | None = None
    message: str
    response_id: str | None = None
    model: str | None = None
    prompt_version: str
    schema_version: str
    data: dict[str, Any] | None = None
    reused: bool = False


class AwardHistoryRefreshRequest(ApiModel):
    keyword: str | None = Field(default=None, min_length=2, max_length=100)
    years: int = Field(default=3, ge=1, le=3)
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages_per_window: int = Field(default=1, ge=1, le=3)
    dry_run: bool = False

    @field_validator("keyword")
    @classmethod
    def normalise_keyword(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value else None


class AwardHistoryRefreshOut(ApiModel):
    job_id: str
    notice_key: str
    status: Literal["COMPLETED", "PARTIAL"]
    keyword: str
    window: dict[str, str]
    api_calls: int
    fetched: int
    created: int
    updated: int
    duplicates: int
    records: int
    dry_run: bool
    warnings: list[str]


class HealthResponse(ApiModel):
    status: str
    service: str
    version: str
    database: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
