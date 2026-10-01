"""Personal notice cards from current, publication-safe stored analysis only."""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from sqlalchemy.orm import Session

from .analysis_pipeline import _select_source_versions
from .api import (
    _curated_public_extraction,
    _detail,
    _pps_authorities_by_notice_id,
    department_manager_go,
    manager_go_qualification,
)
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
# Same wording as the web preview (`RECOMMENDATION_LABELS` in app.js).
RECOMMENDATION_LABELS = {"GO": "적극 검토", "HOLD": "조건부 검토", "NO_GO": "입찰 제외"}
# Teams only offers its own named container colors; these follow the host theme.
QUALIFICATION_STYLES = {"PASS": "good", "REVIEW": "warning", "FAIL": "attention"}
WEEKDAYS = "월화수목금토일"
DETAILS_ID = "pai-analysis-details"


def link_base_url() -> str:
    """The origin a card's buttons open.

    `PAI_TEAMS_PUBLIC_BASE_URL` also names the host in the Teams tab SSO
    audience (`api://<host>/botid-<app id>`), so it must keep matching the Entra
    registration. People sign in to the custom domain instead, and a link to the
    Cloud Run host lands them in a separate, signed-out session. The optional
    `PAI_TEAMS_LINK_BASE_URL` moves only the links.
    """

    return (os.getenv("PAI_TEAMS_LINK_BASE_URL", "").strip()
            or os.getenv("PAI_TEAMS_PUBLIC_BASE_URL", "").strip())


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


def _badge(text: str, style: str) -> dict:
    return {"type": "Column", "width": "auto", "items": [{
        "type": "Container", "style": style, "bleed": False,
        "items": [_block(text, weight="Bolder", size="Small")],
    }]}


def _metric(label: str, value: str) -> dict:
    return {"type": "Column", "width": "stretch", "items": [{
        "type": "Container", "style": "emphasis", "items": [
            _block(label, size="Small", isSubtle=True, spacing="None"),
            _block(value, weight="Bolder", size="Medium", spacing="Small"),
        ],
    }]}


def _deadline_badge(deadline: datetime, now: datetime) -> dict:
    days = (deadline.date() - now.astimezone(KST).date()).days
    date = f"{deadline:%m.%d} ({WEEKDAYS[deadline.weekday()]})"
    if days < 0:
        return _badge(f"마감 · {date}", "emphasis")
    if days == 0:
        return _badge(f"오늘 마감 · {deadline:%H:%M}", "attention")
    return _badge(f"D-{days} · {date}", "warning" if days <= 5 else "accent")


