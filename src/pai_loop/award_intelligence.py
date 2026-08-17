from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import hashlib
from math import exp, log
from statistics import mean, median
from typing import Any, Iterable, Mapping


ANALYTICS_VERSION = "award-intelligence-1.0.0"


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _event_date(item: Any) -> datetime | None:
    value = _value(item, "awarded_at") or _value(item, "opened_at")
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _valid_rate(value: Any) -> float | None:
    result = _number(value)
    # A generous validation ceiling catches corrupt units without silently
    # rewriting unusual procurement records.
    return result if result is not None and 0 < result <= 200 else None


def _rate_fact(item: Any) -> tuple[float | None, str]:
    explicit = _valid_rate(_value(item, "award_rate"))
    if explicit is not None:
        return explicit, "EXPLICIT_SOURCE"
    award_amount = _number(_value(item, "award_amount"))
    estimated_price = _number(_value(item, "estimated_price"))
    if award_amount is not None and estimated_price is not None and estimated_price > 0:
        return round(award_amount / estimated_price * 100, 6), "DERIVED_AWARD_AMOUNT_OVER_ESTIMATED_PRICE"
    return None, "UNAVAILABLE"


def _submitted_rate_fact(item: Any) -> tuple[float | None, str]:
    submitted = _number(_value(item, "submitted_bid_price"))
    estimated = _number(_value(item, "estimated_price"))
    if submitted is not None and estimated is not None and estimated > 0:
        return round(submitted / estimated * 100, 6), "DERIVED_SUBMITTED_BID_OVER_ESTIMATED_PRICE"
    return None, "UNAVAILABLE"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    share = position - lower
    return ordered[lower] * (1 - share) + ordered[upper] * share


def _distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "q25": None, "median": None, "mean": None, "q75": None, "max": None}
    return {
        "count": len(values),
        "min": round(min(values), 4),
        "q25": round(_percentile(values, 0.25) or 0, 4),
        "median": round(median(values), 4),
        "mean": round(mean(values), 4),
        "q75": round(_percentile(values, 0.75) or 0, 4),
        "max": round(max(values), 4),
    }


def _prediction(points: list[tuple[float, datetime | None]], as_of: datetime) -> dict[str, Any]:
    if len(points) < 3:
        return {
            "status": "INSUFFICIENT_DATA",
            "confidence": "INSUFFICIENT",
            "sample_count": len(points),
            "center": None,
            "range_low": None,
            "range_high": None,
            "method": "최근 3년 값에 365일 반감기 지수 가중치를 적용",
            "rationale": "유효 표본이 3건 미만이어서 수치 예측을 제시하지 않습니다.",
        }
    weighted: list[tuple[float, float]] = []
    for value, occurred_at in points:
        age_days = max(0.0, (as_of - (occurred_at or as_of)).total_seconds() / 86400)
        weighted.append((value, exp(-log(2) * age_days / 365.0)))
    weight_sum = sum(weight for _, weight in weighted)
    center = sum(value * weight for value, weight in weighted) / weight_sum
    mad = sum(abs(value - center) * weight for value, weight in weighted) / weight_sum
    half_width = max(0.25, 1.5 * mad)
    confidence = "HIGH" if len(points) >= 8 else "MEDIUM" if len(points) >= 5 else "LOW"
    return {
        "status": "MODEL_ESTIMATE",
        "confidence": confidence,
        "sample_count": len(points),
        "center": round(center, 4),
        "range_low": round(max(0.0, center - half_width), 4),
        "range_high": round(center + half_width, 4),
        "method": "최근 3년 값에 365일 반감기 지수 가중치를 적용한 평균 ± 1.5×가중 절대편차",
        "rationale": f"최근 관측값에 더 큰 가중치를 둔 {len(points)}건의 통계적 참고 범위이며 낙찰 또는 점수를 보장하지 않습니다.",
    }


