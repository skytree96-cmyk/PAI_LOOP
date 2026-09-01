from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ExtractionPayload,
)
from pai_loop.models import Notice, NoticeVersion
from pai_loop.pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_METADATA_KIND,
    PPS_METADATA_SCHEMA,
    PPS_PROCESSING_VERSION,
)
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord,
    build_quantitative_candidate_profile,
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint,
)
from pai_loop.quantitative_scoring import (
    _current_dynamic_quantitative_profile,
    quantitative_request_from_candidate_profile,
)


ATTACHMENT_ID = "ATT-QUANT-1"
VALID_SOURCE = """정량평가표
수행실적 20점
10억원 이상 20점
5억원 이상 10억원 미만 15점
5억원 미만 10점
정량평가 총점 20점
통과 최저점 12점
"""


def anchor(quote: str, *, page: int = 4) -> dict:
    return {
        "attachment_id": ATTACHMENT_ID,
        "page": page,
        "section": "정량평가표",
        "quote": quote,
        "confidence": 0.99,
    }


def valid_table() -> dict:
    return {
        "table_id": "QUANT-TABLE-1",
        "label": "정량평가표",
        "criteria": [
            {
                "criterion_id": "PERFORMANCE-AMOUNT-1",
                "label": "수행실적",
                "criterion_literal": "수행실적 20점",
                "max_points": 20,
                "scoring_method": "BRACKET",
                "metric": "PERFORMANCE_AMOUNT",
                "unit": "억원",
                "brackets": [
                    {
                        "label": "10억원 이상",
                        "literal": "10억원 이상 20점",
                        "min_value": 10,
                        "max_value": None,
                        "min_inclusive": True,
                        "max_inclusive": False,
                        "points": 20,
                        "evidence": anchor("10억원 이상 20점"),
                    },
                    {
                        "label": "5억원 이상 10억원 미만",
                        "literal": "5억원 이상 10억원 미만 15점",
                        "min_value": 5,
                        "max_value": 10,
                        "min_inclusive": True,
                        "max_inclusive": False,
                        "points": 15,
                        "evidence": anchor("5억원 이상 10억원 미만 15점"),
                    },
                    {
                        "label": "5억원 미만",
                        "literal": "5억원 미만 10점",
                        "min_value": None,
                        "max_value": 5,
                        "min_inclusive": False,
                        "max_inclusive": False,
                        "points": 10,
                        "evidence": anchor("5억원 미만 10점"),
                    },
                ],
                "threshold": None,
                "formula_literal": None,
                "required_evidence": ["company.performance.amount"],
                "evidence": anchor("수행실적 20점"),
                "ambiguity_reason": None,
            }
        ],
        "total_points": 20,
        "total_evidence": anchor("정량평가 총점 20점"),
        "minimum_score": 12,
        "minimum_evidence": anchor("통과 최저점 12점"),
        "ambiguity_reason": None,
    }


def payload_with_table(table: dict | None = None) -> ExtractionPayload:
    return ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [table or valid_table()],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "정량평가표 원문 규칙 추출",
        }
    )


def payload_with_gap(
    gap: str,
    *,
    table: dict | None = None,
    document_type: str = "RFP",
) -> ExtractionPayload:
    return ExtractionPayload.model_validate(
        {
            "document_type": document_type,
            "requirements": [],
            "quantitative_tables": [table] if table is not None else [],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [gap],
            "summary": "첨부별 원문 결손 보고",
        }
    )


def threshold_table(*, operator: str = "GTE") -> tuple[dict, str]:
    literal = "5억원 이상 충족 10점 미충족 0점"
    source = "\n".join(
        ["정량평가표", "수행실적 10점", literal, "정량평가 총점 10점"]
    )
    table = {
        "table_id": "QUANT-THRESHOLD-1",
        "label": "정량평가표",
        "criteria": [
            {
                "criterion_id": "PERFORMANCE-THRESHOLD-1",
                "label": "수행실적",
                "criterion_literal": "수행실적 10점",
                "max_points": 10,
                "scoring_method": "THRESHOLD",
                "metric": "PERFORMANCE_AMOUNT",
                "unit": "억원",
                "brackets": [],
                "threshold": {
                    "literal": literal,
                    "operator": operator,
                    "threshold_value": 5,
                    "points_if_met": 10,
                    "points_if_not_met": 0,
                    "evidence": anchor(literal),
                },
                "formula_literal": None,
                "required_evidence": ["company.performance.amount"],
                "evidence": anchor("수행실적 10점"),
                "ambiguity_reason": None,
            }
        ],
        "total_points": 10,
        "total_evidence": anchor("정량평가 총점 10점"),
        "minimum_score": None,
        "minimum_evidence": None,
        "ambiguity_reason": None,
    }
    return table, source


def single_range_table(
    literal: str,
    *,
    min_inclusive: bool,
    max_inclusive: bool,
) -> tuple[dict, str]:
    source = "\n".join(
        ["정량평가표", "수행실적 15점", literal, "정량평가 총점 15점"]
    )
    table = {
        "table_id": "QUANT-RANGE-1",
        "label": "정량평가표",
        "criteria": [
            {
                "criterion_id": "PERFORMANCE-RANGE-1",
                "label": "수행실적",
                "criterion_literal": "수행실적 15점",
                "max_points": 15,
                "scoring_method": "BRACKET",
                "metric": "PERFORMANCE_AMOUNT",
                "unit": "억원",
                "brackets": [
                    {
                        "label": "수행실적 범위",
                        "literal": literal,
                        "min_value": 5,
                        "max_value": 10,
                        "min_inclusive": min_inclusive,
                        "max_inclusive": max_inclusive,
                        "points": 15,
                        "evidence": anchor(literal),
                    }
                ],
                "threshold": None,
                "formula_literal": None,
                "required_evidence": ["company.performance.amount"],
                "evidence": anchor("수행실적 15점"),
                "ambiguity_reason": None,
            }
        ],
        "total_points": 15,
        "total_evidence": anchor("정량평가 총점 15점"),
        "minimum_score": None,
        "minimum_evidence": None,
        "ambiguity_reason": None,
    }
    return table, source


