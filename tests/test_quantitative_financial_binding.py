"""SYN source/statement regressions for criterion-scoped financial inputs."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.models import CompanyFact, Evidence
from pai_loop.quantitative_rule_extraction import (
    merge_validated_quantitative_records, validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QuantitativeFact, estimate_for_notice, estimate_quantitative_score,
    quantitative_company_fact_payload_sha256, quantitative_request_from_candidate_profile,
    resolve_financial_register_facts, resolve_verified_quantitative_facts,
)


AS_OF = datetime(2026, 9, 10, tzinfo=timezone.utc)
KEY = "company.financial.ratio"


def _profile(labels=("SYN 자기자본비율", "SYN 유동비율")):
    def anchor(quote, section):
        return dict(attachment_id="SYN-FIN", page=1, section=section,
                    quote=quote, confidence=0.99)

    criteria, sections = [], []
    for index, label in enumerate(labels):
        header = f"{label} (%) 2.5점"
        cases = []
        for order, (operator, cutoff, word, points) in enumerate(
            [("GTE", 100, "이상", 2.5), ("GTE", 60, "이상", 2), ("LT", 60, "미만", 1.5)], 1
        ):
            literal = f"{cutoff}% {word} {points:g}점"
            cases.append(dict(literal=literal, operator=operator, comparison_value=cutoff,
                              category_values=[], award_kind="POINTS", award_value=points,
                              row_order=order, evidence=anchor(literal, label)))
        sections.append("\n".join([header, *(case["literal"] for case in cases)]))
        criteria.append(dict(
            criterion_id=f"SYN-RATIO-{index}", label=label, criterion_literal=header,
            max_points=2.5, scoring_method="CASE_TABLE", metric="FINANCIAL_RATIO", unit="%",
            brackets=[], threshold=None, formula_literal=None, cases=cases,
            recognition_conditions=[], required_evidence=[KEY], evidence=anchor(header, label),
            ambiguity_reason=None,
        ))
    total = "SYN 정량평가 총점 5점"
    source = "\n".join([*sections, total])
    payload = ExtractionPayload.model_validate(dict(
        document_type="RFP", requirements=[], missing_or_unreadable=[], summary="SYN ratios",
        quantitative_table_not_applicable=None,
        quantitative_tables=[dict(table_id="SYN-TABLE", label="SYN ratios", criteria=criteria,
                                 total_points=5, total_evidence=anchor(total, "SYN ratios"),
                                 minimum_score=None, minimum_evidence=None, ambiguity_reason=None)],
    ))
    record = validate_quantitative_attachment_extraction(
        payload, source_text=source, attachment_id="SYN-FIN", document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    assert record.status == "AVAILABLE", record.issues
    return merge_validated_quantitative_records(
        [record], expected_documents={"SYN-FIN": "a" * 64}, manifest_sha256="b" * 64,
    )


def _company_fact(binding, value, *, key=KEY):
    evidence_id = "SYN-EVIDENCE-" + str(binding or "generic")[:12]
    evidence = Evidence(
        id=evidence_id, evidence_key=evidence_id, name="SYN verified source",
        evidence_type="QUANTITATIVE_FACT", status="VERIFIED",
        issued_at=AS_OF - timedelta(days=30), source_location="SYN/source",
        sha256="c" * 64,
    )
    raw = dict(value=value, unit="%")
    if binding is not None:
        raw["fact_binding_sha256"] = binding
    fact = CompanyFact(
        fact_key=key, value=raw, verified=True, effective_from=AS_OF - timedelta(days=30),
        source="SYN", evidence_id=evidence_id, evidence=evidence,
    )
    evidence.metadata_json = dict(
        quantitative_fact_key=key, fact_binding_sha256=binding,
        company_fact_payload_sha256=quantitative_company_fact_payload_sha256(fact),
    )
    return fact


def _statement(*, duplicate=None):
    row = dict(fiscal_year=2025, total_assets=1000, equity=400,
               current_assets=1600, current_liabilities=1000, non_current_liabilities=0)
    rows = [row]
    if duplicate is not None:
        rows.append({**row, "equity": duplicate})
    return CompanyFact(fact_key="company.financial.statement", value={"years": rows})


def _criteria(request):
    return {item.financial_scope.ratio_kind: item for item in request.criteria}


def _run_notice(monkeypatch, profile, facts):
    monkeypatch.setattr("pai_loop.quantitative_scoring._current_dynamic_quantitative_profile", lambda _notice: profile)
    return estimate_for_notice(SimpleNamespace(deadline=AS_OF, published_at=AS_OF), facts)


def test_validated_distinct_ratios_activate_and_register_derives_separate_inputs():
    request = quantitative_request_from_candidate_profile(_profile())
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    criteria = _criteria(request)
    assert len({item.fact_binding_sha256 for item in request.criteria}) == 2
    facts = resolve_financial_register_facts(request.criteria, [_statement()], as_of=AS_OF)
    inputs = {item.fact_binding_sha256: item.value for item in facts}
    assert inputs[criteria["EQUITY_TO_ASSETS"].fact_binding_sha256] == 40
    assert inputs[criteria["CURRENT_RATIO"].fact_binding_sha256] == 160
    result = estimate_quantitative_score(request.model_copy(update={"facts": facts}))
    points = {item.label: item.estimated_points for item in result.criteria}
    assert points == {"SYN 자기자본비율": 1.5, "SYN 유동비율": 2.5}


@pytest.mark.parametrize("labels", [
    ("SYN 자기자본비율", "SYN 자기자본비율 추가"),
    ("SYN 자기자본비율", "SYN 자기자본비율 및 유동비율"),
    ("SYN 자기자본비율", "SYN 경영상태 비율"),
])
def test_same_or_unnamed_ratio_scope_keeps_ambiguous_gate(labels):
    request = quantitative_request_from_candidate_profile(_profile(labels))
    assert request.activation_status == "REVIEW_REQUIRED"
    assert "FACT_KEY_AMBIGUOUS" in request.activation_reasons


def test_one_verified_ratio_does_not_poison_or_suppress_its_register_sibling(monkeypatch):
    profile = _profile()
    request = quantitative_request_from_candidate_profile(profile)
    equity = _criteria(request)["EQUITY_TO_ASSETS"]
    fact = _company_fact(equity.fact_binding_sha256, 40)
    resolved = resolve_verified_quantitative_facts(request.criteria, [fact], as_of=AS_OF)
    assert len(resolved) == 1 and resolved[0].status == "CONFIRMED"
    result = _run_notice(monkeypatch, profile, [fact, _statement()])
    by_label = {item.label: item for item in result.criteria}
    assert by_label["SYN 자기자본비율"].status == "CONFIRMED"
    assert by_label["SYN 자기자본비율"].estimated_points == 1.5
    assert by_label["SYN 유동비율"].status == "ESTIMATED"
    assert by_label["SYN 유동비율"].estimated_points == 2.5


@pytest.mark.parametrize("reverse", [False, True])
def test_two_verified_ratios_remain_independent_of_fact_order(reverse):
    request = quantitative_request_from_candidate_profile(_profile())
    criteria = _criteria(request)
    facts = [_company_fact(criteria["EQUITY_TO_ASSETS"].fact_binding_sha256, 40),
             _company_fact(criteria["CURRENT_RATIO"].fact_binding_sha256, 160)]
    resolved = resolve_verified_quantitative_facts(request.criteria, facts[::-1] if reverse else facts, as_of=AS_OF)
    assert len(resolved) == 2
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))
    assert {item.label: item.estimated_points for item in result.criteria} == {
        "SYN 자기자본비율": 1.5, "SYN 유동비율": 2.5,
    }


@pytest.mark.parametrize("mutation", ["generic", "stale_binding", "stale_payload", "wrong_evidence_binding"])
def test_invalid_ratio_input_cannot_transfer_to_either_criterion(mutation):
    request = quantitative_request_from_candidate_profile(_profile())
    binding = _criteria(request)["EQUITY_TO_ASSETS"].fact_binding_sha256
    fact = _company_fact(None if mutation == "generic" else "f" * 64 if mutation == "stale_binding" else binding, 160)
    if mutation == "stale_payload":
        fact.value = {**fact.value, "value": 999}
    if mutation == "wrong_evidence_binding":
        fact.evidence.metadata_json = {**fact.evidence.metadata_json, "fact_binding_sha256": "f" * 64}
    resolved = resolve_verified_quantitative_facts(request.criteria, [fact], as_of=AS_OF)
    result = estimate_quantitative_score(request.model_copy(update={"facts": resolved}))
    assert all(item.estimated_points is None for item in result.criteria)


def test_duplicate_verified_binding_blocks_only_own_ratio(monkeypatch):
    profile = _profile()
    request = quantitative_request_from_candidate_profile(profile)
    binding = _criteria(request)["EQUITY_TO_ASSETS"].fact_binding_sha256
    result = _run_notice(monkeypatch, profile, [_company_fact(binding, 40), _company_fact(binding, 80), _statement()])
    by_label = {item.label: item for item in result.criteria}
    assert by_label["SYN 자기자본비율"].status == "REVIEW"
    assert by_label["SYN 자기자본비율"].estimated_points is None
    assert by_label["SYN 유동비율"].estimated_points == 2.5


def test_correct_binding_with_wrong_metric_cannot_select_a_fact():
    request = quantitative_request_from_candidate_profile(_profile())
    binding = request.criteria[0].fact_binding_sha256
    fact = QuantitativeFact(metric_key="company.business.years", status="CONFIRMED", value=160,
                            evidence_key=KEY, fact_binding_sha256=binding, confidence=1)
    result = estimate_quantitative_score(request.model_copy(update={"facts": [fact]}))
    assert all(item.estimated_points is None for item in result.criteria)


@pytest.mark.parametrize("duplicate", [400, 800])
def test_conflicting_same_year_statements_never_choose_last_row(duplicate):
    request = quantitative_request_from_candidate_profile(_profile())
    facts = resolve_financial_register_facts(request.criteria, [_statement(duplicate=duplicate)], as_of=AS_OF)
    if duplicate == 800:
        assert all(item.status == "REVIEW" and item.value is None for item in facts)
        assert all("상충" in item.rationale for item in facts)
    else:
        assert sorted(item.value for item in facts) == [40, 160]
