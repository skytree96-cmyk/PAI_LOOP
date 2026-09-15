"""Set a row aside instead of letting it sink the notice.

A 배점표 mixes rows the company can be measured against with rows only a
review panel can answer -- 사업이해도, 제안서의 독창성, 기술능력평가 총괄.
Scoring treated the second kind the same as a row it merely failed to
validate, so each one returned REVIEW, and a single REVIEW makes the whole
notice REVIEW. Since almost every notice carries such a row, that one rule
withheld nearly every estimate.

A row set aside here retains its original maximum, reason and uncomputed
row-level range for audit. Its points are kept in ``out_of_scope_points`` and
excluded from all quantitative totals, bounds, readiness and coverage. Thus
"정량 40점 확보, 별도 정성 배점 60점" never implies that a panel will award
either zero or full marks. A minimum in a mixed request remains undecided
because the request does not bind it to the quantitative subtotal.

The classifier only ever fires on a row whose metric is outside the canonical
registry. A row the engine can actually score is never set aside.
"""

from __future__ import annotations

import re

OutOfScopeReason = str

# 심사위원 주관 평가. 회사 자료로는 계산할 수 없다.
_QUALITATIVE_RE = re.compile(
    r"이해도|접근\s*방법|수행\s*계획|추진\s*계획|방법론|계획서|추진\s*체계"
    r"|독창성|창의|충실도|적절성|우수성|완성도|질적\s*수준|타당성|구체성"
    r"|차별성|전문성|역량|기획|방안|부합|사후\s*관리|홍보|수행\s*능력"
)
# 개별 평가항목이 아니라 표의 머리말·합계·기준선.
_AGGREGATE_RE = re.compile(
    r"기술\s*능력\s*평가|기술\s*평가|정성적?\s*평가|정량적?\s*평가"
    r"|협상\s*적격|적격자\s*선정|협상\s*대상자|우선\s*협상"
    r"|종합\s*평(?:점|가)|합산\s*점수|총점|합계|소계"
    r"|평가\s*항목|평가\s*분야|평가\s*기준|배점\s*기준|배점표|심사\s*기준"
    r"|점수\s*산정|점수\s*산식|평점\s*산정"
)
# 우리 투찰가가 정해져야 계산되는 행. 회사 자료가 아니라 사람의 결정이다.
_BID_PRICE_RE = re.compile(
    r"입찰\s*가격|가격\s*평가|투찰|제안\s*가격|견적\s*가격|낙찰\s*률|낙찰\s*하한"
)
# 항목명이 아니라 번호 칸을 라벨로 잡은 추출 결함.
_DEGENERATE_RE = re.compile(r"^(?:[0-9]+(?:[-.][0-9]+)*|[A-Za-z]|[①-⑳])$")

_REASONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_AGGREGATE_RE, "표의 총괄·기준선 행이라 개별 배점 대상이 아닙니다."),
    (
        _BID_PRICE_RE,
        "투찰가가 정해져야 계산되는 항목이라 회사 자료만으로는 산정하지 않습니다.",
    ),
    (_QUALITATIVE_RE, "심사위원 정성평가 항목이라 회사 자료로 산정하지 않습니다."),
)


def out_of_scope_reason(
    *,
    label: str | None,
    criterion_literal: str | None,
    metric_in_registry: bool,
) -> OutOfScopeReason | None:
    """Say why this row is set aside, or None to score it normally.

    ``metric_in_registry`` gates everything: a row the engine has a metric for
    is scored, whatever its wording. Only rows the engine could not score
    anyway are eligible to be set aside, so this can never silently drop a
    criterion the company data covers.
    """

    if metric_in_registry:
        return None
    text = " ".join((label or "").split())
    if not text:
        text = " ".join((criterion_literal or "").split())
    if not text:
        return None

    compact = re.sub(r"\s+", "", text)
    if _DEGENERATE_RE.match(compact):
        return "평가항목명이 아니라 번호 칸이 추출되어 산정 대상이 아닙니다."

    for pattern, reason in _REASONS:
        if pattern.search(text):
            return reason
    return None


__all__ = ["OutOfScopeReason", "out_of_scope_reason"]
