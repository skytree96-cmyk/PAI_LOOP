"""Recognize notices whose proposal evaluation has no company quantitative rows.

Many negotiated-contract RFPs score the technical part only by evaluator
judgement (우수/보통/미흡 grades, 적정성·타당성) and the rest by the bid price
formula. Their extraction correctly yields no company quantitative table, but
the manifest then reads as "table not established" and looks like a failure.

This module only reads the stored extraction summaries and gap statements of an
already complete manifest. It never yields points: a positive answer turns the
estimate into NOT_APPLICABLE. Anything that hints at a company-scored row
(정량 N점, 신용등급·실적 배점, 적격심사, company-fact row labels) keeps the
notice in review.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

QUALITATIVE_AND_PRICE_ONLY = "QUALITATIVE_AND_PRICE_ONLY"
QUALITATIVE_AND_PRICE_ONLY_REASON = (
    "제안서 평가표가 평가위원 정성평가와 입찰가격 평가로만 구성되어 회사 정량 항목이 없습니다."
)

_QUALITATIVE = re.compile(
    r"정성|우수\s*[/·~]\s*(?:보통|미흡)|매우\s*우수"
    r"|(?:평가|심사)위원의?\s*(?:주관|판단)"
)
_QUANTITATIVE_POINTS = re.compile(
    r"정량\s*(?:적\s*)?(?:평가)?\s*[(（]?\s*\d+(?:\.\d+)?\s*(?:점|%)"
    r"|정량\s*평가\s*[(（]\s*[가-힣]"
)
_COMPANY_SCORED = re.compile(
    r"(?:신용\s*(?:평가)?\s*등급|경영\s*상태|재무\s*(?:구조|상태)"
    r"|(?:유사|수행|이행)\s*(?:용역\s*)?실적|사업\s*수행\s*능력|기술\s*인력\s*보유)"
    r"[^.。\n]{0,40}(?:배점|\d+\s*점)"
)
_QUALIFICATION_REVIEW = re.compile(r"적격\s*심사")
_COMPANY_ROW_LABEL = re.compile(
    r"실적|경력|인력|신용|경영|재무|인증|보유|자격|매출|부채|상생|사회적"
)


def _attachment_text(result: Mapping[str, Any]) -> str:
    parts = [str(result.get("summary") or "")]
    gaps = result.get("missing_or_unreadable")
    if isinstance(gaps, (list, tuple)):
        parts.extend(str(item) for item in gaps)
    return " ".join(parts)


def qualitative_and_price_only(
    results: Sequence[Mapping[str, Any]],
    review_row_labels: Sequence[str] = (),
) -> bool:
    """True when every accepted attachment result says only 정성 + 가격 remain.

    ``results`` are the ACCEPTED extraction results of every current manifest
    attachment; the caller guarantees the manifest is complete.
    """

    if not results or not any(item.get("document_type") == "RFP" for item in results):
        return False
    if any(_COMPANY_ROW_LABEL.search(label or "") for label in review_row_labels):
        return False
    texts = [_attachment_text(item) for item in results]
    combined = " ".join(texts)
    if (
        _QUALIFICATION_REVIEW.search(combined)
        or _QUANTITATIVE_POINTS.search(combined)
        or _COMPANY_SCORED.search(combined)
    ):
        return False
    rfp_text = " ".join(
        text for item, text in zip(results, texts) if item.get("document_type") == "RFP"
    )
    return bool(_QUALITATIVE.search(rfp_text))
