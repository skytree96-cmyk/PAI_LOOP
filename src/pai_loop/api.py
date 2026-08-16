from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .demo import FIXTURE_VERSION, seed_synthetic_replay
from .auth import require_api_key
from .enums import Eligibility
from .evaluator import evaluate_notice
from .models import AtomicRequirement, CompanyFact, Evaluation, Evidence, Notice, NoticeVersion, UserDecision
from .schemas import (
    AtomicRequirementCreate,
    CompanyFactCreate,
    DecisionCreate,
    DecisionOut,
    EvaluateRequest,
    EvaluationOut,
    EvidenceCreate,
    NoticeCreate,
    NoticeDetail,
    NoticeSummary,
    NoticeVersionCreate,
    ReplayResponse,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def _latest_evaluation(notice: Notice) -> Evaluation | None:
    return max(notice.evaluations, key=lambda item: item.evaluated_at) if notice.evaluations else None


def _summary(notice: Notice) -> NoticeSummary:
    latest = _latest_evaluation(notice)
    return NoticeSummary(
        notice_key=notice.notice_key,
        bid_notice_no=notice.bid_notice_no,
        revision_no=notice.revision_no,
        title=notice.title,
        agency=notice.agency,
        deadline=notice.deadline,
        status=notice.status,
        estimated_amount=notice.estimated_amount,
        latest_evaluation=EvaluationOut.model_validate(latest) if latest else None,
    )


def _requirement_dict(requirement: AtomicRequirement) -> dict[str, Any]:
    return {
        "id": requirement.id,
        "requirement_key": requirement.requirement_key,
        "group_key": requirement.group_key,
        "path_key": requirement.path_key,
        "sequence": requirement.sequence,
        "label": requirement.label,
        "fact_key": requirement.fact_key,
        "operator": requirement.operator,
        "required_value": requirement.required_value,
        "evidence_required": requirement.evidence_required,
        "mandatory": requirement.mandatory,
        "pass_rule_id": requirement.pass_rule_id,
        "linked_review_code": requirement.linked_review_code,
        "parse_confidence": requirement.parse_confidence,
        "source_excerpt": requirement.source_excerpt,
        "source_location": requirement.source_location,
    }


def _detail(notice: Notice) -> NoticeDetail:
    latest_version = max(notice.versions, key=lambda item: item.version_no) if notice.versions else None
    return NoticeDetail(
        **_summary(notice).model_dump(),
        id=notice.id,
        published_at=notice.published_at,
        category=notice.category,
        source_url=notice.source_url,
        risk_dimensions=notice.risk_dimensions,
        versions=notice.versions,
        requirements=[_requirement_dict(item) for item in (latest_version.requirements if latest_version else [])],
        decisions=notice.decisions,
    )


def _load_notice(session: Session, notice_key: str) -> Notice:
    notice = session.scalar(
        select(Notice)
        .where(Notice.notice_key == notice_key)
        .options(
            selectinload(Notice.versions).selectinload(NoticeVersion.requirements),
            selectinload(Notice.evaluations),
            selectinload(Notice.decisions),
        )
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다.")
    return notice


def _make_notice_key(payload: NoticeCreate) -> str:
    safe_no = re.sub(r"[^A-Za-z0-9_-]+", "-", payload.bid_notice_no).strip("-") or "notice"
    identity = f"{payload.bid_notice_no}|{payload.revision_no}|{payload.deadline.isoformat()}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"{safe_no}-{payload.revision_no}-{suffix}"[:160]


def _comparable_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@router.get("/dashboard")
def dashboard(session: DbSession) -> dict[str, Any]:
    notices = list(
        session.scalars(
            select(Notice)
            .options(selectinload(Notice.evaluations))
            .order_by(Notice.deadline.asc())
        ).all()
    )
    decisions_total = session.scalar(select(func.count(UserDecision.id))) or 0
    evaluation_total = session.scalar(select(func.count(Evaluation.id))) or 0
    eligibility_counts = {item.value: 0 for item in Eligibility}
    readiness_counts = {item: 0 for item in ("GREEN", "YELLOW", "RED", "GRAY")}
    for notice in notices:
        latest = _latest_evaluation(notice)
        if latest:
            eligibility_counts[latest.eligibility] = eligibility_counts.get(latest.eligibility, 0) + 1
            readiness_counts[latest.readiness_status] = readiness_counts.get(latest.readiness_status, 0) + 1
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=7)
    return {
        "generated_at": now,
        "totals": {
            "notices": len(notices),
            "evaluations": evaluation_total,
            "decisions": decisions_total,
        },
        "eligibility_counts": eligibility_counts,
        "readiness_counts": readiness_counts,
        "pending_review": eligibility_counts[Eligibility.REVIEW.value],
        "deadline_soon": sum(1 for item in notices if now <= _comparable_utc(item.deadline) <= soon),
        "recent_notices": [_summary(item).model_dump() for item in notices[:10]],
        "synthetic_data_warning": "SYN- 접두 데이터는 데모용이며 실제 성과 지표가 아닙니다.",
    }


@router.get("/notices", response_model=list[NoticeSummary])
def list_notices(
    session: DbSession,
    q: str | None = None,
    eligibility: Eligibility | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NoticeSummary]:
    statement = select(Notice).options(selectinload(Notice.evaluations)).order_by(Notice.deadline.asc())
    if q:
        pattern = f"%{q}%"
        statement = statement.where(or_(Notice.title.ilike(pattern), Notice.agency.ilike(pattern)))
    notices = list(session.scalars(statement.offset(offset).limit(limit)).all())
    summaries = [_summary(notice) for notice in notices]
    if eligibility:
        summaries = [
            item for item in summaries
            if item.latest_evaluation and item.latest_evaluation.eligibility == eligibility
        ]
    return summaries


@router.post("/notices", response_model=NoticeDetail, status_code=status.HTTP_201_CREATED)
def create_notice(payload: NoticeCreate, session: DbSession) -> NoticeDetail:
    notice = Notice(**payload.model_dump(exclude={"notice_key"}), notice_key=payload.notice_key or _make_notice_key(payload))
    session.add(notice)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="동일 notice_key의 공고가 이미 있습니다.") from exc
    return _detail(_load_notice(session, notice.notice_key))


