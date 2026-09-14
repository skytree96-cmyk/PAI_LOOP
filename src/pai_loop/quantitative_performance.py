from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


_KST = timezone(timedelta(hours=9))


class PerformanceQuantModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


PerformanceAggregation = Literal["SUM_AMOUNT", "MAX_SINGLE_AMOUNT", "COUNT"]
PerformanceVatBasis = Literal["INCLUDED", "EXCLUDED", "UNSPECIFIED"]
PerformanceCounterpartyScope = Literal["UNSPECIFIED", "PUBLIC_SECTOR"]
PerformanceLookbackAnchor = Literal[
    "UNSPECIFIED",
    "BID_NOTICE_DATE",
    "SUBMISSION_DEADLINE",
]


class PerformanceRecognitionScope(PerformanceQuantModel):
    metric_key: Literal["company.performance.amount", "company.performance.count"]
    lookback_years: int = Field(ge=1, le=10)
    similarity_keywords: tuple[str, ...] = Field(default=(), max_length=12)
    # Each group is AND; groups are alternatives. This preserves a shared
    # qualifier in e.g. "telephone OR video foreign-language courses".
    similarity_keyword_groups: tuple[tuple[str, ...], ...] = Field(default=(), max_length=4)
    match_mode: Literal["ALL", "ANY"] = "ALL"
    counterparty_scope: PerformanceCounterpartyScope = "UNSPECIFIED"
    counterparty_keywords: tuple[str, ...] = Field(default=(), max_length=8)
    lookback_anchor_basis: PerformanceLookbackAnchor = "UNSPECIFIED"
    minimum_single_contract_amount_krw: int = Field(default=0, ge=0)
    vat_basis: PerformanceVatBasis
    completion_required: bool
    aggregation: PerformanceAggregation
    consortium_share_rule: Literal["APPLY_SHARE", "FULL_AMOUNT", "UNSPECIFIED"]
    certificate_required: bool = False
    # A valid source rule can require facts that the register cannot prove.
    # Keep those conditions visible while allowing a separately verified,
    # criterion-bound company fact; never derive counts from the register here.
    manual_verification_conditions: tuple[str, ...] = Field(default=(), max_length=12)
    source_literal: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_metric_aggregation(self) -> "PerformanceRecognitionScope":
        allowed = (
            {"SUM_AMOUNT", "MAX_SINGLE_AMOUNT"}
            if self.metric_key == "company.performance.amount"
            else {"COUNT"}
        )
        if self.aggregation not in allowed:
            raise ValueError("performance aggregation does not match metric_key")
        if self.counterparty_scope == "PUBLIC_SECTOR" and not self.counterparty_keywords:
            raise ValueError("public-sector scope requires source-bound counterparty keywords")
        if self.counterparty_scope == "UNSPECIFIED" and self.counterparty_keywords:
            raise ValueError("unspecified counterparty scope must not carry keywords")
        if self.similarity_keyword_groups and (
            self.similarity_keywords
            or any(not group or len(group) > 6 or any(not word.strip() for word in group)
                   for group in self.similarity_keyword_groups)
        ):
            raise ValueError("compound similarity groups must be bounded and unambiguous")
        if not self.similarity_keywords and not self.similarity_keyword_groups and not self.manual_verification_conditions:
            raise ValueError("automatic register recognition requires a similarity scope")
        if self.manual_verification_conditions != _manual_recognition_conditions(self.source_literal):
            # Old persisted scopes may predate these fields. Loading them is
            # allowed, but derive_performance_value rechecks the original text.
            if self.manual_verification_conditions:
                raise ValueError("manual recognition conditions must match the source")
        return self


class PerformanceRecordBandInput(PerformanceQuantModel):
    """One record-level input retained for VAT and threshold sensitivity."""

    record_key: str = Field(min_length=1, max_length=180)
    recognized_amount_krw: float = Field(ge=0)
    vat_basis: Literal["INCLUDED", "EXCLUDED"]
    meets_raw_minimum: bool


class PerformanceScoreBandInput(PerformanceQuantModel):
    """Auditable numeric input for downstream score-band sensitivity checks."""

    metric_key: Literal["company.performance.amount", "company.performance.count"]
    aggregation: PerformanceAggregation
    range_status: Literal["EXACT", "RANGE", "LOWER_BOUND_ONLY"]
    lower_value: float = Field(ge=0)
    upper_value: float | None = Field(default=None, ge=0)
    source_vat_basis: PerformanceVatBasis
    observed_record_vat_bases: tuple[Literal["INCLUDED", "EXCLUDED"], ...] = ()
    recognized_record_amounts_krw: tuple[float, ...] = ()
    record_inputs: tuple[PerformanceRecordBandInput, ...] = ()
    lookback_anchor_basis: PerformanceLookbackAnchor = "UNSPECIFIED"
    evaluation_as_of_basis: PerformanceLookbackAnchor = "UNSPECIFIED"
    evaluation_as_of_date: date | None = None
    sensitivity_dimensions: tuple[
        Literal["VAT_BASIS", "RECORD_ELIGIBILITY"], ...
    ] = ()

    @model_validator(mode="after")
    def validate_range(self) -> "PerformanceScoreBandInput":
        if self.range_status in {"EXACT", "RANGE"} and self.upper_value is None:
            raise ValueError("bounded score-band input requires an upper value")
        if self.range_status == "LOWER_BOUND_ONLY" and self.upper_value is not None:
            raise ValueError("lower-bound-only score-band input must not set an upper value")
        if self.range_status == "EXACT" and self.upper_value != self.lower_value:
            raise ValueError("exact score-band input requires equal bounds")
        if self.range_status == "RANGE" and self.upper_value == self.lower_value:
            raise ValueError("range score-band input requires distinct bounds")
        if self.upper_value is not None and self.lower_value > self.upper_value:
            raise ValueError("score-band lower value must not exceed upper value")
        return self


