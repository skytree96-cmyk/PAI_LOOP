from __future__ import annotations

import pytest

from pai_loop.quantitative_rule_extraction import (
    AttachmentDocumentBinding,
    ImmutableEvidenceAnchor,
    ImmutableQuantitativeBracket,
    ImmutableQuantitativeRuleCandidate,
    ImmutableQuantitativeTable,
    QuantitativeCandidateProfile,
    QuantitativeReviewCandidate,
    QuantitativeValidationIssue,
)
from pai_loop.quantitative_scoring import (
    QuantitativeFact,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
)


ATTACHMENT_ID = "ATT-PARTIAL"
TABLE_ID = "TABLE-PARTIAL"


def _anchor(quote: str) -> ImmutableEvidenceAnchor:
    return ImmutableEvidenceAnchor(
        attachment_id=ATTACHMENT_ID,
        page=2,
        section="정량평가표",
        quote=quote,
        confidence=1,
    )


def _mixed_review_profile() -> QuantitativeCandidateProfile:
    available_literal = "업력 20점"
    available = ImmutableQuantitativeRuleCandidate(
        source_attachment_id=ATTACHMENT_ID,
        table_id=TABLE_ID,
        criterion_id="business-years",
        label="업력",
        criterion_literal=available_literal,
        max_points=20,
        scoring_method="BRACKET",
        metric="BUSINESS_YEARS",
        unit="년",
        brackets=(
            ImmutableQuantitativeBracket(
                label="10년 이상",
                literal="10년 이상 20점",
                min_value=10,
                max_value=None,
                min_inclusive=True,
                max_inclusive=False,
                points=20,
                evidence=_anchor("10년 이상 20점"),
            ),
            ImmutableQuantitativeBracket(
                label="10년 미만",
                literal="10년 미만 10점",
                min_value=None,
                max_value=10,
                min_inclusive=False,
                max_inclusive=False,
                points=10,
                evidence=_anchor("10년 미만 10점"),
            ),
        ),
        threshold=None,
        formula_literal=None,
        required_evidence=("company.business.years",),
        evidence=_anchor(available_literal),
    )
    review = QuantitativeReviewCandidate(
        status="REVIEW",
        source_attachment_id=ATTACHMENT_ID,
        table_id=TABLE_ID,
        criterion_id="unmapped-criterion",
        label="기타 정량항목",
        max_points=10,
        scoring_method="UNKNOWN",
        metric="UNKNOWN",
        issue_codes=("UNKNOWN_METRIC",),
    )
    table = ImmutableQuantitativeTable(
        source_attachment_id=ATTACHMENT_ID,
        table_id=TABLE_ID,
        label="정량평가표",
        status="REVIEW",
        total_points=30,
        total_evidence=_anchor("정량평가 총점 30점"),
        minimum_score=None,
        minimum_evidence=None,
        criterion_ids=(available.criterion_id, review.criterion_id),
        available_criterion_ids=(available.criterion_id,),
        review_criterion_ids=(review.criterion_id,),
    )
    issue = QuantitativeValidationIssue(
        code="UNKNOWN_METRIC",
        disposition="REVIEW",
        message="정량 지표를 canonical 회사 사실 키에 연결할 수 없습니다.",
        attachment_id=ATTACHMENT_ID,
        table_id=TABLE_ID,
        criterion_id=review.criterion_id,
    )
    return QuantitativeCandidateProfile(
        status="REVIEW",
        manifest_sha256="a" * 64,
        document_bindings=(
            AttachmentDocumentBinding(
                attachment_id=ATTACHMENT_ID,
                document_sha256="b" * 64,
            ),
        ),
        expected_attachment_ids=(ATTACHMENT_ID,),
        processed_attachment_ids=(ATTACHMENT_ID,),
        tables=(table,),
        available_candidates=(available,),
        review_candidates=(review,),
        not_applicable_evidence=(),
        issues=(issue,),
    )


