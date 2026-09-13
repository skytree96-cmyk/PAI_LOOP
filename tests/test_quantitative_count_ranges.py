"""Synthetic count rules: closed intervals and an independent submission state.

No production documents or company facts. In particular, missing evidence is
not evidence of non-submission, and a zero count is not non-submission either.
"""
import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import ExtractionPayload, QuantitativeCaseLiteral
from pai_loop.quantitative_formula import CaseTableRowLiteral, case_table_points, compile_case_table
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord, merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QuantitativeCriterion, QuantitativeEstimateRequest, QuantitativeFact, SourceAnchor,
    estimate_quantitative_score, quantitative_request_from_candidate_profile,
)

ATT = "SYN-COUNT-RANGE"
DOC = "a" * 64
MANIFEST = "b" * 64


def anchor(quote):
    return dict(attachment_id=ATT, page=1, section="SYN-정량", quote=quote, confidence=1)


def fixture(*, inline=False):
    heading = "SYN-해외연수 수행실적 건수 (5점)"
    scope = "공고일 기준 최근 3년 해외연수 관련 수행완료 실적의 건수, 부가세 포함, 공동수급 전체 인정"
    specs = [("GTE", 7, None, 5, "7건 이상"),
             ("BETWEEN", 5, 6, 4.5, "5건∼6건"),
             ("BETWEEN", 3, 4, 4, "3건∼4건"),
             ("BETWEEN", 1, 2, 3.5, "1건∼2건"),
             ("NOT_SUBMITTED", None, None, 0, "미제출")]
    cases = []
    for index, (op, lower, upper, points, condition) in enumerate(specs, 1):
        literal = f"{condition} {points:g}점" if inline else f"{condition}\n{points:g}"
        cases.append(dict(literal=literal, operator=op, comparison_value=lower,
                          comparison_upper_value=upper, category_values=[], award_kind="POINTS",
                          award_value=points, row_order=index, evidence=anchor(literal)))
    payload = dict(document_type="RFP", requirements=[], missing_or_unreadable=[],
                   quantitative_table_not_applicable=None, summary="SYN",
                   quantitative_tables=[dict(table_id="SYN-T", label="정량평가", total_points=5,
                       total_evidence=anchor("정량평가 합계 5점"), minimum_score=None,
                       minimum_evidence=None, ambiguity_reason=None, criteria=[dict(
                           criterion_id="SYN-C", label="해외연수 수행실적", criterion_literal=heading,
                           max_points=5, scoring_method="CASE_TABLE", metric="PERFORMANCE_COUNT",
                           unit="건", brackets=[], threshold=None, formula_literal=None, cases=cases,
                           recognition_conditions=[dict(literal=scope, evidence=anchor(scope))],
                           required_evidence=["company.performance.count"], evidence=anchor(heading),
                           ambiguity_reason=None)])])
    source = "\n".join([heading, scope, *(r["literal"] for r in cases), "정량평가 합계 5점"])
    return payload, source


def record(payload, source):
    return validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(payload), source_text=source,
        attachment_id=ATT, document_sha256=DOC, manifest_sha256=MANIFEST)


def request(payload=None, source=None):
    if payload is None:
        payload, source = fixture()
    stored = record(payload, source)
    assert stored.status == "AVAILABLE", [i.code for i in stored.issues]
    restored = ValidatedQuantitativeAttachmentRecord.model_validate_json(stored.model_dump_json())
    profile = merge_validated_quantitative_records([restored],
        expected_documents={ATT: DOC}, manifest_sha256=MANIFEST)
    result = quantitative_request_from_candidate_profile(profile)
    assert result.activation_status == "AUTO_ACTIVE", result.activation_reasons
    assert result.criteria[0].performance_scope is not None
    return result


def fact(req, **changes):
    values = dict(metric_key="company.performance.count", status="CONFIRMED",
                  evidence_key="company.performance.count", evidence_reference="SYN-verified-audit",
                  evidence_sha256="c" * 64, fact_binding_sha256=req.criteria[0].fact_binding_sha256,
                  confidence=1)
    values.update(changes)
    return QuantitativeFact(**values)


@pytest.mark.parametrize("inline", [False, True])
def test_source_to_bound_engine_keeps_ranges_and_submission_separate(inline):
    req = request(*fixture(inline=inline))
    for value, expected in [(1, 3.5), (2, 3.5), (3, 4), (4, 4), (5, 4.5), (6, 4.5), (7, 5), (20, 5)]:
        req.facts = [fact(req, value=value)]
        result = estimate_quantitative_score(req)
        assert result.estimated_points == expected
        assert result.criteria[0].status == "CONFIRMED"
    req.facts = [fact(req, submission_status="NOT_SUBMITTED")]
    result = estimate_quantitative_score(req)
    assert result.estimated_points == 0
    assert result.criteria[0].fact_binding_sha256 == req.criteria[0].fact_binding_sha256
    for value in (0, -1, 1.5, None, "미제출", "NOT_SUBMITTED"):
        req.facts = [fact(req, value=value)]
        assert estimate_quantitative_score(req).estimated_points is None
    req.facts = []
    assert estimate_quantitative_score(req).estimated_points is None


