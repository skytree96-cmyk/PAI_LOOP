"""같은 배점표 한 줄을 여러 첨부에서 읽었을 때 배점을 두 번 세지 않는다.

운영에서 두 가지 형태가 나왔다.

  한국투자공사   같은 제안요청서를 hwp 와 pdf 로 함께 첨부했다. 문서 sha256 이 달라
                결속 해시도 달라지므로 기존 중복 탐지에 걸리지 않았다. 경영상태 5점이
                10점으로 합산됐다.
  한국과학기술연구원  같은 파일이 첨부 두 개로 등록됐다. 문서·표·항목 식별자가 모두
                같고 첨부 id 만 달랐다. 경영상태 10점이 20점이 됐다.

원문 배점표의 "경영상태 5점" 이 10점이 될 수는 없다. 합산은 어느 경우에도 틀린다.
"""

from __future__ import annotations

import pytest

from pai_loop.quantitative_rule_extraction import (
    AttachmentDocumentBinding,
    ImmutableEvidenceAnchor,
    ImmutableQuantitativeCase,
    ImmutableQuantitativeRuleCandidate,
    ImmutableQuantitativeTable,
    QuantitativeCandidateProfile,
    QuantitativeValidationIssue,
)
from pai_loop.quantitative_scoring import quantitative_request_from_candidate_profile

QUOTE = "경영상태 경영 및 재무상태(신용평가등급) 5"
CASE_ROWS = (
    ("AAA, AA+, AA0, AA-, A+, A0, A-, BBB+, BBB0 배점의 100%",
     ("AAA", "AA+", "AA0", "AA-", "A+", "A0", "A-", "BBB+", "BBB0"), 100),
    ("BBB-, BB+, BB0, BB- 배점의 95%", ("BBB-", "BB+", "BB0", "BB-"), 95),
    ("B+, B0, B- 배점의 90%", ("B+", "B0", "B-"), 90),
    ("CCC+ 이하 배점의 70%", ("CCC+ 이하",), 70),
)


def _anchor(attachment_id: str, quote: str, *, page: int | None, section: str):
    return ImmutableEvidenceAnchor(
        attachment_id=attachment_id, page=page, section=section, quote=quote, confidence=1
    )


def _credit_candidate(
    *,
    attachment_id: str,
    table_id: str,
    criterion_id: str,
    label: str = "경영상태",
    section: str = "기술제안서 평가항목 및 배점한도",
    page: int | None = None,
    quote: str = QUOTE,
    max_points: float = 5,
) -> ImmutableQuantitativeRuleCandidate:
    literal = "제안업체 경영상태 신용평가등급 평가"
    return ImmutableQuantitativeRuleCandidate(
        source_attachment_id=attachment_id,
        table_id=table_id,
        criterion_id=criterion_id,
        label=label,
        criterion_literal=literal,
        max_points=max_points,
        scoring_method="CASE_TABLE",
        metric="CREDIT_RATING",
        unit="등급",
        brackets=(),
        threshold=None,
        formula_literal=None,
        cases=tuple(
            ImmutableQuantitativeCase(
                literal=case_literal,
                operator="IN",
                comparison_value=None,
                category_values=values,
                award_kind="PERCENT_OF_MAX",
                award_value=award,
                row_order=order,
                evidence=_anchor(attachment_id, case_literal, page=page, section=section),
            )
            for order, (case_literal, values, award) in enumerate(CASE_ROWS, start=1)
        ),
        required_evidence=("company.credit_rating",),
        evidence=_anchor(attachment_id, quote, page=page, section=section),
    )


def _table(candidate: ImmutableQuantitativeRuleCandidate) -> ImmutableQuantitativeTable:
    return ImmutableQuantitativeTable(
        source_attachment_id=candidate.source_attachment_id,
        table_id=candidate.table_id,
        label=f"physical-{candidate.table_id}",
        status="AVAILABLE",
        total_points=candidate.max_points,
        total_evidence=_anchor(
            candidate.source_attachment_id,
            f"{candidate.table_id} 소계 {candidate.max_points:g}점",
            page=candidate.evidence.page,
            section=candidate.evidence.section,
        ),
        minimum_score=None,
        minimum_evidence=None,
        criterion_ids=(candidate.criterion_id,),
        available_criterion_ids=(candidate.criterion_id,),
        review_criterion_ids=(),
    )


def _profile(*candidates: ImmutableQuantitativeRuleCandidate) -> QuantitativeCandidateProfile:
    """운영 공고가 실제로 놓인 상태, 곧 부분 원문(PARTIAL_SOURCE)으로 만든다.

    배점표를 통째로 확보한 공고는 ``_logical_quantitative_program`` 이 같은 표를 두 번
    읽은 것을 이미 모호성으로 잡아 REVIEW 로 돌린다. 그러나 부분 원문 경로는 그 검사를
    건너뛰고 후보를 그대로 쓴다. 저장된 공고 23건이 전부 이 경로에 있다.
    """

    attachments = tuple(dict.fromkeys(c.source_attachment_id for c in candidates))
    return QuantitativeCandidateProfile(
        status="INCOMPLETE",
        manifest_sha256="a" * 64,
        document_bindings=tuple(
            AttachmentDocumentBinding(
                attachment_id=attachment_id,
                document_sha256=f"{index + 1:x}".rjust(64, "b"),
            )
            for index, attachment_id in enumerate(attachments)
        ),
        expected_attachment_ids=attachments,
        processed_attachment_ids=attachments,
        tables=tuple(_table(c) for c in candidates),
        available_candidates=candidates,
        review_candidates=(),
        not_applicable_evidence=(),
        issues=(
            QuantitativeValidationIssue(
                code="ATTACHMENT_UNRESOLVED",
                disposition="INCOMPLETE",
                message="첨부 하나를 아직 읽지 못했습니다.",
            ),
        ),
    )