def build(payload: ExtractionPayload, *, source: str = VALID_SOURCE):
    return build_quantitative_candidate_profile(
        {ATTACHMENT_ID: payload},
        {ATTACHMENT_ID: source},
        expected_attachment_ids={ATTACHMENT_ID},
    )


def issue_codes(profile) -> set[str]:
    return {item.code for item in profile.issues}


def test_valid_literal_table_becomes_immutable_available_candidate_profile() -> None:
    profile = build(payload_with_table())

    assert profile.status == "AVAILABLE"
    assert profile.expected_attachment_ids == (ATTACHMENT_ID,)
    assert profile.processed_attachment_ids == (ATTACHMENT_ID,)
    assert len(profile.tables) == 1
    assert profile.tables[0].status == "AVAILABLE"
    assert profile.tables[0].minimum_score == 12
    assert profile.tables[0].minimum_evidence is not None
    assert len(profile.available_candidates) == 1
    assert profile.available_candidates[0].metric == "PERFORMANCE_AMOUNT"
    assert profile.available_candidates[0].required_evidence == (
        "company.performance.amount",
    )
    assert profile.review_candidates == ()
    with pytest.raises(ValidationError):
        profile.status = "REVIEW"  # type: ignore[misc]


def test_exact_quote_mismatch_is_incomplete_and_never_available() -> None:
    table = valid_table()
    table["criteria"][0]["evidence"]["quote"] = "원문에 없는 수행실적 20점"
    profile = build(payload_with_table(table))

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "UNVERIFIED_QUOTE" in issue_codes(profile)
    assert profile.review_candidates[0].status == "INCOMPLETE"


def test_table_total_must_reconcile_with_criterion_maximums() -> None:
    table = valid_table()
    table["total_points"] = 30
    table["total_evidence"] = anchor("정량평가 총점 30점")
    source = VALID_SOURCE.replace("정량평가 총점 20점", "정량평가 총점 30점")
    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "TABLE_TOTAL_MISMATCH" in issue_codes(profile)


def test_overlapping_inclusive_brackets_are_incomplete() -> None:
    table = valid_table()
    lower = table["criteria"][0]["brackets"][2]
    lower["literal"] = "6억원 이하 10점"
    lower["label"] = "6억원 이하"
    lower["max_value"] = 6
    lower["max_inclusive"] = True
    lower["evidence"] = anchor("6억원 이하 10점")
    source = VALID_SOURCE.replace("5억원 미만 10점", "6억원 이하 10점")
    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "OVERLAPPING_BRACKETS" in issue_codes(profile)


def test_bracket_inclusivity_must_match_literal_comparator() -> None:
    table = valid_table()
    table["criteria"][0]["brackets"][0]["min_inclusive"] = False
    profile = build(payload_with_table(table))

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "BRACKET_COMPARATOR_MISMATCH" in issue_codes(profile)


def test_threshold_lt_cannot_bind_to_korean_gte_literal() -> None:
    table, source = threshold_table(operator="LT")
    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "THRESHOLD_COMPARATOR_MISMATCH" in issue_codes(profile)


def test_threshold_operator_matching_literal_is_available() -> None:
    table, source = threshold_table(operator="GTE")
    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE"
    assert profile.available_candidates[0].threshold is not None
    assert profile.available_candidates[0].threshold.operator == "GTE"


@pytest.mark.parametrize(
    ("literal", "min_inclusive", "max_inclusive"),
    [
        ("5억원 초과 10억원 이하 15점", False, True),
        ("performance >= 5 and performance < 10, 15점", True, False),
        ("5 <= performance < 10, 15점", True, False),
    ],
)
def test_korean_and_ascii_range_comparators_bind_exactly(
    literal: str,
    min_inclusive: bool,
    max_inclusive: bool,
) -> None:
    table, source = single_range_table(
        literal,
        min_inclusive=min_inclusive,
        max_inclusive=max_inclusive,
    )
    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)


def test_ascii_range_reversed_inclusivity_is_incomplete() -> None:
    literal = "performance >= 5 and performance < 10, 15점"
    table, source = single_range_table(
        literal,
        min_inclusive=False,
        max_inclusive=False,
    )
    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert "BRACKET_COMPARATOR_MISMATCH" in issue_codes(profile)


def test_unknown_metric_is_review_not_available() -> None:
    table = valid_table()
    table["criteria"][0]["metric"] = "UNKNOWN"
    profile = build(payload_with_table(table))

    assert profile.status == "REVIEW"
    assert profile.available_candidates == ()
    assert profile.review_candidates[0].status == "REVIEW"
    assert "UNKNOWN_METRIC" in issue_codes(profile)


@pytest.mark.parametrize("placeholder", ["TBD", "UNKNOWN", "확인 필요", "placeholder"])
def test_missing_fact_placeholder_is_incomplete(placeholder: str) -> None:
    table = valid_table()
    table["criteria"][0]["required_evidence"] = [placeholder]
    profile = build(payload_with_table(table))

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "REQUIRED_EVIDENCE_INCOMPLETE" in issue_codes(profile)


