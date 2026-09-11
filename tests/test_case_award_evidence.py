from __future__ import annotations

import hashlib
import re
from copy import deepcopy

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_formula import case_table_points
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord,
    _record_fingerprint_data,
    _CASE_AWARD_CONDITION_UNIT_PATTERN,
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import (
    QUANTITATIVE_ENGINE_VERSION,
    _CANONICAL_METRIC_REGISTRY,
    _compiled_case_table_contract,
    quantitative_request_from_candidate_profile,
)

AID = "SYN-CASE-AWARD"
MANIFEST = "a" * 64


def synthetic_payload(shape="inline", *, metric="PERSONNEL_COUNT"):
    unit = "건" if metric == "PERFORMANCE_COUNT" else "명"
    key = "company.performance.count" if metric == "PERFORMANCE_COUNT" else "company.personnel.count"
    def anchor(quote):
        return dict(attachment_id=AID, page=1, section="SYN 평가표", quote=quote, confidence=0.99)
    header = "SYN 평가항목 7점"
    cases = []
    for order, (prefix, operator, number, suffix) in enumerate([
        ("A.", "GTE", 7, " 이상"), ("B.", "EQ", 5, ""), ("C.", "LTE", 3, " 이하"),
    ], 1):
        condition = f"{prefix} {number}{unit}{suffix}"
        award = f"{number}점"
        if shape == "date": literal = condition + f"\n집계기준 2026.09.\n{number}"
        elif shape == "orphan": literal = condition + f"\n총원\n{number}"
        elif shape == "missing": literal = condition
        elif shape == "wrong": literal = condition + " 0점"
        elif shape == "split": literal = condition + "\n" + award
        elif shape == "bare_split": literal = condition + "\n" + str(number)
        elif shape == "compact": literal = condition.replace(" ", "") + award
        elif shape == "numbered": literal = condition.replace(prefix, str(order) + ")", 1) + " " + award
        elif shape == "fragmented": literal = condition.replace(f"{number}{unit}", f"{number}\n{unit}").replace(" 이상", "\n이상").replace(" 이하", "\n이하") + "\n" + award
        elif shape == "wrapped": literal = condition + f" ({number}점)"
        elif shape == "labeled": literal = condition + f" 배점 {number}"
        elif shape == "colon": literal = condition + f" : {number}점"
        elif shape == "wrong_kind": literal = condition + f" {number}%"
        else: literal = condition + " " + award
        cases.append(dict(literal=literal, operator=operator, comparison_value=number,
            category_values=[], award_kind="POINTS", award_value=number, row_order=order,
            evidence=anchor(literal)))
    criterion = dict(criterion_id="SYN-CRITERION", label="SYN 평가항목", criterion_literal=header,
        max_points=7, scoring_method="CASE_TABLE", metric=metric, unit=unit,
        brackets=[], threshold=None, formula_literal=None, cases=cases, recognition_conditions=[],
        required_evidence=[key], evidence=anchor(header), ambiguity_reason=None)
    total = "정량평가 총점 7점"
    table = dict(table_id="SYN-TABLE", label="SYN 평가표", criteria=[criterion], total_points=7,
        total_evidence=anchor(total), minimum_score=None, minimum_evidence=None, ambiguity_reason=None)
    raw = dict(document_type="RFP", summary="SYN 배점 근거 검사", requirements=[],
        quantitative_tables=[table], quantitative_table_not_applicable=None, missing_or_unreadable=[])
    return raw, "\n".join([header, *(case["literal"] for case in cases), total])


def validate(raw, source):
    sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(ExtractionPayload.model_validate(raw),
        source_text=source, attachment_id=AID, document_sha256=sha, manifest_sha256=MANIFEST)
    profile = merge_validated_quantitative_records([record], expected_documents={AID: sha},
        manifest_sha256=MANIFEST)
    return record, profile, quantitative_request_from_candidate_profile(profile)