def build_award_intelligence(
    records: Iterable[Any],
    *,
    as_of: datetime | None = None,
    target_estimated_price: float | None = None,
) -> dict[str, Any]:
    """Build read-only intelligence from stored facts; this function performs no I/O."""

    rows = list(records)
    dated = [_event_date(row) for row in rows]
    reference = as_of or max((value for value in dated if value), default=datetime.now(timezone.utc))
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    facts: list[dict[str, Any]] = []
    winners: list[str] = []
    award_points: list[tuple[float, datetime | None]] = []
    submitted_points: list[tuple[float, datetime | None]] = []
    coverage = Counter()
    for row, occurred_at in zip(rows, dated):
        award_rate, award_rate_basis = _rate_fact(row)
        submitted_rate, submitted_rate_basis = _submitted_rate_fact(row)
        participant_count = _number(_value(row, "participant_count"))
        opened_at = _event_date({"opened_at": _value(row, "opened_at")})
        awarded_at = _event_date({"awarded_at": _value(row, "awarded_at")})
        winner = str(_value(row, "winner_name") or "").strip()
        if winner:
            winners.append(winner)
            coverage["winner"] += 1
        for field in ("award_amount", "estimated_price", "submitted_bid_price", "technical_score", "price_score"):
            if _number(_value(row, field)) is not None:
                coverage[field] += 1
        if award_rate is not None:
            coverage["award_rate"] += 1
            award_points.append((award_rate, occurred_at))
        if submitted_rate is not None:
            coverage["submitted_bid_rate"] += 1
            submitted_points.append((submitted_rate, occurred_at))
        public_record_key = hashlib.sha256(
            "|".join((
                str(_value(row, "bid_notice_no") or ""),
                str(_value(row, "title") or ""),
                winner,
                occurred_at.isoformat() if occurred_at else "",
            )).encode("utf-8")
        ).hexdigest()[:20]
        facts.append({
            "id": public_record_key,
            "bid_notice_no": str(_value(row, "bid_notice_no") or ""),
            "title": str(_value(row, "title") or ""),
            "agency": str(_value(row, "agency") or ""),
            "winner_name": winner or None,
            "participant_count": int(participant_count) if participant_count is not None else None,
            "award_amount": _number(_value(row, "award_amount")),
            "estimated_price": _number(_value(row, "estimated_price")),
            "submitted_bid_price": _number(_value(row, "submitted_bid_price")),
            "award_rate": award_rate,
            "award_rate_basis": award_rate_basis,
            "submitted_bid_rate": submitted_rate,
            "submitted_bid_rate_basis": submitted_rate_basis,
            "technical_score": _number(_value(row, "technical_score")),
            "price_score": _number(_value(row, "price_score")),
            "opened_at": opened_at.isoformat() if opened_at else None,
            "awarded_at": awarded_at.isoformat() if awarded_at else None,
            "similarity_score": _number(_value(row, "similarity_score")),
            "source": str(_value(row, "source") or "STORED"),
        })

    counts = Counter(winners)
    total_winners = len(winners)
    ranking = [
        {"winner_name": name, "count": count, "share": round(count / total_winners, 6)}
        for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ] if total_winners else []
    hhi = round(sum((item["share"] * 100) ** 2 for item in ranking), 2) if ranking else None
    concentration = {
        "winner_observations": total_winners,
        "unique_winners": len(counts),
        "repeat_winner_count": sum(1 for count in counts.values() if count > 1),
        "top_winner": ranking[0] if ranking else None,
        "hhi": hhi,
        "hhi_interpretation": None if hhi is None else "HIGH" if hhi > 2500 else "MODERATE" if hhi >= 1500 else "LOW",
        "ranking": ranking,
        "note": "HHI는 저장된 유사 공고 표본 내 수주 빈도 집중도이며 시장 전체 점유율이 아닙니다.",
    }

    award_prediction = _prediction(award_points, reference)
    submitted_prediction = _prediction(submitted_points, reference)
    amount_range = None
    basis = _number(target_estimated_price)
    if basis is not None and basis > 0 and award_prediction["status"] == "MODEL_ESTIMATE":
        amount_range = {
            "basis_amount": basis,
            "basis_kind": "NOTICE_ESTIMATED_AMOUNT",
            "low": round(basis * award_prediction["range_low"] / 100, 2),
            "center": round(basis * award_prediction["center"] / 100, 2),
            "high": round(basis * award_prediction["range_high"] / 100, 2),
        }

    warnings = [
        "제목 유사 후보는 동일 과업·동일 평가방식의 확정 비교군이 아닙니다.",
        "MODEL_ESTIMATE는 의사결정 참고치이며 낙찰·투찰가·기술점수·가격점수를 보장하지 않습니다.",
        "누락 필드는 다른 값으로 추정하거나 사실처럼 채우지 않았습니다.",
    ]
    if coverage["submitted_bid_price"] == 0:
        warnings.append("저장 이력에 명시적 투찰금액이 없어 투찰률 예측은 제공하지 않습니다.")
    if coverage["technical_score"] == 0 or coverage["price_score"] == 0:
        warnings.append("기술점수 또는 가격점수가 저장되지 않아 점수 비교를 제공하지 않습니다.")
    if basis is None or basis <= 0:
        warnings.append("대상 공고의 기준/추정금액이 없어 금액 예측 범위를 계산하지 않았습니다.")

    return {
        "analytics_version": ANALYTICS_VERSION,
        "boundary": "STORED_HISTORY_ONLY",
        "generated_as_of": reference.isoformat(),
        "record_count": len(rows),
        "records": facts,
        "field_coverage": {field: {"available": coverage[field], "total": len(rows)} for field in (
            "winner", "award_amount", "estimated_price", "submitted_bid_price", "award_rate",
            "submitted_bid_rate", "technical_score", "price_score",
        )},
        "concentration": concentration,
        "award_rate_distribution": _distribution([value for value, _ in award_points]),
        "submitted_bid_rate_distribution": _distribution([value for value, _ in submitted_points]),
        "prediction": {
            "award_rate": award_prediction,
            "submitted_bid_rate": submitted_prediction,
            "award_amount_range": amount_range,
        },
        "warnings": warnings,
    }
