from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .demo import FIXTURE_VERSION, seed_synthetic_replay
from .auth import public_read_allowed, require_api_key
from .enums import Eligibility
from .evaluator import evaluate_notice
from .eligibility_policy import classify_requirements, load_public_company_profile
from .department_ranking import (
    get_department_profile,
    load_department_keyword_profiles,
    notice_matches_user_keywords,
    parse_search_keywords,
    rank_notice_across_departments,
    rank_notice_for_department,
)
from .integrations.awards import PpsAwardClient
from .award_intelligence import build_award_intelligence
from .integrations.pps import PpsApiError, PpsClient, redact_url
from .integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    OpenAIExtractionClient,
)
from .models import (
    AtomicRequirement,
    AwardHistoryItem,
    CompanyFact,
    Evaluation,
    Evidence,
    IngestionJob,
    MockNotification,
    Notice,
    NoticeVersion,
    UserDecision,
)
from .pricing_profiles import pricing_profile_for_document
from .public_notice_seed import load_public_notice_seed
from .schemas import (
    AtomicRequirementCreate,
    AwardHistoryItemOut,
    AwardIntelligenceOut,
    AwardHistoryRefreshOut,
    AwardHistoryRefreshRequest,
    CompanyFactCreate,
    DecisionCreate,
    DecisionOut,
    EvaluateRequest,
    EvaluationOut,
    EvidenceCreate,
    IngestionJobOut,
    NoticeCreate,
    NoticeDetail,
    NoticeSummary,
    NoticeVersionCreate,
    OpenAIExtractionRunOut,
    OpenAIExtractionRunRequest,
    PpsIngestionRequest,
    PpsIngestionResponse,
    ReplayResponse,
    TeamsMockNotificationCreate,
    TeamsMockNotificationOut,
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


def _summary(notice: Notice, *, public_view: bool = False) -> NoticeSummary:
    latest = _latest_evaluation(notice)
    latest_version = max(notice.versions, key=lambda item: item.version_no) if notice.versions else None
    source_kind = _source_kind(notice)
    ingestion_state = "EVALUATED" if latest else "VERSIONED" if latest_version else "COLLECTED"
    evaluation = EvaluationOut.model_validate(latest) if latest else None
    if evaluation is not None and public_view:
        evaluation = evaluation.model_copy(
            update={
                "atomic_results": [],
                "explanation": {
                    "public_view": True,
                    "note": "회사 사실값과 내부 증빙 식별자는 공개 화면에서 제외됩니다.",
                },
            }
        )
    return NoticeSummary(
        notice_key=notice.notice_key,
        bid_notice_no=notice.bid_notice_no,
        revision_no=notice.revision_no,
        title=notice.title,
        agency=notice.agency,
        deadline=notice.deadline,
        status=_effective_notice_status(notice),
        estimated_amount=notice.estimated_amount,
        source_kind=source_kind,
        ingestion_state=ingestion_state,
        analysis_updated_at=(latest.evaluated_at if latest else latest_version.created_at if latest_version else None),
        latest_evaluation=evaluation,
    )


def _source_kind(notice: Notice) -> str:
    key = notice.notice_key.upper()
    if key.startswith("SYN-") or "-SYN-" in key:
        return "SYNTHETIC"
    if key.startswith("PPS-"):
        return "PPS"
    return "MANUAL"


def _effective_notice_status(notice: Notice) -> str:
    status_value = notice.status.upper()
    if status_value == "OPEN" and _comparable_utc(notice.deadline) < datetime.now(timezone.utc):
        return "EXPIRED"
    return status_value


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


def _latest_document_analyses(versions: list[NoticeVersion]) -> list[dict[str, Any]]:
    """Expose only the latest extraction attempt per attachment to the product UI.

    Full attempt history remains available through ``versions`` for audit. This
    prevents a recovered R07 attempt from leaving the current document card in a
    permanent review state.
    """

    latest_by_attachment: dict[str, dict[str, Any]] = {}
    for item in sorted(versions, key=lambda value: value.version_no):
        payload = item.source_payload
        if not isinstance(payload, dict) or payload.get("kind") != "OPENAI_REQUIREMENT_EXTRACTION":
            continue
        attachment_key = str(
            payload.get("attachment_id")
            or payload.get("source_label")
            or f"version:{item.version_no}"
        )
        latest_by_attachment[attachment_key] = payload
    return list(latest_by_attachment.values())


def _curated_public_extraction(payload: Any) -> dict[str, Any] | None:
    """Return only an extraction that exactly matches the packaged public seed.

    A server-authenticated ingestion may persist provider response IDs, model
    metadata, arbitrary attachment labels, or unreviewed extracted text.  None
    of those fields cross the anonymous contest-demo boundary.  The v0.3 public
    surface therefore fails closed and accepts only the reviewed, digest-bound
    extraction shipped with this release.
    """

    if not isinstance(payload, dict):
        return None
    seed = load_public_notice_seed()
    extraction = seed["extraction"]
    provenance = seed["provenance"]
    expected_result = {
        "document_type": extraction["document_type"],
        "summary": extraction["summary"],
        "requirements": extraction["requirements"],
    }
    if (
        payload.get("kind") != extraction["kind"]
        or payload.get("status") != extraction["status"]
        or payload.get("classification") != seed["classification"]
        or payload.get("seed_version") != seed["seed_version"]
        or payload.get("seed_digest") != provenance["payload_sha256"]
        or payload.get("prompt_version") != extraction["prompt_version"]
        or payload.get("schema_version") != extraction["schema_version"]
        or payload.get("document_sha256") != provenance["document_sha256"]
        or payload.get("result") != expected_result
    ):
        return None
    return {
        "kind": extraction["kind"],
        "status": extraction["status"],
        "document_name": provenance["source_label"],
        "summary": extraction["summary"],
        "requirements": extraction["requirements"],
    }


def _public_document_analyses(versions: list[NoticeVersion]) -> list[dict[str, Any]]:
    analyses: list[dict[str, Any]] = []
    for payload in _latest_document_analyses(versions):
        curated = _curated_public_extraction(payload)
        if curated is not None:
            analyses.append(curated)
    return analyses


def _publication_safe_source_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    blocked = {"servicekey", "api_key", "apikey", "access_token", "token", "key"}
    query = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key.casefold() not in blocked]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _detail(notice: Notice, *, public_view: bool = False) -> NoticeDetail:
    latest_version = max(notice.versions, key=lambda item: item.version_no) if notice.versions else None
    return NoticeDetail(
        **_summary(notice, public_view=public_view).model_dump(),
        id=notice.id,
        published_at=notice.published_at,
        category=notice.category,
        source_url=(
            _publication_safe_source_url(notice.source_url)
            if public_view
            else notice.source_url
        ),
        risk_dimensions=notice.risk_dimensions,
        versions=notice.versions,
        requirements=[_requirement_dict(item) for item in (latest_version.requirements if latest_version else [])],
        decisions=[] if public_view else notice.decisions,
        document_analyses=(
            _public_document_analyses(notice.versions)
            if public_view
            else _latest_document_analyses(notice.versions)
        ),
        award_history=notice.award_history,
    )