@pytest.mark.parametrize("metric", ["PERSONNEL_COUNT", "PERFORMANCE_COUNT"])
@pytest.mark.parametrize("shape", ["missing", "wrong", "wrong_kind"])
def test_comparison_number_cannot_replace_missing_or_different_award(shape, metric):
    raw, source = synthetic_payload(shape, metric=metric)
    before = deepcopy(raw)
    record, _, request = validate(raw, source)
    assert record.status == "INCOMPLETE"
    assert "CASE_NUMBER_MISMATCH" in {issue.code for issue in record.issues}
    assert request.activation_status == "REVIEW_REQUIRED"
    assert raw == before


@pytest.mark.parametrize("shape", ["inline", "split", "bare_split", "compact", "wrapped", "labeled", "colon", "numbered", "fragmented"])
def test_distinct_equal_value_condition_and_award_remain_valid(shape):
    raw, source = synthetic_payload(shape)
    record, profile, request = validate(raw, source)
    assert record.status == "AVAILABLE"
    assert request.activation_status == "AUTO_ACTIVE"
    program = _compiled_case_table_contract(profile.available_candidates[0])
    assert [case_table_points(program, value) for value in (1, 3, 5, 7, 9)] == [3, 3, 5, 7, 7]


@pytest.mark.parametrize("literal", [
    "A. 7명 이상 5명 미만", "A. 7명 이상 5명 미만 7점",
    "A. 7명 이상 0점 7점", "A. 7명 이상 -7점", "A. 7명 이상 +7점",
    "A. 7명 이상 7점 참고", "A. 7명 이상 7", "7점 A. 7명 이상",
    "A. 7명 이상\n집계기준 2026.09.\n7", "A. 7명 이상\n2026-09-\n7",
    "A. 7명 이상\n총원\n7", "A. 7명 이상\n집계기준\n7점",
])
def test_other_condition_or_award_tokens_do_not_supply_missing_proof(literal):
    raw, source = synthetic_payload()
    row = raw["quantitative_tables"][0]["criteria"][0]["cases"][0]
    old = row["literal"]; row["literal"] = literal; row["evidence"]["quote"] = literal
    record, _, request = validate(raw, source.replace(old, literal))
    assert request.activation_status == "REVIEW_REQUIRED"
    assert record.status != "AVAILABLE"


@pytest.mark.parametrize("shape", ["missing", "wrong", "date", "orphan"])
def test_serialized_legacy_available_proof_fails_current_invariants(shape):
    valid_raw, valid_source = synthetic_payload()
    record, _, _ = validate(valid_raw, valid_source)
    bad_raw, bad_source = synthetic_payload(shape)
    legacy = record.model_dump(mode="json")
    legacy["document_sha256"] = hashlib.sha256(bad_source.encode()).hexdigest()
    for frozen, bad in zip(legacy["available_candidates"][0]["cases"],
            bad_raw["quantitative_tables"][0]["criteria"][0]["cases"], strict=True):
        frozen["literal"] = bad["literal"]
        frozen["evidence"]["quote"] = bad["evidence"]["quote"]
    legacy.pop("validation_fingerprint_sha256")
    legacy["validation_fingerprint_sha256"] = _record_fingerprint_data(legacy)
    # The fingerprint is recomputed: this must fail the present evidence
    # invariant, not merely a stale digest or contract-version mismatch.
    with pytest.raises(ValidationError, match="CASE numbers do not match"):
        ValidatedQuantitativeAttachmentRecord.model_validate(legacy)


@pytest.mark.parametrize("shape", ["missing", "wrong", "date", "orphan"])
def test_already_materialized_legacy_profile_cannot_activate(shape):
    raw, source = synthetic_payload()
    _, profile, _ = validate(raw, source)
    bad_raw, _ = synthetic_payload(shape)
    candidate = profile.available_candidates[0]
    cases = tuple(case.model_copy(update={"literal": bad["literal"],
        "evidence": case.evidence.model_copy(update={"quote": bad["evidence"]["quote"]})})
        for case, bad in zip(candidate.cases,
            bad_raw["quantitative_tables"][0]["criteria"][0]["cases"], strict=True))
    candidate = candidate.model_copy(update={"cases": cases})
    profile = profile.model_copy(update={"available_candidates": (candidate,)})
    assert _compiled_case_table_contract(candidate) is None
    assert quantitative_request_from_candidate_profile(profile).activation_status == "REVIEW_REQUIRED"