def _credit_criteria(profile):
    request = quantitative_request_from_candidate_profile(profile, allow_partial_source=True)
    assert request.activation_status == "PARTIAL_SOURCE", request.activation_reasons
    return [c for c in request.criteria if c.metric_key == "company.credit_rating"]


def test_the_same_row_read_from_two_file_formats_is_scored_once():
    # 같은 제안요청서의 hwp 와 pdf. 형식이 다르니 라벨과 구역명도 갈린다.
    hwp = _credit_candidate(
        attachment_id="ATT-RFP-HWP", table_id="TBL-001", criterion_id="credit_rating",
        label="경영상태(신용평가등급)", section="Ⅶ. 기술제안서 평가항목 및 배점한도",
    )
    pdf = _credit_candidate(
        attachment_id="ATT-RFP-PDF", table_id="TBL-1-CREDIT-RATING",
        criterion_id="CRIT-CREDIT-RATING", label="경영상태", section="프로젝트 관리", page=33,
    )
    criteria = _credit_criteria(_profile(hwp, pdf))
    assert len(criteria) == 1
    assert criteria[0].max_points == 5


def test_the_same_file_registered_as_two_attachments_is_scored_once():
    first = _credit_candidate(
        attachment_id="ATT-A", table_id="T1", criterion_id="CREDIT_RATING_1", max_points=10
    )
    second = _credit_candidate(
        attachment_id="ATT-B", table_id="T1", criterion_id="CREDIT_RATING_1", max_points=10
    )
    criteria = _credit_criteria(_profile(first, second))
    assert len(criteria) == 1
    assert criteria[0].max_points == 10


def test_the_surviving_row_is_the_same_whichever_order_they_arrive_in():
    # 실행마다 다른 쪽이 살아남으면 결속 해시가 흔들리고, 운영자가 맺어 둔 결속이
    # 까닭 없이 무효가 된다.
    hwp = _credit_candidate(
        attachment_id="ATT-RFP-HWP", table_id="TBL-001", criterion_id="credit_rating"
    )
    pdf = _credit_candidate(
        attachment_id="ATT-RFP-PDF", table_id="TBL-2", criterion_id="CRIT-CREDIT"
    )
    forward = _credit_criteria(_profile(hwp, pdf))
    backward = _credit_criteria(_profile(pdf, hwp))
    assert forward[0].source_anchor.document_label == backward[0].source_anchor.document_label


def test_two_different_rows_in_one_notice_still_both_count():
    # 접기가 과하면 진짜로 다른 배점 줄을 잃는다. 인용이 다르면 다른 줄이다.
    first = _credit_candidate(
        attachment_id="ATT-A", table_id="T1", criterion_id="C1",
        quote="경영상태 신용평가등급 5", max_points=5,
    )
    second = _credit_candidate(
        attachment_id="ATT-A", table_id="T1", criterion_id="C2",
        quote="대외신인도 신용평가등급 가점 2", max_points=2,
    )
    criteria = _credit_criteria(_profile(first, second))
    assert len(criteria) == 2
    assert sorted(c.max_points for c in criteria) == [2.0, 5.0]


def test_rows_with_the_same_quote_but_different_points_both_count():
    # 같은 문장을 인용했어도 배점이 다르면 같은 줄이라고 단정할 수 없다.
    first = _credit_candidate(attachment_id="ATT-A", table_id="T1", criterion_id="C1", max_points=5)
    second = _credit_candidate(attachment_id="ATT-B", table_id="T2", criterion_id="C2", max_points=10)
    assert len(_credit_criteria(_profile(first, second))) == 2


def test_a_row_without_a_source_quote_is_never_folded():
    # 인용이 없으면 무엇을 근거로 같다고 할 수 없다. 접지 않는 편이 안전하다.
    first = _credit_candidate(
        attachment_id="ATT-A", table_id="T1", criterion_id="C1", quote="",
    )
    second = _credit_candidate(
        attachment_id="ATT-B", table_id="T2", criterion_id="C2", quote="",
    )
    assert len(_credit_criteria(_profile(first, second))) == 2


def test_a_single_row_is_untouched():
    only = _credit_candidate(attachment_id="ATT-A", table_id="T1", criterion_id="C1")
    criteria = _credit_criteria(_profile(only))
    assert len(criteria) == 1
    assert criteria[0].max_points == 5
    assert criteria[0].fact_binding_sha256


def test_folding_does_not_change_the_surviving_binding():
    # 남은 쪽의 결속 해시는 그 후보 단독으로 계산했을 때와 같아야 한다. 달라지면
    # 이미 맺어 둔 결속이 깨진다.
    hwp = _credit_candidate(
        attachment_id="ATT-RFP-HWP", table_id="TBL-001", criterion_id="credit_rating"
    )
    pdf = _credit_candidate(
        attachment_id="ATT-RFP-PDF", table_id="TBL-2", criterion_id="CRIT-CREDIT"
    )
    folded = _credit_criteria(_profile(hwp, pdf))[0]
    alone = _credit_criteria(_profile(hwp))[0]
    assert folded.fact_binding_sha256 == alone.fact_binding_sha256


@pytest.mark.parametrize("points", [3.0, 5.0, 10.0, 15.0])
def test_the_total_never_doubles_whatever_the_row_is_worth(points):
    first = _credit_candidate(
        attachment_id="ATT-A", table_id="T1", criterion_id="C1", max_points=points
    )
    second = _credit_candidate(
        attachment_id="ATT-B", table_id="T2", criterion_id="C2", max_points=points
    )
    criteria = _credit_criteria(_profile(first, second))
    assert sum(c.max_points for c in criteria) == points