def test_model_invented_evidence_key_is_never_available() -> None:
    table = valid_table()
    table["criteria"][0]["required_evidence"] = ["company.fake.max"]
    profile = build(payload_with_table(table))

    assert profile.status == "REVIEW"
    assert profile.available_candidates == ()
    assert profile.review_candidates[0].status == "REVIEW"
    assert "UNREGISTERED_REQUIRED_EVIDENCE" in issue_codes(profile)


def test_explicit_no_table_statement_is_not_applicable_with_exact_evidence() -> None:
    statement = "본 사업은 정량평가가 해당 없음"
    payload = ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [],
            "quantitative_table_not_applicable": {
                "reason_literal": "정량평가가 해당 없음",
                "evidence": anchor(statement),
            },
            "missing_or_unreadable": [],
            "summary": "정량평가 비적용",
        }
    )
    profile = build(payload, source=statement)

    assert profile.status == "NOT_APPLICABLE"
    assert len(profile.not_applicable_evidence) == 1
    assert profile.tables == ()
    assert profile.available_candidates == ()


def test_plain_absence_is_not_treated_as_no_table_evidence() -> None:
    payload = ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "정량평가 언급 없음",
        }
    )
    profile = build(payload, source="과업 일반사항")

    assert profile.status == "INCOMPLETE"
    assert "QUANTITATIVE_TABLE_NOT_ESTABLISHED" in issue_codes(profile)


def test_current_manifest_source_set_must_be_complete() -> None:
    profile = build_quantitative_candidate_profile(
        {ATTACHMENT_ID: payload_with_table()},
        {ATTACHMENT_ID: VALID_SOURCE},
        expected_attachment_ids={ATTACHMENT_ID, "ATT-MISSING"},
        incomplete_attachment_ids={"ATT-MISSING"},
    )

    assert profile.status == "INCOMPLETE"
    assert {"ATTACHMENT_EXTRACTION_MISSING", "ATTACHMENT_INCOMPLETE"} <= issue_codes(
        profile
    )


def test_payload_schema_rejects_unexpected_model_decision_fields() -> None:
    raw = payload_with_table().model_dump(mode="python")
    raw["quantitative_tables"][0]["criteria"][0]["company_score"] = 19
    raw["quantitative_tables"][0]["criteria"][0]["go_decision"] = "GO"

    with pytest.raises(ValidationError):
        ExtractionPayload.model_validate(raw)


def test_persisted_attachment_record_has_exact_bindings_but_no_raw_source() -> None:
    manifest_sha = "b" * 64
    document_sha = "a" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )

    assert record.status == "AVAILABLE"
    assert record.document_sha256 == document_sha
    assert record.manifest_sha256 == manifest_sha
    assert record.validation_fingerprint_sha256 == validated_quantitative_record_fingerprint(
        record
    )
    serialized = record.model_dump_json()
    assert VALID_SOURCE not in serialized
    assert "source_text" not in serialized
    restored = ValidatedQuantitativeAttachmentRecord.model_validate_json(serialized)

    profile = merge_validated_quantitative_records(
        [restored],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )
    assert profile.status == "AVAILABLE"
    assert profile.manifest_sha256 == manifest_sha
    assert profile.document_bindings[0].document_sha256 == document_sha
    assert len(profile.available_candidates) == 1


@pytest.mark.parametrize(
    "runtime_profile",
    [
        {
            "document_type": "RFP",
            "source_label": "제안요청서.hwp",
        },
        {
            "document_type": "RFP",
            "source_label": "제안요청서.hwp",
            "missing_or_unreadable": None,
        },
        {
            "document_type": "RFP",
            "source_label": "제안요청서.hwp",
            "missing_or_unreadable": "not-a-list",
        },
    ],
)
def test_supplied_runtime_profile_requires_a_valid_source_gap_list(
    runtime_profile: dict[str, object],
) -> None:
    manifest_sha = "b" * 64
    document_sha = "a" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )

    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
        attachment_profiles={ATTACHMENT_ID: runtime_profile},
    )

    assert profile.status == "INCOMPLETE"
    assert "SOURCE_GAP_BINDING_MISMATCH" in issue_codes(profile)