def test_normal_serialized_proof_survives_without_provider_or_extraction_version_migration():
    raw, source = synthetic_payload("split")
    record, _, _ = validate(raw, source)
    reread = ValidatedQuantitativeAttachmentRecord.model_validate_json(record.model_dump_json())
    assert reread == record
    # Count-domain execution advanced; persisted extraction proofs did not.
    # 1.8.0 adds roster-derived personnel facts, which changes what a score
    # comes out as and so must invalidate cached scores -- but the proof above
    # round-trips unchanged, so no re-extraction follows from the bump.
    assert QUANTITATIVE_ENGINE_VERSION == "pai-loop-quantitative-engine-1.8.0"


@pytest.mark.parametrize("metric,unit,condition,label", [
    ("FINANCIAL_RATIO", "%", "80% 이상", "집계비율"),
    ("PERSONNEL_COUNT", "명", "5명 이상", "보유지분율"),
])
@pytest.mark.parametrize("separator", ["\n", " "])
def test_percent_other_field_is_not_an_award(metric, unit, condition, label, separator):
    raw, _ = synthetic_payload()
    criterion = raw["quantitative_tables"][0]["criteria"][0]
    criterion.update(metric=metric, unit=unit,
        required_evidence=["company.financial.ratio" if metric == "FINANCIAL_RATIO" else "company.personnel.count"])
    row = criterion["cases"][0]
    row.update(comparison_value=80 if metric == "FINANCIAL_RATIO" else 5,
        award_kind="PERCENT_OF_MAX", award_value=100)
    row["literal"] = separator.join([condition, label, "100%"])
    row["evidence"]["quote"] = row["literal"]
    criterion["cases"] = [row]
    source = "\n".join([criterion["criterion_literal"], row["literal"], "정량평가 총점 7점"])
    record, _, request = validate(raw, source)
    assert record.status != "AVAILABLE"
    assert request.activation_status == "REVIEW_REQUIRED"


def test_existing_numeric_unit_aliases_are_recognized_only_as_complete_units():
    for spec in _CANONICAL_METRIC_REGISTRY.values():
        if spec.get("value_kind", "NUMERIC") != "NUMERIC":
            continue
        for unit in spec["unit_scales"]:
            assert re.fullmatch(_CASE_AWARD_CONDITION_UNIT_PATTERN, unit, re.IGNORECASE)
    assert not re.fullmatch(_CASE_AWARD_CONDITION_UNIT_PATTERN, "SYN-METADATA")


def test_supported_english_year_condition_retains_explicit_points():
    raw, _ = synthetic_payload()
    criterion = raw["quantitative_tables"][0]["criteria"][0]
    header = "SYN 사업기간 7점 (단위: year)"
    criterion.update(metric="BUSINESS_YEARS", unit="year", criterion_literal=header,
        required_evidence=["company.business.years"])
    criterion["evidence"]["quote"] = header
    for row, number, points in zip(criterion["cases"], (7, 5, 0), (7, 5, 3), strict=True):
        row.update(operator="GTE", comparison_value=number, award_value=points,
            literal=f"x >= {number} year {points}점")
        row["evidence"]["quote"] = row["literal"]
    source = "\n".join([header, *(row["literal"] for row in criterion["cases"]), "정량평가 총점 7점"])
    record, _, request = validate(raw, source)
    assert record.status == "AVAILABLE"
    assert request.activation_status == "AUTO_ACTIVE"


