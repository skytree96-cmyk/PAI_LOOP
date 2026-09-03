from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .auth import require_private_evidence_access
from .models import CompanyFact, Evidence, Notice
from .quantitative_formula import parse_credit_rating
from .quantitative_scoring import (
    _current_dynamic_quantitative_profile,
    quantitative_company_fact_payload_sha256,
    quantitative_request_from_candidate_profile,
)


_KST = timezone(timedelta(hours=9))
_FACT_KEY = "company.credit_rating"
_FACT_SOURCE = "PRIVATE_DOCUMENT"
_EVIDENCE_TYPE = "QUANTITATIVE_FACT"
_BINDING_SCHEMA = "pai-loop-private-credit-binding-1.0.0"


class PrivateCreditRatingRegistration(BaseModel):
    """Metadata-only registration; the certificate body never crosses this API."""

    model_config = ConfigDict(extra="forbid")

    rating: str = Field(min_length=1, max_length=4)
    document_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    evidence_reference: str = Field(min_length=1, max_length=500)
    issued_on: date
    effective_on: date
    valid_until: date
    verification_attestation: Literal["HUMAN_REVIEWED_COMPLETE_DOCUMENT"]

    @field_validator("rating")
    @classmethod
    def normalize_rating(cls, value: str) -> str:
        normalized = parse_credit_rating(value)
        if normalized is None:
            raise ValueError("지원하는 기업신용평가등급이 아닙니다.")
        return normalized

    @field_validator("document_sha256")
    @classmethod
    def normalize_document_sha256(cls, value: str) -> str:
        return value.casefold()

    @field_validator("evidence_reference")
    @classmethod
    def validate_private_reference(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if (
            parsed.scheme.casefold() != "private-evidence"
            or not parsed.netloc
            or not parsed.path.strip("/")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,79}", parsed.netloc)
            or not re.fullmatch(
                r"/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]+",
                parsed.path,
            )
        ):
            raise ValueError(
                "evidence_reference는 식별정보가 없는 private-evidence:// 참조여야 합니다."
            )
        return normalized

    @model_validator(mode="after")
    def validate_dates(self) -> "PrivateCreditRatingRegistration":
        if self.effective_on > self.valid_until:
            raise ValueError("유효 시작일은 만료일 이후일 수 없습니다.")
        if self.issued_on > self.valid_until:
            raise ValueError("발급일은 만료일 이후일 수 없습니다.")
        return self


class PrivateCreditRatingRegistrationResult(BaseModel):
    """Deliberately omits row IDs, evidence references and all digests."""

    model_config = ConfigDict(extra="forbid")

    notice_key: str
    fact_key: Literal["company.credit_rating"] = _FACT_KEY
    rating: str
    binding_status: Literal["CREATED", "UNCHANGED"]
    valid_at_deadline: Literal[True] = True


def _get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(_get_session)]

def _require_operator_evidence_access(request: Request) -> None:
    require_private_evidence_access(request)


router = APIRouter(
    prefix="/api/v1/operator-evidence",
    dependencies=[Depends(_require_operator_evidence_access)],
    tags=["operator evidence"],
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _start_of_korean_date(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=_KST).astimezone(timezone.utc)


def _end_of_korean_date(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=_KST).astimezone(timezone.utc)


def _korean_deadline_date(value: datetime) -> date:
    return _as_utc(value).astimezone(_KST).date()


def _credit_rating_binding_for_notice(notice: Notice) -> str:
    profile = _current_dynamic_quantitative_profile(notice)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="현재 첨부 manifest에 결합된 정량평가표가 없습니다.",
        )
    try:
        request = quantitative_request_from_candidate_profile(profile)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="현재 정량평가표를 안전한 회사 사실 binding으로 변환할 수 없습니다.",
        ) from exc
    criteria = [
        criterion
        for criterion in request.criteria
        if criterion.metric_key == _FACT_KEY
    ]
    bindings = {
        str(criterion.fact_binding_sha256 or "").casefold()
        for criterion in criteria
        if re.fullmatch(
            r"[a-f0-9]{64}",
            str(criterion.fact_binding_sha256 or "").casefold(),
        )
    }
    if len(criteria) != 1 or len(bindings) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="신용평가 항목의 공고별 조건 binding을 하나로 확정할 수 없습니다.",
        )
    return next(iter(bindings))


def _registration_key(document_sha256: str, fact_binding_sha256: str) -> str:
    private_identity = hashlib.sha256(
        f"{document_sha256}:{fact_binding_sha256}".encode("ascii")
    ).hexdigest()
    return f"PRIVATE-CREDIT-{private_identity}"