@pytest.mark.parametrize(
    ("with_fact", "available_status", "lower_points", "confirmed_points"),
    [
        (True, "CONFIRMED", 20, 20),
        (False, "UNSCORABLE", 0, 0),
    ],
)
def test_partial_activation_scores_only_verified_rows_and_preserves_full_denominator(
    with_fact: bool,
    available_status: str,
    lower_points: float,
    confirmed_points: float,
) -> None:
    request = quantitative_request_from_candidate_profile(_mixed_review_profile())

    assert request.rule_source_status == "AVAILABLE"
    assert request.source_validation_status == "REVIEW_REQUIRED"
    assert request.activation_status == "PARTIAL_ACTIVE"
    assert request.activation_reasons == ["UNKNOWN_METRIC"]
    assert len(request.criteria) == 1
    assert len(request.review_criteria) == 1
    assert request.review_criteria[0].label == "기타 정량항목"
    assert request.review_criteria[0].max_points == 10
    assert list(request.review_criteria[0].issue_codes) == ["UNKNOWN_METRIC"]

    if with_fact:
        binding = request.criteria[0].fact_binding_sha256
        assert binding is not None
        request = request.model_copy(
            update={
                "facts": [
                    QuantitativeFact(
                        metric_key="company.business.years",
                        status="CONFIRMED",
                        value=10,
                        evidence_key="company.business.years",
                        fact_binding_sha256=binding,
                        confidence=1,
                    )
                ]
            }
        )

    result = estimate_quantitative_score(request)

    assert result.activation_status == "PARTIAL_ACTIVE"
    assert result.overall_status == "REVIEW"
    assert result.total_max_points == 30
    assert result.confirmed_points == confirmed_points
    assert result.estimated_points is None
    assert result.lower_points == lower_points
    assert result.upper_points == 30
    assert result.unscorable_points == 30 - lower_points
    assert len(result.criteria) == 2

    available_result = next(item for item in result.criteria if item.label == "업력")
    assert available_result.status == available_status
    assert available_result.lower_points == lower_points
    assert available_result.upper_points == 20

    review_result = next(
        item for item in result.criteria if item.label == "기타 정량항목"
    )
    assert review_result.status == "REVIEW"
    assert review_result.estimated_points is None
    assert review_result.lower_points == 0
    assert review_result.upper_points == 10
    assert review_result.source_anchor is None
    assert "UNKNOWN_METRIC" in review_result.rationale


@pytest.mark.parametrize("unsafe_kind", ["incomplete", "global_issue"])
def test_partial_activation_remains_fail_closed_for_incomplete_or_global_issues(
    unsafe_kind: str,
) -> None:
    profile = _mixed_review_profile()
    review = profile.review_candidates[0]
    issue = profile.issues[0]
    table = profile.tables[0]
    if unsafe_kind == "incomplete":
        profile = profile.model_copy(
            update={
                "status": "INCOMPLETE",
                "review_candidates": (
                    review.model_copy(
                        update={
                            "status": "INCOMPLETE",
                            "issue_codes": ("REQUIRED_EVIDENCE_INCOMPLETE",),
                        }
                    ),
                ),
                "tables": (table.model_copy(update={"status": "INCOMPLETE"}),),
                "issues": (
                    issue.model_copy(
                        update={
                            "code": "REQUIRED_EVIDENCE_INCOMPLETE",
                            "disposition": "INCOMPLETE",
                        }
                    ),
                ),
            }
        )
        expected_reason = "REQUIRED_EVIDENCE_INCOMPLETE"
    else:
        profile = profile.model_copy(
            update={
                "issues": (
                    issue.model_copy(
                        update={
                            "code": "ATTACHMENT_INCOMPLETE",
                            "criterion_id": None,
                        }
                    ),
                ),
            }
        )
        expected_reason = "ATTACHMENT_INCOMPLETE"

    request = quantitative_request_from_candidate_profile(profile)
    result = estimate_quantitative_score(request)

    assert request.activation_status == "REVIEW_REQUIRED"
    assert expected_reason in request.activation_reasons
    assert request.criteria == []
    assert request.review_criteria == []
    assert result.overall_status == "REVIEW"
    assert result.total_max_points is None
    assert result.criteria == []