def _load_notice(session: Session, notice_key: str) -> Notice:
    notice = session.scalar(
        select(Notice)
        .where(Notice.notice_key == notice_key)
        .options(
            selectinload(Notice.versions).selectinload(NoticeVersion.requirements),
            selectinload(Notice.evaluations),
            selectinload(Notice.decisions),
            selectinload(Notice.award_history),
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
def dashboard(request: Request, session: DbSession) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    notices = list(
        session.scalars(
            select(Notice)
            .options(selectinload(Notice.evaluations), selectinload(Notice.versions))
            .order_by((Notice.deadline < now).asc(), Notice.deadline.asc())
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
    soon = now + timedelta(days=7)
    return {
        "generated_at": now,
        "totals": {
            "notices": len(notices),
            "active": sum(_effective_notice_status(item) == "OPEN" for item in notices),
            "evaluations": evaluation_total,
            "decisions": decisions_total,
        },
        "eligibility_counts": eligibility_counts,
        "readiness_counts": readiness_counts,
        "pending_review": eligibility_counts[Eligibility.REVIEW.value],
        "deadline_soon": sum(1 for item in notices if now <= _comparable_utc(item.deadline) <= soon),
        "recent_notices": [
            _summary(item, public_view=public_read_allowed(request)).model_dump()
            for item in notices[:10]
        ],
        "synthetic_data_warning": "SYN- 접두 데이터는 데모용이며 실제 성과 지표가 아닙니다.",
    }


@router.get("/runtime-profile")
def runtime_profile(request: Request) -> dict[str, Any]:
    public_mode = bool(request.app.state.settings.public_read_only)
    return {
        "access_mode": "PUBLIC_READ_ONLY" if public_mode else "SERVER_AUTHENTICATED",
        "write_controls_enabled": not public_mode,
        "data_boundary": (
            "공개 안전 GET만 익명 허용; 변경·수집·내부 로그는 서버 인증 필요"
            if public_mode
            else "서버 인증 정책 적용"
        ),
    }


@router.get("/departments/keyword-profiles")
def department_keyword_profiles() -> dict[str, Any]:
    """Return the public, versioned organization search catalog.

    The catalog contains department business terms only; employee names and
    contact details are intentionally absent.
    """

    return load_department_keyword_profiles()


@router.get("/notices", response_model=list[NoticeSummary])
def list_notices(
    request: Request,
    session: DbSession,
    q: str | None = None,
    eligibility: Eligibility | None = None,
    department_id: Annotated[str | None, Query(max_length=80)] = None,
    search_keywords: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NoticeSummary]:
    try:
        parsed_keywords = parse_search_keywords(search_keywords)
        # Resolve early so an invalid selector is a clear client error even
        # when the notice table is empty.
        get_department_profile(department_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"부서 또는 검색 키워드를 확인하세요: {exc}") from exc

    ranking_requested = bool(department_id or parsed_keywords)
    now = datetime.now(timezone.utc)
    statement = (
        select(Notice)
        .options(selectinload(Notice.evaluations), selectinload(Notice.versions))
        .order_by((Notice.deadline < now).asc(), Notice.deadline.asc())
    )
    if q:
        pattern = f"%{q}%"
        statement = statement.where(or_(Notice.title.ilike(pattern), Notice.agency.ilike(pattern)))
    if ranking_requested:
        notices = list(session.scalars(statement).all())
    else:
        notices = list(session.scalars(statement.offset(offset).limit(limit)).all())

    if parsed_keywords:
        notices = [
            notice
            for notice in notices
            if notice_matches_user_keywords(
                title=notice.title,
                agency=notice.agency,
                category=notice.category or "",
                user_keywords=parsed_keywords,
            )
        ]
    if eligibility:
        notices = [
            notice
            for notice in notices
            if (latest := _latest_evaluation(notice)) and latest.eligibility == eligibility
        ]

    if not ranking_requested:
        return [
            _summary(notice, public_view=public_read_allowed(request))
            for notice in notices
        ]

    ranked: list[NoticeSummary] = []
    for notice in notices:
        selected_ranking = rank_notice_for_department(
            title=notice.title,
            agency=notice.agency,
            category=notice.category or "",
            department_id=department_id,
            user_keywords=parsed_keywords,
        )
        top_rankings = rank_notice_across_departments(
            title=notice.title,
            agency=notice.agency,
            category=notice.category or "",
            user_keywords=parsed_keywords,
            limit=5,
        )
        ranked.append(
            NoticeSummary.model_validate(
                {
                    **_summary(
                        notice,
                        public_view=public_read_allowed(request),
                    ).model_dump(),
                    "department_ranking": selected_ranking,
                    "top_department_rankings": top_rankings,
                }
            )
        )
    ranked.sort(
        key=lambda item: (
            -(item.department_ranking.score if item.department_ranking else 0),
            _comparable_utc(item.deadline),
        )
    )
    return ranked[offset : offset + limit]


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
def get_notice(notice_key: str, request: Request, session: DbSession) -> NoticeDetail:
    return _detail(
        _load_notice(session, notice_key),
        public_view=public_read_allowed(request),
    )


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


def _pps_notice_key(item: dict[str, Any]) -> str:
    notice_no = str(item["bid_notice_no"])
    revision = str(item.get("revision_no") or "00")
    safe_no = re.sub(r"[^A-Za-z0-9_-]+", "-", notice_no).strip("-") or "notice"
    identity = str(item.get("identity") or f"{notice_no}|{revision}|{item['deadline'].isoformat()}")
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"PPS-{safe_no}-{revision}-{suffix}"[:160]


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return _comparable_utc(left) == _comparable_utc(right)


@router.post("/ingestion/pps/notices", response_model=PpsIngestionResponse)
def ingest_pps_notices(
    payload: PpsIngestionRequest,
    request: Request,
    session: DbSession,
) -> PpsIngestionResponse:
    """Fetch a bounded live PPS window and idempotently upsert canonical metadata.

    Only public canonical fields are stored. Provider payloads, contacts and the
    service key are intentionally discarded.
    """

    settings = request.app.state.settings
    if not settings.pps_api_key:
        raise HTTPException(status_code=503, detail="PPS_API_KEY가 서버에 설정되지 않았습니다.")

    window = {"from": payload.from_date.isoformat(), "to": payload.to_date.isoformat()}
    job = IngestionJob(
        source="PPS",
        mode="DRY_RUN" if payload.dry_run else "LIVE",
        status="RUNNING",
        window_json=window,
        keyword=payload.keyword,
        request_json={"page_size": payload.page_size, "max_pages": payload.max_pages},
        notice_keys=[],
        warnings=[],
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    try:
        with PpsClient(
            service_key=settings.pps_api_key,
            base_url=settings.pps_base_url,
        ) as client:
            fetched_rows = list(
                client.iter_notices(
                    operation_path=settings.pps_notice_operation,
                    start=payload.from_date,
                    end=payload.to_date,
                    rows=payload.page_size,
                    max_pages=payload.max_pages,
                )
            )
            api_calls = client.request_count
            hit_page_limit = client.hit_page_limit
    except PpsApiError as exc:
        job.status = "FAILED"
        job.error_code = "PPS_API_ERROR"
        job.warnings = ["조달청 API 호출이 실패했습니다. 키와 승인 상태를 확인하세요."]
        job.completed_at = datetime.now(timezone.utc)
        session.commit()
        raise HTTPException(status_code=502, detail="조달청 API 호출에 실패했습니다.") from exc

    warnings: list[str] = []
    if hit_page_limit:
        warnings.append("max_pages 제한에서 수집을 중단했습니다. 다음 실행에서 기간을 더 좁히세요.")
    if payload.keyword:
        warnings.append("keyword는 수집된 공고명에 대해 서버에서 후처리되었습니다.")
    if payload.dry_run:
        warnings.append("dry_run이므로 공고 테이블에는 변경을 저장하지 않았습니다.")

    keyword = payload.keyword.casefold() if payload.keyword else None
    quarantined = 0
    matched_rows: list[dict[str, Any]] = []
    for item in fetched_rows:
        if not item.get("bid_notice_no") or not item.get("title") or item.get("deadline") is None:
            quarantined += 1
            continue
        if keyword and keyword not in str(item["title"]).casefold():
            continue
        matched_rows.append(item)

    candidates: dict[str, dict[str, Any]] = {}
    provider_duplicates = 0
    for item in matched_rows:
        notice_key = _pps_notice_key(item)
        if notice_key in candidates:
            provider_duplicates += 1
        candidates[notice_key] = item

    created = 0
    updated = 0
    duplicates = provider_duplicates
    notice_keys: list[str] = []
    for notice_key, item in candidates.items():
        notice_keys.append(notice_key)
        existing = session.scalar(select(Notice).where(Notice.notice_key == notice_key))
        if existing is None:
            created += 1
            if not payload.dry_run:
                source_url = item.get("source_url")
                session.add(
                    Notice(
                        notice_key=notice_key,
                        bid_notice_no=item["bid_notice_no"],
                        revision_no=item["revision_no"],
                        title=item["title"],
                        agency=item.get("agency") or "",
                        published_at=(
                            _comparable_utc(item["published_at"])
                            if item.get("published_at")
                            else None
                        ),
                        deadline=_comparable_utc(item["deadline"]),
                        status="OPEN",
                        category="용역",
                        estimated_amount=item.get("estimated_amount"),
                        source_url=(redact_url(str(source_url)) if source_url else None),
                    )
                )
            continue

        changes: dict[str, Any] = {}
        for field, value in {
            "title": item["title"],
            "agency": item.get("agency") or existing.agency,
            "revision_no": item["revision_no"],
            "estimated_amount": (
                item.get("estimated_amount")
                if item.get("estimated_amount") is not None
                else existing.estimated_amount
            ),
        }.items():
            if getattr(existing, field) != value:
                changes[field] = value
        for field, value in {
            "published_at": (
                _comparable_utc(item["published_at"])
                if item.get("published_at")
                else existing.published_at
            ),
            "deadline": _comparable_utc(item["deadline"]),
        }.items():
            if not _same_datetime(getattr(existing, field), value):
                changes[field] = value
        source_url = item.get("source_url")
        if source_url:
            safe_source_url = redact_url(str(source_url))
            if existing.source_url != safe_source_url:
                changes["source_url"] = safe_source_url
        if changes:
            updated += 1
            if not payload.dry_run:
                for field, value in changes.items():
                    setattr(existing, field, value)
        else:
            duplicates += 1

    job.status = "COMPLETED"
    job.api_calls = api_calls
    job.fetched = len(fetched_rows)
    job.matched = len(matched_rows)
    job.created_count = created
    job.updated_count = updated
    job.duplicate_count = duplicates
    job.quarantined_count = quarantined
    job.notice_keys = notice_keys
    job.warnings = warnings
    job.completed_at = datetime.now(timezone.utc)
    session.commit()

    return PpsIngestionResponse(
        job_id=job.id,
        window=window,
        api_calls=api_calls,
        fetched=len(fetched_rows),
        matched=len(matched_rows),
        created=created,
        updated=updated,
        duplicates=duplicates,
        quarantined=quarantined,
        notice_keys=notice_keys,
        next_watermark=payload.to_date.isoformat(),
        warnings=warnings,
        dry_run=payload.dry_run,
    )


@router.get("/ingestion/jobs", response_model=list[IngestionJobOut])
def list_ingestion_jobs(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[IngestionJob]:
    return list(
        session.scalars(
            select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(limit)
        ).all()
    )


_AWARD_TITLE_STOPWORDS = {
    "공고",
    "긴급",
    "변경",
    "재공고",
    "입찰",
    "사업",
    "용역",
    "위탁",
    "위탁운영",
    "운영",
    "시행",
    "계약",
}


def _award_title_tokens(title: str) -> list[str]:
    tokens = re.findall(r"[0-9A-Za-z가-힣]+", title.casefold())
    return [
        token
        for token in tokens
        if token not in _AWARD_TITLE_STOPWORDS
        and not re.fullmatch(r"(?:19|20)\d{2}년?", token)
        and len(token) > 1
    ]


def _derive_award_keyword(title: str) -> str:
    tokens = _award_title_tokens(title)
    if not tokens:
        raise HTTPException(status_code=422, detail="낙찰 이력 검색 키워드를 직접 입력해 주세요.")
    return " ".join(tokens[:3])[:100]


def _award_similarity(target_title: str, candidate_title: str) -> float:
    target = set(_award_title_tokens(target_title))
    candidate = set(_award_title_tokens(candidate_title))
    if not target or not candidate:
        return 0.0
    token_jaccard = len(target & candidate) / len(target | candidate)
    target_compact = "".join(_award_title_tokens(target_title))
    candidate_compact = "".join(_award_title_tokens(candidate_title))

    def trigrams(value: str) -> set[str]:
        if len(value) < 3:
            return {value} if value else set()
        return {value[index:index + 3] for index in range(len(value) - 2)}

    target_grams = trigrams(target_compact)
    candidate_grams = trigrams(candidate_compact)
    char_dice = (
        2 * len(target_grams & candidate_grams) / (len(target_grams) + len(candidate_grams))
        if target_grams and candidate_grams
        else 0.0
    )
    # Korean procurement titles often concatenate rank/prefixes with the same
    # core phrase (for example 5급승진후보자 vs 7급 승진후보자). Blend token
    # overlap with character trigrams so those candidates do not become a
    # misleading zero while an identical normalised title remains 100.
    return round(100 * (0.35 * token_jaccard + 0.65 * char_dice), 2)


def _minus_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:  # February 29 to a non-leap year.
        return value.replace(year=value.year - years, day=28)


@router.get(
    "/notices/{notice_key}/award-history",
    response_model=list[AwardHistoryItemOut],
)
def list_award_history(notice_key: str, session: DbSession) -> list[AwardHistoryItem]:
    notice = _load_notice(session, notice_key)
    return list(
        session.scalars(
            select(AwardHistoryItem)
            .where(AwardHistoryItem.target_notice_id == notice.id)
            .order_by(AwardHistoryItem.awarded_at.desc(), AwardHistoryItem.created_at.desc())
        ).all()
    )


@router.get("/notices/{notice_key}/award-intelligence", response_model=AwardIntelligenceOut)
def get_award_intelligence(notice_key: str, session: DbSession) -> dict[str, Any]:
    """Analyse only stored three-year candidates; never performs a PPS request."""

    notice = _load_notice(session, notice_key)
    as_of = _comparable_utc(notice.published_at) if notice.published_at else datetime.now(timezone.utc)
    cutoff = _minus_years(as_of.date(), 3)
    candidates = list(
        session.scalars(
            select(AwardHistoryItem)
            .where(AwardHistoryItem.target_notice_id == notice.id)
            .order_by(AwardHistoryItem.awarded_at.desc(), AwardHistoryItem.created_at.desc())
        ).all()
    )
    # Keep undated stored candidates visible but mark their missing date in the
    # response. Dated facts outside the explicit three-year window are excluded.
    history = [
        item for item in candidates
        if (item.awarded_at or item.opened_at) is None
        or cutoff <= _comparable_utc(item.awarded_at or item.opened_at).date() <= as_of.date()
    ]
    result = build_award_intelligence(
        history,
        as_of=as_of,
        target_estimated_price=notice.estimated_amount,
    )
    result["notice_key"] = notice.notice_key
    result["period"] = {"from": cutoff.isoformat(), "to": as_of.date().isoformat(), "years": 3}
    result["target_amount_basis"] = {
        "kind": "NOTICE_ESTIMATED_AMOUNT" if notice.estimated_amount else "UNAVAILABLE",
        "amount": notice.estimated_amount,
        "note": "공고 저장값이며 조달청 예정가격과 동일하다고 간주하지 않습니다.",
    }
    matched_pricing = next(
        (
            pricing_profile_for_document(version.file_sha256)
            for version in sorted(notice.versions, key=lambda item: item.version_no, reverse=True)
            if pricing_profile_for_document(version.file_sha256) is not None
        ),
        None,
    )
    result["pricing_method"] = matched_pricing
    if matched_pricing is None:
        result["warnings"].append("현재 공고 문서와 SHA-256이 정확히 일치하는 가격평가 산식 근거가 없습니다.")
    return result


@router.post(
    "/notices/{notice_key}/award-history/refresh",
    response_model=AwardHistoryRefreshOut,
)
def refresh_award_history(
    notice_key: str,
    payload: AwardHistoryRefreshRequest,
    request: Request,
    session: DbSession,
) -> AwardHistoryRefreshOut:
    """Load bounded public award candidates without retaining provider PII.

    The result is deliberately a keyword/similarity candidate history. It does
    not assert that an older award is the same scope, and it never changes an
    eligibility or bid decision.
    """

    notice = _load_notice(session, notice_key)
    settings = request.app.state.settings
    if not settings.pps_api_key:
        raise HTTPException(status_code=503, detail="PPS_API_KEY가 서버에 설정되지 않았습니다.")

    keyword = payload.keyword or _derive_award_keyword(notice.title)
    as_of = (
        _comparable_utc(notice.published_at).date()
        if notice.published_at
        else datetime.now(timezone.utc).date()
    )
    start = _minus_years(as_of, payload.years)
    window = {"from": start.isoformat(), "to": as_of.isoformat()}
    warnings = [
        "검색 결과는 제목 유사 후보이며 동일 사업 확정 이력이 아닙니다. 담당자 검토가 필요합니다."
    ]
    if payload.keyword is None:
        warnings.append("공고명에서 검색 키워드를 자동 생성했습니다.")
    if payload.dry_run:
        warnings.append("dry_run이므로 낙찰 이력 테이블에는 변경을 저장하지 않았습니다.")

    job = IngestionJob(
        source="PPS_AWARD",
        mode="DRY_RUN" if payload.dry_run else "LIVE",
        status="RUNNING",
        window_json=window,
        keyword=keyword,
        request_json={
            "notice_key": notice.notice_key,
            "years": payload.years,
            "page_size": payload.page_size,
            "max_pages_per_window": payload.max_pages_per_window,
        },
        notice_keys=[notice.notice_key],
        warnings=[],
    )
    session.add(job)
    session.commit()
    session.refresh(job)

    try:
        with PpsAwardClient(
            service_key=settings.pps_api_key,
            base_url=settings.pps_base_url,
        ) as client:
            fetched_rows = list(
                client.iter_awards(
                    operation_path=settings.pps_award_operation,
                    start=start,
                    end=as_of,
                    keyword=keyword,
                    rows=payload.page_size,
                    max_pages_per_window=payload.max_pages_per_window,
                    continue_on_window_error=True,
                )
            )
            api_calls = client.request_count
            hit_page_limit = client.hit_page_limit
            fallback_window_count = client.fallback_window_count
            window_errors = list(client.window_errors)
    except PpsApiError as exc:
        job.status = "FAILED"
        job.error_code = "PPS_AWARD_API_ERROR"
        job.warnings = ["조달청 낙찰정보 API 호출이 실패했습니다. 키와 승인 상태를 확인하세요."]
        job.completed_at = datetime.now(timezone.utc)
        session.commit()
        raise HTTPException(status_code=502, detail="조달청 낙찰정보 API 호출에 실패했습니다.") from exc

    if hit_page_limit:
        warnings.append("일부 30일 구간이 페이지 제한에 도달했습니다. 구간 또는 키워드를 좁혀 재조회하세요.")
    if fallback_window_count:
        warnings.append(
            f"비표준 응답을 받은 {fallback_window_count}개 구간은 7일 단위로 재조회했습니다."
        )
    if window_errors:
        warnings.append(
            f"재조회에도 실패한 {len(window_errors)}개 7일 구간은 누락 상태로 기록했습니다."
        )

    quarantined = 0
    candidates: dict[str, dict[str, Any]] = {}
    provider_duplicates = 0
    for item in fetched_rows:
        if not item.get("identity") or not item.get("bid_notice_no") or not item.get("title") or not item.get("winner_name"):
            quarantined += 1
            continue
        identity = str(item["identity"])
        if identity in candidates:
            provider_duplicates += 1
        candidates[identity] = item
    if quarantined:
        warnings.append(f"필수 공개 필드가 없는 {quarantined}건은 격리했습니다.")

    created = 0
    updated = 0
    duplicates = provider_duplicates
    for identity, item in candidates.items():
        existing = session.scalar(
            select(AwardHistoryItem).where(
                AwardHistoryItem.target_notice_id == notice.id,
                AwardHistoryItem.external_identity == identity,
            )
        )
        values = {
            "bid_notice_no": item["bid_notice_no"],
            "revision_no": item.get("revision_no") or "000",
            "title": item["title"],
            "agency": item.get("agency") or "",
            "winner_name": item["winner_name"],
            "participant_count": item.get("participant_count"),
            "award_amount": item.get("award_amount"),
            "award_rate": item.get("award_rate"),
            "opened_at": (
                _comparable_utc(item["opened_at"]) if item.get("opened_at") else None
            ),
            "awarded_at": (
                _comparable_utc(item["awarded_at"]) if item.get("awarded_at") else None
            ),
            "similarity_score": _award_similarity(notice.title, item["title"]),
            "source": "PPS",
        }
        if existing is None:
            created += 1
            if not payload.dry_run:
                session.add(
                    AwardHistoryItem(
                        target_notice_id=notice.id,
                        external_identity=identity,
                        **values,
                    )
                )
            continue

        changes = {
            field: value
            for field, value in values.items()
            if (
                not _same_datetime(getattr(existing, field), value)
                if field in {"opened_at", "awarded_at"}
                else getattr(existing, field) != value
            )
        }
        if changes:
            updated += 1
            if not payload.dry_run:
                for field, value in changes.items():
                    setattr(existing, field, value)
        else:
            duplicates += 1

    job.status = "PARTIAL" if window_errors else "COMPLETED"
    job.api_calls = api_calls
    job.fetched = len(fetched_rows)
    job.matched = len(candidates)
    job.created_count = created
    job.updated_count = updated
    job.duplicate_count = duplicates
    job.quarantined_count = quarantined
    job.warnings = warnings
    job.completed_at = datetime.now(timezone.utc)
    session.commit()

    return AwardHistoryRefreshOut(
        job_id=job.id,
        notice_key=notice.notice_key,
        status=job.status,
        keyword=keyword,
        window=window,
        api_calls=api_calls,
        fetched=len(fetched_rows),
        created=created,
        updated=updated,
        duplicates=duplicates,
        records=len(candidates),
        dry_run=payload.dry_run,
        warnings=warnings,
    )


@router.get("/company-profile")
def public_company_profile() -> dict[str, Any]:
    """Return the repository-backed, publication-safe company profile.

    Certificate bodies, registration numbers, addresses, contact details, and
    personal names are excluded. The curated loader fails closed if those
    fields or common sensitive value patterns are introduced.
    """

    return load_public_company_profile()


@router.get("/notices/{notice_key}/analysis/requirement-policy")
def requirement_policy(
    notice_key: str,
    request: Request,
    session: DbSession,
) -> dict[str, Any]:
    """Separate eligibility, required actions, checklist work, and information.

    The result uses only the repository-backed public profile. Eligibility
    evidence retains its notice-deadline recheck policy; procedural clauses do
    not become eligibility REVIEW merely because they are mandatory.
    """

    notice = _load_notice(session, notice_key)
    public_view = public_read_allowed(request)
    analysis_version = next(
        (
            item
            for item in sorted(notice.versions, key=lambda value: value.version_no, reverse=True)
            if isinstance(item.source_payload, dict)
            and item.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
            and item.source_payload.get("status") == "ACCEPTED"
            and isinstance(item.source_payload.get("result"), dict)
            and (
                not public_view
                or _curated_public_extraction(item.source_payload) is not None
            )
        ),
        None,
    )
    if analysis_version is None:
        detail = (
            "공개 검증이 완료된 공고 분석이 없습니다."
            if public_view
            else "먼저 공고문 근거 추출을 실행해야 합니다."
        )
        raise HTTPException(status_code=422, detail=detail)
    result_payload = analysis_version.source_payload["result"]
    requirements = list(result_payload.get("requirements") or [])
    classified = classify_requirements(
        requirements,
        profile=load_public_company_profile(),
        deadline=notice.deadline,
    )
    return {
        "notice_key": notice.notice_key,
        "analysis_version_id": analysis_version.id,
        **classified,
    }


def _extraction_run_out(
    notice_key: str,
    version: NoticeVersion,
    *,
    reused: bool,
) -> OpenAIExtractionRunOut:
    payload = version.source_payload or {}
    return OpenAIExtractionRunOut(
        notice_key=notice_key,
        version_id=version.id,
        version_no=version.version_no,
        file_sha256=version.file_sha256,
        status=payload.get("status", "REVIEW"),
        review_code=payload.get("review_code"),
        error_code=payload.get("error_code"),
        message=payload.get("message", "추출 결과를 확인할 수 없습니다."),
        response_id=payload.get("response_id"),
        model=payload.get("model"),
        prompt_version=payload.get("prompt_version", PROMPT_VERSION),
        schema_version=payload.get("schema_version", SCHEMA_VERSION),
        data=payload.get("result"),
        reused=reused,
    )


@router.post(
    "/notices/{notice_key}/analysis/extractions",
    response_model=OpenAIExtractionRunOut,
    status_code=status.HTTP_201_CREATED,
)
def run_openai_extraction(
    notice_key: str,
    payload: OpenAIExtractionRunRequest,
    request: Request,
    session: DbSession,
) -> OpenAIExtractionRunOut:
    """Extract evidence from public notice text without storing the source text.

    The model is not allowed to decide eligibility or a score. Its strict JSON
    output is persisted as a versioned evidence candidate for later deterministic
    matching against the reviewed public company profile.
    """

    notice = _load_notice(session, notice_key)
    settings = request.app.state.settings
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY가 서버에 설정되지 않았습니다.")
    calculated_sha = hashlib.sha256(payload.document_text.encode("utf-8")).hexdigest()
    if payload.document_sha256 and payload.document_sha256.casefold() != calculated_sha:
        raise HTTPException(status_code=422, detail="document_sha256이 입력 텍스트와 일치하지 않습니다.")

    if not payload.force:
        prior_versions = session.scalars(
            select(NoticeVersion)
            .where(
                NoticeVersion.notice_id == notice.id,
                NoticeVersion.file_sha256 == calculated_sha,
            )
            .order_by(NoticeVersion.version_no.desc())
        ).all()
        prior = next(
            (
                item
                for item in prior_versions
                if isinstance(item.source_payload, dict)
                and item.source_payload.get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"
                and item.source_payload.get("status") == "ACCEPTED"
                and item.source_payload.get("prompt_version") == PROMPT_VERSION
                and item.source_payload.get("attachment_id") == payload.attachment_id
            ),
            None,
        )
        if prior:
            return _extraction_run_out(notice_key, prior, reused=True)

    with OpenAIExtractionClient(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
    ) as client:
        outcome = client.extract(
            document_text=payload.document_text,
            allowed_attachment_ids={payload.attachment_id},
        )

    data = outcome.data.model_dump(mode="json") if outcome.data else None
    confidence_values = [
        anchor.confidence
        for requirement in (outcome.data.requirements if outcome.data else [])
        for anchor in requirement.evidence
    ]
    confidence = (
        sum(confidence_values) / len(confidence_values)
        if confidence_values
        else 0.0
    )
    source_payload = {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "source_kind": "PUBLIC_NOTICE",
        "attachment_id": payload.attachment_id,
        "source_label": payload.source_label,
        "document_sha256": calculated_sha,
        "status": outcome.status,
        "review_code": outcome.review_code,
        "error_code": outcome.error_code,
        "message": outcome.message,
        "response_id": outcome.response_id,
        "model": outcome.model,
        "prompt_version": outcome.prompt_version,
        "schema_version": outcome.schema_version,
        "result": data,
    }
    version = NoticeVersion(
        notice_id=notice.id,
        version_no=max((item.version_no for item in notice.versions), default=0) + 1,
        file_sha256=calculated_sha,
        document_complete=(
            outcome.status == "ACCEPTED"
            and outcome.data is not None
            and not outcome.data.missing_or_unreadable
        ),
        extraction_status=outcome.status,
        extraction_confidence=confidence,
        source_payload=source_payload,
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return _extraction_run_out(notice_key, version, reused=False)


def _default_teams_card(notice: Notice) -> dict[str, Any]:
    evaluation = _latest_evaluation(notice)
    eligibility = evaluation.eligibility if evaluation else "분석 대기"
    readiness = evaluation.readiness_status if evaluation else "미산정"
    competition = build_award_intelligence(
        notice.award_history,
        as_of=_comparable_utc(notice.published_at) if notice.published_at else datetime.now(timezone.utc),
    )["competition_risk"]
    competition_value = (
        f"{competition['band']} · {competition['score']}/100 · {competition['confidence']}"
        if competition["status"] == "MODEL_ESTIMATE"
        else "UNKNOWN · 표본/커버리지 부족"
    )
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "PAI LOOP · Teams 모의 알림",
            "weight": "Bolder",
            "size": "Medium",
        },
        {"type": "TextBlock", "text": notice.title, "wrap": True},
        {
            "type": "FactSet",
            "facts": [
                {"title": "기관", "value": notice.agency or "-"},
                {"title": "마감", "value": notice.deadline.isoformat()},
                {"title": "참가자격", "value": eligibility},
                {"title": "준비도", "value": readiness},
                {"title": "경쟁·집중 리스크", "value": competition_value},
            ],
        },
        {
            "type": "TextBlock",
            "text": "회사 Teams 승인 전이므로 실제 전송하지 않고 로컬 로그에만 기록합니다. 경쟁·집중 점수는 저장 유사후보 기반 예상이며 참가자격과 별도입니다.",
            "wrap": True,
            "isSubtle": True,
        },
    ]
    actions: list[dict[str, Any]] = []
    if notice.source_url and notice.source_url.startswith(("https://", "http://")):
        actions.append({"type": "Action.OpenUrl", "title": "공고 원문", "url": notice.source_url})
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
        "actions": actions,
    }


def _mock_notification_out(item: MockNotification) -> TeamsMockNotificationOut:
    return TeamsMockNotificationOut(
        id=item.id,
        notice_key=item.notice.notice_key,
        channel="teams",
        delivery_mode="mock",
        status=item.status,
        correlation_id=item.correlation_id,
        card=item.card,
        created_at=item.created_at,
    )


@router.post(
    "/notices/{notice_key}/notifications/teams/mock",
    response_model=TeamsMockNotificationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_teams_mock_notification(
    notice_key: str,
    payload: TeamsMockNotificationCreate,
    session: DbSession,
) -> TeamsMockNotificationOut:
    notice = _load_notice(session, notice_key)
    if payload.correlation_id:
        existing = session.scalar(
            select(MockNotification)
            .where(MockNotification.correlation_id == payload.correlation_id)
            .options(selectinload(MockNotification.notice))
        )
        if existing:
            if existing.notice_id != notice.id:
                raise HTTPException(status_code=409, detail="correlation_id가 다른 공고에 사용되었습니다.")
            return _mock_notification_out(existing)
    card = payload.card or _default_teams_card(notice)
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > 28 * 1024:
        raise HTTPException(status_code=422, detail="Adaptive Card가 Teams 28KB 제한을 초과합니다.")
    notification = MockNotification(
        notice=notice,
        channel="teams",
        delivery_mode="mock",
        status="MOCK_RECORDED",
        correlation_id=payload.correlation_id,
        card=card,
    )
    session.add(notification)
    session.commit()
    session.refresh(notification)
    return _mock_notification_out(notification)


@router.get("/notifications/mock", response_model=list[TeamsMockNotificationOut])
def list_mock_notifications(
    session: DbSession,
    notice_key: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TeamsMockNotificationOut]:
    statement = (
        select(MockNotification)
        .join(MockNotification.notice)
        .options(selectinload(MockNotification.notice))
        .order_by(MockNotification.created_at.desc())
        .limit(limit)
    )
    if notice_key:
        statement = statement.where(Notice.notice_key == notice_key)
    return [_mock_notification_out(item) for item in session.scalars(statement).all()]
