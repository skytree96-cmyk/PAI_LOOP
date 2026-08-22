from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.models import CompanyFact, Evidence
from pai_loop.quantitative_rule_extraction import (
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QuantitativeEstimateRequest,
    QuantitativeFact,
    estimate_quantitative_score,
    quantitative_request_from_candidate_profile,
    resolve_verified_quantitative_facts,
)


def _validated_profile(
    *,
    metric: str = "BUSINESS_YEARS",
    unit: str = "년",
    fact_key: str = "company.business.years",
    label: str = "업력",
):
    attachment_id = "ATT-AUTO-ACTIVE"
    source = (
        f"{label} 20점\n"
        f"10{unit} 이상 20점\n"
        f"5{unit} 이상 10{unit} 미만 15점\n"
        f"5{unit} 미만 10점\n"
        "정량평가 총점 20점\n"
        "통과 최저점 12점\n"
    )

    def evidence(quote: str) -> dict:
        return {
            "attachment_id": attachment_id,
            "page": 1,
            "section": "정량평가표",
            "quote": quote,
            "confidence": 0.99,
        }

    payload = ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [
                {
                    "table_id": "TABLE-1",
                    "label": "정량평가",
                    "criteria": [
                        {
                            "criterion_id": metric,
                            "label": label,
                            "criterion_literal": f"{label} 20점",
                            "max_points": 20,
                            "scoring_method": "BRACKET",
                            "metric": metric,
                            "unit": unit,
                            "brackets": [
                                {
                                    "label": f"10{unit} 이상",
                                    "literal": f"10{unit} 이상 20점",
                                    "min_value": 10,
                                    "max_value": None,
                                    "min_inclusive": True,
                                    "max_inclusive": False,
                                    "points": 20,
                                    "evidence": evidence(f"10{unit} 이상 20점"),
                                },
                                {
                                    "label": f"5{unit} 이상 10{unit} 미만",
                                    "literal": f"5{unit} 이상 10{unit} 미만 15점",
                                    "min_value": 5,
                                    "max_value": 10,
                                    "min_inclusive": True,
                                    "max_inclusive": False,
                                    "points": 15,
                                    "evidence": evidence(
                                        f"5{unit} 이상 10{unit} 미만 15점"
                                    ),
                                },
                                {
                                    "label": f"5{unit} 미만",
                                    "literal": f"5{unit} 미만 10점",
                                    "min_value": None,
                                    "max_value": 5,
                                    "min_inclusive": False,
                                    "max_inclusive": False,
                                    "points": 10,
                                    "evidence": evidence(f"5{unit} 미만 10점"),
                                },
                            ],
                            "threshold": None,
                            "formula_literal": None,
                            "required_evidence": [fact_key],
                            "evidence": evidence(f"{label} 20점"),
                            "ambiguity_reason": None,
                        }
                    ],
                    "total_points": 20,
                    "total_evidence": evidence("정량평가 총점 20점"),
                    "minimum_score": 12,
                    "minimum_evidence": evidence("통과 최저점 12점"),
                    "ambiguity_reason": None,
                }
            ],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "기계 검증 가능한 정량평가표",
        }
    )
    manifest_sha256 = "b" * 64
    document_sha256 = "a" * 64
    record = validate_quantitative_attachment_extraction(
        payload,
        source_text=source,
        attachment_id=attachment_id,
        document_sha256=document_sha256,
        manifest_sha256=manifest_sha256,
    )
    return merge_validated_quantitative_records(
        [record],
        expected_documents={attachment_id: document_sha256},
        manifest_sha256=manifest_sha256,
    )


def _activation_request(profile):
    request = quantitative_request_from_candidate_profile(profile)
    result = estimate_quantitative_score(request)
    return request, result


def test_fully_machine_validated_rule_becomes_auto_active_but_not_auto_scored() -> None:
    request, result = _activation_request(_validated_profile())

    assert request.rule_source_status == "AVAILABLE"  # legacy compatibility
    assert request.source_validation_status == "SOURCE_VALIDATED"
    assert request.activation_status == "AUTO_ACTIVE"
    assert request.activation_reasons == []
    assert request.criteria[0].metric_key == "company.business.years"
    assert request.criteria[0].unit == "YEAR"
    assert request.criteria[0].fact_binding_sha256 is not None
    assert sorted(
        value
        for bracket in request.criteria[0].brackets
        for value in (bracket.min_value, bracket.max_value)
        if value is not None
    ) == [5, 5, 10, 10]
    assert result.overall_status == "UNSCORABLE"
    assert result.estimated_points is None