class DerivedPerformanceValue(PerformanceQuantModel):
    status: Literal["CONFIRMED", "ESTIMATED", "UNSCORABLE", "REVIEW"]
    value: float | None = None
    lower_value: float | None = None
    upper_value: float | None = None
    evidence_reference: str | None = None
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    matched_record_keys: tuple[str, ...] = ()
    score_band_input: PerformanceScoreBandInput | None = None
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
_AMOUNT_UNIT_PATTERN = (
    r"(?:천\s*만\s*원|천\s*만|백\s*만\s*원|백\s*만|억\s*원|억|"
    r"만\s*원|만|천\s*원|천|원)"
)
_LOOKBACK_RE = re.compile(r"최근\s*(?P<years>\d{1,2})\s*(?:개)?년")
_MINIMUM_RE = re.compile(
    r"(?:단일\s*(?:규모\s*)?계약|건당|1\s*건당)[^\d]{0,30}"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    rf"(?P<unit>{_AMOUNT_UNIT_PATTERN})\s*이\s*상"
)
_COUNT_MINIMUM_RE = re.compile(
    r"(?:실적\s*건수|건수|건별)[^()]{0,30}\(\s*"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    rf"(?P<unit>{_AMOUNT_UNIT_PATTERN})\s*이\s*상\s*\)"
)
_MINIMUM_HINT_RE = re.compile(
    r"(?:단일\s*계약|건당|1\s*건당).{0,80}?"
    r"\d[\d,.\s]*.{0,16}?(?:억|만|천|원).{0,20}?이\s*상"
)
_COUNT_MINIMUM_HINT_RE = re.compile(
    r"(?:실적\s*건수|건수|건별).{0,80}?"
    r"\d[\d,.\s]*.{0,16}?(?:억|만|천|원).{0,20}?이\s*상"
)
_PARENTHETICAL_SCOPE_RE = re.compile(
    r"\(\s*(?P<scope>[^()]{2,100}?)\s*\)\s*(?:관련\s*)?(?:사업|용역)"
)
_MAX_SINGLE_AMOUNT_RE = re.compile(
    r"(?:단일\s*(?:용역|계약)(?:\s*의)?\s*(?:최고|최대)\s*(?:계약\s*)?금액"
    r"|(?:최고|최대)\s*단일\s*(?:용역|계약)\s*(?:계약\s*)?금액)"
)
_BID_NOTICE_ANCHOR_RE = re.compile(
    # Preserve attached qualifiers such as 본입찰공고일 in the established
    # explicit 입찰 form; only the newly supported bare 공고일 needs a boundary.
    r"(?:입찰\s*공고일|(?<![가-힣A-Za-z0-9])공고일)(?:자)?(?:을|를)?\s*기준"
)
_BID_NOTICE_PRIOR_DAY_RE = re.compile(
    r"(?:입찰\s*공고일|(?<![가-힣A-Za-z0-9])공고일)(?:자)?\s*전일까지\s*완료"
)
_PARTICIPANT_BOUND_RE = re.compile(
    r"\d[\d,]*\s*(?:인|명)\s*(?:이상|초과|이하|미만)"
    r"|최소\s*\d[\d,]*\s*(?:인|명)(?![가-힣])"
)
_ANNUAL_CONTRACT_AMOUNT_RE = re.compile(
    r"연간\s*(?:기준\s*)?(?:총\s*)?(?:계약\s*)?금액"
)
_ANNUAL_CONTRACT_CONDITION_RE = re.compile(
    _ANNUAL_CONTRACT_AMOUNT_RE.pattern
    + rf"\s*\d[\d,]*(?:\.\d+)?\s*{_AMOUNT_UNIT_PATTERN}\s*(?:이상|초과|이하|미만)"
)
_FIXED_PERIOD_DATE_PATTERN = (
    r"(?:20\d{2}\s*(?:년(?:\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?)?"
    r"|[./-]\s*\d{1,2}(?:\s*[./-]\s*\d{1,2})?\.?))"
)
_FIXED_RECOGNITION_PERIOD_RE = re.compile(
    _FIXED_PERIOD_DATE_PATTERN
    + r"\s*(?:부터|[~∼～])\s*(?:(?:입찰\s*)?공고일(?:\s*(?:전일|전|까지))?"
    + "|" + _FIXED_PERIOD_DATE_PATTERN + ")"
)
_SUBCONTRACT_CONDITION_RE = re.compile(
    r"(?:발주(?:처|기관|자)[^.\n;]{0,50}승인[^.\n;]{0,35}하도급[^.\n;]{0,70}"
    r"|하도급[^.\n;]{0,70}(?:승인|인정|제외|포함)[^.\n;]{0,35})"
)
_ISSUER_CONDITION_RE = re.compile(
    r"(?:관련\s*협회\s*(?:또는|및)\s*)?"
    r"(?:공공\s*기관|국가\s*기관|정부\s*기관|지방\s*자치\s*단체|지자체)"
    r"(?:의|에서|으로부터|이)?\s*(?:직접\s*)?(?:확인(?:을)?\s*(?:받|한)|발급|발행)"
    r"[^.\n;]{0,100}?(?:실적\s*증명(?:서|원))"
)
_COMPOUND_SERVICE_RE = re.compile(
    r"(?P<left>[가-힣A-Za-z]{2,20})\s+또는\s+(?P<right>[가-힣A-Za-z]{2,20})\s+"
    r"(?P<qualifier>[가-힣A-Za-z]{2,20})\s*(?:과정|프로그램)\s*(?:수행\s*)?실적"
)
_DIRECT_SERVICE_COUNT_RE = re.compile(
    r"(?P<scope>[가-힣A-Za-z]{2,20})\s*(?:실시|수행)\s*(?:건수|실적)"
)
_DEADLINE_ANCHOR_RE = re.compile(
    r"(?:제안서\s*)?(?:제출\s*)?(?:마감|마감일|기한)(?:을|를)?\s*기준"
)
_PRIVATE_AGENCY_RE = re.compile(
    r"(?:주식회사|유한회사|유한책임회사|개인사업자|\(주\)|㈜)",
    re.IGNORECASE,
)
_PUBLIC_AGENCY_RE = re.compile(
    r"(?:공공기관|지방자치단체|지자체|정부기관|국가기관|교육지원청|교육청|"
    r"시청|군청|구청|도청|국립|공립|정부|테크노파크)",
    re.IGNORECASE,
)
_PUBLIC_REGION_PREFIXES = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시", "세종특별자치시", "경기도", "강원특별자치도",
    "충청북도", "충청남도", "전북특별자치도", "전라남도", "경상북도",
    "경상남도", "제주특별자치도",
)
_NON_SERVICE_KEYWORD_SUFFIXES = (
    "교육지원청", "지원청", "교육청", "지원센터", "기관", "시설", "센터", "청", "부", "원", "장",
)
_NON_SERVICE_KEYWORD_PREFIXES = {"행사": ("대", "여")}
_SERVICE_OUTPUT_RE = re.compile(
    r"(?:기획|운영|개발|제작|진행|실시|수행|과정|프로그램|컨설팅|박람회|"
    r"세미나|워크숍|채용|상담|매칭)"
)
_SCOPE_RE = re.compile(
    r"(?P<scope>[A-Za-z0-9가-힣][A-Za-z0-9가-힣·/+\-\s]{1,80}?)\s*"
    r"(?:관련|분야의?)\s*(?:유사\s*)?(?:사업|용역|교육|컨설팅)?"
)
_GENERIC_SCOPE_WORDS = {
    "당해용역", "당해", "이행실적", "이행실적은", "이행실적으로", "자체",
    "과정", "프로그램", "사업수행", "용역수행",
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


def _normalized_source_text(text: str | None) -> str:
    # Every recognition regex assumes ASCII digits, letters and punctuation.
    # Full-width characters in a source literal must not hide a condition from
    # the request/derivation path while the fact-binding path already sees it.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def _normalize_keyword(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _filtered_scope_parts(raw: str) -> tuple[str, ...]:
    parts = [
        _normalize_keyword(item)
        for item in re.split(r"[·/,]|\s+", raw)
        if _normalize_keyword(item)
    ]
    filtered = [
        item
        for item in parts
        if item not in _GENERIC_SCOPE_WORDS
        and item not in {"및", "또는"}
        and not item.isdigit()
        and not re.fullmatch(r"\d{1,2}(?:개)?년(?:간)?", item)
        and not re.search(r"\d\s*(?:점|원|억|만|천|%)", item)
        and 1 < len(item) <= 30
    ]
    return tuple(dict.fromkeys(filtered[-12:]))


def _parenthetical_scope_keywords(literal: str) -> tuple[str, ...]:
    for match in _PARENTHETICAL_SCOPE_RE.finditer(literal):
        keywords = _filtered_scope_parts(match.group("scope"))
        if len(keywords) >= 2:
            return keywords
    return ()


def _scope_keywords(literal: str) -> tuple[str, ...]:
    if parenthetical := _parenthetical_scope_keywords(literal):
        return parenthetical
    direct = tuple(dict.fromkeys(
        match.group("scope") for match in _DIRECT_SERVICE_COUNT_RE.finditer(literal)
        if match.group("scope") not in _GENERIC_SCOPE_WORDS
    ))
    if len(direct) == 1:
        return direct
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
    # Preserve order while preventing one model-produced duplicate from
    # changing the binding digest.
    return _filtered_scope_parts(raw)


def _amount_from_match(match: re.Match[str]) -> int | None:
    try:
        raw_amount = Decimal(match.group("amount").replace(",", ""))
        unit = re.sub(r"\s+", "", match.group("unit"))
        return int(
            (raw_amount * _AMOUNT_SCALES[unit]).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
    except (InvalidOperation, KeyError):
        return None


def _performance_aggregation(text: str, metric_key: str) -> PerformanceAggregation:
    if metric_key == "company.performance.count":
        return "COUNT"
    if _MAX_SINGLE_AMOUNT_RE.search(text):
        return "MAX_SINGLE_AMOUNT"
    return "SUM_AMOUNT"


def _counterparty_keywords(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for label, pattern in (
        ("지방자치단체", r"지방\s*자치\s*단체"),
        ("지자체", r"지자체"),
        ("공공기관", r"공공\s*기관"),
        ("국가기관", r"국가\s*기관"),
        ("정부기관", r"정부\s*기관"),
    ):
        # Only an explicit certificate-issuer phrase is excluded. A separate
        # occurrence imposing a public-client restriction must still survive.
        issuer_spans = [match.span() for match in _ISSUER_CONDITION_RE.finditer(text)]
        client_mentions = [match for match in re.finditer(pattern, text)
                           if not any(start <= match.start() < end for start, end in issuer_spans)]
        if client_mentions and label not in values:
            values.append(label)
    return tuple(values)


def _compound_service_groups(text: str) -> tuple[tuple[str, ...], ...]:
    matches = list(_COMPOUND_SERVICE_RE.finditer(text))
    for match in matches:
        # Compile only a closed scope clause. An arbitrary learner qualifier
        # before the alternatives (e.g. adult learners) cannot be discarded.
        prefix = re.split(r"[.;\n]", text[:match.start()])[-1]
        if not re.fullmatch(
            r"\s*(?:\d+\)\s*)?(?:(?:주요\s*)?(?:사업|용역)\s*"
            r"(?:내역|내용|실적)(?:은|는)?\s*)?"
            r"(?:최근\s*\d{1,2}\s*(?:개)?년\s*(?:이내|동안)?\s*)?", prefix
        ):
            return ()
        suffix = re.split(r"[.;\n]", text[match.end():], maxsplit=1)[0].strip()
        if suffix and not (
            suffix.startswith("중 ") and _ANNUAL_CONTRACT_AMOUNT_RE.search(suffix)
        ):
            # The one supported tail is retained as a manual-only condition;
            # other trailing modifiers need an explicit grammar of their own.
            return ()
    groups = {((match.group("left"), match.group("qualifier")),
               (match.group("right"), match.group("qualifier"))) for match in matches}
    residual = _COMPOUND_SERVICE_RE.sub(" ", text)
    if _scope_keywords(residual) or re.search(r"[가-힣A-Za-z]{2,30}\s*(?:대상|전용|한정)", residual):
        # Do not discard another service/learner restriction when compiling
        # the compact OR grammar. The combined scope needs explicit review.
        return ()
    return next(iter(groups)) if len(groups) == 1 else ()


def _consortium_share_rule(text: str) -> Literal["APPLY_SHARE", "FULL_AMOUNT", "UNSPECIFIED"] | None:
    # Weighting this bid's members' evaluation scores is a different program
    # from recognizing the bidder's share of a past performance contract.
    if re.search(r"공동수급[^.\n;]{0,180}(?:점수|평점|배점)[^.\n;]{0,75}(?:합산|가중|적용)", text):
        return None
    apply = bool(re.search(
        r"(?:공동수급|공동도급|공동계약|컨소시엄)[^.\n;]{0,80}"
        r"(?:지분(?:율)?|참여\s*비율)[^.\n;]{0,35}(?:적용|반영|따른|실적(?:만)?\s*인정|만\s*기재)", text))
    full = bool(re.search(r"(?:공동수급|공동도급|컨소시엄)[^.\n;]{0,40}(?:전체|전액)\s*인정", text))
    if apply and full:
        return None
    return "APPLY_SHARE" if apply else "FULL_AMOUNT" if full else "UNSPECIFIED"


def _lookback_anchor_basis(text: str) -> PerformanceLookbackAnchor | None:
    anchors: list[PerformanceLookbackAnchor] = []
    if _BID_NOTICE_ANCHOR_RE.search(text):
        anchors.append("BID_NOTICE_DATE")
    if _DEADLINE_ANCHOR_RE.search(text):
        anchors.append("SUBMISSION_DEADLINE")
    if len(anchors) > 1:
        return None
    return anchors[0] if anchors else "UNSPECIFIED"


def _unsupported_recognition_reason(text: str) -> str | None:
    # These are source-bound eligibility dimensions, not project-description
    # keywords. The register has no attested per-contract participant count or
    # annual contract-amount basis; a total amount/overview cannot prove them.
    text = _normalized_source_text(text)
    if _PARTICIPANT_BOUND_RE.search(text):
        return "원문 실적 인정조건의 참여 인원 기준을 실적별 검증자료에 연결할 수 없어 자동 집계를 중지했습니다."
    if _ANNUAL_CONTRACT_AMOUNT_RE.search(text):
        return "원문 실적 인정조건의 연간 계약금액 기준을 실적별 검증자료에 연결할 수 없어 자동 집계를 중지했습니다."
    if _FIXED_RECOGNITION_PERIOD_RE.search(text):
        return "원문의 고정 실적기간과 최근 연수 기준의 적용관계를 확인해야 하므로 자동 집계를 중지했습니다."
    if _SUBCONTRACT_CONDITION_RE.search(text):
        return "하도급 실적의 승인·인정 조건을 실적별 증빙에 연결할 수 없어 자동 집계를 중지했습니다."
    if _ISSUER_CONDITION_RE.search(text):
        return "실적증명 확인기관 조건을 발주처 제한과 구분했으나 발급기관 증빙을 확인할 수 없어 자동 집계를 중지했습니다."
    return None


def _manual_recognition_conditions(text: str) -> tuple[str, ...]:
    """Preserve known, record-unprovable conditions without inventing facts."""
    normalized = _normalized_source_text(text)
    spans = [(m.start(), m.group()) for m in _PARTICIPANT_BOUND_RE.finditer(normalized)]
    annual = list(_ANNUAL_CONTRACT_CONDITION_RE.finditer(normalized))
    if annual:
        spans.extend((m.start(), m.group()) for m in annual)
    else:
        spans.extend((m.start(), m.group()) for m in _ANNUAL_CONTRACT_AMOUNT_RE.finditer(normalized))
    for pattern in (_FIXED_RECOGNITION_PERIOD_RE, _SUBCONTRACT_CONDITION_RE, _ISSUER_CONDITION_RE):
        spans.extend((match.start(), match.group()) for match in pattern.finditer(normalized))
    return tuple(dict.fromkeys(value for _, value in sorted(spans)))


def parse_performance_recognition_scope(
    literal: str,
    *,
    metric_key: str,
) -> PerformanceRecognitionScope | None:
    """Parse only explicit recognition dimensions from a source-bound rule.

    A generic phrase such as "최근 실적" is insufficient. Automatic scoring
    requires an explicit lookback, similarity scope, and completion rule. If
    the notice is silent about VAT, that silence is preserved as UNSPECIFIED.
    """

    if metric_key not in {"company.performance.amount", "company.performance.count"}:
        return None
    text = _normalized_source_text(literal)
    if not text or len(text) > 2_000:
        return None
    manual_conditions = _manual_recognition_conditions(text)
    lookback = _LOOKBACK_RE.search(text)
    groups = _compound_service_groups(text)
    if _COMPOUND_SERVICE_RE.search(text) and not groups:
        return None
    keywords = () if groups else _scope_keywords(text)
    if lookback is None or (not keywords and not groups and not manual_conditions):
        return None
    if len({match.group("years") for match in _LOOKBACK_RE.finditer(text)}) > 1:
        return None
    parenthetical_keywords = _parenthetical_scope_keywords(text)
    counterparties = _counterparty_keywords(text)
    anchor_basis = _lookback_anchor_basis(text)
    if anchor_basis is None:
        return None
    vat_excluded = re.search(
        r"(?:VAT|부가(?:가치)?세)\s*(?:제외|별도|미포함)",
        text,
        re.IGNORECASE,
    )
    vat_included = re.search(r"(?:VAT|부가(?:가치)?세)\s*포함", text, re.IGNORECASE)
    if vat_excluded and vat_included:
        return None
    if vat_excluded:
        vat_basis: PerformanceVatBasis = "EXCLUDED"
    elif vat_included:
        vat_basis = "INCLUDED"
    else:
        vat_basis = "UNSPECIFIED"
    if re.search(r"(?:이행|수행|계약)(?:이|가)?\s*완료(?:된|한)?\s*실적", text) or re.search(
        r"완료(?:된|한)?\s*실적",
        text,
    ) or re.search(
        r"완성\s*(?:\(\s*준공\s*\))?\s*된\s*(?:용역\s*)?이행\s*실적",
        text,
    ):
        completion_required = True
    elif re.search(r"완료\s*여부\s*무관", text):
        completion_required = False
    else:
        return None

    minimum_match = _MINIMUM_RE.search(text)
    if minimum_match is None and metric_key == "company.performance.count":
        minimum_match = _COUNT_MINIMUM_RE.search(text)
    minimum_hint = (
        _COUNT_MINIMUM_HINT_RE.search(text)
        if metric_key == "company.performance.count"
        else _MINIMUM_HINT_RE.search(text)
    )
    if minimum_match is None and minimum_hint is not None:
        return None
    minimum = 0
    if minimum_match is not None and not _ANNUAL_CONTRACT_AMOUNT_RE.search(minimum_match.group()):
        parsed_minimum = _amount_from_match(minimum_match)
        if parsed_minimum is None:
            return None
        minimum = parsed_minimum
    share_rule = _consortium_share_rule(text)
    if share_rule is None:
        return None

    try:
        return PerformanceRecognitionScope(
            metric_key=metric_key,
            lookback_years=int(lookback.group("years")),
            similarity_keywords=keywords,
            similarity_keyword_groups=groups,
            match_mode="ANY" if parenthetical_keywords else "ALL",
            counterparty_scope=("PUBLIC_SECTOR" if counterparties else "UNSPECIFIED"),
            counterparty_keywords=counterparties,
            lookback_anchor_basis=anchor_basis,
            minimum_single_contract_amount_krw=minimum,
            vat_basis=vat_basis,
            completion_required=completion_required,
            aggregation=_performance_aggregation(text, metric_key),
            consortium_share_rule=share_rule,
            certificate_required=bool(re.search(r"실적\s*증명(?:서|원)", text)),
            manual_verification_conditions=manual_conditions,
            source_literal=text,
        )
    except ValidationError:
        return None


def _date_years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _keyword_occurs_in_service_text(text: str, keyword: str) -> bool:
    haystack = re.sub(r"\s+", " ", text).casefold()
    needle = re.sub(r"\s+", " ", keyword).casefold()
    if not haystack or not needle:
        return False
    offset = 0
    while (index := haystack.find(needle, offset)) >= 0:
        prefix = haystack[:index]
        suffix = haystack[index + len(needle):]
        blocked_prefixes = _NON_SERVICE_KEYWORD_PREFIXES.get(needle, ())
        if (
            not any(prefix.endswith(value.casefold()) for value in blocked_prefixes)
            and not any(
                suffix.startswith(value.casefold())
                for value in _NON_SERVICE_KEYWORD_SUFFIXES
            )
        ):
            return True
        offset = index + len(needle)
    return False


def _record_service_matches(record: Any, keyword: str) -> bool:
    declared = tuple(str(value) for value in (getattr(record, "keywords", None) or ()))
    if any(_keyword_occurs_in_service_text(value, keyword) for value in declared):
        return True
    project_name = str(getattr(record, "project_name", "") or "")
    if _keyword_occurs_in_service_text(project_name, keyword):
        return True
    overview = str(getattr(record, "overview", "") or "")
    return bool(
        _SERVICE_OUTPUT_RE.search(overview)
        and _keyword_occurs_in_service_text(overview, keyword)
    )


def _public_sector_agency_status(record: Any) -> bool | None:
    agency = re.sub(r"\s+", "", str(getattr(record, "agency", "") or ""))
    if not agency:
        return None
    if _PRIVATE_AGENCY_RE.search(agency):
        return False
    if agency.startswith(_PUBLIC_REGION_PREFIXES) or _PUBLIC_AGENCY_RE.search(agency):
        return True
    return None


def _record_digest_payload(record: Any) -> dict[str, Any]:
    def date_value(value: Any) -> str | None:
        return value.isoformat() if isinstance(value, (date, datetime)) else None

    return {
        "record_key": str(getattr(record, "record_key", "")),
        "revision": int(getattr(record, "revision", 0) or 0),
        "project_name": str(getattr(record, "project_name", "")),
        "agency": str(getattr(record, "agency", "")),
        "division": str(getattr(record, "division", "")),
        "overview": str(getattr(record, "overview", "") or ""),
        "contract_date": date_value(getattr(record, "contract_date", None)),
        "start_date": date_value(getattr(record, "start_date", None)),
        "end_date": date_value(getattr(record, "end_date", None)),
        "contract_amount": getattr(record, "contract_amount", None),
        "gross_contract_amount_krw": getattr(
            record, "gross_contract_amount_krw", None
        ),
        "recognized_performance_amount_krw": getattr(
            record, "recognized_performance_amount_krw", None
        ),
        "recognized_amount_is_net_of_share": getattr(
            record, "recognized_amount_is_net_of_share", None
        ),
        "vat_basis": str(getattr(record, "vat_basis", "")),
        "completed": bool(getattr(record, "completed", False)),
        "share_pct": getattr(record, "share_pct", None),
        "certificate_status": str(getattr(record, "certificate_status", "")),
        "evidence_reference": str(getattr(record, "evidence_reference", "") or ""),
        "keywords": list(getattr(record, "keywords", None) or []),
    }


def _performance_register_digest(
    scope: PerformanceRecognitionScope,
    records: Iterable[Any],
    *,
    as_of_basis: PerformanceLookbackAnchor,
    as_of_date: date,
) -> str:
    payload = {
        "binding_schema": "pai-loop-performance-quantitative-binding-1.1.0",
        "algorithm_version": "performance-recognition-0.4.1",
        "evaluation": {
            "as_of_basis": as_of_basis,
            "as_of_date": as_of_date.isoformat(),
            "vat_factor": str(_VAT_FACTOR),
        },
        "scope": scope.model_dump(mode="json"),
        "records": [_record_digest_payload(record) for record in records],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _aggregate_matched_value(
    scope: PerformanceRecognitionScope,
    matched: list[tuple[Any, Decimal]],
) -> float:
    if not matched:
        return 0.0
    if scope.aggregation == "SUM_AMOUNT":
        return float(sum((item[1] for item in matched), Decimal("0")))
    if scope.aggregation == "MAX_SINGLE_AMOUNT":
        return float(max(item[1] for item in matched))
    return float(len(matched))


_VAT_FACTOR = Decimal("1.1")


def _amount_at_vat_basis(
    amount: Decimal,
    *,
    observed_basis: str,
    target_basis: Literal["INCLUDED", "EXCLUDED"],
) -> Decimal:
    if observed_basis == target_basis:
        return amount
    if observed_basis == "INCLUDED" and target_basis == "EXCLUDED":
        return amount / _VAT_FACTOR
    return amount * _VAT_FACTOR


def _aggregate_at_vat_basis(
    scope: PerformanceRecognitionScope,
    candidate_records: list[tuple[Any, Decimal]],
    *,
    target_basis: Literal["INCLUDED", "EXCLUDED"],
) -> float:
    eligible: list[Decimal] = []
    for record, raw_amount in candidate_records:
        normalized = _amount_at_vat_basis(
            raw_amount,
            observed_basis=str(getattr(record, "vat_basis", "")).upper(),
            target_basis=target_basis,
        )
        if normalized >= scope.minimum_single_contract_amount_krw:
            eligible.append(normalized)
    if scope.aggregation == "COUNT":
        return float(len(eligible))
    if not eligible:
        return 0.0
    if scope.aggregation == "MAX_SINGLE_AMOUNT":
        return float(max(eligible))
    return float(sum(eligible, Decimal("0")))


def _bounded_aggregate(
    scope: PerformanceRecognitionScope,
    matched: list[tuple[Any, Decimal]],
    candidate_records: list[tuple[Any, Decimal]],
) -> tuple[float, float]:
    if scope.vat_basis != "UNSPECIFIED":
        value = _aggregate_matched_value(scope, matched)
        return value, value
    scenarios = (
        _aggregate_at_vat_basis(scope, candidate_records, target_basis="INCLUDED"),
        _aggregate_at_vat_basis(scope, candidate_records, target_basis="EXCLUDED"),
    )
    return min(scenarios), max(scenarios)


def _eligible_record_keys(
    scope: PerformanceRecognitionScope,
    candidate_records: list[tuple[Any, Decimal]],
) -> tuple[str, ...]:
    keys: list[str] = []
    for record, raw_amount in candidate_records:
        observed = str(getattr(record, "vat_basis", "")).upper()
        if scope.vat_basis == "UNSPECIFIED":
            eligible = any(
                _amount_at_vat_basis(
                    raw_amount,
                    observed_basis=observed,
                    target_basis=target,
                )
                >= scope.minimum_single_contract_amount_krw
                for target in ("INCLUDED", "EXCLUDED")
            )
        else:
            eligible = raw_amount >= scope.minimum_single_contract_amount_krw
        if eligible:
            keys.append(str(getattr(record, "record_key", "")))
    return tuple(keys)


def _score_band_input(
    scope: PerformanceRecognitionScope,
    matched: list[tuple[Any, Decimal]],
    candidate_records: list[tuple[Any, Decimal]],
    *,
    complete: bool,
    as_of_basis: PerformanceLookbackAnchor,
    as_of_date: date,
) -> PerformanceScoreBandInput | None:
    if not matched and not candidate_records:
        return None
    lower, upper = _bounded_aggregate(scope, matched, candidate_records)
    dimensions: list[Literal["VAT_BASIS", "RECORD_ELIGIBILITY"]] = []
    if scope.vat_basis == "UNSPECIFIED":
        dimensions.append("VAT_BASIS")
    if not complete:
        dimensions.append("RECORD_ELIGIBILITY")
    return PerformanceScoreBandInput(
        metric_key=scope.metric_key,
        aggregation=scope.aggregation,
        range_status=(
            "LOWER_BOUND_ONLY"
            if not complete
            else ("EXACT" if lower == upper else "RANGE")
        ),
        lower_value=lower,
        upper_value=upper if complete else None,
        source_vat_basis=scope.vat_basis,
        observed_record_vat_bases=tuple(
            sorted(
                {
                    str(getattr(item[0], "vat_basis", "")).upper()
                    for item in candidate_records
                }
            )
        ),
        recognized_record_amounts_krw=tuple(float(item[1]) for item in matched),
        record_inputs=tuple(
            PerformanceRecordBandInput(
                record_key=str(getattr(item[0], "record_key", "")),
                recognized_amount_krw=float(item[1]),
                vat_basis=str(getattr(item[0], "vat_basis", "")).upper(),
                meets_raw_minimum=(
                    item[1] >= scope.minimum_single_contract_amount_krw
                ),
            )
            for item in candidate_records
        ),
        lookback_anchor_basis=scope.lookback_anchor_basis,
        evaluation_as_of_basis=as_of_basis,
        evaluation_as_of_date=as_of_date,
        sensitivity_dimensions=tuple(dimensions),
    )


def derive_performance_value(
    scope: PerformanceRecognitionScope,
    records: Iterable[Any],
    *,
    as_of: datetime,
    as_of_basis: PerformanceLookbackAnchor = "UNSPECIFIED",
) -> DerivedPerformanceValue:
    source_literal = _normalized_source_text(scope.source_literal)
    unsupported_reason = _unsupported_recognition_reason(source_literal)
    if unsupported_reason is not None:
        # Rule parsing is independent of company proof. Recheck stored scopes
        # too, including scopes created before manual conditions were modeled.
        # Never return a numeric lower bound that could saturate a score band.
        return DerivedPerformanceValue(status="REVIEW", rationale=unsupported_reason)
    if scope.manual_verification_conditions:
        return DerivedPerformanceValue(
            status="REVIEW", rationale="저장된 추가 실적인정 조건과 원문이 일치하지 않아 자동 집계를 중지했습니다.",
        )
    groups = _compound_service_groups(source_literal)
    if groups != scope.similarity_keyword_groups:
        return DerivedPerformanceValue(status="REVIEW", rationale="원문의 복합 유사범위와 저장된 인정조건이 일치하지 않아 자동 집계를 중지했습니다.")
    source_share = _consortium_share_rule(source_literal)
    if source_share is None or (source_share != "UNSPECIFIED" and source_share != scope.consortium_share_rule):
        return DerivedPerformanceValue(status="REVIEW", rationale="원문의 공동수급 지분 조건과 저장된 인정조건이 일치하지 않아 자동 집계를 중지했습니다.")
    source_anchor = _lookback_anchor_basis(source_literal)
    if source_anchor is None or (
        source_anchor != "UNSPECIFIED"
        and source_anchor != scope.lookback_anchor_basis
    ):
        return DerivedPerformanceValue(
            status="REVIEW",
            rationale="원문 실적 인정기간 기준일과 저장된 인정조건이 일치하지 않아 자동 계산을 중지했습니다.",
        )
    if _LOOKBACK_RE.search(source_literal):
        reparsed = parse_performance_recognition_scope(source_literal, metric_key=scope.metric_key)
        if reparsed is None or reparsed.model_dump(exclude={"source_literal"}) != scope.model_dump(exclude={"source_literal"}):
            return DerivedPerformanceValue(status="REVIEW", rationale="원문 실적인정 조건의 현재 해석과 저장된 범위가 일치하지 않아 자동 집계를 중지했습니다.")
    if as_of_basis not in {
        "UNSPECIFIED",
        "BID_NOTICE_DATE",
        "SUBMISSION_DEADLINE",
    }:
        return DerivedPerformanceValue(
            status="REVIEW",
            rationale="실적 인정기간 기준일의 의미를 확인할 수 없어 자동 계산을 중지했습니다.",
        )
    if (
        scope.lookback_anchor_basis != "UNSPECIFIED"
        and as_of_basis != scope.lookback_anchor_basis
    ):
        return DerivedPerformanceValue(
            status="REVIEW",
            rationale=(
                "원문 실적 인정기간 기준일과 전달된 기준일의 의미가 일치하지 않아 "
                "자동 계산을 중지했습니다."
            ),
        )
    normalized_as_of = (
        as_of
        if as_of.tzinfo is not None and as_of.utcoffset() is not None
        else as_of.replace(tzinfo=timezone.utc)
    )
    deadline = normalized_as_of.astimezone(_KST).date()
    start = _date_years_before(deadline, scope.lookback_years)
    completion_cutoff = deadline
    if scope.completion_required and _BID_NOTICE_PRIOR_DAY_RE.search(source_literal):
        if as_of_basis != "BID_NOTICE_DATE":
            return DerivedPerformanceValue(
                status="REVIEW",
                rationale="원문은 공고일 전일까지 완료된 실적을 요구하지만 전달된 공고일을 확인할 수 없어 자동 계산을 중지했습니다.",
            )
        completion_cutoff -= timedelta(days=1)
    matched: list[tuple[Any, Decimal]] = []
    candidate_records: list[tuple[Any, Decimal]] = []
    excluded_uncertain: list[str] = []
    for record in records:
        record_status = str(getattr(record, "record_status", "")).upper()
        if record_status != "VALIDATED":
            # A private workbook is authoritative as a whole. Active rows that
            # have not reached VALIDATED therefore represent unresolved
            # register evidence; silently dropping them could turn a validated
            # subset into an incorrectly exact score. Legacy/manual drafts stay
            # excluded as before because they are not part of that atomic
            # private-import contract.
            if (
                str(getattr(record, "source", "")).upper().startswith(
                    "PRIVATE_IMPORT"
                )
                and record_status != "ARCHIVED"
            ):
                raw_record_key = getattr(record, "record_key", None)
                excluded_uncertain.append(
                    raw_record_key.strip()
                    if isinstance(raw_record_key, str) and raw_record_key.strip()
                    else "UNKNOWN"
                )
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
        checks = [
            _record_service_matches(record, keyword)
            for keyword in scope.similarity_keywords
        ]
        service_matches = (any(all(_record_service_matches(record, word) for word in group)
                               for group in scope.similarity_keyword_groups)
                           if scope.similarity_keyword_groups else
                           all(checks) if scope.match_mode == "ALL" else any(checks))
        if not service_matches:
            continue
        if scope.counterparty_scope == "PUBLIC_SECTOR":
            public_status = _public_sector_agency_status(record)
            if public_status is None:
                excluded_uncertain.append(record_key)
                continue
            if not public_status:
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
        if not start <= basis_date <= completion_cutoff:
            continue
        if scope.completion_required and not bool(getattr(record, "completed", False)):
            continue
        if not str(getattr(record, "evidence_reference", "") or "").strip():
            excluded_uncertain.append(record_key)
            continue
        record_vat_basis = str(getattr(record, "vat_basis", "")).upper()
        if record_vat_basis not in {"INCLUDED", "EXCLUDED"}:
            excluded_uncertain.append(record_key)
            continue
        if scope.vat_basis != "UNSPECIFIED" and record_vat_basis != scope.vat_basis:
            excluded_uncertain.append(record_key)
            continue
        if scope.certificate_required and str(
            getattr(record, "certificate_status", "")
        ).upper() not in {"ISSUED", "VERIFIED", "AVAILABLE"}:
            excluded_uncertain.append(record_key)
            continue
        legacy_amount = getattr(record, "contract_amount", None)
        gross_amount = getattr(record, "gross_contract_amount_krw", None)
        certificate_amount = getattr(
            record, "recognized_performance_amount_krw", None
        )
        certificate_amount_is_net = getattr(
            record, "recognized_amount_is_net_of_share", None
        )
        amount = gross_amount if gross_amount is not None else legacy_amount
        amount_required = (
            scope.aggregation in {"SUM_AMOUNT", "MAX_SINGLE_AMOUNT"}
            or scope.minimum_single_contract_amount_krw > 0
        )
        supplied_amounts = tuple(
            value for value in (amount, certificate_amount) if value is not None
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in supplied_amounts
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
        if certificate_amount is not None and not isinstance(
            certificate_amount_is_net, bool
        ):
            # A distinct certificate amount without an explicit share basis is
            # ambiguous. Never guess whether the workbook already applied the
            # consortium share.
            excluded_uncertain.append(record_key)
            continue
        if scope.consortium_share_rule == "FULL_AMOUNT":
            if amount is None and amount_required:
                excluded_uncertain.append(record_key)
                continue
            recognized_amount = Decimal(amount or 0)
        elif scope.consortium_share_rule == "APPLY_SHARE":
            if certificate_amount is not None and certificate_amount_is_net:
                # Certificate-backed ``실적금액`` is already attributable to
                # the company. Applying ``share_pct`` again would double
                # deduct consortium participation.
                recognized_amount = Decimal(certificate_amount)
            else:
                pre_share_amount = (
                    certificate_amount
                    if certificate_amount is not None
                    else amount
                )
                if pre_share_amount is None and amount_required:
                    excluded_uncertain.append(record_key)
                    continue
                recognized_amount = Decimal(pre_share_amount or 0)
                recognized_amount *= Decimal(str(share)) / Decimal("100")
        else:
            source_amount = (
                certificate_amount if certificate_amount is not None else amount
            )
            if source_amount is None and amount_required:
                excluded_uncertain.append(record_key)
                continue
            recognized_amount = Decimal(source_amount or 0)
        candidate_records.append((record, recognized_amount))
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
    candidate_records.sort(
        key=lambda item: (
            str(getattr(item[0], "record_key", "")),
            int(getattr(item[0], "revision", 0) or 0),
        )
    )

    if excluded_uncertain:
        score_band_input = _score_band_input(
            scope,
            matched,
            candidate_records,
            complete=False,
            as_of_basis=as_of_basis,
            as_of_date=deadline,
        )
        digest = _performance_register_digest(
            scope,
            (item[0] for item in matched),
            as_of_basis=as_of_basis,
            as_of_date=deadline,
        )
        return DerivedPerformanceValue(
            status="REVIEW",
            lower_value=(score_band_input.lower_value if score_band_input else None),
            evidence_reference=f"PERFORMANCE-REGISTER:{digest[:20]}",
            evidence_sha256=digest,
            matched_record_keys=tuple(str(getattr(item[0], "record_key", "")) for item in matched),
            score_band_input=score_band_input,
            rationale=(
                "검증된 실적 중 발주기관·VAT·실적증명서·금액·공동수급 지분 조건을 확정할 수 "
                f"없는 기록이 {len(excluded_uncertain)}건 있어 자동 집계를 중지했습니다. "
                "확정 가능한 기록의 값은 score-band 민감도 검토용 하한으로만 제공합니다."
            ),
        )

    score_band_input = _score_band_input(
        scope,
        matched,
        candidate_records,
        complete=True,
        as_of_basis=as_of_basis,
        as_of_date=deadline,
    )
    if not matched and (
        score_band_input is None or (score_band_input.upper_value or 0) == 0
    ):
        return DerivedPerformanceValue(
            status="UNSCORABLE",
            score_band_input=score_band_input,
            rationale=(
                "원문 인정조건에 맞는 검증 완료 실적 증빙이 없습니다. "
                "누락된 실적을 0으로 추정하지 않고 자동 계산을 중지했습니다."
            ),
        )

    record_keys = tuple(str(getattr(item[0], "record_key", "")) for item in matched)
    scenario_record_keys = _eligible_record_keys(scope, candidate_records)
    scenario_record_key_set = set(scenario_record_keys)
    digest_records = (
        [
            item
            for item in candidate_records
            if str(getattr(item[0], "record_key", "")) in scenario_record_key_set
        ]
        if scope.vat_basis == "UNSPECIFIED"
        else matched
    )
    value = _aggregate_matched_value(scope, matched)
    digest = _performance_register_digest(
        scope,
        (item[0] for item in digest_records),
        as_of_basis=as_of_basis,
        as_of_date=deadline,
    )
    return DerivedPerformanceValue(
        status="ESTIMATED",
        value=value,
        lower_value=(score_band_input.lower_value if score_band_input else value),
        upper_value=(score_band_input.upper_value if score_band_input else value),
        evidence_reference=f"PERFORMANCE-REGISTER:{digest[:20]}",
        evidence_sha256=digest,
        matched_record_keys=(
            scenario_record_keys
            if scope.vat_basis == "UNSPECIFIED"
            else record_keys
        ),
        score_band_input=score_band_input,
        rationale=(
            f"운영자가 VALIDATED로 확정한 실적대장에 원문 인정조건을 적용해 {len(matched)}건을 "
            "결정론적으로 집계했습니다. "
            + (
                "원문에 VAT 기준이 없어 포함·제외 두 시나리오를 계산하고 그 범위를 "
                "score-band 입력으로 적용했습니다. "
                if scope.vat_basis == "UNSPECIFIED"
                else ""
            )
            + "발주기관의 최종 인정 전까지 ESTIMATED입니다."
        ),
    )
