"""Personal notice cards from current, publication-safe stored analysis only."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from sqlalchemy.orm import Session

from .analysis_pipeline import _select_source_versions
from .api import _curated_public_extraction, _detail, _pps_authorities_by_notice_id
from .award_intelligence import build_award_intelligence
from .eligibility_policy import classify_requirements, load_public_company_profile
from .integrations.openai_extraction import PROMPT_VERSION
from .models import Notice
from .pps_enrichment import safe_public_bound_extraction
from .quantitative_scoring import _stored_public_quantitative_projection

KST = timezone(timedelta(hours=9))
EVENT_LABELS = {
    "REGISTERED": "관심 공고 등록",
    "D_MINUS_5": "마감 5일 전",
    "DEADLINE_DAY": "오늘 마감",
}
STATUS_LABELS = {
    "PASS": "충족", "REVIEW": "확인 필요", "FAIL": "미충족",
    "NOT_EVALUATED": "미판정", "CONFIRMED": "확정", "ESTIMATED": "추정",
    "UNSCORABLE": "산정 불가", "REVIEW_REQUIRED": "확인 필요",
    "RED": "보완 필요", "AMBER": "일부 확인 필요", "GREEN": "준비됨",
}


def _plain(value: object, limit: int = 240) -> str:
    # Card markdown must never turn a source title/condition into a link, image,
    # mention, or fabricated heading. We do not copy raw provider/evidence data.
    text = " ".join(str(value or "").split())
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "미산정"
    return f"{value:g}"


def _block(text: str, **options) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, **options}


def _detail_url(base_url: str, notice_key: str) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or parsed.port not in {None, 443}
            or "\\" in base_url or any(ord(char) < 33 for char in base_url)):
        raise ValueError("Teams public base URL must be an HTTPS origin")
    return f"{base_url.rstrip('/')}/?{urlencode({'notice': notice_key})}"


def _current_requirement_groups(session: Session, notice: Notice, now: datetime) -> dict[str, list[str]]:
    """Select current anchored source clauses, then separate duties from gates.

    The detail's public document history is safe to publish but may still contain
    removed attachments. Use the same current source selector as requirement-policy.
    Classification supplies labels only; its computed outcomes never replace a
    stored eligibility verdict or quantitative score in the card.
    """
    requirements: list[dict] = []
    for version in _select_source_versions(
        session, notice_id=notice.id, prompt_version=PROMPT_VERSION, source_version_ids=None,
    ):
        payload = version.source_payload
        if not isinstance(payload, dict) or payload.get("status") != "ACCEPTED":
            continue
        extraction = (_curated_public_extraction(payload)
                      or safe_public_bound_extraction(payload, list(notice.versions)))
        if extraction is None:
            continue
        for requirement in extraction.get("requirements", []):
            if not isinstance(requirement, dict):
                continue
            if not any(isinstance(anchor, dict) and str(anchor.get("quote") or "").strip()
                       for anchor in requirement.get("evidence", [])):
                continue
            requirements.append(requirement)
    grouped: dict[str, list[str]] = {kind: [] for kind in ("ELIGIBILITY", "ACTION_REQUIRED", "CHECKLIST")}
    if not requirements:
        return grouped
    classified = classify_requirements(
        requirements, profile=load_public_company_profile(), deadline=notice.deadline, evaluation_date=now,
    )
    for item in classified["items"]:
        group = grouped.get(item["policy_class"])
        condition = item.get("condition")
        if group is not None and isinstance(condition, str) and condition.strip() and condition not in group:
            group.append(condition)
    return grouped


def build_notice_card(session: Session, notice: Notice, event_kind: str, *,
                      now: datetime | None = None, base_url: str) -> dict:
    """No provider calls, score recomputation, or persistence while rendering."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    authority = _pps_authorities_by_notice_id(session, [notice]).get(notice.id)
    detail = _detail(notice, public_view=True, provider_authority=authority, now=now)
    evaluation = detail.latest_evaluation if detail.qualification_status != "NOT_EVALUATED" else None
    current = detail.provider_disposition not in {"CANCELLED", "QUARANTINED"} and detail.status == "OPEN"
    quantitative = _stored_public_quantitative_projection(session, notice) if current else None
    deadline = detail.deadline.astimezone(KST)
    facts = [
        {"title": "발주기관", "value": _plain(detail.agency) or "미확인"},
        {"title": "마감", "value": deadline.strftime("%Y-%m-%d %H:%M (한국시간)")},
        {"title": "참가자격", "value": STATUS_LABELS.get(detail.qualification_status, "미판정") if current else "현재 검토 제외"},
    ]
    if detail.estimated_amount is not None:
        facts.append({"title": "사업금액", "value": f"{detail.estimated_amount:,.0f}원"})
    body = [
        _block(f"PAI · {EVENT_LABELS.get(event_kind, '관심 공고 알림')}", weight="Bolder", color="Accent"),
        _block(_plain(detail.title, 350), weight="Bolder", size="Medium"),
        {"type": "FactSet", "facts": facts},
        _block("분석 현황", weight="Bolder", separator=True),
        _block(_plain(detail.analysis_reason, 350)),
        _block(f"첨부 분석 {detail.analysis_attachments_accepted}/{detail.analysis_attachment_count} · "
               + ("현재 첨부 확인 완료" if detail.analysis_attachment_coverage_complete else "미확인 첨부 있음")),
        _block("자격사항 · 확인할 조건", weight="Bolder", separator=True),
    ]
    groups = _current_requirement_groups(session, notice, now) if current else {}
    requirements = groups.get("ELIGIBILITY", [])
    for condition in requirements[:6]:
        body.append(_block("• " + _plain(condition, 220)))
    if not requirements:
        body.append(_block("검증된 자격조건 요약이 아직 없습니다. 상세 화면에서 분석 상태를 확인해 주세요."))
    if len(requirements) > 6:
        body.append(_block(f"나머지 {len(requirements) - 6}개 조건과 원문 근거는 공고 상세에서 확인할 수 있습니다."))
    for kind, heading in (("ACTION_REQUIRED", "참여 전 처리할 사항"), ("CHECKLIST", "제출·계약 확인사항")):
        conditions = groups.get(kind, [])
        if conditions:
            body.append(_block(heading, weight="Bolder", separator=True))
            body.extend(_block("• " + _plain(condition, 200)) for condition in conditions[:4])
            if len(conditions) > 4:
                body.append(_block(f"나머지 {len(conditions) - 4}개 사항은 공고 상세에서 확인할 수 있습니다."))
    body.append(_block("정량점수", weight="Bolder", separator=True))
    if quantitative is None:
        body.append(_block("미산정 · 현재 공고 기준으로 검증된 정량점수 기록이 없습니다."))
    else:
        status = quantitative.overall_status
        if status == "CONFIRMED" and quantitative.confirmed_points is not None:
            score = f"확정 {_number(quantitative.confirmed_points)} / {_number(quantitative.total_max_points)}점"
        elif status == "ESTIMATED" and quantitative.lower_points is not None and quantitative.upper_points is not None:
            score = f"추정 {_number(quantitative.lower_points)}~{_number(quantitative.upper_points)} / {_number(quantitative.total_max_points)}점 · 확정 아님"
        else:
            score = f"{STATUS_LABELS.get(status, '확인 필요')} · 필요한 증빙과 배점표 확인 후 산정"
        body.append(_block(score))
        for criterion in quantitative.criteria[:6]:
            label = _plain(criterion.label, 90)
            if criterion.status == "CONFIRMED" and criterion.estimated_points is not None:
                value = f"확정 {_number(criterion.estimated_points)}점"
            elif criterion.status == "ESTIMATED":
                value = f"추정 {_number(criterion.lower_points)}~{_number(criterion.upper_points)}점"
            else:
                value = "확인 필요"
            body.append(_block(f"• {label}: {value} / 배점 {_number(criterion.max_points)}점"))
        body.append(_block(_plain(quantitative.opinion, 280)))
    body.append(_block("리스크 · 의사결정", weight="Bolder", separator=True))
    if evaluation and current:
        body.append(_block(f"자격·준비 리스크: {_plain(evaluation.risk_band)} · {_number(evaluation.risk_score)} / 100"))
    else:
        body.append(_block("자격·준비 리스크: 미산정"))
    intelligence = build_award_intelligence(notice.award_history, as_of=now)
    competition = intelligence["competition_risk"]
    if competition["status"] == "MODEL_ESTIMATE":
        body.append(_block(f"경쟁·집중 리스크: 추정 {_number(competition['score'])} / 100 · 3년 유사공고 표본 {competition['sample_count']}건"))
    else:
        body.append(_block("경쟁·집중 리스크: 표본·사실 부족으로 미산정"))
    body.append(_block(f"시스템 검토 의견: {_plain(detail.recommendation) if current and detail.recommendation else '확인 필요'} · 최종 입찰 판단은 담당자가 결정합니다."))
    for condition in detail.recommendation_conditions[:4] if current else []:
        body.append(_block("• " + _plain(condition, 200)))
    body.append(_block("자격 판정·정량점수·경쟁 리스크는 각각 별도 지표입니다. 전체 항목과 근거는 공고 상세에서 확인해 주세요.", isSubtle=True))
    body.append(_block(f"발송 기준 {now.astimezone(KST):%Y-%m-%d %H:%M} (한국시간) · 관심 등록한 본인에게만 발송", size="Small", isSubtle=True))
    return {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "body": body,
            "actions": [{"type": "Action.OpenUrl", "title": "공고 상세 · 전체 분석 확인", "url": _detail_url(base_url, notice.notice_key)}]}
