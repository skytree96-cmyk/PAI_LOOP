from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import hashlib
from math import exp, log
from statistics import mean, median
from typing import Any, Iterable, Mapping


ANALYTICS_VERSION = "award-intelligence-1.1.0"
COMPETITION_RISK_VERSION = "competition-risk-1.0.0"


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


def _participant_count(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 1 or number > 1_000_000 or not number.is_integer():
        return None
    return int(number)


def _similarity_score(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _event_date(item: Any) -> datetime | None:
    value = _value(item, "awarded_at") or _value(item, "opened_at")
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _minus_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


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


def _piecewise_score(value: float, anchors: tuple[tuple[float, float], ...]) -> float:
    if value <= anchors[0][0]:
        return anchors[0][1]
    for (left_value, left_score), (right_value, right_score) in zip(anchors, anchors[1:]):
        if value <= right_value:
            span = right_value - left_value
            if span <= 0:
                return right_score
            ratio = (value - left_value) / span
            return left_score + ratio * (right_score - left_score)
    return anchors[-1][1]


def _competition_risk(
    *,
    record_count: int,
    winner_observations: int,
    ranking: list[dict[str, Any]],
    hhi: float | None,
    participant_counts: list[int],
    event_date_observations: int,
    similarity_scores: list[float],
    duplicate_record_keys: int,
) -> dict[str, Any]:
    """Estimate sample competition risk without claiming a legal market monopoly."""

    winner_pct = round(100 * winner_observations / record_count, 2) if record_count else 0.0
    participant_pct = round(100 * len(participant_counts) / record_count, 2) if record_count else 0.0
    event_date_pct = round(100 * event_date_observations / record_count, 2) if record_count else 0.0
    similarity_pct = round(100 * len(similarity_scores) / record_count, 2) if record_count else 0.0
    requirements = {
        "records": record_count >= 5,
        "winner": winner_observations >= 5 and winner_pct >= 60,
        "participant_count": len(participant_counts) >= 5 and participant_pct >= 60,
        "event_date": event_date_observations >= 5 and event_date_pct >= 80,
        "unique_records": duplicate_record_keys == 0,
    }
    sufficient = all(requirements.values())
    top_share_pct = round(ranking[0]["share"] * 100, 2) if ranking else None
    participant_median = round(float(median(participant_counts)), 2) if participant_counts else None
    participant_mean = round(float(mean(participant_counts)), 2) if participant_counts else None
    similarity_median = round(float(median(similarity_scores)), 2) if similarity_scores else None
    single_share_pct = (
        round(100 * sum(value == 1 for value in participant_counts) / len(participant_counts), 2)
        if participant_counts
        else None
    )

    hhi_risk = None if hhi is None else round(_piecewise_score(hhi, (
        (0, 0), (1_500, 30), (2_500, 60), (10_000, 100),
    )), 2)
    top_share_risk = None if top_share_pct is None else round(_piecewise_score(top_share_pct, (
        (0, 0), (20, 0), (80, 100), (100, 100),
    )), 2)
    participant_risk = (
        None
        if participant_median is None
        else round(max(0.0, min(100.0, (6 - participant_median) * 20)), 2)
    )

    components = {
        "hhi": {
            "value": hhi,
            "unit": "0-10000",
            "risk_score": hhi_risk,
            "weight": 0.45,
            "source_status": "DERIVED_FROM_STORED_FACTS" if hhi is not None else "UNAVAILABLE",
            "facts": {"winner_observations": winner_observations, "unique_winners": len(ranking)},
            "rationale": "저장된 유사 후보의 낙찰 빈도 비율 제곱합이며 전체 시장 HHI가 아닙니다.",
        },
        "top_winner_share": {
            "value": top_share_pct,
            "unit": "percent",
            "risk_score": top_share_risk,
            "weight": 0.3,
            "source_status": "DERIVED_FROM_STORED_FACTS" if top_share_pct is not None else "UNAVAILABLE",
            "facts": {"winner_observations": winner_observations},
            "rationale": "저장 표본에서 최다 수주기관이 차지한 관측 비율입니다.",
        },
        "participant_count": {
            "value": participant_median,
            "unit": "organizations_median",
            "risk_score": participant_risk,
            "weight": 0.25,
            "source_status": "DERIVED_FROM_STORED_FACTS" if participant_median is not None else "UNAVAILABLE",
            "facts": {
                "available": len(participant_counts),
                "mean": participant_mean,
                "single_participant_share_pct": single_share_pct,
            },
            "rationale": "참여기관 중앙값 6곳 이상을 0점, 1곳을 100점으로 선형 변환했습니다.",
        },
        "candidate_similarity": {
            "value": similarity_median,
            "unit": "percent_median",
            "risk_score": None,
            "weight": 0,
            "source_status": "DERIVED_FROM_STORED_FACTS" if similarity_median is not None else "UNAVAILABLE",
            "facts": {"available": len(similarity_scores)},
            "rationale": "제목 유사도는 리스크 점수에 섞지 않고 신뢰도 상한에만 사용합니다.",
        },
    }
    coverage = {
        "records_total": record_count,
        "minimum_records": 5,
        "winner": {
            "available": winner_observations,
            "total": record_count,
            "pct": winner_pct,
            "minimum_available": 5,
            "minimum_pct": 60,
            "sufficient": requirements["winner"],
        },
        "participant_count": {
            "available": len(participant_counts),
            "total": record_count,
            "pct": participant_pct,
            "minimum_available": 5,
            "minimum_pct": 60,
            "sufficient": requirements["participant_count"],
        },
        "event_date": {
            "available": event_date_observations,
            "total": record_count,
            "pct": event_date_pct,
            "minimum_available": 5,
            "minimum_pct": 80,
            "sufficient": requirements["event_date"],
        },
        "similarity_score": {
            "available": len(similarity_scores),
            "total": record_count,
            "pct": similarity_pct,
            "minimum_available": 0,
            "minimum_pct": 0,
            "sufficient": bool(similarity_scores),
        },
        "duplicate_record_keys": duplicate_record_keys,
        "sufficient": sufficient,
    }
    warnings = [
        "이 점수는 제목 유사 후보 표본의 경쟁 집중 신호이며 법적 독점·시장지배력 또는 전체 시장점유율 판정이 아닙니다.",
        "참가자격 risk_band 및 담당자 GO/NO-GO와 분리된 MODEL_ESTIMATE입니다.",
    ]
    if not requirements["records"]:
        warnings.append("저장 후보가 5건 미만이어서 경쟁·집중 리스크 점수를 제시하지 않습니다.")
    if not requirements["winner"]:
        warnings.append("낙찰자 사실이 5건 또는 전체의 60%에 미달해 점수를 제시하지 않습니다.")
    if not requirements["participant_count"]:
        warnings.append("참여기관 수 사실이 5건 또는 전체의 60%에 미달해 점수를 제시하지 않습니다.")
    if not requirements["event_date"]:
        warnings.append("개찰·낙찰 일자 사실이 5건 또는 전체의 80%에 미달해 3년 표본 점수를 제시하지 않습니다.")
    if not requirements["unique_records"]:
        warnings.append("동일 공개 레코드 키가 중복되어 표본 빈도를 신뢰할 수 없습니다.")

    base = {
        "method_version": COMPETITION_RISK_VERSION,
        "scope": "STORED_3Y_SIMILARITY_CANDIDATES",
        "confidence_basis": "INPUT_COMPLETENESS_AND_CANDIDATE_RELEVANCE_ONLY",
        "market_claim": "NOT_DETERMINED",
        "sample_count": record_count,
        "components": components,
        "coverage": coverage,
        "method": "HHI 45% + 상위 수주 비중 30% + 참여기관 수 중앙값 25%",
        "warnings": warnings,
        "separation_notice": "경쟁·집중 리스크는 참가자격 risk_band 및 최종 입찰결정과 별도입니다.",
    }

    component_scores = (hhi_risk, top_share_risk, participant_risk)
    if not sufficient or any(value is None for value in component_scores):
        return {
            **base,
            "status": "UNKNOWN",
            "score": None,
            "band": "UNKNOWN",
            "confidence": "INSUFFICIENT",
            "rationale": "필수 사실 커버리지가 기준에 미달해 수치 리스크를 보류했습니다.",
        }

    score = round(0.45 * hhi_risk + 0.3 * top_share_risk + 0.25 * participant_risk, 2)
    band = "VERY_HIGH" if score >= 75 else "HIGH" if score >= 50 else "MODERATE" if score >= 25 else "LOW"
    minimum_coverage = min(winner_pct, participant_pct, event_date_pct)
    minimum_count = min(winner_observations, len(participant_counts), event_date_observations)
    confidence = (
        "HIGH"
        if minimum_count >= 20 and minimum_coverage >= 90
        else "MEDIUM"
        if minimum_count >= 10 and minimum_coverage >= 80
        else "LOW"
    )
    if similarity_median is None:
        confidence = "LOW"
        warnings.append("후보 제목유사도 사실이 없어 신뢰도를 LOW로 제한했습니다.")
    elif similarity_median < 40:
        confidence = "LOW"
        warnings.append("후보 제목유사도 중앙값이 40% 미만이어서 신뢰도를 LOW로 제한했습니다.")
    elif similarity_median < 70 and confidence == "HIGH":
        confidence = "MEDIUM"
        warnings.append("후보 제목유사도 중앙값이 70% 미만이어서 신뢰도를 MEDIUM으로 제한했습니다.")
    if winner_pct < 100 or participant_pct < 100:
        warnings.append("일부 후보의 낙찰자 또는 참여기관 수가 없어 관측 가능한 행만 계산했습니다.")
    return {
        **base,
        "status": "MODEL_ESTIMATE",
        "score": score,
        "band": band,
        "confidence": confidence,
        "rationale": f"저장 후보 {record_count}건의 수주 집중과 참여기관 수를 0~100 탐색 점수로 변환했습니다.",
    }


def build_award_intelligence(
    records: Iterable[Any],
    *,
    as_of: datetime | None = None,
    target_estimated_price: float | None = None,
) -> dict[str, Any]:
    """Build read-only intelligence from stored facts; this function performs no I/O."""

    source_rows = list(records)
    source_dates = [_event_date(row) for row in source_rows]
    reference = as_of or max((value for value in source_dates if value), default=datetime.now(timezone.utc))
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff = _minus_years(reference.date(), 3)
    retained = [
        (row, occurred_at)
        for row, occurred_at in zip(source_rows, source_dates)
        if occurred_at is None or cutoff <= occurred_at.date() <= reference.date()
    ]
    rows = [row for row, _ in retained]
    dated = [occurred_at for _, occurred_at in retained]

    facts: list[dict[str, Any]] = []
    winners: list[str] = []
    participant_counts: list[int] = []
    similarity_scores: list[float] = []
    public_record_keys: list[str] = []
    award_points: list[tuple[float, datetime | None]] = []
    submitted_points: list[tuple[float, datetime | None]] = []
    coverage = Counter()
    for row, occurred_at in zip(rows, dated):
        award_rate, award_rate_basis = _rate_fact(row)
        submitted_rate, submitted_rate_basis = _submitted_rate_fact(row)
        participant_count = _participant_count(_value(row, "participant_count"))
        similarity_score = _similarity_score(_value(row, "similarity_score"))
        opened_at = _event_date({"opened_at": _value(row, "opened_at")})
        awarded_at = _event_date({"awarded_at": _value(row, "awarded_at")})
        winner = str(_value(row, "winner_name") or "").strip()
        if winner:
            winners.append(winner)
            coverage["winner"] += 1
        if participant_count is not None:
            participant_counts.append(participant_count)
            coverage["participant_count"] += 1
        if occurred_at is not None:
            coverage["event_date"] += 1
        if similarity_score is not None:
            similarity_scores.append(similarity_score)
            coverage["similarity_score"] += 1
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
        public_record_keys.append(public_record_key)
        facts.append({
            "id": public_record_key,
            "bid_notice_no": str(_value(row, "bid_notice_no") or ""),
            "title": str(_value(row, "title") or ""),
            "agency": str(_value(row, "agency") or ""),
            "winner_name": winner or None,
            "participant_count": participant_count,
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
            "similarity_score": similarity_score,
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
    competition_risk = _competition_risk(
        record_count=len(rows),
        winner_observations=total_winners,
        ranking=ranking,
        hhi=hhi,
        participant_counts=participant_counts,
        event_date_observations=coverage["event_date"],
        similarity_scores=similarity_scores,
        duplicate_record_keys=len(public_record_keys) - len(set(public_record_keys)),
    )

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
    if coverage["event_date"] < len(rows):
        warnings.append("일자 미확인 후보는 표시하되 3년 창 충족을 가정하지 않고 커버리지 문턱에 반영했습니다.")
    if competition_risk["status"] == "UNKNOWN":
        warnings.append("경쟁·집중 리스크는 필수 사실 커버리지 부족으로 UNKNOWN입니다.")

    return {
        "analytics_version": ANALYTICS_VERSION,
        "boundary": "STORED_HISTORY_ONLY",
        "generated_as_of": reference.isoformat(),
        "candidate_window": {
            "from": cutoff.isoformat(),
            "to": reference.date().isoformat(),
            "years": 3,
            "undated_policy": "KEPT_BUT_COVERAGE_GATED",
        },
        "record_count": len(rows),
        "records": facts,
        "field_coverage": {field: {"available": coverage[field], "total": len(rows)} for field in (
            "winner", "participant_count", "event_date", "similarity_score", "award_amount", "estimated_price", "submitted_bid_price", "award_rate",
            "submitted_bid_rate", "technical_score", "price_score",
        )},
        "concentration": concentration,
        "competition_risk": competition_risk,
        "award_rate_distribution": _distribution([value for value, _ in award_points]),
        "submitted_bid_rate_distribution": _distribution([value for value, _ in submitted_points]),
        "prediction": {
            "award_rate": award_prediction,
            "submitted_bid_rate": submitted_prediction,
            "award_amount_range": amount_range,
        },
        "warnings": warnings,
    }