def test_coherent_review_and_incomplete_records_survive_strict_invariants() -> None:
    review_table = valid_table()
    review_table["criteria"][0]["metric"] = "UNKNOWN"
    review_record = validate_quantitative_attachment_extraction(
        payload_with_table(review_table),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    assert review_record.status == "REVIEW"
    assert review_record.review_candidates[0].status == "REVIEW"

    incomplete_table = valid_table()
    incomplete_table["total_points"] = 30
    incomplete_table["total_evidence"] = anchor("정량평가 총점 30점")
    incomplete_record = validate_quantitative_attachment_extraction(
        payload_with_table(incomplete_table),
        source_text=VALID_SOURCE.replace("정량평가 총점 20점", "정량평가 총점 30점"),
        attachment_id=ATTACHMENT_ID,
        document_sha256="c" * 64,
        manifest_sha256="d" * 64,
    )
    assert incomplete_record.status == "INCOMPLETE"
    assert incomplete_record.review_candidates[0].status == "INCOMPLETE"


def test_durable_merge_accepts_neutral_no_table_record_from_another_chunk() -> None:
    manifest_sha = "c" * 64
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    neutral_payload = ExtractionPayload.model_validate(
        {
            "document_type": "FORM",
            "requirements": [],
            "quantitative_tables": [],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "제출 양식",
        }
    )
    neutral_record = validate_quantitative_attachment_extraction(
        neutral_payload,
        source_text="제출 양식 일반사항",
        attachment_id="ATT-FORM-2",
        document_sha256="d" * 64,
        manifest_sha256=manifest_sha,
    )

    assert neutral_record.status == "NO_TABLE"
    profile = merge_validated_quantitative_records(
        [neutral_record, table_record],
        expected_documents={
            ATTACHMENT_ID: "a" * 64,
            "ATT-FORM-2": "d" * 64,
        },
        manifest_sha256=manifest_sha,
    )
    assert profile.status == "AVAILABLE"
    assert set(profile.processed_attachment_ids) == {ATTACHMENT_ID, "ATT-FORM-2"}


def test_explicit_qualitative_only_omission_does_not_block_quantitative_table() -> None:
    gap = (
        "가. 평가 항목별 배점 표에서 매우우수/우수/보통/미흡 등급의 실제 "
        "점수 구간 기준이 정성평가 항목에 대해 상세 서술되지 않음"
        "(정성평가이므로 정량 테이블에서 제외)"
    )
    record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, table=valid_table()),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )

    assert record.status == "AVAILABLE"
    assert "EXTRACTION_DECLARED_INCOMPLETE" not in {
        issue.code for issue in record.issues
    }


