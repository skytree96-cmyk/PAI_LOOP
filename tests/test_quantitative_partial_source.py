"""Partial subtotal from an unresolved manifest (PARTIAL_SOURCE).

An incomplete PPS manifest can still contain attachments whose rules passed
their own source validation. Notice scoring reports those as a subtotal and
names the unresolved remainder, without ever claiming a confirmed notice total.
Every other caller stays fail-closed.
"""

from __future__ import annotations

import pytest

from pai_loop import quantitative_scoring as qs
from pai_loop.quantitative_scoring import (
    QuantitativeEstimateRequest,
    QuantitativeReviewCriterion,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
)

from test_quantitative_partial_activation import _mixed_review_profile


def _incomplete_profile():
    return _mixed_review_profile().model_copy(update={"status": "INCOMPLETE"})


def test_incomplete_manifest_reports_validated_rows_as_a_partial_subtotal():
    request = quantitative_request_from_candidate_profile(
        _incomplete_profile(), allow_partial_source=True,
    )
    assert request.activation_status == "PARTIAL_SOURCE"
    assert request.rule_source_status == "INCOMPLETE"
    assert request.source_validation_status == "INCOMPLETE"
    assert request.criteria, "source-validated rows must survive"
    assert request.activation_reasons, "the unresolved remainder must be named"
    # A minimum printed on one table cannot judge a subtotal.
    assert request.minimum_score is None
    assert not request.review_criteria

    result = estimate_quantitative_score(request)
    # The unread remainder is never assumed to be zero, full or absent.
    assert result.overall_status == "REVIEW"
    assert result.meets_minimum is None
    assert result.total_max_points is not None
    assert result.lower_points is not None and result.upper_points is not None
    assert result.lower_points <= result.upper_points
    assert any("공고 총점이 아닙니다" in item for item in result.assumptions)


def test_partial_source_is_off_for_every_caller_that_did_not_ask():
    request = quantitative_request_from_candidate_profile(_incomplete_profile())
    assert request.activation_status == "REVIEW_REQUIRED"
    assert not request.criteria
    assert estimate_quantitative_score(request).lower_points is None


def test_an_incomplete_profile_without_validated_rows_stays_fail_closed():
    profile = _incomplete_profile().model_copy(update={"available_candidates": ()})
    request = quantitative_request_from_candidate_profile(
        profile, allow_partial_source=True,
    )
    assert request.activation_status == "REVIEW_REQUIRED"
    assert not request.criteria


def test_an_available_profile_is_unaffected_by_the_partial_source_switch():
    profile = _mixed_review_profile()
    baseline = quantitative_request_from_candidate_profile(profile)
    opted_in = quantitative_request_from_candidate_profile(
        profile, allow_partial_source=True,
    )
    assert opted_in.activation_status == baseline.activation_status
    assert opted_in.activation_status != "PARTIAL_SOURCE"
    assert len(opted_in.criteria) == len(baseline.criteria)


@pytest.mark.parametrize(
    "override",
    [
        {"rule_source_status": "AVAILABLE"},
        {"source_validation_status": "SOURCE_VALIDATED"},
        {"activation_reasons": []},
        {"minimum_score": 60.0},
    ],
)
def test_partial_source_contract_rejects_a_stronger_claim_than_the_source(override):
    request = quantitative_request_from_candidate_profile(
        _incomplete_profile(), allow_partial_source=True,
    )
    payload = request.model_dump(mode="python")
    payload.update(override)
    with pytest.raises(ValueError):
        QuantitativeEstimateRequest.model_validate(payload)


def test_partial_source_never_carries_review_rows():
    request = quantitative_request_from_candidate_profile(
        _incomplete_profile(), allow_partial_source=True,
    )
    payload = request.model_dump(mode="python")
    payload["review_criteria"] = [
        QuantitativeReviewCriterion(
            criterion_id="review-synthetic", category="OTHER", label="SYN",
            max_points=5, issue_codes=["UNKNOWN_METRIC"],
        ).model_dump(mode="python")
    ]
    with pytest.raises(ValueError):
        QuantitativeEstimateRequest.model_validate(payload)


def test_scoring_engine_refuses_a_partial_source_result_it_cannot_justify():
    request = quantitative_request_from_candidate_profile(
        _incomplete_profile(), allow_partial_source=True,
    )
    # Same rows, but the caller drops the reasons that name what is unresolved.
    unjustified = request.model_copy(update={"activation_status": "AUTO_ACTIVE"})
    assert estimate_quantitative_score(unjustified).lower_points is None


def test_verified_company_evidence_still_resolves_against_partial_rows():
    request = quantitative_request_from_candidate_profile(
        _incomplete_profile(), allow_partial_source=True,
    )
    bound = qs.bind_quantitative_company_inputs(request, (), (), as_of=None)
    # The resolver ran rather than returning the request untouched.
    assert bound is not request
    assert bound.activation_status == "PARTIAL_SOURCE"


@pytest.mark.parametrize("status", ["REVIEW_REQUIRED", "NOT_APPLICABLE"])
def test_inactive_requests_still_never_read_company_inputs(status):
    request = quantitative_request_from_candidate_profile(_incomplete_profile())
    assert request.activation_status == "REVIEW_REQUIRED"
    inactive = request.model_copy(update={"activation_status": status})

    def forbidden():
        raise AssertionError("an inactive request read company inputs")
        yield

    assert qs.bind_quantitative_company_inputs(
        inactive, forbidden(), (), as_of=None,
    ) is inactive


def test_notice_scoring_opts_in(monkeypatch):
    captured: dict[str, object] = {}
    original = qs.quantitative_request_from_candidate_profile

    def spy(profile, **kwargs):
        captured.update(kwargs)
        return original(profile, **kwargs)

    monkeypatch.setattr(qs, "_current_dynamic_quantitative_profile",
                        lambda notice: _incomplete_profile())
    monkeypatch.setattr(qs, "quantitative_request_from_candidate_profile", spy)

    class _Notice:
        deadline = None
        published_at = None

    qs.estimate_for_notice(_Notice())
    assert captured.get("allow_partial_source") is True