@pytest.mark.parametrize("change", [dict(fact_binding_sha256="d" * 64),
                                   dict(evidence_key="company.other"), dict(status="REVIEW")])
def test_submission_requires_the_actual_criterion_and_verified_evidence(change):
    req = request()
    if change.get("status") == "REVIEW":
        with pytest.raises(ValidationError):
            fact(req, submission_status="NOT_SUBMITTED", **change)
        return
    req.facts = [fact(req, submission_status="NOT_SUBMITTED", **change)]
    assert estimate_quantitative_score(req).estimated_points is None


@pytest.mark.parametrize("change", [dict(value=0), dict(lower_value=0), dict(evidence_sha256=None),
                                   dict(evidence_reference=None), dict(fact_binding_sha256=None)])
def test_submission_is_not_an_optional_label_on_missing_or_numeric_facts(change):
    req = request()
    with pytest.raises(ValidationError):
        fact(req, submission_status="NOT_SUBMITTED", **change)


def test_absent_submission_row_does_not_invent_a_zero_score():
    payload, source = fixture()
    payload["quantitative_tables"][0]["criteria"][0]["cases"].pop()
    req = request(payload, source)
    req.facts = [fact(req, submission_status="NOT_SUBMITTED")]
    assert estimate_quantitative_score(req).estimated_points is None


def test_estimated_range_checks_upper_edges_and_unprinted_integer_gaps():
    payload, source = fixture()
    cases = payload["quantitative_tables"][0]["criteria"][0]["cases"]
    # Delete an entire printed row from this synthetic SOURCE too. Its values
    # remain unscorable, not an inferred continuation of the preceding row.
    removed = cases.pop(2)
    source = source.replace(removed["literal"] + "\n", "")
    for index, row in enumerate(cases, 1):
        row["row_order"] = index
    req = request(payload, source)
    req.facts = [fact(req, status="ESTIMATED", lower_value=2, upper_value=5)]
    result = estimate_quantitative_score(req)
    assert result.estimated_points is None
    assert "포괄" in result.criteria[0].rationale


@pytest.mark.parametrize("literal", ["5건 이상 6건 미만\n4.5", "5건∼6건 또는 별도 심사\n4.5",
                                      "5건∼6건\n5", "5명∼6명\n4.5", "6건∼5건\n4.5",
                                      "5건∼6건 수상실적 4.5점"])
def test_range_cannot_change_source_comparators_awards_or_condition_scope(literal):
    payload, source = fixture()
    row = payload["quantitative_tables"][0]["criteria"][0]["cases"][1]
    source = source.replace(row["literal"], literal)
    row.update(literal=literal, evidence=anchor(literal))
    result = record(payload, source)
    assert not result.available_candidates
    assert "CASE_NUMBER_MISMATCH" in {i.code for i in result.issues}


def test_count_in_is_still_rejected_and_other_blockers_are_preserved():
    payload, source = fixture()
    row = payload["quantitative_tables"][0]["criteria"][0]["cases"][1]
    row.update(operator="IN", comparison_value=None, comparison_upper_value=None, category_values=["5건∼6건"])
    result = record(payload, source)
    assert not result.available_candidates
    assert "CASE_TABLE_NOT_DETERMINISTIC" in {i.code for i in result.issues}
    payload, source = fixture()
    payload["missing_or_unreadable"] = ["정량평가 원문 일부가 판독 불가"]
    assert record(payload, source).status == "INCOMPLETE"


@pytest.mark.parametrize("bounds", [(True, 2), (1, True), (-1, 2), (1.5, 2), (2, 2), (3, 2)])
def test_range_bounds_are_nonnegative_distinct_integers(bounds):
    with pytest.raises(ValidationError):
        CaseTableRowLiteral(operator="BETWEEN", comparison_value=bounds[0], comparison_upper_value=bounds[1], award_value=1)
    raw = fixture()[0]["quantitative_tables"][0]["criteria"][0]["cases"][1]
    raw.update(comparison_value=bounds[0], comparison_upper_value=bounds[1])
    with pytest.raises(ValidationError):
        QuantitativeCaseLiteral.model_validate(raw)


def test_compiler_rejects_overlap_status_reordering_and_mixed_domains():
    rows=[CaseTableRowLiteral(operator="GTE", comparison_value=7, award_value=5),
          CaseTableRowLiteral(operator="BETWEEN", comparison_value=5, comparison_upper_value=7, award_value=4.5)]
    assert compile_case_table(rows, value_kind="DISCRETE", maximum_points=5) is None
    rows[-1]=CaseTableRowLiteral(operator="BETWEEN", comparison_value=5, comparison_upper_value=6, award_value=4.5)
    assert compile_case_table(rows, value_kind="NUMERIC", maximum_points=5) is None
    rows.insert(0, CaseTableRowLiteral(operator="NOT_SUBMITTED", award_value=0))
    assert compile_case_table(rows, value_kind="DISCRETE", maximum_points=5) is None