def flattened_payload(joiner, *, award_offset=8):
    """조건값과 배점이 다른 표. 배점 칸이 joiner 로만 분리돼 있다.

    실제 PDF 표는 행을 한 줄로 눌러 ``C.2~3개교 11`` 처럼 내보낸다. 기본 합성
    payload 는 비교값과 배점을 같은 수로 만들므로 여기서 떼어 놓는다.
    """

    raw, _ = synthetic_payload("missing")
    table = raw["quantitative_tables"][0]
    criterion = table["criteria"][0]
    top = max(case["comparison_value"] for case in criterion["cases"]) + award_offset
    header = f"SYN 평가항목 {top}점"
    criterion["criterion_literal"] = header
    criterion["evidence"]["quote"] = header
    criterion["max_points"] = top
    table["total_points"] = top
    total = f"정량평가 총점 {top}점"
    table["total_evidence"]["quote"] = total
    for case in criterion["cases"]:
        award = case["comparison_value"] + award_offset
        case["award_value"] = award
        case["literal"] = case["literal"] + joiner + str(award)
        case["evidence"]["quote"] = case["literal"]
    source = chr(10).join(
        [header, *(case["literal"] for case in criterion["cases"]), total]
    )
    return raw, source


@pytest.mark.parametrize("joiner", [" ", chr(10)])
def test_award_cell_proves_the_row_whether_a_space_or_a_line_break_splits_it(joiner):
    """``C.2~3개교 11`` 은 줄바꿈판과 같은 행이다. 셀 경계만 공백으로 눌렸다."""

    raw, source = flattened_payload(joiner)
    record, profile, request = validate(raw, source)
    assert record.status == "AVAILABLE"
    assert request.activation_status == "AUTO_ACTIVE"
    program = _compiled_case_table_contract(profile.available_candidates[0])
    assert [case_table_points(program, value) for value in (1, 3, 5, 7, 9)] == [
        11,
        11,
        13,
        15,
        15,
    ]


def test_a_space_split_award_that_repeats_the_comparison_stays_unproven():
    """배점이 조건값을 되풀이하면 그 숫자가 배점 칸이라는 근거가 없다."""

    raw, source = flattened_payload(" ", award_offset=0)
    record, _, request = validate(raw, source)
    assert record.status != "AVAILABLE"
    assert "CASE_NUMBER_MISMATCH" in {issue.code for issue in record.issues}
    assert request.activation_status == "REVIEW_REQUIRED"


def test_a_line_break_still_proves_a_repeated_number_the_space_cannot():
    """같은 값이라도 줄바꿈이면 셀 경계가 원문에 남아 있어 증명된다."""

    raw, source = flattened_payload(chr(10), award_offset=0)
    record, _, request = validate(raw, source)
    assert record.status == "AVAILABLE"
    assert request.activation_status == "AUTO_ACTIVE"


def school_count_payload():
    """교육여행 실적표. 학교 단위로 실적을 센다.

    ``A.5개교이상 15`` 는 서울시교육청 소규모테마형교육여행 공고의 실제 행이다.
    """

    raw, _ = synthetic_payload("missing", metric="PERFORMANCE_COUNT")
    table = raw["quantitative_tables"][0]
    criterion = table["criteria"][0]
    criterion["unit"] = "개교"
    header = "소규모테마형교육여행 수행실적 15점"
    criterion["criterion_literal"] = header
    criterion["evidence"]["quote"] = header
    criterion["max_points"] = 15
    table["total_points"] = 15
    total = "정량평가 총점 15점"
    table["total_evidence"]["quote"] = total
    for case in criterion["cases"]:
        count = case["comparison_value"]
        award = count + 8
        case["award_value"] = award
        bound = {"GTE": "이상", "LTE": "이하", "EQ": ""}[case["operator"]]
        case["literal"] = f"{count}개교{bound} {award}"
        case["evidence"]["quote"] = case["literal"]
    source = chr(10).join(
        [header, *(case["literal"] for case in criterion["cases"]), total]
    )
    return raw, source


def test_a_school_is_a_performance_count_the_condition_can_read():
    """개교는 실적 건수 단위다. 배율표와 조건 어휘가 함께 알아야 한다."""

    raw, source = school_count_payload()
    record, profile, _ = validate(raw, source)
    # 자동 활성화는 실적 인정범위를 따로 요구하므로 여기서는 묻지 않는다.
    assert record.status == "AVAILABLE"
    program = _compiled_case_table_contract(profile.available_candidates[0])
    # 7개교 이상 15점 / 5개교 13점 / 3개교 이하 11점
    assert [case_table_points(program, value) for value in (1, 3, 5, 7, 9)] == [
        11,
        11,
        13,
        15,
        15,
    ]


