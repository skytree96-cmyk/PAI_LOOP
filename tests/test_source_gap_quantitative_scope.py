"""Scope regression for the quantitative source-gap gate.

Every statement below is a verbatim production gap declaration. The gate used
to transcribe one regex per observed sentence and match with ``fullmatch``,
which recognised 6 of 3,027 distinct statements and withheld a quantitative
score whenever a model noted an unrelated omission. These cases pin the
classifying predicate that replaced it.
"""

from __future__ import annotations

import pytest

from pai_loop.source_gap_policy import (
    asserts_scoring_artifact_absence,
    is_quantitative_irrelevant_gap,
)

# The missing subject is the scoring rule itself, so no score can be derived.
BLOCKING_GAPS = [
    "제안요청서(붙임) 본문이 제공되지 않아 세부 평가배점표(정량평가 기준)를 확인할 수 없음",
    "제안서 평가 배점표(정량평가 기준표)가 본 공고문에는 포함되어 있지 않음",
    "정량평가 배점표(제안요청서 등)는 본 공고문에 포함되어 있지 않음",
    "제안요청서(과업지시서, 규격서 등) 및 기술평가 배점표 원문이 본 SOURCE에 "
    "포함되어 있지 않아 정량평가표를 확인할 수 없음",
    "정량 평가 20점, 정성 평가 80점의 세부 배점 기준(항목별 세부 평가표)이 "
    "본문에 제시되지 않음",
    "정량 평가 20점 항목의 세부 배점표(구간별 점수, 인정 기준 등)가 본 공고문 "
    "발췌본에 포함되어 있지 않음",
    "기술능력평가 세부 배점표(제안요청서)가 본 공고문에는 포함되어 있지 않아 "
    "정량적 채점 기준 확인 불가",
    "평가위원회 평가 배점표(세부 평가항목 및 배점 기준)가 본 문서에 포함되어 있지 않음",
    "제안서 평가의 정량적 배점표 및 정성적 평가기준은 본문에 포함되지 않아 확인할 수 없음",
    "입찰서류 제출기한, 제출방법, 평가기준 및 평가배점은 제공된 문서에서 확인되지 않음",
    "합성 첨부의 장비 수 평가 구간을 판독할 수 없습니다.",
]

# Nothing an objective score reads is missing, so the score must still be shown.
IRRELEVANT_GAPS = [
    # 정성평가를 의도적으로 뺐다는 설명은 결손이 아니라 범위 결정이다.
    "정성적 평가(80점) 세부 평가항목은 등급 척도(매우우수/우수/보통/미흡)만 "
    "제시되어 있어 정성 판단 항목으로 정량 테이블에서 제외됨",
    "정성적 평가(80점) 세부 항목은 우수/보통/미흡 등급제 판단 기준으로 "
    "정량적 배점표에서 제외됨",
    # 일정·서식·계약조건은 배점을 바꾸지 않는다.
    "입찰공고문(제출기한 등 구체 일정) 원문은 본 첨부에 포함되어 있지 않음",
    "제안서 제출기한의 구체적 날짜는 본문에 명시되지 않고 '공고문 명시'로만 "
    "표기되어 있어 실제 마감일자는 확인 불가",
    "제안서 평가 일정은 변동 가능하다고 기재되어 있음.",
    "추진 일정 표의 월별 수행 시점 표시가 본문 텍스트에서 확인되지 않음",
    "입찰공고서, 제안요청서 및 관련 계약조건은 본 문서에 첨부되지 않아 "
    "세부 입찰·계약 요건을 확인할 수 없음",
    "제안요청서의 세부 과업내용 및 별지 서식 원문이 본문에 포함되어 있지 않음.",
    "기술지원의 구체적인 응답시간과 서비스 수준은 명시되지 않음",
    # 근거 인용 위치 표기는 채점 대상이 아니다.
    "원문에 페이지 번호가 없어 페이지 단위 근거를 특정할 수 없음",
]


@pytest.mark.parametrize("gap", BLOCKING_GAPS)
def test_scoring_artifact_absence_still_blocks(gap: str) -> None:
    assert asserts_scoring_artifact_absence(gap) is True
    assert is_quantitative_irrelevant_gap(gap) is False


@pytest.mark.parametrize("gap", IRRELEVANT_GAPS)
def test_unrelated_omission_no_longer_withholds_a_score(gap: str) -> None:
    assert is_quantitative_irrelevant_gap(gap) is True


@pytest.mark.parametrize(
    "gap",
    [
        "배점표가 본문에 포함되지 않음",
        "배점표가 본문에 포함되어 있지 않음",
        "배점표가 본문에 제시되지 않음",
        "배점표가 본문에 명시되지 않았습니다",
        "채점 기준이 원문에서 확인되지 않음",
    ],
)
def test_every_negation_form_of_an_absence_claim_is_recognised(gap: str) -> None:
    """``-되지 않다`` and ``-되어 있지 않다`` must both count as an absence.

    Half of the stems once carried only the ``-어 있지`` form, so a plain
    ``제시되지 않음`` slipped through and released a score with no rule behind it.
    """

    assert asserts_scoring_artifact_absence(gap) is True


def test_empty_and_whitespace_gaps_are_not_absence_claims() -> None:
    for gap in ("", "   ", "​"):
        assert asserts_scoring_artifact_absence(gap) is False
        assert is_quantitative_irrelevant_gap(gap) is True
