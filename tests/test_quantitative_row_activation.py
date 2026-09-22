"""SYN rules: quarantine one compiler failure without weakening source proof."""
from copy import deepcopy
import hashlib
import json

import pytest

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_rule_extraction import (
    QuantitativeValidationIssue, merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
import pai_loop.quantitative_scoring as qs
from test_dense_case_source_binding import credit_fixture, anchor, ATT, MANIFEST
from test_quantitative_partial_activation import _mixed_review_profile


def mixed_inputs():
    raw, source = credit_fixture()
    literal = "SYN 최근 3년 유사 교육 실적 건수 6점"
    brackets = [dict(label="4건 이상", literal="4건 이상 6점", min_value=4,
        max_value=None, min_inclusive=True, max_inclusive=False, points=6,
        evidence=anchor("4건 이상 6점")),
        dict(label="4건 미만", literal="4건 미만 2점", min_value=None,
        max_value=4, min_inclusive=False, max_inclusive=False, points=2,
        evidence=anchor("4건 미만 2점"))]
    raw["quantitative_tables"][0]["criteria"].append(dict(
        criterion_id="SYN-PERFORMANCE", label="SYN 수행실적", criterion_literal=literal,
        max_points=6, scoring_method="BRACKET", metric="PERFORMANCE_COUNT", unit="건",
        brackets=brackets, threshold=None, formula_literal=None, cases=[], recognition_conditions=[],
        required_evidence=["company.performance.count"], evidence=anchor(literal), ambiguity_reason=None))
    total = "정량평가 합계 15점"
    raw["quantitative_tables"][0].update(total_points=15, total_evidence=anchor(total))
    source = source.replace("정량평가 합계 9점", "") + "\n" + "\n".join(
        [literal, *(b["literal"] for b in brackets), total])
    return raw, source


def mixed_profile():
    raw, source = mixed_inputs()
    before = deepcopy(raw)
    sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(ExtractionPayload.model_validate(raw),
        source_text=source, attachment_id=ATT, document_sha256=sha, manifest_sha256=MANIFEST)
    assert record.status == "AVAILABLE", record.issues
    assert raw == before
    return merge_validated_quantitative_records([record], expected_documents={ATT: sha},
        manifest_sha256=MANIFEST)


@pytest.mark.parametrize("credit_unit", ["등급", None])
def test_raw_to_notice_score_preserves_credit_while_performance_is_review(credit_unit):
    from test_extraction_contract_compatibility import notice_fixture
    from test_quantitative_auto_activation import _company_fact
    raw, source = mixed_inputs()
    raw["quantitative_tables"][0]["criteria"][0]["unit"] = credit_unit
    original = deepcopy(raw)
    notice, metadata, attempt, _ = notice_fixture(legacy=False)
    aid = attempt.source_payload["attachment_id"]
    payload = ExtractionPayload.model_validate(json.loads(json.dumps(raw).replace(ATT, aid)))
    sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(payload, source_text=source,
        attachment_id=aid, document_sha256=sha,
        manifest_sha256=attempt.source_payload["current_manifest_sha256"])
    attempt.file_sha256 = sha
    attempt.source_payload.update(document_sha256=sha, result=payload.model_dump(mode="json"),
        quantitative_validation_record=record.model_dump(mode="json"))
    attempt.source_payload["document_processing"].update(source_text_sha256=sha, analysis_input_sha256=sha)
    profile = qs._current_dynamic_quantitative_profile(notice)
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert profile.status == "AVAILABLE"
    assert request.activation_status == "PARTIAL_ACTIVE"
    assert request.activation_reasons == ["FACT_DIMENSIONS_UNMODELED"]
    assert len(request.criteria) == len(request.review_criteria) == 1
    criterion = request.criteria[0]
    fact = _company_fact(fact_key="company.credit_rating", value={"value": "A0", "unit": "등급",
        "fact_binding_sha256": criterion.fact_binding_sha256})
    result = qs.estimate_for_notice(notice, company_facts=[fact])
    assert result.overall_status == "REVIEW" and result.estimated_points is None
    assert result.total_max_points == result.upper_points == 15
    assert result.confirmed_points == result.lower_points == 9
    assert [(c.status, c.estimated_points) for c in result.criteria] == [("CONFIRMED", 9), ("REVIEW", None)]
    assert result.criteria[1].lower_points == 0 and result.criteria[1].upper_points == 6
    assert qs.build_public_quantitative_criteria_snapshot(result) is not None
    public = qs._public_quantitative_projection(result)
    assert public.total_max_points == 15 and public.overall_status == "REVIEW"
    assert all(c.source_anchor is None and c.evidence_key is None for c in public.criteria)
    assert raw == original


@pytest.mark.parametrize("defect,reason", [
    ("scope", "FACT_DIMENSIONS_UNMODELED"), ("unit", "UNSUPPORTED_UNIT"),
    ("unit_binding", "UNIT_NOT_SOURCE_BOUND"), ("fact", "FACT_EVIDENCE_KEY_UNREGISTERED"),
    ("dsl", "UNSUPPORTED_SCORING_DSL"), ("gap", "BRACKETS_NOT_EXHAUSTIVE_OR_OVERLAPPING"),
    ("anchor", "SOURCE_ANCHOR_INCOMPLETE"),
])
def test_row_diagnostics_remain_stable_and_only_bad_row_is_withheld(defect, reason):
    profile = mixed_profile()
    good, bad = profile.available_candidates
    if defect == "unit": bad = bad.model_copy(update={"unit": "달러"})
    elif defect == "unit_binding": bad = bad.model_copy(update={"unit": "회"})
    elif defect == "fact": bad = bad.model_copy(update={"required_evidence": ("SYN-UNREGISTERED",)})
    elif defect == "dsl": bad = bad.model_copy(update={"scoring_method": "FORMULA", "formula_literal": "SYN unknown"})
    elif defect == "gap": bad = bad.model_copy(update={"brackets": (
        bad.brackets[0].model_copy(update={"min_value": 5}), bad.brackets[1])})
    elif defect == "anchor": bad = bad.model_copy(update={"evidence": bad.evidence.model_copy(update={"quote": ""})})
    profile = profile.model_copy(update={"available_candidates": (good, bad)})
    before = profile.model_dump_json()
    split = qs._profile_activation_reason_partition(profile)
    assert split.notice_reasons == ()
    assert len(split.row_reasons) == 1
    assert reason in split.row_reasons[0][1]
    assert qs._profile_activation_reasons(profile) == sorted(split.row_reasons[0][1])
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "PARTIAL_ACTIVE"
    assert [c.category for c in request.criteria] == ["CREDIT_RATING"]
    assert reason in request.review_criteria[0].issue_codes
    assert profile.model_dump_json() == before


@pytest.mark.parametrize("defect,reason", [
    ("coverage", "CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE"),
    ("total", "TABLE_TOTAL_MISMATCH"), ("alternative", "ALTERNATIVE_TABLE_AMBIGUOUS"),
    ("issue", "SOURCE_VALIDATION_ISSUES_PRESENT"), ("fact_ambiguity", "FACT_KEY_AMBIGUOUS"),
])
def test_notice_level_failures_cannot_be_hidden_by_row_quarantine(defect, reason):
    profile = mixed_profile()
    table = profile.tables[0]
    if defect == "coverage": profile = profile.model_copy(update={"processed_attachment_ids": ()})
    elif defect == "total": profile = profile.model_copy(update={"tables": (table.model_copy(update={"total_points": 16}),)})
    elif defect == "alternative": profile = profile.model_copy(update={"tables": (table, table)})
    elif defect == "issue": profile = profile.model_copy(update={"issues": (
        QuantitativeValidationIssue(code="SYN_GLOBAL_DEFECT", disposition="REVIEW", message="SYN global",
            attachment_id=ATT),)})
    else:
        good, bad = profile.available_candidates
        duplicate = good.model_copy(update={"criterion_id": "SYN-SECOND-CREDIT"})
        profile = profile.model_copy(update={"available_candidates": (good, bad, duplicate),
            "tables": (table.model_copy(update={"total_points": 24,
                "criterion_ids": (*table.criterion_ids, duplicate.criterion_id),
                "available_criterion_ids": (*table.available_criterion_ids, duplicate.criterion_id)}),)})
    assert reason in qs._profile_activation_reason_partition(profile).notice_reasons
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "REVIEW_REQUIRED"
    assert not request.criteria and not request.review_criteria


@pytest.mark.parametrize("one_detail_table", [True, False])
def test_proven_summary_detail_program_keeps_total_and_good_fact_bindings(one_detail_table):
    from test_quantitative_logical_program import _logical_profile

    profile = _logical_profile(one_detail_table=one_detail_table)
    original = qs.quantitative_request_from_candidate_profile(profile)
    profile = profile.model_copy(update={"available_candidates": tuple(
        candidate.model_copy(update={"unit": "SYN-UNSUPPORTED"})
        if candidate.criterion_id == "detail-years" else candidate
        for candidate in profile.available_candidates)})
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "PARTIAL_ACTIVE"
    bindings = {c.criterion_id: c.fact_binding_sha256 for c in original.criteria}
    assert all(c.fact_binding_sha256 == bindings[c.criterion_id] for c in request.criteria)
    assert len(request.criteria) == 2 and len(request.review_criteria) == 1
    assert request.review_criteria[0].max_points == 6
    assert qs.estimate_quantitative_score(request).total_max_points == 20


def test_multiple_affected_tables_require_explicit_program_ownership():
    from test_quantitative_logical_program import _logical_profile

    profile = _logical_profile()
    profile = profile.model_copy(update={"available_candidates": tuple(
        candidate.model_copy(update={"unit": "SYN-UNSUPPORTED"})
        if candidate.criterion_id in {"detail-years", "detail-certificates"} else candidate
        for candidate in profile.available_candidates)})
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "REVIEW_REQUIRED"
    assert not request.criteria and not request.review_criteria


def test_no_compilable_row_is_never_labeled_partial_active():
    profile = mixed_profile()
    good, bad = profile.available_candidates
    profile = profile.model_copy(update={"available_candidates": (
        good.model_copy(update={"unit": "SYN-UNSUPPORTED"}), bad)})
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "REVIEW_REQUIRED"
    assert not request.criteria and not request.review_criteria


def empty_companion(profile):
    table = profile.tables[0]
    return table.model_copy(update={"table_id": "SYN-EMPTY-SURVEY", "label": "SYN 설문표",
        "status": "AVAILABLE", "total_points": 0, "criterion_ids": (), "available_criterion_ids": (),
        "review_criterion_ids": (), "minimum_score": None, "minimum_evidence": None,
        "total_evidence": table.total_evidence.model_copy(update={"quote": "SYN 설문 총점 0점"})})


def test_explicit_zero_empty_companion_preserves_existing_partial_denominator():
    # Consumer compatibility only: the current extraction writer rejects empty
    # tables, as the raw-path test below verifies. This is not a recovery claim.
    profile = _mixed_review_profile()
    profile = profile.model_copy(update={"tables": (*profile.tables, empty_companion(profile))})
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "PARTIAL_ACTIVE"
    assert qs.estimate_quantitative_score(request).total_max_points == 30


@pytest.mark.parametrize("defect", ["positive", "unknown", "unproved_zero", "mixed_zero", "minimum", "candidate", "duplicate", "unbound", "second_review"])
def test_empty_label_never_proves_an_inert_or_alternative_table(defect):
    profile = _mixed_review_profile()
    other = empty_companion(profile)
    if defect == "positive": other = other.model_copy(update={"total_points": 30})
    elif defect == "unknown": other = other.model_copy(update={"total_points": None})
    elif defect == "unproved_zero": other = other.model_copy(update={"total_evidence": other.total_evidence.model_copy(update={"quote": "SYN 설문 총점 100점"})})
    elif defect == "mixed_zero": other = other.model_copy(update={"total_evidence": other.total_evidence.model_copy(update={"quote": "SYN 총점 30점 미제출 0점"})})
    elif defect == "minimum": other = other.model_copy(update={"minimum_score": 1})
    elif defect == "candidate": other = other.model_copy(update={"criterion_ids": ("SYN-HIDDEN",)})
    elif defect == "duplicate": other = profile.tables[0]
    elif defect == "unbound": other = other.model_copy(update={"source_attachment_id": "SYN-OTHER"})
    else: other = other.model_copy(update={"status": "REVIEW"})
    request = qs.quantitative_request_from_candidate_profile(profile.model_copy(update={"tables": (*profile.tables, other)}))
    assert request.activation_status == "REVIEW_REQUIRED"
    assert not request.criteria


def test_raw_empty_table_still_requires_source_validation_and_blocks_activation():
    raw, source = mixed_inputs()
    empty = deepcopy(raw["quantitative_tables"][0])
    literal = "SYN 설문 총점 0점"
    empty.update(table_id="SYN-EMPTY-SURVEY", label="SYN 설문표", criteria=[],
        total_points=0, total_evidence=anchor(literal))
    raw["quantitative_tables"].append(empty)
    source += "\n" + literal
    sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(ExtractionPayload.model_validate(raw),
        source_text=source, attachment_id=ATT, document_sha256=sha, manifest_sha256=MANIFEST)
    assert record.status == "INCOMPLETE"
    assert "TABLE_CRITERIA_MISSING" in {issue.code for issue in record.issues}
    profile = merge_validated_quantitative_records([record], expected_documents={ATT: sha},
        manifest_sha256=MANIFEST)
    request = qs.quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "REVIEW_REQUIRED"
    assert not request.criteria
