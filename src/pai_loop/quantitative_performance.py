from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PerformanceQuantModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PerformanceRecognitionScope(PerformanceQuantModel):
    metric_key: Literal["company.performance.amount", "company.performance.count"]
    lookback_years: int = Field(ge=1, le=10)
    similarity_keywords: tuple[str, ...] = Field(min_length=1, max_length=12)
    match_mode: Literal["ALL", "ANY"] = "ALL"
    minimum_single_contract_amount_krw: int = Field(default=0, ge=0)
    vat_basis: Literal["INCLUDED", "EXCLUDED"]
    completion_required: bool
    aggregation: Literal["SUM_AMOUNT", "COUNT"]
    consortium_share_rule: Literal["APPLY_SHARE", "FULL_AMOUNT", "UNSPECIFIED"]
    certificate_required: bool = False
    source_literal: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_metric_aggregation(self) -> "PerformanceRecognitionScope":
        expected = (
            "SUM_AMOUNT"
            if self.metric_key == "company.performance.amount"
            else "COUNT"
        )
        if self.aggregation != expected:
            raise ValueError("performance aggregation does not match metric_key")
        return self


class DerivedPerformanceValue(PerformanceQuantModel):
    status: Literal["CONFIRMED", "ESTIMATED", "UNSCORABLE", "REVIEW"]
    value: float | None = None
    lower_value: float | None = None
    upper_value: float | None = None
    evidence_reference: str | None = None
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    matched_record_keys: tuple[str, ...] = ()
    rationale: str


_AMOUNT_SCALES = {
    "원": Decimal("1"),
    "천": Decimal("1000"),
    "천원": Decimal("1000"),
    "만": Decimal("10000"),
    "만원": Decimal("10000"),
    "백만": Decimal("1000000"),
    "백만원": Decimal("1000000"),
    "천만": Decimal("10000000"),
    "천만원": Decimal("10000000"),
    "억": Decimal("100000000"),
    "억원": Decimal("100000000"),
}
_LOOKBACK_RE = re.compile(r"최근\s*(?P<years>\d{1,2})\s*(?:개)?년")
_MINIMUM_RE = re.compile(
    r"(?:단일\s*계약|건당|1\s*건당)[^\d]{0,30}"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>천만원|천만|백만원|백만|억원|억|만원|만|천원|천|원)\s*이상"
)
_SCOPE_RE = re.compile(
    r"(?P<scope>[A-Za-z0-9가-힣][A-Za-z0-9가-힣·/+\-\s]{1,80}?)\s*"
    r"(?:관련|분야의?)\s*(?:유사\s*)?(?:사업|용역|교육|컨설팅)?"
)
_GENERIC_SCOPE_WORDS = {
    "공고일",
    "공고일자",
    "입찰공고일",
    "기준",
    "현재",
    "이내",
    "동안",
    "최근",
    "해당",
    "동일",
    "업무",
    "사업",
    "용역",
    "유사",
    "관련",
    "분야",
    "수행실적",
    "실적",
    "정량평가",
}