def test_old_case_shape_still_loads_with_an_absent_upper_bound():
    raw=fixture()[0]["quantitative_tables"][0]["criteria"][0]["cases"][0]
    raw.pop("comparison_upper_value")
    assert QuantitativeCaseLiteral.model_validate(raw).comparison_upper_value is None


def test_continuous_numeric_tail_is_exactly_complementary_not_an_integer_conversion():
    rows = [CaseTableRowLiteral(operator=op, comparison_value=value, award_value=points)
            for op, value, points in [("GTE",100,2.5),("GTE",60,2),("LT",60,1.5)]]
    table = compile_case_table(rows, value_kind="NUMERIC", maximum_points=2.5)
    assert table is not None
    criterion = QuantitativeCriterion(criterion_id="SYN-RATIO", category="FINANCIAL_RATIO",
        label="SYN-자기자본비율", max_points=2.5, metric_key="company.financial.ratio", unit="%",
        formula_type="CASE_TABLE", formula="100% 이상 2.5점, 60% 이상 2점, 60% 미만 1.5점",
        case_table=table, source_anchor=SourceAnchor(document_label="SYN",document_sha256=DOC,
            section="SYN",quote="100% 이상 2.5점, 60% 이상 2점, 60% 미만 1.5점"),
        required_evidence_keys=["company.financial.ratio"], fact_binding_sha256="d"*64)
    req = QuantitativeEstimateRequest(ruleset_version="SYN-RATIO", criteria=[criterion],
        rule_source_status="AVAILABLE", source_validation_status="SOURCE_VALIDATED",activation_status="AUTO_ACTIVE")
    for value, points in [(100,2.5),(99.99,2),(60,2),(59.99,1.5),(0,1.5),(-0.1,1.5)]:
        req.facts=[QuantitativeFact(metric_key=criterion.metric_key,status="CONFIRMED",value=value,
            evidence_key=criterion.metric_key,fact_binding_sha256=criterion.fact_binding_sha256)]
        assert estimate_quantitative_score(req).estimated_points == points
    # The old forbidden example remains forbidden: it has an undefined gap
    # and is not the new explicitly complementary continuous grammar.
    rows[-1]=CaseTableRowLiteral(operator="LT",comparison_value=59,award_value=1.5)
    assert compile_case_table(rows,value_kind="NUMERIC",maximum_points=2.5) is None
    rows[-1]=CaseTableRowLiteral(operator="LTE",comparison_value=60,award_value=1.5)
    assert compile_case_table(rows,value_kind="NUMERIC",maximum_points=2.5) is None


def test_continuous_ratio_full_source_validation_and_bound_engine():
    payload, _ = fixture()
    table = payload["quantitative_tables"][0]
    heading = "SYN-자기자본비율 (2.5점)"
    rows = []
    for index, (op, value, points, literal) in enumerate([
        ("GTE",100,2.5,"100% 이상\n2.5"),
        ("GTE",60,2,"60% 이상\n2"), ("LT",60,1.5,"60% 미만\n1.5")],1):
        rows.append(dict(operator=op,comparison_value=value,comparison_upper_value=None,
            category_values=[],award_kind="POINTS",award_value=points,row_order=index,
            literal=literal,evidence=anchor(literal)))
    table.update(total_points=2.5,total_evidence=anchor("정량평가 합계 2.5점"))
    table["criteria"][0].update(label="자기자본비율",criterion_literal=heading,max_points=2.5,
        metric="FINANCIAL_RATIO",unit="%",cases=rows,recognition_conditions=[],
        required_evidence=["company.financial.ratio"],evidence=anchor(heading))
    source="\n".join([heading,*(r["literal"] for r in rows),"정량평가 합계 2.5점"])
    stored=record(payload,source)
    assert stored.status=="AVAILABLE", [i.code for i in stored.issues]
    profile=merge_validated_quantitative_records([stored],expected_documents={ATT:DOC},manifest_sha256=MANIFEST)
    req=quantitative_request_from_candidate_profile(profile)
    assert req.activation_status=="AUTO_ACTIVE", req.activation_reasons
    criterion=req.criteria[0]
    assert criterion.financial_scope is not None
    for value,expected in [(59.99,1.5),(60,2),(100,2.5)]:
        req.facts=[QuantitativeFact(metric_key=criterion.metric_key,status="CONFIRMED",value=value,
            evidence_key=criterion.metric_key,fact_binding_sha256=criterion.fact_binding_sha256)]
        assert estimate_quantitative_score(req).estimated_points==expected
