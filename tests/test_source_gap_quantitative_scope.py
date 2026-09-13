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
        # -되지 않다 / -되어 있지 않다
        "배점표가 본문에 포함되지 않음",
        "배점표가 본문에 포함되어 있지 않음",
        "배점표가 본문에 제시되지 않음",
        "배점표가 본문에 명시되지 않았습니다",
        "채점 기준이 원문에서 확인되지 않음",
        # 미- 접두 (verbatim production statements)
        "기술평가 세부 배점표(항목별 점수) 미제공",
        "신용평가등급확인서 등급 기준표 미제시",
        "가격평가 산식 미제공",
        "평가 기준 및 정량평가표 미확인",
        "제안요청서 본문(세부과업내용, 제출서류, 평가배점표) 미첨부",
        # -하지 않다 / 불가 / 손상 (verbatim production statements)
        "정량평가(배점표) 관련 내용이 본 문서에 존재하지 않음",
        "가격점수 산식의 구체적 수식 기호(분수식)가 HWP 표 이미지로 되어 있어 텍스트로 추출 불가",
        "입찰가격 평점산식의 계산식 기호가 OCR 손상으로 완전히 판독되지 않음",
        "입찰가격 평가 계산식의 분수 서식이 표 손상으로 완전히 재현되지 않음",
    ],
)
def test_every_negation_form_of_an_absence_claim_is_recognised(gap: str) -> None:
    """Match the negation form, not a list of verbs.

    Enumerating stems leaked three times over: ``제시되지 않음`` (only the
    ``-어 있지`` form was listed), every ``배점표 미제공`` (only ``미포함`` was),
    then ``존재하지 않음`` / ``추출 불가`` / ``판독되지 않음``. Each named a scoring
    artifact and each was read as irrelevant, which releases a score with no
    rule behind it. 46 distinct production statements failed on the ``미-`` case
    alone.
    """

    assert asserts_scoring_artifact_absence(gap) is True


def test_a_missing_submission_document_is_not_a_scoring_artifact() -> None:
    """``증빙 미제공`` is procedural: paperwork, not a rule the score reads.

    Of the 8 production statements naming 증빙, the scoring-relevant ones say
    배점 outright and block on that; the rest are submission-document notes.
    """

    assert asserts_scoring_artifact_absence("필요한 증빙 미제공") is False
    assert asserts_scoring_artifact_absence("제출서류 및 증빙 요건 미확인") is False
    assert asserts_scoring_artifact_absence("가점사항 증빙서류의 구체적 배점 기준이 본 문서에 명시되지 않음") is True


def test_empty_and_whitespace_gaps_are_not_absence_claims() -> None:
    for gap in ("", "   ", "​"):
        assert asserts_scoring_artifact_absence(gap) is False
        assert is_quantitative_irrelevant_gap(gap) is True


# Synthetic labels/scores vary independently of the scope/method relationship.
SCOPED_EXCLUSION = (
    "정성평가(합성기획12, 운영계획18, 지원방안25) 및 가격평가(15)는 "
    "평가위원 정성 판단 또는 별도 가격산식에 의해 결정되어 "
    "계량 규칙을 특정할 수 없어 정량 테이블에서 제외함"
)


@pytest.mark.parametrize("gap", [
    SCOPED_EXCLUSION,
    SCOPED_EXCLUSION.replace(" ", ""),
    SCOPED_EXCLUSION.replace("정성평가", "정성적 평가").replace("(15)", "(15점)"),
    SCOPED_EXCLUSION.replace("정성평가", "정성\u200b평가").replace("(", "（").replace(")", "）"),
    "가격평가(25점) 및 정성평가(합성내용35점)는 별도의 가격 산식 또는 "
    "평가위원회의 정성적 판단으로 산정되므로 정량 평가표에서 제외됨.",
    "가격평가는 별도 가격산식으로 계산하므로 정량 테이블에서 제외함",
])
def test_explicit_scope_exclusion_binds_each_subject_to_its_separate_method(gap: str) -> None:
    assert asserts_scoring_artifact_absence(gap) is False
    assert is_quantitative_irrelevant_gap(gap) is True


@pytest.mark.parametrize("gap", [
    SCOPED_EXCLUSION.replace("가격평가(15)", "정량평가(15)"),
    SCOPED_EXCLUSION.replace("합성기획12", "신용등급12"),
    SCOPED_EXCLUSION.replace("합성기획12", "재무비율12"),
    SCOPED_EXCLUSION.replace("합성기획12", "정량실적12"),
    SCOPED_EXCLUSION.replace("가격평가(15)", "가격평가(산식 미제공)"),
    SCOPED_EXCLUSION.replace("별도 가격산식", "가격산식"),
    SCOPED_EXCLUSION.replace("별도 가격산식", "별도 산식"),
    SCOPED_EXCLUSION.replace("별도 가격산식", "판독 불가한 가격산식"),
    SCOPED_EXCLUSION.replace("제외함", "제외함. 가격평가 산식 미제공"),
    SCOPED_EXCLUSION + ". 정량 실적 배점표도 누락됨",
    SCOPED_EXCLUSION + "; 가격평가 산식을 확인할 수 없음",
    "가격평가 산식이 미제공되어 정성평가와 함께 정량 테이블에서 제외함",
    "정성평가 및 가격평가의 산식과 정량 배점표를 확인할 수 없어 정량 테이블에서 제외함",
])
def test_scope_exclusion_cannot_hide_objective_or_price_source_defects(gap: str) -> None:
    assert asserts_scoring_artifact_absence(gap) is True
    assert is_quantitative_irrelevant_gap(gap) is False


@pytest.mark.parametrize("extra_gaps,expected_status", [
    ([], "AVAILABLE"),
    (["합성 가격평가 산식 미제공"], "INCOMPLETE"),
    (["합성 정량 실적 배점표 일부 누락"], "INCOMPLETE"),
])
def test_scope_note_does_not_change_valid_tables_or_erase_a_separate_gap(
    extra_gaps: list[str], expected_status: str,
) -> None:
    from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction
    from test_quantitative_rule_extraction import ATTACHMENT_ID, VALID_SOURCE, payload_with_table

    original = payload_with_table()
    payload = original.model_copy(update={"missing_or_unreadable": [SCOPED_EXCLUSION, *extra_gaps]})
    record = validate_quantitative_attachment_extraction(
        payload, source_text=VALID_SOURCE, attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64, manifest_sha256="b" * 64,
    )
    assert record.status == expected_status
    assert bool(extra_gaps) == any(issue.code == "EXTRACTION_DECLARED_INCOMPLETE" for issue in record.issues)
    assert record.available_candidates
    assert payload.quantitative_tables == original.quantitative_tables