def _detail_url(base_url: str, notice_key: str, *, decision: bool = False) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or parsed.port not in {None, 443}
            or "\\" in base_url or any(ord(char) < 33 for char in base_url)):
        raise ValueError("Teams public base URL must be an HTTPS origin")
    query = {"notice": notice_key, **({"decision": "1"} if decision else {})}
    return f"{base_url.rstrip('/')}/?{urlencode(query)}"


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
                      now: datetime | None = None, base_url: str,
                      department_id: str | None = None) -> dict:
    """No provider calls, score recomputation, or persistence while rendering.

    The visible part mirrors the web "Teams 알림 미리보기": source, title, status
    badges, one reason line and three stored metrics. Every section the plain
    card used to list stays in a collapsed details container.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Load every version first. The authority projection re-selects this notice
    # with only its metadata version; in a freshly loaded notice (the dispatcher
    # renders right after `session.get`) that partial collection would become
    # `notice.versions`, and an analysed notice would render as never analysed.
    notice.versions  # noqa: B018 - populate the full collection
    authority = _pps_authorities_by_notice_id(session, [notice]).get(notice.id)
    detail = _detail(notice, public_view=True, provider_authority=authority, now=now)
    evaluation = detail.latest_evaluation if detail.qualification_status != "NOT_EVALUATED" else None
    current = detail.provider_disposition not in {"CANCELLED", "QUARANTINED"} and detail.status == "OPEN"
    quantitative = _stored_public_quantitative_projection(session, notice) if current else None
    deadline = detail.deadline.astimezone(KST)
    event_label = EVENT_LABELS.get(event_kind, "관심 공고 알림")
    title = _plain(detail.title, 350)

    # 받는 사람 부서가 참여로 판단한 공고는 웹 화면과 같이 참가자격을 충족으로 보낸다.
    qualification_status = manager_go_qualification(
        detail.qualification_status, department_manager_go(session, notice.id, department_id),
    )
    qualification = STATUS_LABELS.get(qualification_status, "미판정") if current else "현재 검토 제외"
    badges = [
        _badge(f"자격 {qualification}", QUALIFICATION_STYLES.get(qualification_status, "emphasis"))
        if current else _badge(qualification, "emphasis"),
        _deadline_badge(deadline, now),
    ]

    if quantitative is None:
        score = "미산정 · 현재 공고 기준으로 검증된 정량점수 기록이 없습니다."
    else:
        status = quantitative.overall_status
        if status == "CONFIRMED" and quantitative.confirmed_points is not None:
            score = f"확정 {_number(quantitative.confirmed_points)} / {_number(quantitative.total_max_points)}점"
        elif status == "ESTIMATED" and quantitative.lower_points is not None and quantitative.upper_points is not None:
            score = f"추정 {_number(quantitative.lower_points)}~{_number(quantitative.upper_points)} / {_number(quantitative.total_max_points)}점 · 확정 아님"
        else:
            score = f"{STATUS_LABELS.get(status, '확인 필요')} · 필요한 증빙과 배점표 확인 후 산정"

    scored = evaluation is not None and current
    readiness = f"{_number(evaluation.readiness_score)} / 100" if scored else "미산정"
    risk = f"{_number(evaluation.risk_score)} / 100" if scored and evaluation.risk_score is not None else "미산정"
    recommendation = (RECOMMENDATION_LABELS.get(detail.recommendation, "확인 필요")
                      if current and detail.recommendation else "확인 필요")

    facts = [
        {"title": "마감", "value": deadline.strftime("%Y-%m-%d %H:%M (한국시간)")},
        {"title": "참가자격", "value": qualification},
        {"title": "정량점수", "value": score},
    ]
    if detail.estimated_amount is not None:
        facts.append({"title": "사업금액", "value": f"{detail.estimated_amount:,.0f}원"})
    facts.append({"title": "첨부 분석", "value": f"{detail.analysis_attachments_accepted}/{detail.analysis_attachment_count} · "
                  + ("현재 첨부 확인 완료" if detail.analysis_attachment_coverage_complete else "미확인 첨부 있음")})

    body = [
        _block(f"PAI · {event_label}", weight="Bolder", color="Accent", size="Small"),
        _block("나라장터 공고", size="Small", isSubtle=True, spacing="None"),
        _block(title, weight="Bolder", size="Large", spacing="Medium"),
        _block(_plain(detail.agency) or "발주기관 미확인", isSubtle=True, spacing="None"),
        {"type": "ColumnSet", "spacing": "Medium", "columns": badges},
        {"type": "Container", "style": "emphasis", "spacing": "Medium",
         "items": [_block(_plain(detail.analysis_reason, 350))]},
        {"type": "ColumnSet", "spacing": "Medium", "columns": [
            _metric("준비도", readiness), _metric("리스크", risk), _metric("입찰 판단", recommendation)]},
        {"type": "FactSet", "spacing": "Medium", "facts": facts},
        {"type": "ActionSet", "actions": [{"type": "Action.ToggleVisibility", "title": "전체 분석 펼치기",
                                           "targetElements": [DETAILS_ID]}]},
    ]

    details: list[dict] = [_block("자격사항 · 확인할 조건", weight="Bolder")]
    groups = _current_requirement_groups(session, notice, now) if current else {}
    requirements = groups.get("ELIGIBILITY", [])
    for condition in requirements[:6]:
        details.append(_block("• " + _plain(condition, 220)))
    if not requirements:
        details.append(_block("검증된 자격조건 요약이 아직 없습니다. 상세 화면에서 분석 상태를 확인해 주세요."))
    if len(requirements) > 6:
        details.append(_block(f"나머지 {len(requirements) - 6}개 조건과 원문 근거는 공고 상세에서 확인할 수 있습니다."))
    for kind, heading in (("ACTION_REQUIRED", "참여 전 처리할 사항"), ("CHECKLIST", "제출·계약 확인사항")):
        conditions = groups.get(kind, [])
        if conditions:
            details.append(_block(heading, weight="Bolder", separator=True))
            details.extend(_block("• " + _plain(condition, 200)) for condition in conditions[:4])
            if len(conditions) > 4:
                details.append(_block(f"나머지 {len(conditions) - 4}개 사항은 공고 상세에서 확인할 수 있습니다."))
    details.append(_block("정량점수", weight="Bolder", separator=True))
    details.append(_block(score))
    if quantitative is not None:
        for criterion in quantitative.criteria[:6]:
            label = _plain(criterion.label, 90)
            if criterion.status == "CONFIRMED" and criterion.estimated_points is not None:
                value = f"확정 {_number(criterion.estimated_points)}점"
            elif criterion.status == "ESTIMATED":
                value = f"추정 {_number(criterion.lower_points)}~{_number(criterion.upper_points)}점"
            else:
                value = "확인 필요"
            details.append(_block(f"• {label}: {value} / 배점 {_number(criterion.max_points)}점"))
        details.append(_block(_plain(quantitative.opinion, 280)))
    details.append(_block("리스크 · 의사결정", weight="Bolder", separator=True))
    if scored:
        band = STATUS_LABELS.get(evaluation.risk_band) or _plain(evaluation.risk_band)
        details.append(_block(f"자격·준비 리스크: {band} · {risk if evaluation.risk_score is not None else '점수 미산정'}"))
    else:
        details.append(_block("자격·준비 리스크: 미산정"))
    intelligence = build_award_intelligence(notice.award_history, as_of=now)
    competition = intelligence["competition_risk"]
    if competition["status"] == "MODEL_ESTIMATE":
        details.append(_block(f"경쟁·집중 리스크: 추정 {_number(competition['score'])} / 100 · 3년 유사공고 표본 {competition['sample_count']}건"))
    else:
        details.append(_block("경쟁·집중 리스크: 표본·사실 부족으로 미산정"))
    details.append(_block(f"시스템 입찰 판단: {recommendation} · 최종 입찰 판단은 담당자가 결정합니다."))
    for condition in detail.recommendation_conditions[:4] if current else []:
        details.append(_block("• " + _plain(condition, 200)))
    details.append(_block("자격 판정·정량점수·경쟁 리스크는 각각 별도 지표입니다. 전체 항목과 근거는 공고 상세에서 확인해 주세요.", isSubtle=True))
    body.append({"type": "Container", "id": DETAILS_ID, "isVisible": False, "separator": True, "items": details})
    body.append(_block(f"발송 기준 {now.astimezone(KST):%Y-%m-%d %H:%M} (한국시간) · 관심 등록한 본인에게만 발송", size="Small", isSubtle=True))

    actions = [{"type": "Action.OpenUrl", "title": "근거 상세보기", "style": "positive",
                "url": _detail_url(base_url, notice.notice_key)}]
    if current:
        actions.append({"type": "Action.OpenUrl", "title": "담당자 판단",
                        "url": _detail_url(base_url, notice.notice_key, decision=True)})
    return {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "msteams": {"width": "Full"},
            # Shown in the Teams notification and in clients that cannot draw the card.
            "fallbackText": f"PAI · {event_label} · {title} · 자격 {qualification} · 마감 {deadline:%Y-%m-%d %H:%M}",
            "body": body, "actions": actions}