def _instant_equal(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return _as_utc(left) == _as_utc(right)


def _expected_rows(
    payload: PrivateCreditRatingRegistration,
    *,
    fact_binding_sha256: str,
) -> tuple[Evidence, CompanyFact]:
    effective_from = _start_of_korean_date(payload.effective_on)
    effective_to = _end_of_korean_date(payload.valid_until)
    evidence = Evidence(
        evidence_key=_registration_key(
            payload.document_sha256,
            fact_binding_sha256,
        ),
        name="비공개 입찰용 신용평가등급 확인서",
        evidence_type=_EVIDENCE_TYPE,
        status="VERIFIED",
        issued_at=_start_of_korean_date(payload.issued_on),
        valid_from=effective_from,
        valid_until=effective_to,
        source_location=payload.evidence_reference,
        sha256=payload.document_sha256,
        metadata_json=None,
    )
    fact = CompanyFact(
        fact_key=_FACT_KEY,
        value={
            "value": payload.rating,
            "unit": "등급",
            "fact_binding_sha256": fact_binding_sha256,
        },
        value_label=f"기업신용평가등급 {payload.rating}",
        effective_from=effective_from,
        effective_to=effective_to,
        evidence=evidence,
        verified=True,
        source=_FACT_SOURCE,
    )
    evidence.metadata_json = {
        "binding_schema": _BINDING_SCHEMA,
        "classification": "PRIVATE_EVIDENCE",
        "verification_assurance": "DOCUMENT_REVIEWED_NOT_ISSUER_AUTHENTICATED",
        "quantitative_fact_key": _FACT_KEY,
        "fact_binding_sha256": fact_binding_sha256,
        "company_fact_payload_sha256": quantitative_company_fact_payload_sha256(
            fact
        ),
    }
    return evidence, fact


def _existing_registration_matches(
    evidence: Evidence,
    fact: CompanyFact | None,
    *,
    expected_evidence: Evidence,
    expected_fact: CompanyFact,
) -> bool:
    return bool(
        fact is not None
        and evidence.evidence_key == expected_evidence.evidence_key
        and evidence.name == expected_evidence.name
        and evidence.evidence_type == expected_evidence.evidence_type
        and evidence.status == expected_evidence.status
        and _instant_equal(evidence.issued_at, expected_evidence.issued_at)
        and _instant_equal(evidence.valid_from, expected_evidence.valid_from)
        and _instant_equal(evidence.valid_until, expected_evidence.valid_until)
        and evidence.source_location == expected_evidence.source_location
        and str(evidence.sha256 or "").casefold() == expected_evidence.sha256
        and evidence.metadata_json == expected_evidence.metadata_json
        and fact.fact_key == expected_fact.fact_key
        and fact.value == expected_fact.value
        and fact.value_label == expected_fact.value_label
        and _instant_equal(fact.effective_from, expected_fact.effective_from)
        and _instant_equal(fact.effective_to, expected_fact.effective_to)
        and fact.verified is True
        and fact.source == expected_fact.source
        and fact.evidence_id == evidence.id
        and quantitative_company_fact_payload_sha256(fact)
        == quantitative_company_fact_payload_sha256(expected_fact)
    )


def _load_existing_fact(session: Session, evidence: Evidence) -> CompanyFact | None:
    facts = list(
        session.scalars(
            select(CompanyFact).where(CompanyFact.evidence_id == evidence.id)
        ).all()
    )
    if len(facts) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="기존 비공개 증빙 binding이 중복되어 운영 검토가 필요합니다.",
        )
    return facts[0] if facts else None


def register_private_credit_rating(
    session: Session,
    *,
    payload: PrivateCreditRatingRegistration,
    fact_binding_sha256: str,
) -> Literal["CREATED", "UNCHANGED"]:
    expected_evidence, expected_fact = _expected_rows(
        payload,
        fact_binding_sha256=fact_binding_sha256,
    )
    existing = session.scalar(
        select(Evidence)
        .where(Evidence.evidence_key == expected_evidence.evidence_key)
        .with_for_update()
    )
    if existing is not None:
        if not _existing_registration_matches(
            existing,
            _load_existing_fact(session, existing),
            expected_evidence=expected_evidence,
            expected_fact=expected_fact,
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="같은 문서와 평가항목에 다른 등록 내용이 이미 존재합니다.",
            )
        return "UNCHANGED"

    session.add(expected_fact)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(Evidence).where(
                Evidence.evidence_key == expected_evidence.evidence_key
            )
        )
        if existing is None or not _existing_registration_matches(
            existing,
            _load_existing_fact(session, existing),
            expected_evidence=expected_evidence,
            expected_fact=expected_fact,
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="비공개 증빙을 동시에 등록하여 다시 확인해야 합니다.",
            )
        return "UNCHANGED"
    return "CREATED"


@router.post(
    "/notices/{notice_key}/credit-rating",
    response_model=PrivateCreditRatingRegistrationResult,
)
def register_private_credit_rating_for_notice(
    notice_key: str,
    payload: PrivateCreditRatingRegistration,
    request: Request,
    response: Response,
    session: DbSession,
) -> PrivateCreditRatingRegistrationResult:
    response.headers["Cache-Control"] = "no-store"

    notice = session.scalar(
        select(Notice)
        .where(Notice.notice_key == notice_key)
        .options(selectinload(Notice.versions))
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")

    deadline_date = _korean_deadline_date(notice.deadline)
    if not (
        payload.issued_on <= deadline_date
        and payload.effective_on <= deadline_date <= payload.valid_until
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="신용평가 증빙이 공고 마감일 기준 유효하지 않습니다.",
        )
    binding = _credit_rating_binding_for_notice(notice)
    binding_status = register_private_credit_rating(
        session,
        payload=payload,
        fact_binding_sha256=binding,
    )
    return PrivateCreditRatingRegistrationResult(
        notice_key=notice.notice_key,
        rating=payload.rating,
        binding_status=binding_status,
    )