@router.get("/notices/{notice_key}", response_model=NoticeDetail)
def get_notice(notice_key: str, session: DbSession) -> NoticeDetail:
    return _detail(_load_notice(session, notice_key))


@router.post("/notices/{notice_key}/versions", status_code=status.HTTP_201_CREATED)
def create_version(notice_key: str, payload: NoticeVersionCreate, session: DbSession) -> dict[str, Any]:
    notice = _load_notice(session, notice_key)
    version = NoticeVersion(notice_id=notice.id, **payload.model_dump())
    session.add(version)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="동일 버전 번호가 이미 있습니다.") from exc
    session.refresh(version)
    return {"id": version.id, "notice_key": notice_key, "version_no": version.version_no}


@router.post("/notice-versions/{version_id}/requirements", status_code=status.HTTP_201_CREATED)
def create_requirement(version_id: str, payload: AtomicRequirementCreate, session: DbSession) -> dict[str, Any]:
    version = session.get(NoticeVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="공고 버전을 찾을 수 없습니다.")
    requirement = AtomicRequirement(notice_version_id=version.id, **payload.model_dump())
    session.add(requirement)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="동일 requirement_key가 이미 있습니다.") from exc
    return _requirement_dict(requirement)


@router.post("/evidence", status_code=status.HTTP_201_CREATED)
def create_evidence(payload: EvidenceCreate, session: DbSession) -> dict[str, Any]:
    evidence = Evidence(**payload.model_dump())
    session.add(evidence)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="동일 evidence_key가 이미 있습니다.") from exc
    return {"id": evidence.id, "evidence_key": evidence.evidence_key, "status": evidence.status}


