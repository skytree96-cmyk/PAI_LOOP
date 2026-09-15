"""SYN-only integration checks for the shared company-evidence connection."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from pai_loop.quantitative_scoring import (
    QuantitativeFact,
    QuantitativeReviewCriterion,
    bind_quantitative_company_inputs,
    estimate_for_notice,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
)
from test_quantitative_financial_binding import (
    AS_OF, KEY, _company_fact, _criteria, _profile, _statement,
)


def test_one_shot_company_inputs_match_list_through_actual_financial_resolver():
    request = quantitative_request_from_candidate_profile(_profile())
    statement = _statement()
    before = request.model_dump(mode="json"), deepcopy(statement.value)
    listed = bind_quantitative_company_inputs(request, [statement], as_of=AS_OF)
    streamed = bind_quantitative_company_inputs(
        request, (fact for fact in [statement]), as_of=AS_OF,
    )

    assert listed.model_dump() == streamed.model_dump()
    result = estimate_quantitative_score(streamed)
    assert [(row.status, row.estimated_points) for row in result.criteria] == [
        ("ESTIMATED", 1.5), ("ESTIMATED", 2.5),
    ]
    assert (request.model_dump(mode="json"), statement.value) == before
    assert streamed is not request


def test_notice_runtime_uses_same_connection_for_one_shot_company_inputs(monkeypatch):
    profile = _profile()
    monkeypatch.setattr(
        "pai_loop.quantitative_scoring._current_dynamic_quantitative_profile",
        lambda _notice: profile,
    )
    notice = SimpleNamespace(deadline=AS_OF, published_at=AS_OF)
    listed = estimate_for_notice(notice, [_statement()])
    streamed = estimate_for_notice(notice, (fact for fact in [_statement()]))
    assert listed.model_dump() == streamed.model_dump()
    assert streamed.estimated_points == 4


def test_exact_confirmed_fact_wins_while_other_ratio_uses_statement():
    request = quantitative_request_from_candidate_profile(_profile())
    binding = _criteria(request)["EQUITY_TO_ASSETS"].fact_binding_sha256
    # The exact evidence says 80%; the separate SYN statement derives 40%.
    # Existing authority deliberately gives the exact verified fact precedence.
    fact = _company_fact(binding, 80)
    before = deepcopy(fact.value), deepcopy(fact.evidence.metadata_json)
    connected = bind_quantitative_company_inputs(
        request, [fact, _statement()], as_of=AS_OF,
    )
    result = estimate_quantitative_score(connected)
    assert [(row.status, row.estimated_points) for row in result.criteria] == [
        ("CONFIRMED", 2), ("ESTIMATED", 2.5),
    ]
    assert len(connected.facts) == 2
    assert (fact.value, fact.evidence.metadata_json) == before


@pytest.mark.parametrize("binding", [None, "f" * 64])
def test_generic_or_stale_fact_is_not_rebound_to_a_supported_rule(binding):
    request = quantitative_request_from_candidate_profile(_profile())
    fact = _company_fact(binding, 160)
    before = deepcopy(fact.value)
    connected = bind_quantitative_company_inputs(request, [fact], as_of=AS_OF)
    result = estimate_quantitative_score(connected)
    assert all(row.estimated_points is None for row in result.criteria)
    assert all(row.status != "CONFIRMED" for row in result.criteria)
    assert fact.value == before


def test_active_request_replaces_caller_scoring_facts_without_mutating_them():
    request = quantitative_request_from_candidate_profile(_profile())
    request.facts = [QuantitativeFact(
        metric_key=KEY, status="CONFIRMED", value=160,
        fact_binding_sha256=request.criteria[0].fact_binding_sha256,
        evidence_key="SYN-CALLER-ASSUMPTION", confidence=1,
    )]
    before = request.model_dump(mode="json")
    connected = bind_quantitative_company_inputs(request, as_of=AS_OF)
    result = estimate_quantitative_score(connected)
    assert all(row.estimated_points is None for row in result.criteria)
    assert all(fact.evidence_key != "SYN-CALLER-ASSUMPTION" for fact in connected.facts)
    assert request.model_dump(mode="json") == before


def test_partial_request_preserves_review_row_and_source_denominator():
    request = quantitative_request_from_candidate_profile(_profile())
    request = type(request).model_validate({
        **request.model_dump(),
        "activation_status": "PARTIAL_ACTIVE",
        "source_validation_status": "REVIEW_REQUIRED",
        "activation_reasons": ["SYN_ROW_REVIEW"],
        "review_criteria": [QuantitativeReviewCriterion(
            criterion_id="SYN-REVIEW", category="UNKNOWN", label="SYN unresolved row",
            max_points=5, issue_codes=["UNKNOWN_METRIC"],
        ).model_dump()],
    })
    before = request.model_dump(mode="json")
    connected = bind_quantitative_company_inputs(request, [_statement()], as_of=AS_OF)
    result = estimate_quantitative_score(connected)
    assert connected.activation_status == "PARTIAL_ACTIVE"
    assert connected.review_criteria == request.review_criteria
    assert connected.activation_reasons == ["SYN_ROW_REVIEW"]
    assert result.total_max_points == 10
    assert result.estimated_points is None
    assert next(row for row in result.criteria if row.criterion_id == "SYN-REVIEW").status == "REVIEW"
    assert sum(row.estimated_points or 0 for row in result.criteria) == 4
    assert request.model_dump(mode="json") == before


@pytest.mark.parametrize("status", ["REVIEW_REQUIRED", "NOT_APPLICABLE"])
def test_inactive_request_neither_resolves_nor_consumes_company_inputs(monkeypatch, status):
    request = quantitative_request_from_candidate_profile(_profile())
    request = type(request).model_validate({
        **request.model_dump(),
        "activation_status": status,
        "source_validation_status": "REVIEW_REQUIRED",
        "activation_reasons": ["SYN_SOURCE_UNVERIFIED"],
    })

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Inactive source request must not read company evidence")

    for name in ("verified_quantitative", "performance_register", "financial_register", "personnel_register"):
        monkeypatch.setattr(f"pai_loop.quantitative_scoring.resolve_{name}_facts", forbidden)

    class UnreadableInputs:
        def __iter__(self):
            forbidden()

    connected = bind_quantitative_company_inputs(
        request, UnreadableInputs(), UnreadableInputs(), as_of=AS_OF,
    )
    assert connected is request
