"""SYN literal review only: no source files, company inputs, or external effects."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from pai_loop.quantitative_performance import parse_performance_recognition_scope
from pai_loop.verified_rule_input import (
    VerifiedRuleDraft,
    VerifiedRuleDraftResult,
    verify_rule_draft,
)


SOURCE = "최근 3년간 (SYN교육, SYN행사) 용역 수행완료 실적 VAT 포함"
FLAGS = ("source_content_verified", "coverage_verified", "engine_eligible", "persistence_eligible")


def draft(literal=SOURCE, **changes):
    return {"attachment_sha256": "a" * 64, "literal": literal,
            "metric_key": "company.performance.count", **changes}


@pytest.mark.parametrize("metric,aggregation", [
    ("company.performance.count", "COUNT"),
    ("company.performance.amount", "SUM_AMOUNT"),
])
def test_supported_literal_is_parsed_without_source_or_execution_authority(metric, aggregation):
    supplied = draft(metric_key=metric)
    before = deepcopy(supplied)
    result = verify_rule_draft(supplied)
    assert supplied == before
    assert result.status == "SOURCE_VERIFIED" and result.reason_code is None
    assert result.verification_scope == "SUPPLIED_LITERAL_PARSE_ONLY"
    assert all(getattr(result, field) is False for field in FLAGS)
    assert result.parsed_scope == parse_performance_recognition_scope(SOURCE, metric_key=metric)
    assert result.parsed_scope.aggregation == aggregation
    assert result.draft.literal == SOURCE
    assert VerifiedRuleDraftResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("clause", [
    "계약별 50인 이상",
    "단일 계약 당 연간 기준 총 계약금액 1억원 이상",
    "실적 인정기간은 2025년부터 2026년까지",
    "하도급 실적은 발주기관의 승인을 받은 경우에 인정",
    "공공기관의 확인을 받은 실적증명서를 제출한다.",
    "계약별 50인 이상, 연간 기준 계약금액 1억원 이상",
])
def test_manual_conditions_are_preserved_as_attested_only(clause):
    result = verify_rule_draft(draft(SOURCE + " " + clause))
    assert result.status == "ATTESTED_ONLY"
    assert result.reason_code == "MANUAL_CONDITIONS_REMAIN"
    assert result.parsed_scope.manual_verification_conditions
    assert result.parsed_scope == parse_performance_recognition_scope(
        result.draft.literal, metric_key=result.draft.metric_key,
    )
    if "계약별" in clause and "연간" in clause:
        assert len(result.parsed_scope.manual_verification_conditions) == 2
    assert all(getattr(result, field) is False for field in FLAGS)


def test_fullwidth_input_is_retained_while_normalized_manual_period_stays_blocked():
    literal = "  최근 ３년간\n(SYN교육, SYN행사) 용역 수행완료 실적 VAT 포함 " \
              "실적 인정기간은 ２０２５년부터 ２０２６년까지  "
    result = verify_rule_draft(draft(literal))
    assert result.draft.literal == literal
    assert result.status == "ATTESTED_ONLY"
    assert "2025년부터 2026년" in result.parsed_scope.manual_verification_conditions[0]
    assert result.parsed_scope.source_literal != literal
    assert "\n" not in result.parsed_scope.source_literal


@pytest.mark.parametrize("literal", ["자료 없음", "", " \n\t", "SYN 최근 실적",
    SOURCE + " VAT 별도", SOURCE + " 최근 2년 실적도 인정"])
def test_missing_or_unparseable_input_has_no_scope_count_or_score(literal):
    result = verify_rule_draft(draft(literal))
    assert result.status == "ATTESTED_ONLY"
    assert result.reason_code == "LITERAL_PARSE_UNSUPPORTED"
    assert result.parsed_scope is None
    assert result.draft.literal == literal
    assert all(getattr(result, field) is False for field in FLAGS)
    assert set(result.model_dump()) == {
        "draft", "parsed_scope", "status", "reason_code", "verification_scope", *FLAGS,
    }


@pytest.mark.parametrize("changes", [
    {"attachment_sha256": "a" * 63}, {"attachment_sha256": "g" * 64},
    {"attachment_sha256": "a" * 64 + "\n"}, {"attachment_sha256": 1},
    {"metric_key": "company.credit_rating"}, {"metric_key": "PERFORMANCE_COUNT"},
    {"literal": 1}, {"literal": None}, {"literal": "SYN " * 501},
    {"scope": {}}, {"parsed_scope": {}}, {"performance_scope": {}},
    {"status": "SOURCE_VERIFIED"}, {"company_value": 7},
    {"fact_binding_sha256": "b" * 64}, {"engine_eligible": True},
])
def test_invalid_or_extra_input_is_rejected_before_parsing(changes):
    with pytest.raises(ValidationError):
        verify_rule_draft(draft(**changes))


def test_models_and_nested_parser_scope_are_immutable():
    supplied = VerifiedRuleDraft.model_validate(draft())
    result = verify_rule_draft(supplied)
    for obj, field, value in (
        (supplied, "literal", "SYN replacement"),
        (result, "status", "ATTESTED_ONLY"),
        (result.draft, "attachment_sha256", "b" * 64),
        (result.parsed_scope, "lookback_years", 1),
        *((result, field, True) for field in FLAGS),
    ):
        with pytest.raises(ValidationError):
            setattr(obj, field, value)


@pytest.mark.parametrize("field", FLAGS)
@pytest.mark.parametrize("value", [True, 0, "false"])
def test_result_authority_flags_cannot_be_enabled_or_coerced(field, value):
    values = verify_rule_draft(draft()).model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        VerifiedRuleDraftResult.model_validate(values)


@pytest.mark.parametrize("mutation", ["scope", "status", "metric", "literal", "verification_scope", "score"])
def test_result_roundtrip_rejects_injected_or_stale_parsing(mutation):
    values = verify_rule_draft(draft()).model_dump()
    if mutation == "scope":
        values["parsed_scope"]["lookback_years"] = 1
    elif mutation == "status":
        values["status"] = "ATTESTED_ONLY"
    elif mutation == "metric":
        values["draft"]["metric_key"] = "company.performance.amount"
    elif mutation == "literal":
        values["draft"]["literal"] = "자료 없음"
    elif mutation == "verification_scope":
        values["verification_scope"] = "WHOLE_SOURCE_VERIFIED"
    else:
        values["score"] = 0
    with pytest.raises(ValidationError):
        VerifiedRuleDraftResult.model_validate(values)