def test_local_quantitative_table_absence_resolves_only_with_available_sibling() -> None:
    manifest_sha = "c" * 64
    local_attachment_id = "ATT-PDF-2"
    local_gap = (
        "제안요청서(붙임) 본문이 제공되지 않아 세부 평가배점표"
        "(정량평가 기준)를 확인할 수 없음"
    )
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(local_gap, document_type="NOTICE"),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="d" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )

    assert local_record.status == "INCOMPLETE"
    local_issue = next(
        issue
        for issue in local_record.issues
        if issue.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
    )
    assert local_issue.required_sibling_document_types == ("RFP",)
    assert local_issue.required_sibling_label_markers == ("제안요청서",)
    profile = merge_validated_quantitative_records(
        [local_record, table_record],
        expected_documents={
            local_attachment_id: "d" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [local_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "부산교육한마당 제안 요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "AVAILABLE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" not in issue_codes(profile)


def test_available_table_in_review_record_resolves_only_the_sibling_absence() -> None:
    manifest_sha = "e" * 64
    local_attachment_id = "ATT-NOTICE-REVIEW-SIBLING"
    local_gap = "공고문에는 제안요청서 본문이 제공되지 않아 평가배점표를 확인할 수 없음"
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(local_gap, document_type="NOTICE"),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="1" * 64,
        manifest_sha256=manifest_sha,
    )
    review_table = json.loads(json.dumps(valid_table()))
    review_table["table_id"] = "QUANT-TABLE-REVIEW"
    review_table["label"] = "검토대상 정량평가표"
    review_table["criteria"][0]["criterion_id"] = "PERFORMANCE-AMOUNT-REVIEW"
    review_table["ambiguity_reason"] = "두 평가표 중 적용 대상을 원문에서 확정할 수 없음"
    mixed_payload = ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [valid_table(), review_table],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "확정 표와 별도 검토 표가 함께 있음",
        }
    )
    mixed_record = validate_quantitative_attachment_extraction(
        mixed_payload,
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )

    assert mixed_record.status == "REVIEW"
    assert any(table.status == "AVAILABLE" for table in mixed_record.tables)
    profile = merge_validated_quantitative_records(
        [local_record, mixed_record],
        expected_documents={
            local_attachment_id: "1" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "공고문.pdf",
                "missing_or_unreadable": [local_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "REVIEW"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" not in issue_codes(profile)
    assert profile.issues


def test_named_same_type_sibling_resolves_misclassified_notice_gap() -> None:
    manifest_sha = "3" * 64
    local_attachment_id = "ATT-MISCLASSIFIED-NOTICE"
    local_gap = (
        "제안요청서(붙임) 본문이 제공되지 않아 세부 평가배점표"
        "(정량평가 기준)를 확인할 수 없음"
    )
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(local_gap, document_type="RFP"),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="4" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )

    local_issue = next(
        issue
        for issue in local_record.issues
        if issue.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
    )
    assert local_issue.required_sibling_document_types == ("RFP",)
    assert local_issue.required_sibling_label_markers == ("제안요청서",)
    profile = merge_validated_quantitative_records(
        [local_record, table_record],
        expected_documents={
            local_attachment_id: "4" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "RFP",
                "source_label": "공고문(재공고).pdf",
                "missing_or_unreadable": [local_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "부산교육한마당 제안 요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "AVAILABLE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" not in issue_codes(profile)


@pytest.mark.parametrize(
    ("source_label", "document_type", "expected_status"),
    [
        ("부산교육한마당 제안 요청서.hwp", "OTHER", "AVAILABLE"),
        ("입찰공고문_제안요청서.hwp", "RFP", "INCOMPLETE"),
        ("평가자료.hwp", "OTHER", "INCOMPLETE"),
        ("제안요청서 작성양식.hwp", "FORM", "INCOMPLETE"),
        ("제안요청서 작성양식.hwp", "OTHER", "INCOMPLETE"),
        ("제안요청서 작성양식.hwp", "RFP", "INCOMPLETE"),
    ],
)
def test_unambiguous_manifest_label_can_recover_sibling_document_type(
    source_label: str,
    document_type: str,
    expected_status: str,
) -> None:
    manifest_sha = "5" * 64
    local_attachment_id = "ATT-NOTICE-LABEL-ROLE"
    local_gap = "공고문에는 제안요청서 본문이 제공되지 않아 평가배점표를 확인할 수 없음"
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(local_gap, document_type="NOTICE"),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="6" * 64,
        manifest_sha256=manifest_sha,
    )
    other_payload = payload_with_table().model_copy(
        update={"document_type": document_type}
    )
    table_record = validate_quantitative_attachment_extraction(
        other_payload,
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [local_record, table_record],
        expected_documents={
            local_attachment_id: "6" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "공고문.pdf",
                "missing_or_unreadable": [local_gap],
            },
            ATTACHMENT_ID: {
                "document_type": document_type,
                "source_label": source_label,
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == expected_status


def test_current_persisted_manifest_recovers_busan_sibling_table() -> None:
    def digest(value: object) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    notice_attachment_id = "PPS-ATT-111111111111111111111111"
    rfp_attachment_id = "PPS-ATT-222222222222222222222222"
    manifest = [
        {
            "attachment_id": notice_attachment_id,
            "file_name": "공고문(재공고).pdf",
            "media_type": "application/pdf",
            "url": (
                "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
                "?bidPbancNo=R26BK01703600&fileSeq=1"
            ),
            "slot": 1,
        },
        {
            "attachment_id": rfp_attachment_id,
            "file_name": "『2026 부산교육한마당』 위탁 용역 제안 요청서.hwp",
            "media_type": "application/x-hwp",
            "url": (
                "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
                "?bidPbancNo=R26BK01703600&fileSeq=2"
            ),
            "slot": 2,
        },
    ]
    manifest_sha = digest(manifest)
    notice_gap = (
        "제안요청서(붙임) 본문이 제공되지 않아 세부 평가배점표"
        "(정량평가 기준)를 확인할 수 없음"
    )
    notice_payload = payload_with_gap(notice_gap, document_type="RFP")
    rfp_payload = ExtractionPayload.model_validate(
        json.loads(
            json.dumps(payload_with_table().model_dump(mode="json")).replace(
                ATTACHMENT_ID,
                rfp_attachment_id,
            )
        )
    )
    notice_document_sha = "7" * 64
    rfp_document_sha = "8" * 64
    notice_record = validate_quantitative_attachment_extraction(
        notice_payload,
        source_text="입찰공고 일반사항",
        attachment_id=notice_attachment_id,
        document_sha256=notice_document_sha,
        manifest_sha256=manifest_sha,
    )
    rfp_record = validate_quantitative_attachment_extraction(
        rfp_payload,
        source_text=VALID_SOURCE,
        attachment_id=rfp_attachment_id,
        document_sha256=rfp_document_sha,
        manifest_sha256=manifest_sha,
    )

    def attempt(
        *,
        version_no: int,
        attachment: dict,
        document_sha: str,
        payload: ExtractionPayload,
        record: ValidatedQuantitativeAttachmentRecord,
    ) -> NoticeVersion:
        return NoticeVersion(
            version_no=version_no,
            file_sha256=document_sha,
            document_complete=True,
            extraction_status="ACCEPTED",
            extraction_confidence=1,
            source_payload={
                "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                "source_kind": PPS_ATTACHMENT_SOURCE,
                "attachment_id": attachment["attachment_id"],
                "source_label": attachment["file_name"],
                "manifest_sha256": digest(attachment),
                "current_manifest_sha256": manifest_sha,
                "document_sha256": document_sha,
                "prompt_version": PROMPT_VERSION,
                "processing_version": PPS_PROCESSING_VERSION,
                "schema_version": SCHEMA_VERSION,
                "status": "ACCEPTED",
                "result": payload.model_dump(mode="json"),
                "quantitative_validation_record": record.model_dump(mode="json"),
            },
        )

    notice = Notice(
        notice_key="PPS-R26BK01703600-000-TEST",
        bid_notice_no="R26BK01703600",
        revision_no="000",
        title="2026 부산교육한마당 위탁 용역",
        agency="부산광역시교육청",
        deadline=datetime.now(timezone.utc) + timedelta(days=7),
        status="OPEN",
    )
    notice.versions = [
        NoticeVersion(
            version_no=1,
            file_sha256="9" * 64,
            document_complete=False,
            extraction_status="METADATA",
            extraction_confidence=1,
            source_payload={
                "kind": PPS_METADATA_KIND,
                "schema_version": PPS_METADATA_SCHEMA,
                "attachment_manifest": manifest,
            },
        ),
        attempt(
            version_no=2,
            attachment=manifest[0],
            document_sha=notice_document_sha,
            payload=notice_payload,
            record=notice_record,
        ),
        attempt(
            version_no=3,
            attachment=manifest[1],
            document_sha=rfp_document_sha,
            payload=rfp_payload,
            record=rfp_record,
        ),
    ]

    profile = _current_dynamic_quantitative_profile(notice)

    assert profile is not None
    assert profile.status == "AVAILABLE", profile.issues
    request = quantitative_request_from_candidate_profile(profile)
    assert request.rule_source_status == "AVAILABLE"
    assert request.source_validation_status == "SOURCE_VALIDATED"
    assert len(profile.tables) == 1
    assert profile.tables[0].total_points == 20
    assert len(profile.available_candidates) == 1


def test_current_document_label_is_not_mistaken_for_a_required_sibling() -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_gap(
            "공고문에는 제안요청서 본문이 제공되지 않아 세부 평가배점표를 확인할 수 없음",
            document_type="NOTICE",
        ),
        source_text="입찰공고 일반사항",
        attachment_id="ATT-PDF-CONTEXT",
        document_sha256="9" * 64,
        manifest_sha256="0" * 64,
    )
    local_issues = [
        issue
        for issue in record.issues
        if issue.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
    ]

    assert {
        (
            issue.required_sibling_document_types,
            issue.required_sibling_label_markers,
        )
        for issue in local_issues
    } == {
        (("NOTICE",), ("공고문",)),
        (("RFP",), ("제안요청서",)),
    }


def test_manifest_label_recovers_multi_role_gap_from_misclassified_source() -> None:
    manifest_sha = "1" * 64
    source_id = "ATT-MISCLASSIFIED-DUAL-ROLE"
    gap = "공고문에는 제안요청서 본문이 제공되지 않아 평가배점표를 확인할 수 없음"
    source_record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, document_type="RFP"),
        source_text="입찰공고 일반사항",
        attachment_id=source_id,
        document_sha256="2" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )

    profile = merge_validated_quantitative_records(
        [source_record, table_record],
        expected_documents={source_id: "2" * 64, ATTACHMENT_ID: "a" * 64},
        manifest_sha256=manifest_sha,
        attachment_profiles={
            source_id: {
                "document_type": "RFP",
                "source_label": "공고문(재공고).pdf",
                "missing_or_unreadable": [gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "AVAILABLE", profile.issues
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" not in issue_codes(profile)


def test_actual_rfp_gap_cannot_borrow_another_rfp_table() -> None:
    manifest_sha = "3" * 64
    source_id = "ATT-ACTUAL-RFP"
    gap = "제안요청서 본문이 제공되지 않아 평가배점표를 확인할 수 없음"
    source_record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, document_type="RFP"),
        source_text="제안요청서 일반사항",
        attachment_id=source_id,
        document_sha256="4" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )

    profile = merge_validated_quantitative_records(
        [source_record, table_record],
        expected_documents={source_id: "4" * 64, ATTACHMENT_ID: "a" * 64},
        manifest_sha256=manifest_sha,
        attachment_profiles={
            source_id: {
                "document_type": "RFP",
                "source_label": "제안요청서 본편.hwp",
                "missing_or_unreadable": [gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서 부록.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "INCOMPLETE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" in issue_codes(profile)


def test_ambiguous_source_label_cannot_select_a_sibling_role() -> None:
    manifest_sha = "5" * 64
    source_id = "ATT-AMBIGUOUS-SOURCE"
    gap = "제안요청서 본문이 제공되지 않아 평가배점표를 확인할 수 없음"
    source_record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, document_type="RFP"),
        source_text="입찰공고 일반사항",
        attachment_id=source_id,
        document_sha256="6" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )

    profile = merge_validated_quantitative_records(
        [source_record, table_record],
        expected_documents={source_id: "6" * 64, ATTACHMENT_ID: "a" * 64},
        manifest_sha256=manifest_sha,
        attachment_profiles={
            source_id: {
                "document_type": "RFP",
                "source_label": "입찰공고문_제안요청서.pdf",
                "missing_or_unreadable": [gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "INCOMPLETE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" in issue_codes(profile)


def test_all_explicit_sibling_role_groups_must_be_satisfied() -> None:
    manifest_sha = "7" * 64
    source_id = "ATT-MULTI-GROUP-SOURCE"
    scope_id = "ATT-MULTI-GROUP-SCOPE"
    gap = (
        "제안요청서와 과업지시서가 별도 제공되지 않아 "
        "평가배점표를 확인할 수 없음"
    )
    source_record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, document_type="NOTICE"),
        source_text="입찰공고 일반사항",
        attachment_id=source_id,
        document_sha256="8" * 64,
        manifest_sha256=manifest_sha,
    )
    rfp_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    scope_payload_data = json.loads(
        json.dumps(payload_with_table().model_dump(mode="json")).replace(
            ATTACHMENT_ID,
            scope_id,
        )
    )
    scope_payload_data["document_type"] = "SCOPE"
    scope_payload_data["quantitative_tables"][0]["table_id"] = "QUANT-TABLE-SCOPE"
    scope_payload_data["quantitative_tables"][0]["criteria"][0][
        "criterion_id"
    ] = "PERFORMANCE-AMOUNT-SCOPE"
    scope_record = validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(scope_payload_data),
        source_text=VALID_SOURCE,
        attachment_id=scope_id,
        document_sha256="b" * 64,
        manifest_sha256=manifest_sha,
    )
    base_profiles = {
        source_id: {
            "document_type": "NOTICE",
            "source_label": "공고문.pdf",
            "missing_or_unreadable": [gap],
        },
        ATTACHMENT_ID: {
            "document_type": "RFP",
            "source_label": "제안요청서.hwp",
            "missing_or_unreadable": [],
        },
    }

    missing_scope = merge_validated_quantitative_records(
        [source_record, rfp_record],
        expected_documents={source_id: "8" * 64, ATTACHMENT_ID: "a" * 64},
        manifest_sha256=manifest_sha,
        attachment_profiles=base_profiles,
    )
    complete = merge_validated_quantitative_records(
        [source_record, rfp_record, scope_record],
        expected_documents={
            source_id: "8" * 64,
            ATTACHMENT_ID: "a" * 64,
            scope_id: "b" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            **base_profiles,
            scope_id: {
                "document_type": "SCOPE",
                "source_label": "과업지시서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert missing_scope.status == "INCOMPLETE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" in issue_codes(
        missing_scope
    )
    assert complete.status == "AVAILABLE", complete.issues
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" not in issue_codes(complete)


def test_multi_type_specification_target_keeps_all_explicit_sibling_types() -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_gap(
            "별도 규격서가 제공되지 않아 평가배점표를 확인할 수 없음",
            document_type="RFP",
        ),
        source_text="제안요청서 일반사항",
        attachment_id="ATT-RFP-CONTEXT",
        document_sha256="1" * 64,
        manifest_sha256="2" * 64,
    )
    local_issue = next(
        issue
        for issue in record.issues
        if issue.code == "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"
    )

    assert local_issue.required_sibling_document_types == ("RFP", "SCOPE")
    assert local_issue.required_sibling_label_markers == ("규격서",)


def test_local_quantitative_table_absence_cannot_resolve_from_self() -> None:
    local_gap = "이 첨부 본문에는 정량평가 배점표가 포함되지 않음"
    record = validate_quantitative_attachment_extraction(
        payload_with_gap(local_gap, table=valid_table()),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
        attachment_profiles={
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [local_gap],
            }
        },
    )

    assert profile.status == "INCOMPLETE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" in issue_codes(profile)


def test_unreadable_quantitative_gap_is_not_resolved_by_available_sibling() -> None:
    manifest_sha = "e" * 64
    local_attachment_id = "ATT-PDF-3"
    unreadable_gap = (
        "공고문 본문의 정량평가 배점표가 제공되지 않았고 일부는 흐려 판독 불가"
    )
    unreadable_record = validate_quantitative_attachment_extraction(
        payload_with_gap(
            unreadable_gap,
            document_type="NOTICE",
        ),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="f" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [unreadable_record, table_record],
        expected_documents={
            local_attachment_id: "f" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [unreadable_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "INCOMPLETE"
    assert "EXTRACTION_DECLARED_INCOMPLETE" in issue_codes(profile)
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" not in issue_codes(profile)


@pytest.mark.parametrize(
    "gap",
    [
        "공고문 본문의 정량평가표 점수 구간이 제공되지 않음",
        "제안요청서의 배점 기준이 제공되지 않아 정량평가표를 완성할 수 없음",
        "이 첨부에는 일부 페이지가 포함되지 않아 정량평가표를 확인할 수 없음",
    ],
)
def test_partial_quantitative_table_gap_is_not_resolved_by_available_sibling(
    gap: str,
) -> None:
    manifest_sha = "1" * 64
    local_attachment_id = "ATT-PDF-4"
    partial_record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, document_type="NOTICE"),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="2" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [partial_record, table_record],
        expected_documents={
            local_attachment_id: "2" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "INCOMPLETE"
    assert "EXTRACTION_DECLARED_INCOMPLETE" in issue_codes(profile)
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" not in issue_codes(profile)


def test_review_table_record_cannot_resolve_local_table_absence() -> None:
    manifest_sha = "3" * 64
    local_attachment_id = "ATT-PDF-5"
    local_gap = "별도 제안요청서의 평가배점표는 이 첨부에 포함되지 않음"
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(
            local_gap,
            document_type="NOTICE",
        ),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="4" * 64,
        manifest_sha256=manifest_sha,
    )
    review_table = valid_table()
    review_table["criteria"][0]["metric"] = "UNKNOWN"
    review_record = validate_quantitative_attachment_extraction(
        payload_with_table(review_table),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [local_record, review_record],
        expected_documents={
            local_attachment_id: "4" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [local_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert review_record.status == "REVIEW"
    assert profile.status == "INCOMPLETE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" in issue_codes(profile)


def test_unrelated_available_table_cannot_resolve_named_sibling_gap() -> None:
    manifest_sha = "5" * 64
    local_attachment_id = "ATT-PDF-6"
    local_gap = "별도 제안요청서의 평가배점표는 이 첨부에 포함되지 않음"
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(
            local_gap,
            document_type="NOTICE",
        ),
        source_text="입찰공고 일반사항",
        attachment_id=local_attachment_id,
        document_sha256="6" * 64,
        manifest_sha256=manifest_sha,
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [local_record, table_record],
        expected_documents={
            local_attachment_id: "6" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            local_attachment_id: {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [local_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "인력 배치 평가표.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert table_record.status == "AVAILABLE"
    assert profile.status == "INCOMPLETE"
    assert "ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT" in issue_codes(profile)


def test_recomputed_fingerprint_cannot_forge_local_gap_sibling_target() -> None:
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(
            "이 첨부 본문에는 평가배점표가 포함되지 않음",
            document_type="NOTICE",
        ),
        source_text="입찰공고 일반사항",
        attachment_id="ATT-PDF-7",
        document_sha256="7" * 64,
        manifest_sha256="8" * 64,
    )
    issue = local_record.issues[0].model_copy(
        update={
            "required_sibling_document_types": ("RFP",),
            "required_sibling_label_markers": ("제안요청서",),
        }
    )
    forged = local_record.model_copy(update={"issues": (issue,)})
    forged = forged.model_copy(
        update={
            "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(
                forged
            )
        }
    )
    profile = merge_validated_quantitative_records(
        [forged],
        expected_documents={"ATT-PDF-7": "7" * 64},
        manifest_sha256="8" * 64,
    )

    assert profile.status == "INCOMPLETE"
    assert "RECORD_INVARIANT_VIOLATION" in issue_codes(profile)


def test_current_extraction_rejects_forged_gap_statement_and_target() -> None:
    manifest_sha = "8" * 64
    original_gap = "이 첨부 본문에는 평가배점표가 포함되지 않음"
    forged_gap = "별도 제안요청서의 평가배점표는 이 첨부에 포함되지 않음"
    local_record = validate_quantitative_attachment_extraction(
        payload_with_gap(original_gap, document_type="NOTICE"),
        source_text="입찰공고 일반사항",
        attachment_id="ATT-PDF-8",
        document_sha256="7" * 64,
        manifest_sha256=manifest_sha,
    )
    issue = local_record.issues[0].model_copy(
        update={
            "required_sibling_document_types": ("RFP",),
            "required_sibling_label_markers": ("제안요청서",),
            "source_gap_statement": forged_gap,
        }
    )
    forged = local_record.model_copy(update={"issues": (issue,)})
    forged = forged.model_copy(
        update={
            "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(
                forged
            )
        }
    )
    table_record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [forged, table_record],
        expected_documents={
            "ATT-PDF-8": "7" * 64,
            ATTACHMENT_ID: "a" * 64,
        },
        manifest_sha256=manifest_sha,
        attachment_profiles={
            "ATT-PDF-8": {
                "document_type": "NOTICE",
                "source_label": "입찰공고문.pdf",
                "missing_or_unreadable": [original_gap],
            },
            ATTACHMENT_ID: {
                "document_type": "RFP",
                "source_label": "제안요청서.hwp",
                "missing_or_unreadable": [],
            },
        },
    )

    assert profile.status == "INCOMPLETE"
    assert "SOURCE_GAP_BINDING_MISMATCH" in issue_codes(profile)


def test_qualitative_exclusion_with_declared_omission_remains_incomplete() -> None:
    gap = "정성평가 일부가 누락되어 정량 테이블에서 제외"
    record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, table=valid_table()),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )

    assert record.status == "INCOMPLETE"
    assert "EXTRACTION_DECLARED_INCOMPLETE" in {
        issue.code for issue in record.issues
    }


@pytest.mark.parametrize(
    "gap",
    [
        (
            "정성평가 기준과 정량평가표의 경영상태 등급별 점수가 상세 서술되지 않음"
            "(정성평가이므로 정량 테이블에서 제외)"
        ),
        "정성평가 기준과 평가배점표의 경영상태 등급별 점수가 상세 서술되지 않음"
        "(정성평가이므로 정량 테이블에서 제외)",
        "정성평가 항목은 정량 테이블에서 제외하며 경영상태 등급별 점수는 제시되지 않음",
        "경영상태 등급별 점수는 제시되지 않으며 정성평가 항목은 정량 테이블에서 제외",
        "정성평가 항목은 정량 테이블에서 제외하며 용역실적 산식은 제시되지 않음",
    ],
)
def test_mixed_qualitative_and_quantitative_gap_remains_incomplete(gap: str) -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_gap(gap, table=valid_table()),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )

    assert record.status == "INCOMPLETE"
    assert "EXTRACTION_DECLARED_INCOMPLETE" in {
        issue.code for issue in record.issues
    }


def test_merge_rejects_stale_manifest_or_tampered_persisted_record() -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    stale = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="c" * 64,
    )
    assert stale.status == "INCOMPLETE"
    assert "MANIFEST_BINDING_MISMATCH" in issue_codes(stale)

    tampered = record.model_copy(update={"validation_fingerprint_sha256": "f" * 64})
    altered = merge_validated_quantitative_records(
        [tampered],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
    )
    assert altered.status == "INCOMPLETE"
    assert "VALIDATION_FINGERPRINT_MISMATCH" in issue_codes(altered)


def test_recomputed_fingerprint_cannot_bypass_record_shape_invariants() -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    invalid = record.model_copy(update={"status": "NO_TABLE"})
    invalid = invalid.model_copy(
        update={
            "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(
                invalid
            )
        }
    )

    profile = merge_validated_quantitative_records(
        [invalid],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
    )
    assert profile.status == "INCOMPLETE"
    assert "RECORD_INVARIANT_VIOLATION" in issue_codes(profile)


def test_recomputed_fingerprint_cannot_bypass_nested_source_binding() -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    candidate = record.available_candidates[0]
    wrong_anchor = candidate.evidence.model_copy(update={"attachment_id": "ATT-OTHER"})
    wrong_candidate = candidate.model_copy(update={"evidence": wrong_anchor})
    invalid = record.model_copy(update={"available_candidates": (wrong_candidate,)})
    invalid = invalid.model_copy(
        update={
            "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(
                invalid
            )
        }
    )

    profile = merge_validated_quantitative_records(
        [invalid],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
    )
    assert profile.status == "INCOMPLETE"
    assert "RECORD_INVARIANT_VIOLATION" in issue_codes(profile)


def test_not_applicable_record_requires_exact_evidence_even_with_new_fingerprint() -> None:
    statement = "본 사업은 정량평가가 해당 없음"
    payload = ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [],
            "quantitative_table_not_applicable": {
                "reason_literal": "정량평가가 해당 없음",
                "evidence": anchor(statement),
            },
            "missing_or_unreadable": [],
            "summary": "정량평가 비적용",
        }
    )
    record = validate_quantitative_attachment_extraction(
        payload,
        source_text=statement,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    invalid = record.model_copy(update={"not_applicable_evidence": ()})
    invalid = invalid.model_copy(
        update={
            "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(
                invalid
            )
        }
    )

    profile = merge_validated_quantitative_records(
        [invalid],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
    )
    assert profile.status == "INCOMPLETE"
    assert "RECORD_INVARIANT_VIOLATION" in issue_codes(profile)