@router.post("/company-facts", status_code=status.HTTP_201_CREATED)
def create_company_fact(payload: CompanyFactCreate, session: DbSession) -> dict[str, Any]:
    values = payload.model_dump(exclude={"evidence_key"})
    evidence_id = None
    if payload.evidence_key:
        evidence = session.scalar(select(Evidence).where(Evidence.evidence_key == payload.evidence_key))
        if evidence is None:
            raise HTTPException(status_code=422, detail="evidence_key를 찾을 수 없습니다.")
        evidence_id = evidence.id
    fact = CompanyFact(**values, evidence_id=evidence_id)
    session.add(fact)
    session.commit()
    return {"id": fact.id, "fact_key": fact.fact_key, "effective_from": fact.effective_from}


@router.post("/notices/{notice_key}/evaluate", response_model=EvaluationOut, status_code=status.HTTP_201_CREATED)
def run_evaluation(notice_key: str, payload: EvaluateRequest, session: DbSession) -> Evaluation:
    notice = _load_notice(session, notice_key)
    versions = notice.versions
    if payload.version_no is not None:
        versions = [item for item in versions if item.version_no == payload.version_no]
    if not versions:
        raise HTTPException(status_code=422, detail="평가할 공고 버전이 없습니다.")
    version = max(versions, key=lambda item: item.version_no)
    facts = list(
        session.scalars(select(CompanyFact).options(selectinload(CompanyFact.evidence))).all()
    )
    result = evaluate_notice(notice, version, version.requirements, facts)
    evaluation = Evaluation(
        notice_id=notice.id,
        notice_version_id=version.id,
        deadline_snapshot_at=notice.deadline,
        eligibility=result.eligibility.value,
        reason_code=result.reason_code,
        readiness_score=result.readiness_score,
        readiness_status=result.readiness_status.value,
        evidence_coverage=result.evidence_coverage,
        risk_score=result.risk_score,
        risk_band=result.risk_band.value,
        ruleset_version=payload.ruleset_version,
        atomic_results=result.atomic_results,
        explanation=result.explanation,
    )
    session.add(evaluation)
    session.commit()
    session.refresh(evaluation)
    return evaluation


@router.get("/notices/{notice_key}/decisions", response_model=list[DecisionOut])
def list_decisions(notice_key: str, session: DbSession) -> list[UserDecision]:
    notice = _load_notice(session, notice_key)
    return notice.decisions


@router.post(
    "/notices/{notice_key}/decisions",
    response_model=DecisionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_decision(notice_key: str, payload: DecisionCreate, session: DbSession) -> UserDecision:
    notice = _load_notice(session, notice_key)
    evaluation = None
    if payload.evaluation_id:
        evaluation = session.get(Evaluation, payload.evaluation_id)
        if evaluation is None or evaluation.notice_id != notice.id:
            raise HTTPException(status_code=422, detail="이 공고의 evaluation_id가 아닙니다.")
    else:
        evaluation = _latest_evaluation(notice)
    if evaluation is None:
        raise HTTPException(status_code=422, detail="먼저 공고 평가를 실행해야 합니다.")
    decision = UserDecision(
        notice_id=notice.id,
        evaluation_id=evaluation.id,
        **payload.model_dump(exclude={"evaluation_id"}),
    )
    session.add(decision)
    session.commit()
    session.refresh(decision)
    return decision


@router.post("/ingestion/replay", response_model=ReplayResponse)
def replay_synthetic(session: DbSession) -> ReplayResponse:
    created, existing, keys = seed_synthetic_replay(session)
    return ReplayResponse(
        fixture_version=FIXTURE_VERSION,
        created=created,
        existing=existing,
        notice_keys=keys,
        note="개인정보·회사정보·실제 공고 원문을 포함하지 않은 합성 회귀 데이터입니다.",
    )