def test_generic_performance_amount_is_auto_active_but_never_scores_without_binding() -> None:
    profile = _validated_profile(
        metric="PERFORMANCE_AMOUNT",
        unit="억원",
        fact_key="company.performance.amount",
        label="유사사업 수행실적",
    )
    request = quantitative_request_from_candidate_profile(profile)
    generic_fact = _company_fact(
        fact_key="company.performance.amount",
        value={"value": 10, "unit": "억원"},
    )
    resolved = resolve_verified_quantitative_facts(
        request.criteria,
        [generic_fact],
        as_of=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))

    assert profile.status == "AVAILABLE"
    assert request.source_validation_status == "SOURCE_VALIDATED"
    assert request.activation_status == "AUTO_ACTIVE"
    assert request.activation_reasons == []
    assert request.criteria[0].fact_binding_sha256 is not None
    assert len(resolved) == 1
    assert resolved[0].status == "UNSCORABLE"
    assert result.overall_status == "UNSCORABLE"
    assert result.estimated_points is None
    assert "generic 값을" in result.criteria[0].rationale

    bound_fact = _company_fact(
        fact_key="company.performance.amount",
        value={
            "value": 10,
            "unit": "억원",
            "fact_binding_sha256": request.criteria[0].fact_binding_sha256,
        },
    )
    bound = resolve_verified_quantitative_facts(
        request.criteria,
        [bound_fact],
        as_of=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    bound_result = estimate_quantitative_score(
        request.model_copy(update={"facts": bound})
    )
    assert bound_result.overall_status == "CONFIRMED"
    assert bound_result.estimated_points == 20


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("coverage", "CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE"),
        ("anchor", "SOURCE_ANCHOR_INCOMPLETE"),
        ("total", "TABLE_TOTAL_MISMATCH"),
        ("alternative", "ALTERNATIVE_TABLE_AMBIGUOUS"),
        ("gap", "BRACKETS_NOT_EXHAUSTIVE_OR_OVERLAPPING"),
        ("unit", "UNSUPPORTED_UNIT"),
        ("unit_binding", "UNIT_NOT_SOURCE_BOUND"),
        ("dsl", "UNSUPPORTED_SCORING_DSL"),
        ("fact_key", "FACT_EVIDENCE_KEY_UNREGISTERED"),
        ("fact_key_duplicate", "FACT_KEY_AMBIGUOUS"),
    ],
)
def test_auto_activation_gate_is_fail_closed(mutation: str, expected_reason: str) -> None:
    profile = _validated_profile()
    candidate = profile.available_candidates[0]
    table = profile.tables[0]
    if mutation == "coverage":
        profile = profile.model_copy(update={"processed_attachment_ids": ()})
    elif mutation == "anchor":
        profile = profile.model_copy(
            update={
                "available_candidates": (
                    candidate.model_copy(
                        update={"evidence": candidate.evidence.model_copy(update={"quote": ""})}
                    ),
                )
            }
        )
    elif mutation == "total":
        profile = profile.model_copy(
            update={"tables": (table.model_copy(update={"total_points": 19}),)}
        )
    elif mutation == "alternative":
        profile = profile.model_copy(update={"tables": (table, table)})
    elif mutation == "gap":
        middle = next(item for item in candidate.brackets if item.min_value == 5)
        brackets = tuple(
            item.model_copy(update={"min_value": 6}) if item is middle else item
            for item in candidate.brackets
        )
        profile = profile.model_copy(
            update={
                "available_candidates": (
                    candidate.model_copy(update={"brackets": brackets}),
                )
            }
        )
    elif mutation in {"unit", "unit_binding"}:
        profile = profile.model_copy(
            update={
                "available_candidates": (
                    candidate.model_copy(
                        update={"unit": "달러" if mutation == "unit" else "YEAR"}
                    ),
                )
            }
        )
    elif mutation == "dsl":
        profile = profile.model_copy(
            update={
                "available_candidates": (
                    candidate.model_copy(
                        update={
                            "scoring_method": "FORMULA",
                            "brackets": (),
                            "formula_literal": "수행실적 금액 × 2",
                        }
                    ),
                )
            }
        )
    elif mutation == "fact_key":
        profile = profile.model_copy(
            update={
                "available_candidates": (
                    candidate.model_copy(update={"required_evidence": ("company.unknown",)}),
                )
            }
        )
    else:
        profile = profile.model_copy(
            update={"available_candidates": (candidate, candidate)}
        )

    request, result = _activation_request(profile)
    assert request.rule_source_status == "AVAILABLE"
    assert request.source_validation_status == "SOURCE_VALIDATED"
    assert request.activation_status == "REVIEW_REQUIRED"
    assert expected_reason in request.activation_reasons
    assert request.criteria == []
    assert result.overall_status == "REVIEW"
    assert result.total_max_points is None


def _company_fact(
    *,
    value=10,
    fact_key: str = "company.business.years",
    verified: bool = True,
    evidence_status: str = "VERIFIED",
    evidence_valid_until: datetime | None = None,
    with_evidence: bool = True,
) -> CompanyFact:
    as_of = datetime(2026, 8, 22, tzinfo=timezone.utc)
    evidence = (
        Evidence(
            evidence_key=f"E-{id(value)}-{evidence_status}-{fact_key}",
            name="검증 증빙",
            evidence_type="QUANTITATIVE_FACT",
            status=evidence_status,
            issued_at=as_of - timedelta(days=30),
            valid_until=evidence_valid_until,
        )
        if with_evidence
        else None
    )
    return CompanyFact(
        fact_key=fact_key,
        value=value,
        effective_from=as_of - timedelta(days=60),
        effective_to=None,
        verified=verified,
        evidence=evidence,
    )


def test_only_effective_verified_canonical_fact_with_valid_evidence_scores() -> None:
    request = quantitative_request_from_candidate_profile(_validated_profile())
    as_of = datetime(2026, 8, 22, tzinfo=timezone.utc)
    fact = _company_fact(
        value={
            "value": 10,
            "unit": "년",
            "fact_binding_sha256": request.criteria[0].fact_binding_sha256,
        }
    )

    resolved = resolve_verified_quantitative_facts(
        request.criteria,
        [fact],
        as_of=as_of,
    )
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))

    assert len(resolved) == 1
    assert resolved[0].value == 10
    assert resolved[0].evidence_key == "company.business.years"
    assert resolved[0].fact_binding_sha256 == request.criteria[0].fact_binding_sha256
    assert result.overall_status == "CONFIRMED"
    assert result.estimated_points == 20


@pytest.mark.parametrize(
    "fact",
    [
        _company_fact(verified=False),
        _company_fact(with_evidence=False),
        _company_fact(evidence_status="PENDING"),
        _company_fact(
            evidence_valid_until=datetime(2026, 8, 21, tzinfo=timezone.utc)
        ),
        _company_fact(fact_key="company.performance.amount"),
    ],
)
def test_unverified_expired_unconnected_or_unit_ambiguous_facts_are_ignored(
    fact: CompanyFact,
) -> None:
    request = quantitative_request_from_candidate_profile(_validated_profile())
    resolved = resolve_verified_quantitative_facts(
        request.criteria,
        [fact],
        as_of=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))

    assert resolved == []
    assert result.overall_status == "UNSCORABLE"
    assert result.estimated_points is None


def test_context_bound_fact_with_unsupported_unit_is_unscorable() -> None:
    request = quantitative_request_from_candidate_profile(_validated_profile())
    fact = _company_fact(
        value={
            "value": 10,
            "unit": "달러",
            "fact_binding_sha256": request.criteria[0].fact_binding_sha256,
        }
    )
    resolved = resolve_verified_quantitative_facts(
        request.criteria,
        [fact],
        as_of=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))

    assert len(resolved) == 1
    assert resolved[0].status == "UNSCORABLE"
    assert "단위" in resolved[0].rationale
    assert result.overall_status == "UNSCORABLE"


def test_duplicate_verified_canonical_facts_require_review() -> None:
    request = quantitative_request_from_candidate_profile(_validated_profile())
    binding = request.criteria[0].fact_binding_sha256
    resolved = resolve_verified_quantitative_facts(
        request.criteria,
        [
            _company_fact(
                value={"value": 9, "unit": "년", "fact_binding_sha256": binding}
            ),
            _company_fact(
                value={"value": 11, "unit": "년", "fact_binding_sha256": binding}
            ),
        ],
        as_of=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))

    assert len(resolved) == 2
    assert result.overall_status == "REVIEW"
    assert result.estimated_points is None
    assert "중복" in result.criteria[0].rationale


def test_available_rules_without_explicit_activation_never_score() -> None:
    active = quantitative_request_from_candidate_profile(_validated_profile())
    request = QuantitativeEstimateRequest(
        ruleset_version="legacy-available-is-not-active",
        rule_source_status="AVAILABLE",
        criteria=active.criteria,
        facts=[
            QuantitativeFact(
                metric_key="company.business.years",
                status="CONFIRMED",
                value=10,
                evidence_key="company.business.years",
            )
        ],
    )
    result = estimate_quantitative_score(request)

    assert result.activation_status == "REVIEW_REQUIRED"
    assert "AUTO_ACTIVATION_NOT_ESTABLISHED" in result.activation_reasons
    assert result.overall_status == "REVIEW"
    assert result.estimated_points is None
