"""Adaptive Card for the personal daily briefing.

Rendering reads stored analysis only: no provider call, no rescoring, no write.
The card never carries raw source payloads, evidence values or provider tokens,
and every number it prints comes from the same stored briefing the web shows.

Several notices share one card, so each is a compact tappable row in the same
visual language as the single-notice alert (``teams_cards``): title, one line
of agency and amount, and badges. Evidence stays behind the detail link.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from .daily_operations import daily_briefing
from .teams_cards import RECOMMENDATION_LABELS, _badge, _block, _deadline_badge, _detail_url, _number, _plain

KST = timezone(timedelta(hours=9), "Asia/Seoul")
BRIEFING_DAYS = 7
BRIEFING_LIMIT = 6
MAX_TITLE = 180
MAX_AGENCY = 60
MAX_DEPARTMENT = 40

ELIGIBILITY_LABELS = {
    "PASS": "적격",
    "REVIEW": "확인 필요",
    "FAIL": "부적격",
    "PENDING": "미판정",
    "NOT_EVALUATED": "미판정",
}
ELIGIBILITY_STYLES = {"PASS": "good", "REVIEW": "warning", "FAIL": "attention"}
# The stored risk band is the evaluator's recommendation band (GO / CONDITIONAL_GO /
# NO_GO / UNKNOWN), so it is printed with the same labels as the web.
RISK_BAND_LABELS = {**RECOMMENDATION_LABELS, "CONDITIONAL_GO": "조건부 GO"}


def _won(value: object) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "금액 미확인"
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:,.1f}".rstrip("0").rstrip(".") + "억원"
    if amount >= 10_000:
        return f"{amount / 10_000:,.0f}만원"
    return f"{amount:,.0f}원"


def _deadline(value: object, now: datetime) -> dict:
    if not isinstance(value, datetime):
        return _badge("마감 미확인", "emphasis")
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return _deadline_badge(moment.astimezone(KST), now)


def _department(item: dict[str, Any]) -> str | None:
    for entry in item.get("top_departments") or []:
        name = str(entry.get("department_name") or entry.get("department_id") or "").strip()
        if name:
            return _plain(name, MAX_DEPARTMENT)
    return None


def _notice_row(item: dict[str, Any], base_url: str, now: datetime) -> dict:
    fit = item.get("fit") or {}
    eligibility = fit.get("eligibility")
    agency = _plain(item.get("agency"), MAX_AGENCY) or "발주기관 미확인"
    signals = []
    band = RISK_BAND_LABELS.get(fit.get("risk_band"))
    if band:
        signals.append(f"추천 {band}")
    if fit.get("readiness_score") is not None:
        signals.append(f"준비도 {_number(fit.get('readiness_score'))}")
    department = _department(item)
    signals.append(f"담당 {department}" if department else "담당 미배정")
    row = {
        "type": "Container",
        "separator": True,
        "spacing": "Medium",
        "items": [
            _block(_plain(item.get("title"), MAX_TITLE) or "제목 미확인", weight="Bolder", maxLines=2),
            _block(f"{agency} · {_won(item.get('estimated_amount'))}", size="Small", isSubtle=True, spacing="None"),
            {"type": "ColumnSet", "spacing": "Small", "columns": [
                _badge(ELIGIBILITY_LABELS.get(eligibility, "미판정"), ELIGIBILITY_STYLES.get(eligibility, "emphasis")),
                _deadline(item.get("deadline"), now),
                {"type": "Column", "width": "stretch", "verticalContentAlignment": "Center",
                 "items": [_block(" · ".join(signals), size="Small", isSubtle=True)]},
            ]},
        ],
    }
    key = item.get("notice_key")
    if key:
        row["selectAction"] = {"type": "Action.OpenUrl", "title": "공고 상세", "url": _detail_url(base_url, str(key))}
    return row


def _summary(items: list[dict[str, Any]]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        status = (item.get("fit") or {}).get("eligibility")
        label = ELIGIBILITY_LABELS.get(status, "미판정")
        counts[label] = counts.get(label, 0) + 1
    columns = []
    for status in ("PASS", "REVIEW", "FAIL", "PENDING"):
        label = ELIGIBILITY_LABELS[status]
        if counts.get(label):
            columns.append(_badge(f"{label} {counts[label]}", ELIGIBILITY_STYLES.get(status, "emphasis")))
    return {"type": "ColumnSet", "spacing": "Small", "columns": columns}


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
        _block(heading, weight="Bolder", size="Medium", color="Accent"),
        _block(f"{now.astimezone(KST):%Y-%m-%d} · {basis}", size="Small", isSubtle=True, spacing="None"),
    ]
    if items:
        body.append(_summary(items))
    else:
        body.append(
            {"type": "Container", "style": "emphasis", "spacing": "Medium",
             "items": [_block("지금 기준으로 전달할 공고가 없습니다.")]}
        )
    body.extend(_notice_row(item, base_url, now) for item in items)
    body.append(
        _block(
            "공고를 누르면 상세 근거가 열립니다. 판단 보조 정보이며 최종 입찰 판단은 담당자가 근거를 확인해 결정합니다.",
            isSubtle=True, size="Small", spacing="Large", separator=True,
        )
    )

    actions = []
    first_key = items[0].get("notice_key") if items else None
    if first_key:
        detail = _detail_url(base_url, str(first_key))
        actions.append({"type": "Action.OpenUrl", "title": "PAI에서 전체 공고 보기", "style": "positive",
                        "url": detail.split("?", 1)[0]})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "fallbackText": f"{heading} · {basis}",
        "body": body,
        "actions": actions,
    }
