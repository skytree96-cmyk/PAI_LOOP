"""SYN-only regression: recognition revisions must not reuse old attestations.

All source validation, register derivation and CompanyFact resolution are local
and in memory. No database, provider, or operational company input is involved.
"""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import pai_loop.quantitative_scoring as scoring
from pai_loop.quantitative_rule_extraction import merge_validated_quantitative_records
from test_dense_case_source_binding import credit_fixture, record as credit_record
from test_quantitative_auto_activation import _company_fact
from test_quantitative_count_ranges import ATT, DOC, MANIFEST, fixture, record
from test_quantitative_financial_binding import _profile as financial_profile


AS_OF = datetime(2026, 8, 22, tzinfo=timezone.utc)


def _legacy_binding(candidate, document_sha256=DOC):
    """The released raw-only contract, independently reconstructed."""
    raw = candidate.model_dump(mode="json")
    for case in raw["cases"]:
        if case.get("comparison_upper_value") is None:
            case.pop("comparison_upper_value", None)
    payload = {
        "binding_schema": "pai-loop-quantitative-fact-binding-1.0.0",
        "document_sha256": document_sha256,
        "candidate": raw,
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _performance_profile(*, manual=False):
    raw, source = fixture()
    if manual:
        condition = raw["quantitative_tables"][0]["criteria"][0]["recognition_conditions"][0]
        original = condition["literal"]
        condition["literal"] += ", 계약별 50인 이상"
        condition["evidence"]["quote"] = condition["literal"]
        source = source.replace(original, condition["literal"])
    stored = record(raw, source)
    assert stored.status == "AVAILABLE", stored.issues
    profile = merge_validated_quantitative_records(
        [stored], expected_documents={ATT: DOC}, manifest_sha256=MANIFEST,
    )
    assert profile.status == "AVAILABLE", profile.issues
    return profile


def _bound_company_fact(criterion, binding, value=7):
    return _company_fact(
        fact_key=criterion.metric_key,
        value={"value": value, "unit": criterion.unit, "fact_binding_sha256": binding},
    )


def _request(profile):
    request = scoring.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    return request


@pytest.fixture
def semantic_caches():
    # Production deployment starts a new process; monkeypatches in this test
    # must also discard cached interpretations of the same immutable source.
    for cache in (scoring._performance_scope_semantic_fingerprint,
                  scoring._candidate_semantic_signature):
        cache.cache_clear()
    yield
    for cache in (scoring._performance_scope_semantic_fingerprint,
                  scoring._candidate_semantic_signature):
        cache.cache_clear()


@pytest.mark.parametrize("manual", [False, True])
def test_old_raw_only_performance_attestation_cannot_score_current_recognition(manual):
    profile = _performance_profile(manual=manual)
    before = profile.model_dump(mode="json")
    request = _request(profile)
    criterion = request.criteria[0]
    old_binding = _legacy_binding(profile.available_candidates[0])
    assert old_binding != criterion.fact_binding_sha256

    old_fact = _bound_company_fact(criterion, old_binding)
    resolved = scoring.resolve_verified_quantitative_facts(
        request.criteria, [old_fact], as_of=AS_OF,
    )
    assert not any(item.status == "CONFIRMED" for item in resolved)
    result = scoring.estimate_quantitative_score(request.model_copy(update={"facts": resolved}))
    assert result.estimated_points is None
    assert result.criteria[0].status != "CONFIRMED"
    assert profile.model_dump(mode="json") == before

    # A newly attested aggregate for the exact current recognition still works,
    # including manual conditions. It is not inferred from the source itself.
    current_fact = _bound_company_fact(criterion, criterion.fact_binding_sha256)
    current = scoring.resolve_verified_quantitative_facts(
        request.criteria, [current_fact], as_of=AS_OF,
    )
    current_result = scoring.estimate_quantitative_score(
        request.model_copy(update={"facts": current}),
    )
    assert current_result.criteria[0].status == "CONFIRMED"
    assert current_result.estimated_points == 5


@pytest.mark.parametrize("semantic_change", [
    {"lookback_years": 2},
    {"similarity_keywords": ("SYN 별도 인정업무",)},
    {"consortium_share_rule": "APPLY_SHARE"},
    {"counterparty_scope": "PUBLIC_SECTOR", "counterparty_keywords": ("공공기관",)},
])
def test_parser_semantic_change_rebinds_the_same_raw_and_document(
    monkeypatch, semantic_caches, semantic_change,
):
    profile = _performance_profile()
    candidate = profile.available_candidates[0]
    original_candidate = candidate.model_dump(mode="json")
    previous = _request(profile)
    previous_criterion = previous.criteria[0]
    new_scope = previous_criterion.performance_scope.model_copy(update=semantic_change)
    monkeypatch.setattr(scoring, "parse_performance_recognition_scope", lambda *args, **kwargs: new_scope)
    scoring._performance_scope_semantic_fingerprint.cache_clear()
    scoring._candidate_semantic_signature.cache_clear()

    revised = _request(profile)
    assert revised.criteria[0].performance_scope == new_scope
    assert revised.criteria[0].fact_binding_sha256 != previous_criterion.fact_binding_sha256
    previous_fact = _bound_company_fact(previous_criterion, previous_criterion.fact_binding_sha256)
    resolved = scoring.resolve_verified_quantitative_facts(
        revised.criteria, [previous_fact], as_of=AS_OF,
    )
    assert not any(item.status == "CONFIRMED" for item in resolved)
    result = scoring.estimate_quantitative_score(revised.model_copy(update={"facts": resolved}))
    assert result.estimated_points is None
    assert candidate.model_dump(mode="json") == original_candidate


def test_old_confirmed_attestation_does_not_override_current_register_review(monkeypatch):
    profile = _performance_profile(manual=True)
    request = _request(profile)
    criterion = request.criteria[0]
    old_fact = _bound_company_fact(criterion, _legacy_binding(profile.available_candidates[0]))
    register_facts = scoring.resolve_performance_register_facts(
        request.criteria, [], as_of=AS_OF, bid_notice_at=AS_OF,
    )
    assert len(register_facts) == 1
    assert register_facts[0].status == "REVIEW"
    assert register_facts[0].value is register_facts[0].lower_value is None

    # Replace only source selection with the source-validated SYN profile. The
    # resolver, new register derivation, merge priority and score engine are real.
    monkeypatch.setattr(scoring, "_current_dynamic_quantitative_profile", lambda notice: profile)
    result = scoring.estimate_for_notice(
        SimpleNamespace(deadline=AS_OF, published_at=AS_OF), [old_fact], [],
    )
    assert result.overall_status == "REVIEW"
    assert result.estimated_points is None
    assert result.confirmed_points == 0
    assert result.criteria[0].status == "REVIEW"


@pytest.mark.parametrize("kind", ["credit", "financial"])
def test_nonperformance_bindings_and_existing_verified_facts_are_unchanged(kind):
    if kind == "credit":
        stored = credit_record(*credit_fixture())
        assert stored.status == "AVAILABLE", stored.issues
        profile = merge_validated_quantitative_records(
            [stored], expected_documents={ATT: DOC}, manifest_sha256=MANIFEST,
        )
    else:
        profile = financial_profile()
    request = _request(profile)
    expected_bindings = {_legacy_binding(candidate) for candidate in profile.available_candidates}
    assert {item.fact_binding_sha256 for item in request.criteria} == expected_bindings
    facts = [
        _bound_company_fact(criterion, criterion.fact_binding_sha256, "A0" if kind == "credit" else 100)
        for criterion in request.criteria
    ]
    resolved = scoring.resolve_verified_quantitative_facts(request.criteria, facts, as_of=AS_OF)
    assert len(resolved) == len(request.criteria)
    assert all(item.status == "CONFIRMED" for item in resolved)
    result = scoring.estimate_quantitative_score(request.model_copy(update={"facts": resolved}))
    assert result.overall_status == "CONFIRMED"
    assert result.estimated_points == (9 if kind == "credit" else 5)