def _normalize_keyword(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _scope_keywords(literal: str) -> tuple[str, ...]:
    match = _SCOPE_RE.search(literal)
    if match is None:
        explicit = re.search(
            r"(?:유사사업|유사용역)\s*[:：]\s*(?P<scope>[^,;]{2,100})",
            literal,
        )
        if explicit is None:
            return ()
        raw = explicit.group("scope")
    else:
        raw = match.group("scope")
    parts = [
        _normalize_keyword(item)
        for item in re.split(r"[·/,]|\s+", raw)
        if _normalize_keyword(item)
    ]
    filtered = [
        item
        for item in parts
        if item not in _GENERIC_SCOPE_WORDS
        and not item.isdigit()
        and not re.fullmatch(r"\d{1,2}(?:개)?년(?:간)?", item)
        and 1 < len(item) <= 30
    ]
    # Preserve order while preventing one model-produced duplicate from
    # changing the binding digest.
    return tuple(dict.fromkeys(filtered[-12:]))


def parse_performance_recognition_scope(
    literal: str,
    *,
    metric_key: str,
) -> PerformanceRecognitionScope | None:
    """Parse only explicit recognition dimensions from a source-bound rule.

    A generic phrase such as "최근 실적" is insufficient.  Automatic scoring
    requires an explicit lookback, similarity scope, completion rule, and VAT
    basis.  This is intentionally narrower than natural-language inference.
    """

    if metric_key not in {"company.performance.amount", "company.performance.count"}:
        return None
    text = re.sub(r"\s+", " ", literal).strip()
    lookback = _LOOKBACK_RE.search(text)
    keywords = _scope_keywords(text)
    if lookback is None or not keywords:
        return None
    if re.search(
        r"(?:VAT|부가(?:가치)?세)\s*(?:제외|별도|미포함)",
        text,
        re.IGNORECASE,
    ):
        vat_basis: Literal["INCLUDED", "EXCLUDED"] = "EXCLUDED"
    elif re.search(r"(?:VAT|부가(?:가치)?세)\s*포함", text, re.IGNORECASE):
        vat_basis = "INCLUDED"
    else:
        return None
    if re.search(r"(?:이행|수행|계약)(?:이|가)?\s*완료(?:된|한)?\s*실적", text) or re.search(
        r"완료(?:된|한)?\s*실적",
        text,
    ):
        completion_required = True
    elif re.search(r"완료\s*여부\s*무관", text):
        completion_required = False
    else:
        return None

    minimum = 0
    if minimum_match := _MINIMUM_RE.search(text):
        try:
            raw_amount = Decimal(minimum_match.group("amount").replace(",", ""))
            minimum = int(
                (raw_amount * _AMOUNT_SCALES[minimum_match.group("unit")]).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )
        except (InvalidOperation, KeyError):
            return None
    if re.search(r"(?:공동수급|공동도급|컨소시엄).{0,40}지분(?:율)?\s*(?:적용|반영)", text):
        share_rule: Literal["APPLY_SHARE", "FULL_AMOUNT", "UNSPECIFIED"] = "APPLY_SHARE"
    elif re.search(r"(?:공동수급|공동도급|컨소시엄).{0,40}(?:전체|전액)\s*인정", text):
        share_rule = "FULL_AMOUNT"
    else:
        share_rule = "UNSPECIFIED"

    return PerformanceRecognitionScope(
        metric_key=metric_key,
        lookback_years=int(lookback.group("years")),
        similarity_keywords=keywords,
        minimum_single_contract_amount_krw=minimum,
        vat_basis=vat_basis,
        completion_required=completion_required,
        aggregation=(
            "SUM_AMOUNT"
            if metric_key == "company.performance.amount"
            else "COUNT"
        ),
        consortium_share_rule=share_rule,
        certificate_required=bool(re.search(r"실적\s*증명(?:서|원)", text)),
        source_literal=text,
    )


def _date_years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _record_text(record: Any) -> str:
    return " ".join(
        [
            str(getattr(record, "project_name", "") or ""),
            str(getattr(record, "overview", "") or ""),
            " ".join(getattr(record, "keywords", None) or []),
        ]
    ).casefold()


def _record_digest_payload(record: Any) -> dict[str, Any]:
    def date_value(value: Any) -> str | None:
        return value.isoformat() if isinstance(value, (date, datetime)) else None

    return {
        "record_key": str(getattr(record, "record_key", "")),
        "revision": int(getattr(record, "revision", 0) or 0),
        "project_name": str(getattr(record, "project_name", "")),
        "contract_date": date_value(getattr(record, "contract_date", None)),
        "start_date": date_value(getattr(record, "start_date", None)),
        "end_date": date_value(getattr(record, "end_date", None)),
        "contract_amount": getattr(record, "contract_amount", None),
        "vat_basis": str(getattr(record, "vat_basis", "")),
        "completed": bool(getattr(record, "completed", False)),
        "share_pct": getattr(record, "share_pct", None),
        "certificate_status": str(getattr(record, "certificate_status", "")),
        "evidence_reference": str(getattr(record, "evidence_reference", "") or ""),
        "keywords": list(getattr(record, "keywords", None) or []),
    }


def derive_performance_value(
    scope: PerformanceRecognitionScope,
    records: Iterable[Any],
    *,
    as_of: datetime,
) -> DerivedPerformanceValue:
    deadline = as_of.date()
    start = _date_years_before(deadline, scope.lookback_years)
    matched: list[Any] = []
    excluded_uncertain: list[str] = []
    for record in records:
        if str(getattr(record, "record_status", "")).upper() != "VALIDATED":
            continue
        raw_record_key = getattr(record, "record_key", None)
        revision = getattr(record, "revision", None)
        if (
            not isinstance(raw_record_key, str)
            or not raw_record_key.strip()
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            excluded_uncertain.append("UNKNOWN")
            continue
        record_key = raw_record_key.strip()
        keywords = getattr(record, "keywords", None)
        if not isinstance(keywords, (list, tuple)) or any(
            not isinstance(item, str) for item in keywords
        ):
            excluded_uncertain.append(record_key)
            continue
        haystack = _record_text(record)
        checks = [keyword.casefold() in haystack for keyword in scope.similarity_keywords]
        if not (all(checks) if scope.match_mode == "ALL" else any(checks)):
            continue
        completion_date = getattr(record, "end_date", None)
        contract_date = getattr(record, "contract_date", None)
        basis_date = completion_date if scope.completion_required else (contract_date or completion_date)
        # ``datetime`` subclasses ``date`` but comparing the two raises in
        # modern Python.  The persisted register contract stores a calendar
        # date, so reject a malformed datetime instead of crashing the daily
        # scoring path or guessing at a timezone conversion.
        if not isinstance(basis_date, date) or isinstance(basis_date, datetime):
            excluded_uncertain.append(record_key)
            continue
        if not start <= basis_date <= deadline:
            continue
        if scope.completion_required and not bool(getattr(record, "completed", False)):
            continue
        if not str(getattr(record, "evidence_reference", "") or "").strip():
            excluded_uncertain.append(record_key)
            continue
        if str(getattr(record, "vat_basis", "")).upper() != scope.vat_basis:
            excluded_uncertain.append(record_key)
            continue
        if scope.certificate_required and str(
            getattr(record, "certificate_status", "")
        ).upper() not in {"ISSUED", "VERIFIED", "AVAILABLE"}:
            excluded_uncertain.append(record_key)
            continue
        amount = getattr(record, "contract_amount", None)
        amount_required = (
            scope.aggregation == "SUM_AMOUNT"
            or scope.minimum_single_contract_amount_krw > 0
        )
        if amount_required:
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                excluded_uncertain.append(record_key)
                continue
        elif amount is not None and (
            isinstance(amount, bool) or not isinstance(amount, int) or amount < 0
        ):
            excluded_uncertain.append(record_key)
            continue
        share = getattr(record, "share_pct", None)
        if (
            isinstance(share, bool)
            or not isinstance(share, (int, float))
            or not 0 <= float(share) <= 100
        ):
            excluded_uncertain.append(record_key)
            continue
        if float(share) == 0:
            # A zero-share consortium row is valid evidence, but represents no
            # attributable contract and therefore must not count as one.
            continue
        if (
            scope.aggregation == "COUNT"
            and float(share) < 100
            and scope.consortium_share_rule == "APPLY_SHARE"
        ):
            # A percentage can be applied deterministically to money, but the
            # source phrase alone does not establish whether a partial share
            # means a fractional count or one whole contract.  Do not silently
            # award a full count.
            excluded_uncertain.append(record_key)
            continue
        if float(share) < 100 and scope.consortium_share_rule == "UNSPECIFIED":
            excluded_uncertain.append(record_key)
            continue
        recognized_amount = Decimal(amount or 0)
        if scope.consortium_share_rule == "APPLY_SHARE":
            recognized_amount *= Decimal(str(share)) / Decimal("100")
        if recognized_amount < scope.minimum_single_contract_amount_krw:
            continue
        matched.append((record, recognized_amount))

    # Record iteration order can differ between SQL backends and callers.  A
    # stable business-key order keeps both the evidence digest and audit keys
    # deterministic for the same validated register state.
    matched.sort(
        key=lambda item: (
            str(getattr(item[0], "record_key", "")),
            int(getattr(item[0], "revision", 0) or 0),
        )
    )

    if excluded_uncertain:
        return DerivedPerformanceValue(
            status="REVIEW",
            matched_record_keys=tuple(str(getattr(item[0], "record_key", "")) for item in matched),
            rationale=(
                "검증된 실적 중 VAT·실적증명서·금액·공동수급 지분 조건을 확정할 수 "
                f"없는 기록이 {len(excluded_uncertain)}건 있어 자동 합산을 중지했습니다."
            ),
        )

    if not matched:
        return DerivedPerformanceValue(
            status="UNSCORABLE",
            rationale=(
                "원문 인정조건에 맞는 검증 완료 실적 증빙이 없습니다. "
                "누락된 실적을 0으로 추정하지 않고 자동 계산을 중지했습니다."
            ),
        )

    record_keys = tuple(str(getattr(item[0], "record_key", "")) for item in matched)
    value = (
        float(sum((item[1] for item in matched), Decimal("0")))
        if scope.aggregation == "SUM_AMOUNT"
        else float(len(matched))
    )
    payload = {
        "binding_schema": "pai-loop-performance-quantitative-binding-1.0.0",
        "scope": scope.model_dump(mode="json"),
        "records": [_record_digest_payload(item[0]) for item in matched],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return DerivedPerformanceValue(
        status="ESTIMATED",
        value=value,
        lower_value=value,
        upper_value=value,
        evidence_reference=f"PERFORMANCE-REGISTER:{digest[:20]}",
        evidence_sha256=digest,
        matched_record_keys=record_keys,
        rationale=(
            f"운영자가 VALIDATED로 확정한 실적대장에 원문 인정조건을 적용해 {len(matched)}건을 "
            "결정론적으로 집계했습니다. 발주기관의 최종 인정 전까지 ESTIMATED입니다."
        ),
    )