def test_a_school_counts_as_one_and_not_as_some_other_scale():
    """한 개교는 실적 한 건이다. 배율이 1이 아니면 구간이 어긋난다."""

    assert _CANONICAL_METRIC_REGISTRY["PERFORMANCE_COUNT"]["unit_scales"]["개교"] == 1
    # 시상 건수는 학교로 세지 않으므로 같은 단위를 물려받지 않는다.
    assert "개교" not in _CANONICAL_METRIC_REGISTRY["AWARD_COUNT"]["unit_scales"]


def award_first_payload(*, reverse_only_first=False, award_offset=8):
    """배점 열을 먼저 인쇄한 표. ``10점 : 5개교 이상`` 은 실제 공고 행이다.

    reverse_only_first 는 조건-먼저 행들 틈에 한 행만 뒤집힌 모양을 만든다.
    표의 열 순서가 아니라 그 행의 추출 오류인 경우다.
    """

    raw, source = flattened_payload(" ", award_offset=award_offset)
    criterion = raw["quantitative_tables"][0]["criteria"][0]
    rewritten = []
    for index, case in enumerate(criterion["cases"]):
        text = " ".join(case["literal"].split())
        condition, _, award = text.rpartition(" ")
        if reverse_only_first and index > 0:
            rewritten.append(case["literal"])
            continue
        flipped = "%s점 : %s" % (award, condition)
        case["literal"] = flipped
        case["evidence"]["quote"] = flipped
        rewritten.append(flipped)
    header = criterion["criterion_literal"]
    total = raw["quantitative_tables"][0]["total_evidence"]["quote"]
    return raw, chr(10).join([header, *rewritten, total])


def test_a_table_that_prints_the_award_first_is_read_in_its_own_order():
    """모든 행이 같은 모양이면 그것이 표의 열 순서다."""

    raw, source = award_first_payload()
    record, profile, _ = validate(raw, source)
    assert record.status == "AVAILABLE"
    program = _compiled_case_table_contract(profile.available_candidates[0])
    assert [case_table_points(program, value) for value in (1, 3, 5, 7, 9)] == [
        11,
        11,
        13,
        15,
        15,
    ]


def test_one_reversed_row_among_condition_first_rows_stays_unproven():
    """열 순서라면 행마다 같아야 한다. 하나만 뒤집힌 것은 추출 오류다."""

    raw, source = award_first_payload(reverse_only_first=True)
    record, _, request = validate(raw, source)
    assert record.status != "AVAILABLE"
    assert request.activation_status == "REVIEW_REQUIRED"


def test_an_award_first_row_may_repeat_its_own_comparison():
    """``5점 : 5명 이상`` 은 임계값과 우연히 같은 배점 열일 뿐이다.

    뒤에 붙은 배점은 경계의 증거가 공백뿐이라 값이 겹치면 가릴 수 없지만,
    앞자리는 모든 행이 같은 모양이라는 표 단위 증거가 이미 열 순서를 정한다.
    """

    raw, source = award_first_payload(award_offset=0)
    record, profile, _ = validate(raw, source)
    assert record.status == "AVAILABLE"
    program = _compiled_case_table_contract(profile.available_candidates[0])
    # 7명 이상 7점 / 5명 5점 / 3명 이하 3점
    assert [case_table_points(program, value) for value in (1, 3, 5, 7, 9)] == [
        3,
        3,
        5,
        7,
        7,
    ]


def test_a_leading_number_carrying_a_comparison_is_a_condition_not_an_award():
    """``60%이상`` 은 기준비율 조건이지 배점이 아니다."""

    from pai_loop.quantitative_rule_extraction import _leading_award_split

    assert _leading_award_split("60%이상-10점") is None
    assert _leading_award_split("10점 : 5개교 이상") == ("10점", "5개교 이상")
