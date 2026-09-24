"""Adaptive Card for the personal daily briefing.

Rendering reads stored analysis only: no provider call, no rescoring, no write.
The card never carries raw source payloads, evidence values or provider tokens,
and every number it prints comes from the same stored briefing the web shows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from .daily_operations import daily_briefing
from .teams_cards import _detail_url, _plain

KST = timezone(timedelta(hours=9), "Asia/Seoul")
BRIEFING_DAYS = 7
BRIEFING_LIMIT = 6
MAX_TITLE = 180
MAX_AGENCY = 120
MAX_DEPARTMENT = 220

ELIGIBILITY_LABELS = {
    "PASS": "적격",
    "REVIEW": "확인 필요",
    "FAIL": "부적격",
    "PENDING": "미판정",
    "NOT_EVALUATED": "미판정",
}


def _won(value: object) -> str:
    try:
        return f"{float(value):,.0f}원"
    except (TypeError, ValueError):
        return "미확인"


def _deadline(value: object) -> str:
    if not isinstance(value, datetime):
        return "미확인"
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _risk(fit: dict[str, Any]) -> str:
    band = fit.get("risk_band") or "UNKNOWN"
    if band == "UNKNOWN":
        return "표본·사실 부족으로 미산정"
    return {"LOW": "낮음", "MEDIUM": "중간", "HIGH": "높음"}.get(band, band)


def _departments(item: dict[str, Any]) -> str:
    names = [
        str(entry.get("department") or entry.get("department_id") or "")
        for entry in (item.get("top_departments") or [])
    ]
    return ", ".join(name for name in names if name) or "미배정"


def _notice_block(item: dict[str, Any], index: int, base_url: str) -> list[dict]:
    fit = item.get("fit") or {}
    eligibility = ELIGIBILITY_LABELS.get(fit.get("eligibility"), "미판정")
    facts = [
        {"title": "발주기관", "value": _plain(item.get("agency"), MAX_AGENCY) or "미확인"},
        {"title": "마감", "value": _deadline(item.get("deadline"))},
        {"title": "사업금액", "value": _won(item.get("estimated_amount"))},
        {"title": "참가자격", "value": eligibility},
        {"title": "리스크", "value": _risk(fit)},
        {"title": "추천 부서", "value": _plain(_departments(item), MAX_DEPARTMENT)},
    ]
    return [
        {
            "type": "TextBlock",
            "text": f"{index}. " + (_plain(item.get("title"), MAX_TITLE) or "제목 미확인"),
            "wrap": True,
            "weight": "Bolder",
            "separator": True,
        },
        {"type": "FactSet", "facts": facts},
    ]


def build_briefing_card(
    session: Session,
    *,
    kind: str,
    department_id: str | None,
    base_url: str,
    now: datetime | None = None,
) -> dict:
    """Build one briefing card scoped to a department.

    ``kind`` only changes what the card claims about its own basis. A DAILY card
    is sent after the day's analysis is confirmed ready; a SUBSCRIBED ping is
    sent on pairing without that gate, so it says it is a snapshot instead of
    presenting itself as the settled view of the day.
    """

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    briefing = daily_briefing(
        session=session,
        days=BRIEFING_DAYS,
        limit=BRIEFING_LIMIT,
        as_of=now,
        department_id=department_id,
    )
    items = [item for item in (briefing.get("notices") or []) if isinstance(item, dict)]
    totals = briefing.get("totals") or {}
    observed = totals.get("observed", len(items))

    heading = "PAI · 오늘의 공고 브리핑" if kind == "DAILY" else "PAI · 지금 기준 공고 브리핑"
    basis = (
        f"최근 {BRIEFING_DAYS}일 관측 {observed}건 중 {len(items)}건"
        if kind == "DAILY"
        else f"연결 시점 기준입니다. 최근 {BRIEFING_DAYS}일 관측 {observed}건 중 {len(items)}건"
    )

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": heading,
            "wrap": True,
            "weight": "Bolder",
            "size": "Medium",
            "color": "Accent",
        },
        {"type": "TextBlock", "text": basis, "wrap": True, "isSubtle": True},
    ]
    if not items:
        body.append(
            {
                "type": "TextBlock",
                "text": "지금 기준으로 전달할 공고가 없습니다.",
                "wrap": True,
            }
        )
    for index, item in enumerate(items, start=1):
        body.extend(_notice_block(item, index, base_url))
    body.append(
        {
            "type": "TextBlock",
            "text": "판단 보조 정보입니다. 최종 입찰 판단은 담당자가 근거를 확인해 결정합니다.",
            "wrap": True,
            "isSubtle": True,
            "size": "Small",
            "spacing": "Large",
        }
    )

    actions = []
    first_key = items[0].get("notice_key") if items else None
    if first_key:
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "웹에서 근거 확인",
                "url": _detail_url(base_url, str(first_key)),
            }
        )
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }
