from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from pai_loop.database import Base, build_engine, build_session_factory
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
    quantitative_company_fact_payload_sha256,
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


@pytest.mark.parametrize(
    ("metric", "fact_key", "unit"),
    [
        ("PERFORMANCE_AMOUNT", "company.performance.amount", "억원"),
        ("PERFORMANCE_COUNT", "company.performance.count", "건"),
    ],
)
def test_performance_rule_stays_review_required_until_fact_dimensions_are_modeled(
    metric: str,
    fact_key: str,
    unit: str,
) -> None:
    profile = _validated_profile(
        metric=metric,
        unit=unit,
        fact_key=fact_key,
        label="유사사업 수행실적",
    )
    request = quantitative_request_from_candidate_profile(profile)
    forged_exact_binding_fact = QuantitativeFact(
        metric_key=fact_key,
        status="CONFIRMED",
        value=10,
        evidence_key=fact_key,
        fact_binding_sha256="f" * 64,
        confidence=1,
    )
    result = estimate_quantitative_score(
        request.model_copy(update={"facts": [forged_exact_binding_fact]})
    )

    assert profile.status == "AVAILABLE"
    assert request.source_validation_status == "SOURCE_VALIDATED"
    assert request.activation_status == "REVIEW_REQUIRED"
    assert request.activation_reasons == ["FACT_DIMENSIONS_UNMODELED"]
    assert request.criteria == []
    assert result.overall_status == "REVIEW"
    assert result.estimated_points is None


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


def test_mixed_monetary_bound_scales_never_auto_activate() -> None:
    profile = _validated_profile(
        metric="PERFORMANCE_AMOUNT",
        unit="억원",
        fact_key="company.performance.amount",
        label="유사사업 수행실적",
    )
    candidate = profile.available_candidates[0]
    mixed_brackets = tuple(
        bracket.model_copy(
            update={
                "literal": bracket.literal.replace("5억원 미만", "5백만원 미만"),
                "evidence": bracket.evidence.model_copy(
                    update={
                        "quote": bracket.evidence.quote.replace(
                            "5억원 미만",
                            "5백만원 미만",
                        )
                    }
                ),
            }
        )
        for bracket in candidate.brackets
    )
    profile = profile.model_copy(
        update={
            "available_candidates": (
                candidate.model_copy(update={"brackets": mixed_brackets}),
            )
        }
    )

    request, result = _activation_request(profile)

    assert request.activation_status == "REVIEW_REQUIRED"
    assert "BOUND_UNIT_INCONSISTENT" in request.activation_reasons
    assert request.criteria == []
    assert result.overall_status == "REVIEW"


def test_header_unit_can_be_inherited_by_unitless_numeric_rows() -> None:
    profile = _validated_profile()
    candidate = profile.available_candidates[0]
    unitless_brackets = tuple(
        bracket.model_copy(
            update={
                "literal": bracket.literal.replace("년", ""),
                "evidence": bracket.evidence.model_copy(
                    update={"quote": bracket.evidence.quote.replace("년", "")}
                ),
            }
        )
        for bracket in candidate.brackets
    )
    criterion_literal = "업력 20점 (단위: 년)"
    candidate = candidate.model_copy(
        update={
            "criterion_literal": criterion_literal,
            "evidence": candidate.evidence.model_copy(
                update={"quote": criterion_literal}
            ),
            "brackets": unitless_brackets,
        }
    )
    profile = profile.model_copy(update={"available_candidates": (candidate,)})

    request, _result = _activation_request(profile)

    assert request.activation_status == "AUTO_ACTIVE"
    assert "BOUND_UNIT_INCONSISTENT" not in request.activation_reasons


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
    binding = (
        str(value.get("fact_binding_sha256"))
        if isinstance(value, dict) and value.get("fact_binding_sha256")
        else None
    )
    evidence_id = "e" * 36
    evidence = (
        Evidence(
            id=evidence_id,
            evidence_key=f"E-{id(value)}-{evidence_status}-{fact_key}",
            name="검증 증빙",
            evidence_type="QUANTITATIVE_FACT",
            status=evidence_status,
            issued_at=as_of - timedelta(days=30),
            valid_until=evidence_valid_until,
            source_location="evidence://quantitative-fact",
            sha256="c" * 64,
            metadata_json={
                "quantitative_fact_key": fact_key,
                "fact_binding_sha256": binding,
            },
        )
        if with_evidence
        else None
    )
    fact = CompanyFact(
        fact_key=fact_key,
        value=value,
        effective_from=as_of - timedelta(days=60),
        effective_to=None,
        verified=verified,
        evidence_id=evidence_id if evidence is not None else None,
        evidence=evidence,
    )
    if evidence is not None:
        evidence.metadata_json = {
            **(evidence.metadata_json or {}),
            "company_fact_payload_sha256": (
                quantitative_company_fact_payload_sha256(fact)
            ),
        }
    return fact


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
    assert resolved[0].evidence_reference == fact.evidence.evidence_key
    assert resolved[0].evidence_sha256 == "c" * 64
    assert resolved[0].fact_binding_sha256 == request.criteria[0].fact_binding_sha256
    assert result.overall_status == "CONFIRMED"
    assert result.estimated_points == 20


def test_company_fact_payload_digest_survives_sqlite_kst_round_trip() -> None:
    request = quantitative_request_from_candidate_profile(_validated_profile())
    binding = request.criteria[0].fact_binding_sha256
    assert binding is not None
    kst = timezone(timedelta(hours=9))
    evidence = Evidence(
        evidence_key="E-KST-ROUNDTRIP",
        name="KST 회사 사실 증빙",
        evidence_type="QUANTITATIVE_FACT",
        status="VERIFIED",
        issued_at=datetime(2026, 7, 1, tzinfo=kst),
        source_location="evidence://kst-roundtrip",
        sha256="9" * 64,
        metadata_json={
            "quantitative_fact_key": "company.business.years",
            "fact_binding_sha256": binding,
        },
    )
    fact = CompanyFact(
        fact_key="company.business.years",
        value={
            "value": 10,
            "unit": "년",
            "fact_binding_sha256": binding,
        },
        effective_from=datetime(2026, 7, 1, 9, tzinfo=kst),
        effective_to=datetime(2026, 12, 31, 18, tzinfo=kst),
        evidence=evidence,
        verified=True,
        source="MANUAL",
    )
    evidence.metadata_json = {
        **(evidence.metadata_json or {}),
        "company_fact_payload_sha256": quantitative_company_fact_payload_sha256(
            fact
        ),
    }
    expected_digest = evidence.metadata_json["company_fact_payload_sha256"]
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        session.add(fact)
        session.commit()
    with factory() as session:
        reloaded = session.scalar(
            select(CompanyFact).where(
                CompanyFact.fact_key == "company.business.years"
            )
        )
        assert reloaded is not None
        assert quantitative_company_fact_payload_sha256(reloaded) == expected_digest
        resolved = resolve_verified_quantitative_facts(
            request.criteria,
            [reloaded],
            as_of=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
        result = estimate_quantitative_score(
            request.model_copy(update={"facts": resolved})
        )
        assert resolved[0].status == "CONFIRMED"
        assert result.overall_status == "CONFIRMED"
        assert result.estimated_points == 20
    engine.dispose()


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


@pytest.mark.parametrize("unit_payload", [{}, {"unit": ""}])
def test_context_bound_fact_requires_explicit_nonblank_unit(
    unit_payload: dict[str, str],
) -> None:
    request = quantitative_request_from_candidate_profile(_validated_profile())
    fact = _company_fact(
        value={
            "value": 10,
            "fact_binding_sha256": request.criteria[0].fact_binding_sha256,
            **unit_payload,
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
    assert "명시적인 단위" in resolved[0].rationale
    assert result.overall_status == "UNSCORABLE"
    assert result.estimated_points is None


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("unrelated_key", "canonical 회사 사실 키"),
        ("wrong_binding", "조건 binding"),
        ("wrong_type", "증빙 유형"),
        ("missing_source", "원본 위치"),
        ("missing_digest", "콘텐츠 해시"),
        ("detached_row", "실제 증빙 행"),
        ("missing_payload_digest", "사실 값·단위·유효기간 payload"),
        ("mutated_fact_value", "사실 값·단위·유효기간 payload"),
    ],
)
def test_dynamic_fact_requires_exact_immutable_evidence_binding(
    mutation: str,
    expected_reason: str,
) -> None:
    request = quantitative_request_from_candidate_profile(_validated_profile())
    binding = request.criteria[0].fact_binding_sha256
    fact = _company_fact(
        value={"value": 10, "unit": "년", "fact_binding_sha256": binding}
    )
    assert fact.evidence is not None
    if mutation == "unrelated_key":
        fact.evidence.metadata_json = {
            "quantitative_fact_key": "company.unrelated.value",
            "fact_binding_sha256": binding,
        }
    elif mutation == "wrong_binding":
        fact.evidence.metadata_json = {
            "quantitative_fact_key": fact.fact_key,
            "fact_binding_sha256": "d" * 64,
        }
    elif mutation == "wrong_type":
        fact.evidence.evidence_type = "UNRELATED_VERIFIED_DOCUMENT"
    elif mutation == "missing_source":
        fact.evidence.source_location = None
    elif mutation == "missing_digest":
        fact.evidence.sha256 = None
    elif mutation == "detached_row":
        fact.evidence_id = "f" * 36
    elif mutation == "missing_payload_digest":
        fact.evidence.metadata_json = {
            key: value
            for key, value in (fact.evidence.metadata_json or {}).items()
            if key != "company_fact_payload_sha256"
        }
    elif mutation == "mutated_fact_value":
        fact.value = {
            "value": 999,
            "unit": "년",
            "fact_binding_sha256": binding,
        }

    resolved = resolve_verified_quantitative_facts(
        request.criteria,
        [fact],
        as_of=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))

    assert len(resolved) == 1
    assert resolved[0].status == "UNSCORABLE"
    assert expected_reason in resolved[0].rationale
    assert result.overall_status == "UNSCORABLE"
    assert result.estimated_points is None


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
