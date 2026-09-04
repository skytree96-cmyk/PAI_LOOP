from __future__ import annotations

import hashlib
import json
import re
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
from pai_loop.quantitative_formula import CREDIT_RATING_ORDER
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord,
    _rebind_flat_split_table_cell_literals,
    _unique_flat_cell_line_span,
    build_quantitative_candidate_profile,
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint,
)
from pai_loop.quantitative_scoring import (
    QuantitativeFact,
    _current_dynamic_quantitative_profile,
    estimate_quantitative_score,
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


def test_targeted_fingerprint_revision_invalidates_only_affected_legacy_record() -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    canonical = record.model_dump(
        mode="json",
        exclude={"validation_fingerprint_sha256"},
    )
    legacy_digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert validated_quantitative_record_fingerprint(record) == legacy_digest
    legacy_unaffected = record.model_copy(
        update={"validation_fingerprint_sha256": legacy_digest}
    )
    unaffected_profile = merge_validated_quantitative_records(
        [legacy_unaffected],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
    )
    assert unaffected_profile.status == "AVAILABLE"
    assert "VALIDATION_FINGERPRINT_MISMATCH" not in issue_codes(unaffected_profile)

    table, source = generic_duplicate_metric_fixture()
    affected = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256="c" * 64,
        manifest_sha256="d" * 64,
    )
    assert "SOURCEWIDE_AMBIGUITY_SIGNATURE_UNSUPPORTED" in {
        issue.code for issue in affected.issues
    }
    affected_canonical = affected.model_dump(
        mode="json",
        exclude={"validation_fingerprint_sha256"},
    )
    affected_legacy_digest = hashlib.sha256(
        json.dumps(
            affected_canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert affected.validation_fingerprint_sha256 != affected_legacy_digest
    assert (
        validated_quantitative_record_fingerprint(affected)
        == affected.validation_fingerprint_sha256
    )
    legacy_affected = affected.model_copy(
        update={"validation_fingerprint_sha256": affected_legacy_digest}
    )
    legacy_profile = merge_validated_quantitative_records(
        [legacy_affected],
        expected_documents={ATTACHMENT_ID: "c" * 64},
        manifest_sha256="d" * 64,
    )
    assert legacy_profile.status == "INCOMPLETE"
    assert "VALIDATION_FINGERPRINT_MISMATCH" in issue_codes(legacy_profile)

    refreshed_profile = merge_validated_quantitative_records(
        [affected],
        expected_documents={ATTACHMENT_ID: "c" * 64},
        manifest_sha256="d" * 64,
    )
    assert "VALIDATION_FINGERPRINT_MISMATCH" not in issue_codes(refreshed_profile)


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


def split_cell_case_table(
    *,
    metric: str = "PERFORMANCE_COUNT",
    unit: str = "건",
    max_points: float = 6,
    cases: list[dict],
) -> dict:
    """One CASE_TABLE criterion whose model anchors name condition cells only."""

    table = valid_table()
    criterion = table["criteria"][0]
    criterion.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-1",
            "label": "수행실적",
            "criterion_literal": f"수행실적 {max_points:g}점",
            "max_points": max_points,
            "scoring_method": "CASE_TABLE",
            "metric": metric,
            "unit": unit,
            "brackets": [],
            "threshold": None,
            "formula_literal": None,
            "cases": cases,
            "recognition_conditions": [],
            "required_evidence": [
                (
                    "company.credit_rating"
                    if metric == "CREDIT_RATING"
                    else (
                        "company.performance.amount"
                        if metric == "PERFORMANCE_AMOUNT"
                        else "company.performance.count"
                    )
                )
            ],
            "evidence": anchor(f"수행실적 {max_points:g}점"),
            "ambiguity_reason": None,
        }
    )
    table["total_points"] = max_points
    table["total_evidence"] = anchor(f"정량평가 총점 {max_points:g}점")
    table["minimum_score"] = None
    table["minimum_evidence"] = None
    return table


def split_case(
    literal: str,
    *,
    operator: str,
    comparison_value: float | None,
    category_values: list[str],
    award_kind: str,
    award_value: float,
    row_order: int,
) -> dict:
    return {
        "literal": literal,
        "operator": operator,
        "comparison_value": comparison_value,
        "category_values": category_values,
        "award_kind": award_kind,
        "award_value": award_value,
        "row_order": row_order,
        # HWP extraction supplies this exact condition cell, while the score
        # cell is a separate next source line.
        "evidence": anchor(literal),
    }


def test_split_hwp_gte_cells_bind_the_immediately_following_award_cell() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상",
            "6점",
            "2건 이상",
            "4점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            ),
            split_case(
                "2건 이상",
                operator="GTE",
                comparison_value=2,
                category_values=[],
                award_kind="POINTS",
                award_value=4,
                row_order=2,
            ),
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert [item.literal for item in profile.available_candidates[0].cases] == [
        "3건 이상\n6점",
        "2건 이상\n4점",
    ]


def test_split_hwp_eq_cells_bind_the_immediately_following_award_cell() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건",
            "6점",
            "2건",
            "4점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건",
                operator="EQ",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            ),
            split_case(
                "2건",
                operator="EQ",
                comparison_value=2,
                category_values=[],
                award_kind="POINTS",
                award_value=4,
                row_order=2,
            ),
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert [item.literal for item in profile.available_candidates[0].cases] == [
        "3건\n6점",
        "2건\n4점",
    ]


def test_split_hwp_categorical_percent_cells_bind_the_immediately_following_award_cell() -> None:
    categories = ["D 이상"]
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 10점",
            "D 이상",
            "100%",
            "정량평가 총점 10점",
        ]
    )
    table = split_cell_case_table(
        metric="CREDIT_RATING",
        unit="등급",
        max_points=10,
        cases=[
            split_case(
                "D 이상",
                operator="IN",
                comparison_value=None,
                category_values=categories,
                award_kind="PERCENT_OF_MAX",
                award_value=100,
                row_order=1,
            )
        ],
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert profile.available_candidates[0].cases[0].literal.endswith("\n100%")


def test_split_hwp_case_cannot_borrow_the_next_rows_award() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상",
            "6점",
            "2건 이상",
            "4점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=4,
                row_order=1,
            ),
            split_case(
                "2건 이상",
                operator="GTE",
                comparison_value=2,
                category_values=[],
                award_kind="POINTS",
                award_value=4,
                row_order=2,
            ),
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_repeated_condition_anchor_is_not_rebound() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상",
            "6점",
            "3건 이상",
            "6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_column_major_cells_are_not_rebound_as_rows() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상",
            "2건 이상",
            "6점",
            "4점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            ),
            split_case(
                "2건 이상",
                operator="GTE",
                comparison_value=2,
                category_values=[],
                award_kind="POINTS",
                award_value=4,
                row_order=2,
            ),
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_score_cell_anchor_recovers_the_unique_previous_condition() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상",
            "배점 6점",
            "정량평가 총점 6점",
        ]
    )
    row = split_case(
        "배점 6점",
        operator="GTE",
        comparison_value=3,
        category_values=[],
        award_kind="POINTS",
        award_value=6,
        row_order=1,
    )
    table = split_cell_case_table(cases=[row])

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert profile.available_candidates[0].cases[0].literal == "3건 이상\n배점 6점"


def test_split_hwp_amount_condition_converts_source_eok_to_candidate_won() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "2억 원 이상",
            "6점",
            "정량평가 총점 6점",
        ]
    )
    row = split_case(
        "2억 원 이상",
        operator="GTE",
        comparison_value=200_000_000,
        category_values=[],
        award_kind="POINTS",
        award_value=6,
        row_order=1,
    )
    table = split_cell_case_table(
        metric="PERFORMANCE_AMOUNT",
        unit="원",
        cases=[row],
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert profile.available_candidates[0].cases[0].literal == "2억 원 이상\n6점"


def test_flat_hwpx_split_cells_are_rebound_from_exact_adjacent_score() -> None:
    source = "\n".join(
        ["수행실적 6점", "3건 이상", "6점", "정량평가 총점 6점"]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert profile.available_candidates[0].cases[0].literal == "3건 이상\n6점"


def test_flat_hwpx_whole_cell_match_does_not_confuse_bb_minus_with_bbb_minus() -> None:
    lines = ("BBB- 미만", "BB- 미만")

    assert _unique_flat_cell_line_span(lines, "BBB- 미만") == (0, 1)
    assert _unique_flat_cell_line_span(lines, "BB- 미만") == (1, 2)


def test_flat_hwpx_credit_repair_keeps_bb_minus_and_bbb_minus_rows_separate() -> None:
    source = "\n".join(
        [
            "수행실적 10점",
            "BBB- 미만",
            "배점의 100%",
            "BB- 미만",
            "배점의 70%",
            "정량평가 총점 10점",
        ]
    )
    table = split_cell_case_table(
        metric="CREDIT_RATING",
        unit="등급",
        max_points=10,
        cases=[
            split_case(
                "BBB- 미만",
                operator="IN",
                comparison_value=None,
                category_values=["BBB- 미만"],
                award_kind="PERCENT_OF_MAX",
                award_value=100,
                row_order=1,
            ),
            split_case(
                "BB- 미만",
                operator="IN",
                comparison_value=None,
                category_values=["BB- 미만"],
                award_kind="PERCENT_OF_MAX",
                award_value=70,
                row_order=2,
            ),
        ],
    )

    repaired = _rebind_flat_split_table_cell_literals(
        payload_with_table(table),
        source=source,
        lines=tuple(source.splitlines()),
    )

    assert [case.literal for case in repaired.quantitative_tables[0].criteria[0].cases] == [
        "BBB- 미만\n배점의 100%",
        "BB- 미만\n배점의 70%",
    ]


def test_source_bound_split_performance_and_credit_ranges_score_a0_at_full_points() -> None:
    amount_literal = (
        "최근 3년간 지자체, 공공기관 등 (교육, 취업, 행사) 용역 "
        "수행완료 실적(금액, 10점) 단일용역 최고금액(1건)"
    )
    anchor_condition = "① ‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
    certificate_condition = "② 증빙서류로 용역수행실적 총괄표, 용역실적증명서를 첨부한다."
    consortium_condition = (
        "③ 공동계약으로 참여한 실적의 경우 공동계약 참여 비율에 따른 금액의 실적"
    )
    amount_rows = [
        ("10억원 이상", 10, None, True, False, 10),
        ("5억원 이상 10억원 미만", 5, 10, True, False, 8),
        ("1억원 이상 5억원 미만", 1, 5, True, False, 6),
        ("1억원 미만", None, 1, False, False, 4),
    ]
    credit_rows = [
        ("A- 이상", 10),
        ("BBB- 이상 A- 미만", 8),
        ("BB- 이상 BBB- 미만", 6),
        ("BB- 미만", 4),
    ]
    total_literal = "정량평가 총점 20점"
    source = "\n".join(
        [
            amount_literal,
            *(value for row in amount_rows for value in (row[0], f"{row[5]}점")),
            "신용평가등급 10점",
            *(value for row in credit_rows for value in (row[0], f"{row[1]}점")),
            total_literal,
            anchor_condition,
            certificate_condition,
            consortium_condition,
        ]
    )
    table = {
        "table_id": "REPRESENTATIVE-SPLIT-RANGE-TABLE",
        "label": "정량평가표",
        "criteria": [
            {
                "criterion_id": "PERFORMANCE-BRACKET-10",
                "label": "수행실적",
                "criterion_literal": amount_literal,
                "max_points": 10,
                "scoring_method": "BRACKET",
                "metric": "PERFORMANCE_AMOUNT",
                "unit": "억원",
                "brackets": [
                    {
                        "label": literal,
                        "literal": literal,
                        "min_value": minimum,
                        "max_value": maximum,
                        "min_inclusive": min_inclusive,
                        "max_inclusive": max_inclusive,
                        "points": points,
                        "evidence": anchor(literal),
                    }
                    for (
                        literal,
                        minimum,
                        maximum,
                        min_inclusive,
                        max_inclusive,
                        points,
                    ) in amount_rows
                ],
                "threshold": None,
                "formula_literal": None,
                "cases": [],
                "recognition_conditions": [
                    {"literal": literal, "evidence": anchor(literal)}
                    for literal in (
                        anchor_condition,
                        certificate_condition,
                        consortium_condition,
                    )
                ],
                "required_evidence": ["company.performance.amount"],
                "evidence": anchor(amount_literal),
                "ambiguity_reason": None,
            },
            {
                "criterion_id": "CREDIT-RANGE-10",
                "label": "신용평가등급",
                "criterion_literal": "신용평가등급 10점",
                "max_points": 10,
                "scoring_method": "CASE_TABLE",
                "metric": "CREDIT_RATING",
                "unit": "등급",
                "brackets": [],
                "threshold": None,
                "formula_literal": None,
                "cases": [
                    split_case(
                        literal,
                        operator="IN",
                        comparison_value=None,
                        category_values=[literal],
                        award_kind="POINTS",
                        award_value=points,
                        row_order=row_order,
                    )
                    for row_order, (literal, points) in enumerate(
                        credit_rows,
                        start=1,
                    )
                ],
                "recognition_conditions": [],
                "required_evidence": ["company.credit_rating"],
                "evidence": anchor("신용평가등급 10점"),
                "ambiguity_reason": None,
            },
        ],
        "total_points": 20,
        "total_evidence": anchor(total_literal),
        "minimum_score": None,
        "minimum_evidence": None,
        "ambiguity_reason": None,
    }
    manifest_sha = "a" * 64
    document_sha = "b" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )

    assert record.status == "AVAILABLE", record.issues
    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert len(profile.available_candidates) == 2
    candidates = {item.metric: item for item in profile.available_candidates}
    assert set(candidates) == {"PERFORMANCE_AMOUNT", "CREDIT_RATING"}
    assert [case.category_values for case in candidates["CREDIT_RATING"].cases] == [
        ("A- 이상",),
        ("BBB- 이상 A- 미만",),
        ("BB- 이상 BBB- 미만",),
        ("BB- 미만",),
    ]

    request = quantitative_request_from_candidate_profile(profile)
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    criteria = {item.metric_key: item for item in request.criteria}
    facts = [
        QuantitativeFact(
            metric_key="company.performance.amount",
            status="ESTIMATED",
            value=1_000_000_000,
            lower_value=1_000_000_000,
            upper_value=1_000_000_000,
            evidence_key="company.performance.amount",
            fact_binding_sha256=criteria[
                "company.performance.amount"
            ].fact_binding_sha256,
            confidence=0.9,
            rationale="검증된 단일 최고 수행실적",
        ),
        QuantitativeFact(
            metric_key="company.credit_rating",
            status="CONFIRMED",
            value="A0",
            evidence_key="company.credit_rating",
            fact_binding_sha256=criteria["company.credit_rating"].fact_binding_sha256,
            confidence=1,
            rationale="유효 신용평가등급",
        ),
    ]

    result = estimate_quantitative_score(request.model_copy(update={"facts": facts}))

    assert result.total_max_points == 20
    assert (result.lower_points, result.upper_points, result.estimated_points) == (
        20,
        20,
        20,
    )
    assert {item.category: item.estimated_points for item in result.criteria} == {
        "PERFORMANCE_AMOUNT": 10,
        "CREDIT_RATING": 10,
    }


def test_flat_hwpx_split_cells_do_not_cross_an_unrelated_line() -> None:
    source = "\n".join(
        ["수행실적 6점", "3건 이상", "별도 설명", "6점", "정량평가 총점 6점"]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_case_cannot_borrow_the_previous_rows_award() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "2건 이상",
            "4점",
            "3건 이상",
            "6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=4,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_case_rejects_gte_when_source_condition_is_lte() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이하",
            "6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이하",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_COMPARATOR_MISMATCH" in issue_codes(profile)


def test_split_hwp_criterion_label_and_score_cells_are_bound_when_unique() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적",
            "6점",
            "3건 이상 6점",
            "정량평가 총점 6점",
        ]
    )
    row = split_case(
        "3건 이상 6점",
        operator="GTE",
        comparison_value=3,
        category_values=[],
        award_kind="POINTS",
        award_value=6,
        row_order=1,
    )
    table = split_cell_case_table(cases=[row])
    table["criteria"][0]["criterion_literal"] = "수행실적"
    table["criteria"][0]["evidence"] = anchor("수행실적")

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    candidate = profile.available_candidates[0]
    assert candidate.criterion_literal == "수행실적\n6점"
    assert candidate.evidence.quote == "수행실적\n6점"


def test_split_hwp_criterion_uses_exact_evidence_when_model_literal_is_rewritten() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적",
            "6점",
            "3건 이상 6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    table["criteria"][0]["criterion_literal"] = "모델이 재작성한 수행실적 기준"
    table["criteria"][0]["evidence"] = anchor("수행실적")

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    candidate = profile.available_candidates[0]
    assert candidate.criterion_literal == "수행실적\n6점"
    assert candidate.evidence.quote == "수행실적\n6점"


def test_split_hwp_numeric_only_criterion_evidence_is_not_rebound() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적",
            "6점",
            "3건 이상 6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    table["criteria"][0]["criterion_literal"] = "모델이 재작성한 수행실적 기준"
    table["criteria"][0]["evidence"] = anchor("6점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()


def test_split_hwp_recognition_extends_exact_window_but_rejects_paraphrase() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상 6점",
            "최근 3년 이내",
            "완료 실적만 인정",
            "정량평가 총점 6점",
        ]
    )
    row = split_case(
        "3건 이상 6점",
        operator="GTE",
        comparison_value=3,
        category_values=[],
        award_kind="POINTS",
        award_value=6,
        row_order=1,
    )
    exact_table = split_cell_case_table(cases=[row])
    exact_table["criteria"][0]["recognition_conditions"] = [
        {
            "literal": "최근 3년 이내 완료 실적만 인정",
            "evidence": anchor("최근 3년 이내"),
        }
    ]
    paraphrase_table = split_cell_case_table(cases=[row])
    paraphrase_table["criteria"][0]["recognition_conditions"] = [
        {
            "literal": "최근 3년간 완료 실적만 인정",
            "evidence": anchor("최근 3년 이내"),
        }
    ]

    exact = build(payload_with_table(exact_table), source=source)
    paraphrase = build(payload_with_table(paraphrase_table), source=source)

    assert exact.status == "AVAILABLE", issue_codes(exact)
    condition = exact.available_candidates[0].recognition_conditions[0]
    assert condition.literal == "최근 3년 이내 완료 실적만 인정"
    assert condition.evidence.quote == "최근 3년 이내\n완료 실적만 인정"
    assert paraphrase.status == "INCOMPLETE"
    assert "RECOGNITION_CONDITION_LITERAL_MISMATCH" in issue_codes(paraphrase)


def test_split_hwp_rejects_exact_line_anchor_with_a_second_longer_substring_match() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상",
            "6점",
            "참고: 3건 이상인 경우 별도 확인",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_two_criteria_cannot_claim_the_same_case_source_row() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 6점",
            "기준 B 6점",
            "3건 이상",
            "6점",
            "정량평가 총점 12점",
        ]
    )
    shared_case = split_case(
        "3건 이상",
        operator="GTE",
        comparison_value=3,
        category_values=[],
        award_kind="POINTS",
        award_value=6,
        row_order=1,
    )
    table = split_cell_case_table(cases=[shared_case])
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-2",
            "label": "기준 B",
            "criterion_literal": "기준 B 6점",
            "evidence": anchor("기준 B 6점"),
        }
    )
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-1",
            "label": "기준 A",
            "criterion_literal": "기준 A 6점",
            "evidence": anchor("기준 A 6점"),
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 12
    table["total_evidence"] = anchor("정량평가 총점 12점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert profile.available_candidates == ()
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_split_hwp_criterion_cannot_borrow_the_previous_criterions_max_cell() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A",
            "6점",
            "기준 B",
            "3건 이상 4점",
            "정량평가 총점 10점",
        ]
    )
    row = split_case(
        "3건 이상 4점",
        operator="GTE",
        comparison_value=3,
        category_values=[],
        award_kind="POINTS",
        award_value=4,
        row_order=1,
    )
    table = split_cell_case_table(max_points=4, cases=[row])
    table["criteria"][0].update(
        {"label": "기준 B", "criterion_literal": "기준 B", "evidence": anchor("기준 B")}
    )
    table["total_points"] = 4
    table["total_evidence"] = anchor("정량평가 총점 10점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "MAX_POINTS_LITERAL_MISMATCH" in issue_codes(profile)


def test_split_hwp_does_not_bind_condition_and_score_across_a_blank_line() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            "3건 이상",
            "",
            "6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_amount_ascii_gte_keeps_exact_won_comparison() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            ">= 200000000 원",
            "6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        metric="PERFORMANCE_AMOUNT",
        unit="원",
        cases=[
            split_case(
                ">= 200000000 원",
                operator="GTE",
                comparison_value=200_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ],
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)


@pytest.mark.parametrize(
    ("source_condition", "comparison_value", "expected_status"),
    [
        ("2만원 이상", 2, "INCOMPLETE"),
        ("200백만원 이상", 2, "AVAILABLE"),
    ],
)
def test_split_hwp_amount_units_never_confuse_manwon_with_eokwon(
    source_condition: str,
    comparison_value: float,
    expected_status: str,
) -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            source_condition,
            "6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        metric="PERFORMANCE_AMOUNT",
        unit="억원",
        cases=[
            split_case(
                source_condition,
                operator="GTE",
                comparison_value=comparison_value,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ],
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == expected_status, issue_codes(profile)
    if expected_status == "INCOMPLETE":
        assert profile.available_candidates == ()
        assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_ambiguous_foreign_structure_anchor_blocks_case_rebind() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 6점",
            "3건 이상",
            "기준 B",
            "참고",
            "6점",
            "기준 B 4점",
            "2건 이상 4점",
            "정량평가 총점 10점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A 6점",
            "evidence": anchor("기준 A 6점"),
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            # This shorter structural literal occurs both inside A's tempting
            # repair window and inside B's unique criterion anchor.
            "criterion_literal": "기준 B",
            "max_points": 4,
            "evidence": anchor("기준 B 4점"),
            "cases": [
                split_case(
                    "2건 이상 4점",
                    operator="GTE",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                )
            ],
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 10
    table["total_evidence"] = anchor("정량평가 총점 10점")

    profile = build(payload_with_table(table), source=source)

    first_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "SPLIT-CELL-CASE-A"
    )
    assert first_review.status == "INCOMPLETE"
    assert "CASE_NUMBER_MISMATCH" in first_review.issue_codes


def test_split_hwp_foreign_case_row_cannot_be_reused_as_criterion_header() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "건수 기준 6점",
            "실적 금액 3건 이상 6점",
            "2억 원 이상",
            "6점",
            "정량평가 총점 12점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "실적 금액 3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    count = table["criteria"][0]
    count.update(
        {
            "criterion_id": "FOREIGN-CASE-COUNT",
            "label": "건수 기준",
            "criterion_literal": "건수 기준 6점",
            "evidence": anchor("건수 기준 6점"),
        }
    )
    amount = json.loads(json.dumps(count))
    amount.update(
        {
            "criterion_id": "FOREIGN-CASE-AMOUNT",
            "label": "실적 금액",
            "criterion_literal": "모델이 재작성한 실적 금액 기준",
            "metric": "PERFORMANCE_AMOUNT",
            "unit": "원",
            "required_evidence": ["company.performance.amount"],
            "evidence": anchor("실적 금액"),
            "cases": [
                split_case(
                    "2억 원 이상",
                    operator="GTE",
                    comparison_value=200_000_000,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=6,
                    row_order=1,
                )
            ],
        }
    )
    table["criteria"] = [count, amount]
    table["total_points"] = 12
    table["total_evidence"] = anchor("정량평가 총점 12점")

    profile = build(payload_with_table(table), source=source)

    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "FOREIGN-CASE-AMOUNT"
    )
    assert amount_review.status == "INCOMPLETE"
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


@pytest.mark.parametrize(
    "source_cell",
    ["3건 이상 / 3건 이상", "3건 이상 / 3 건 이 상"],
)
def test_split_hwp_same_line_duplicate_case_target_is_not_rebound(
    source_cell: str,
) -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "수행실적 6점",
            source_cell,
            "6점",
            "정량평가 총점 6점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_split_hwp_shared_recognition_window_is_reused_for_identical_key() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 6점",
            "3건 이상 6점",
            "기준 B 4점",
            "2건 이상 4점",
            "최근 3년 이내",
            "완료 실적만 인정",
            "정량평가 총점 10점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A 6점",
            "evidence": anchor("기준 A 6점"),
            "recognition_conditions": [
                {
                    "literal": "최근 3년 이내 완료 실적만 인정",
                    "evidence": anchor("최근 3년 이내"),
                }
            ],
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            "criterion_literal": "기준 B 4점",
            "max_points": 4,
            "evidence": anchor("기준 B 4점"),
            "cases": [
                split_case(
                    "2건 이상 4점",
                    operator="GTE",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                )
            ],
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 10
    table["total_evidence"] = anchor("정량평가 총점 10점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert len(profile.available_candidates) == 2
    assert {
        item.recognition_conditions[0].evidence.quote
        for item in profile.available_candidates
    } == {"최근 3년 이내\n완료 실적만 인정"}


def test_split_hwp_partially_overlapping_case_claims_make_table_ambiguous() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 10점",
            "AAA",
            "AA+",
            "100%",
            "기준 B 10점",
            "정량평가 총점 20점",
        ]
    )
    table = split_cell_case_table(
        metric="CREDIT_RATING",
        unit="등급",
        max_points=10,
        cases=[
            split_case(
                "AAA\nAA+\n100%",
                operator="IN",
                comparison_value=None,
                category_values=["AAA", "AA+"],
                award_kind="PERCENT_OF_MAX",
                award_value=100,
                row_order=1,
            )
        ],
    )
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A 10점",
            "evidence": anchor("기준 A 10점"),
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            "criterion_literal": "기준 B 10점",
            "evidence": anchor("기준 B 10점"),
            "cases": [
                split_case(
                    "AA+\n100%",
                    operator="IN",
                    comparison_value=None,
                    category_values=["AA+"],
                    award_kind="PERCENT_OF_MAX",
                    award_value=100,
                    row_order=1,
                )
            ],
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 20
    table["total_evidence"] = anchor("정량평가 총점 20점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)


def test_split_hwp_criterion_rebind_cannot_escape_into_next_criterion() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A",
            "기준 B 6점",
            "2건 이상 6점",
            "정량평가 총점 12점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "2건 이상 6점",
                operator="GTE",
                comparison_value=2,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A",
            "evidence": anchor("기준 A"),
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            "criterion_literal": "기준 B 6점",
            "evidence": anchor("기준 B 6점"),
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 12
    table["total_evidence"] = anchor("정량평가 총점 12점")

    profile = build(payload_with_table(table), source=source)

    assert "MAX_POINTS_LITERAL_MISMATCH" in issue_codes(profile)


def test_busan_hwp_split_cells_recover_all_three_quantitative_criteria() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "1) 용역수행 실적(금액, 6점)",
            "A. 2억 원 이상",
            "6",
            "B. 1.5억 원 이상",
            "5.5",
            "C. 1억 원 이상",
            "5",
            "2) 용역수행 실적(건수, 4점)",
            "A. 5건 이상",
            "4",
            "B. 4건",
            "3.7",
            "C. 3건",
            "3.4",
            "D. 2건",
            "3.1",
            "E. 1건",
            "2.8",
            "※ 실적인정 기준",
            "①‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다.",
            "③ 공동계약으로 참여한 실적의 경우",
            "공동계약 참여 비율에 따른 금액의 실적",
            "❍ 제안업체 경영상태 (10점)",
            "AAA, AA+, AA0, AA-",
            "A+, A0, A-, BBB+, BBB0",
            "배점의 100%",
            "BBB-, BB+, BB0, BB-",
            "배점의 95%",
            "B+, B0, B-",
            "배점의 90%",
            "CCC+ 이하",
            "배점의 70%",
            "총점 20점",
        ]
    )
    amount_table = split_cell_case_table(
        metric="PERFORMANCE_AMOUNT",
        unit="원",
        cases=[
            split_case(
                "A. 2억 원 이상",
                operator="GTE",
                comparison_value=200_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            ),
            split_case(
                "B. 1.5억 원 이상",
                operator="GTE",
                comparison_value=150_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=5.5,
                row_order=2,
            ),
            split_case(
                "C. 1억 원 이상",
                operator="GTE",
                comparison_value=100_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=5,
                row_order=3,
            ),
        ],
    )
    amount = amount_table["criteria"][0]
    amount.update(
        {
            "criterion_id": "BUSAN-AMOUNT",
            "label": "용역수행 실적(금액)",
            "criterion_literal": "1) 용역수행 실적(금액, 6점)",
            "evidence": anchor("1) 용역수행 실적(금액, 6점)"),
            "recognition_conditions": [
                {
                    "literal": (
                        "‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
                    ),
                    "evidence": anchor(
                        "‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
                    ),
                },
                {
                    "literal": (
                        "공동계약으로 참여한 실적의 경우 "
                        "공동계약 참여 비율에 따른 금액의 실적"
                    ),
                    "evidence": anchor("공동계약으로 참여한 실적의 경우"),
                },
            ],
        }
    )

    count = json.loads(json.dumps(amount))
    count.update(
        {
            "criterion_id": "BUSAN-COUNT",
            "label": "용역수행 실적(건수)",
            "criterion_literal": "2) 용역수행 실적(건수, 4점)",
            "max_points": 4,
            "metric": "PERFORMANCE_COUNT",
            "unit": "건",
            "required_evidence": ["company.performance.count"],
            "evidence": anchor("2) 용역수행 실적(건수, 4점)"),
            "recognition_conditions": [
                {
                    "literal": (
                        "‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
                    ),
                    "evidence": anchor(
                        "‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
                    ),
                }
            ],
            "cases": [
                split_case(
                    "A. 5건 이상",
                    operator="GTE",
                    comparison_value=5,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                ),
                *[
                    split_case(
                        literal,
                        operator="EQ",
                        comparison_value=value,
                        category_values=[],
                        award_kind="POINTS",
                        award_value=points,
                        row_order=row_order,
                    )
                    for row_order, (literal, value, points) in enumerate(
                        [
                            ("B. 4건", 4, 3.7),
                            ("C. 3건", 3, 3.4),
                            ("D. 2건", 2, 3.1),
                            ("E. 1건", 1, 2.8),
                        ],
                        start=2,
                    )
                ],
            ],
        }
    )

    credit = json.loads(json.dumps(amount))
    credit.update(
        {
            "criterion_id": "BUSAN-CREDIT",
            "label": "제안업체 경영상태",
            "criterion_literal": "❍ 제안업체 경영상태 (10점)",
            "max_points": 10,
            "metric": "CREDIT_RATING",
            "unit": "등급",
            "required_evidence": ["company.credit_rating"],
            "evidence": anchor("❍ 제안업체 경영상태 (10점)"),
            "recognition_conditions": [],
            "cases": [
                split_case(
                    "AAA, AA+, AA0, AA-\nA+, A0, A-, BBB+, BBB0",
                    operator="IN",
                    comparison_value=None,
                    category_values=[
                        "AAA",
                        "AA+",
                        "AA0",
                        "AA-",
                        "A+",
                        "A0",
                        "A-",
                        "BBB+",
                        "BBB0",
                    ],
                    award_kind="PERCENT_OF_MAX",
                    award_value=100,
                    row_order=1,
                ),
                split_case(
                    "BBB-, BB+, BB0, BB-",
                    operator="IN",
                    comparison_value=None,
                    category_values=["BBB-", "BB+", "BB0", "BB-"],
                    award_kind="PERCENT_OF_MAX",
                    award_value=95,
                    row_order=2,
                ),
                split_case(
                    "B+, B0, B-",
                    operator="IN",
                    comparison_value=None,
                    category_values=["B+", "B0", "B-"],
                    award_kind="PERCENT_OF_MAX",
                    award_value=90,
                    row_order=3,
                ),
                split_case(
                    "CCC+ 이하",
                    operator="IN",
                    comparison_value=None,
                    category_values=["CCC+ 이하"],
                    award_kind="PERCENT_OF_MAX",
                    award_value=70,
                    row_order=4,
                ),
            ],
        }
    )

    amount_table["criteria"] = [amount, count, credit]
    amount_table["total_points"] = 20
    amount_table["total_evidence"] = anchor("총점 20점")

    profile = build(payload_with_table(amount_table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert [item.max_points for item in profile.available_candidates] == [6, 4, 10]
    assert [
        len(item.cases) for item in profile.available_candidates
    ] == [3, 5, 4]


def busan_hwp_multicolumn_credit_fixture(
    *,
    duplicate_100_percent_cell: bool = False,
    omit_100_percent_category_cell: bool = False,
) -> tuple[dict, str]:
    """Production-shaped 부산 HWP credit table with column-major cell text."""

    first_row_cells = [
        "AAA, AA+, AA0, AA-",
        "A+, A0, A-, BBB+, BBB0",
        "A1, A2+, A20,",
        "A2-, A3+, A30",
        "AAA, AA+, AA0, AA-,",
        "A+, A0, A-, BBB+, BBB0",
        "배점의 100%",
    ]
    if omit_100_percent_category_cell:
        first_row_cells.remove("AAA, AA+, AA0, AA-,")
    if duplicate_100_percent_cell:
        first_row_cells.append("배점의 100%")

    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "❍ 제안업체 경영상태 (10점)",
            "- 조달청 협상에 의한 계약 제안서 평가 세부기준 <별표 8>에 따름",
            "신용평가등급",
            "평점",
            "회사채",
            "기업어음",
            "기업신용평가등급",
            *first_row_cells,
            "BBB-, BB+, BB0, BB-",
            "A3-, B+, B0",
            "BBB-, BB+, BB0, BB-",
            "배점의 95%",
            "B+, B0, B-",
            "B-",
            "B+, B0, B-",
            "배점의 90%",
            "CCC+ 이하",
            "C 이하",
            "CCC+ 이하",
            "배점의 70%",
            "* 등급별 평점이 소수점 이하의 숫자가 있는 경우 소수점 다섯째자리에서 반올림 함",
        ]
    )
    table = split_cell_case_table(
        metric="CREDIT_RATING",
        unit="등급",
        max_points=10,
        cases=[
            split_case(
                "AAA, AA+, AA0, AA-\nA+, A0, A-, BBB+, BBB0",
                operator="IN",
                comparison_value=None,
                category_values=[
                    "AAA",
                    "AA+",
                    "AA0",
                    "AA-",
                    "A+",
                    "A0",
                    "A-",
                    "BBB+",
                    "BBB0",
                ],
                award_kind="PERCENT_OF_MAX",
                award_value=100,
                row_order=1,
            ),
            split_case(
                "BBB-, BB+, BB0, BB-",
                operator="IN",
                comparison_value=None,
                category_values=["BBB-", "BB+", "BB0", "BB-"],
                award_kind="PERCENT_OF_MAX",
                award_value=95,
                row_order=2,
            ),
            split_case(
                "B+, B0, B-",
                operator="IN",
                comparison_value=None,
                category_values=["B+", "B0", "B-"],
                award_kind="PERCENT_OF_MAX",
                award_value=90,
                row_order=3,
            ),
            split_case(
                "CCC+ 이하",
                operator="IN",
                comparison_value=None,
                category_values=["CCC+ 이하"],
                award_kind="PERCENT_OF_MAX",
                award_value=70,
                row_order=4,
            ),
        ],
    )
    # CREDIT_RATING intentionally uses only the enterprise-credit-rating
    # column. Its selected cell still repeats in the company-bond column, so
    # category text alone cannot select the owning row.
    table["criteria"][0]["cases"][0]["evidence"] = anchor(
        "A+, A0, A-, BBB+, BBB0"
    )
    table["criteria"][0].update(
        {
            "criterion_id": "BUSAN-MULTICOLUMN-CREDIT",
            "label": "제안업체 경영상태",
            "criterion_literal": "❍ 제안업체 경영상태 (10점)",
            "evidence": anchor("❍ 제안업체 경영상태 (10점)"),
        }
    )
    table["total_evidence"] = anchor("❍ 제안업체 경영상태 (10점)")
    return table, source


def test_busan_hwp_multicolumn_credit_rows_use_unique_percent_cells() -> None:
    table, source = busan_hwp_multicolumn_credit_fixture()

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    cases = profile.available_candidates[0].cases
    assert cases[0].literal.splitlines() == [
        "AAA, AA+, AA0, AA-,",
        "A+, A0, A-, BBB+, BBB0",
        "배점의 100%",
    ]
    assert [case.literal.splitlines()[-1] for case in cases] == [
        "배점의 100%",
        "배점의 95%",
        "배점의 90%",
        "배점의 70%",
    ]


def test_busan_hwp_duplicate_percent_cell_is_not_used_as_a_repair_anchor() -> None:
    table, source = busan_hwp_multicolumn_credit_fixture(
        duplicate_100_percent_cell=True
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_busan_hwp_incomplete_multicolumn_row_is_not_repaired() -> None:
    table, source = busan_hwp_multicolumn_credit_fixture(
        omit_100_percent_category_cell=True
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


@pytest.mark.parametrize(
    "header_mutation",
    ("missing", "duplicate", "out-of-region"),
)
def test_busan_credit_unit_repair_requires_one_owned_header_cluster(
    header_mutation: str,
) -> None:
    table, source = busan_hwp_multicolumn_credit_fixture()
    table["criteria"][0]["unit"] = "점"
    cluster = "\n".join(
        (
            "신용평가등급",
            "평점",
            "회사채",
            "기업어음",
            "기업신용평가등급",
        )
    )
    if header_mutation == "missing":
        source = source.replace("기업어음", "전자어음", 1)
    elif header_mutation == "duplicate":
        source = source.replace(cluster, f"{cluster}\n{cluster}", 1)
    else:
        source = source.replace(f"{cluster}\n", "", 1)
        source = source.replace(
            "[HWP SECTION 0]\n",
            f"[HWP SECTION 0]\n{cluster}\n",
            1,
        )

    manifest_sha = "a" * 64
    document_sha = "b" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )
    request = quantitative_request_from_candidate_profile(profile)

    assert record.status == "AVAILABLE", record.issues
    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert profile.available_candidates[0].unit == "점"
    assert request.activation_status == "REVIEW_REQUIRED"
    assert request.activation_reasons == [
        "UNSUPPORTED_SCORING_DSL",
        "UNSUPPORTED_UNIT",
    ]


def test_busan_credit_unit_repair_rejects_commercial_paper_case_cell() -> None:
    table, source = busan_hwp_multicolumn_credit_fixture()
    credit = table["criteria"][0]
    credit["unit"] = "점"
    borrowed = "C 이하\nCCC+ 이하\n배점의 70%"
    credit["cases"][-1].update(
        {
            "literal": borrowed,
            "evidence": anchor(borrowed),
            "category_values": ["C 이하"],
        }
    )

    manifest_sha = "a" * 64
    document_sha = "b" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )
    request = quantitative_request_from_candidate_profile(profile)

    assert record.status == "REVIEW"
    assert profile.available_candidates == ()
    assert "CASE_TABLE_NOT_DETERMINISTIC" in issue_codes(profile)
    assert request.activation_status == "REVIEW_REQUIRED"


@pytest.mark.parametrize("payload_mutation", ("missing-row", "partial-cell"))
def test_busan_credit_unit_repair_requires_complete_owned_source_rows(
    payload_mutation: str,
) -> None:
    table, source = busan_hwp_multicolumn_credit_fixture()
    credit = table["criteria"][0]
    credit["unit"] = "점"
    if payload_mutation == "missing-row":
        credit["cases"] = credit["cases"][:-1]
    else:
        partial = "A+, A0, A-, BBB+, BBB0"
        credit["cases"][0].update(
            {
                "literal": partial,
                "evidence": anchor(partial),
                "category_values": ["A+", "A0", "A-", "BBB+", "BBB0"],
            }
        )

    manifest_sha = "a" * 64
    document_sha = "b" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )
    profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )
    request = quantitative_request_from_candidate_profile(profile)

    assert record.status == "REVIEW"
    assert profile.status == "REVIEW"
    assert profile.available_candidates == ()
    assert "CASE_TABLE_NOT_DETERMINISTIC" in issue_codes(profile)
    assert request.activation_status == "REVIEW_REQUIRED"


def busan_hwp_duplicate_summary_fixture(
    *,
    duplicate_amount_detail: bool = False,
    repeated_amount_header_quote: bool = False,
) -> tuple[dict, str]:
    """Three 부산 criteria whose summary and detail headers share exact quotes."""

    amount_table = split_cell_case_table(
        metric="PERFORMANCE_AMOUNT",
        unit="원",
        max_points=6,
        cases=[
            split_case(
                "A. 2억 원 이상",
                operator="GTE",
                comparison_value=200_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            ),
            split_case(
                "B. 1.5억 원 이상",
                operator="GTE",
                comparison_value=150_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=5.5,
                row_order=2,
            ),
            split_case(
                "C. 1억 원 이상",
                operator="GTE",
                comparison_value=100_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=5,
                row_order=3,
            ),
        ],
    )
    amount = amount_table["criteria"][0]
    amount.update(
        {
            "criterion_id": "BUSAN-DUPLICATE-AMOUNT",
            "label": "용역수행 실적(금액)",
            "criterion_literal": "모델이 재작성한 용역수행 금액 기준",
            "evidence": anchor("용역수행 실적"),
        }
    )

    count = json.loads(json.dumps(amount))
    count.update(
        {
            "criterion_id": "BUSAN-DUPLICATE-COUNT",
            "label": "용역수행 실적(건수)",
            "criterion_literal": "모델이 재작성한 용역수행 건수 기준",
            "max_points": 4,
            "metric": "PERFORMANCE_COUNT",
            "unit": "건",
            "required_evidence": ["company.performance.count"],
            "evidence": anchor("용역수행 실적"),
            "cases": [
                split_case(
                    "A. 5건 이상",
                    operator="GTE",
                    comparison_value=5,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                ),
                split_case(
                    "B. 4건",
                    operator="EQ",
                    comparison_value=4,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=3.7,
                    row_order=2,
                ),
            ],
        }
    )

    credit_table, credit_source = busan_hwp_multicolumn_credit_fixture()
    credit = credit_table["criteria"][0]
    credit.update(
        {
            "criterion_id": "BUSAN-DUPLICATE-CREDIT",
            "criterion_literal": "모델이 재작성한 제안업체 경영상태 기준",
            "evidence": anchor("제안업체 경영상태"),
        }
    )

    amount_detail = [
        (
            "1) 용역수행 실적(금액 / 용역수행 실적(금액, 6점)"
            if repeated_amount_header_quote
            else "1) 용역수행 실적(금액, 6점)"
        ),
        "A. 2억 원 이상",
        "6",
        "B. 1.5억 원 이상",
        "5.5",
        "C. 1억 원 이상",
        "5",
    ]
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "정량적 평가 요약",
            "용역수행 실적(금액 요약, 6점)",
            "용역수행 실적(건수 요약, 4점)",
            "제안업체 경영상태 요약 (10점)",
            *amount_detail,
            *(amount_detail if duplicate_amount_detail else []),
            "2) 용역수행 실적(건수, 4점)",
            "A. 5건 이상",
            "4",
            "B. 4건",
            "3.7",
            *credit_source.splitlines()[1:],
            "총점 20점",
        ]
    )
    amount_table["criteria"] = [amount, count, credit]
    amount_table["total_points"] = 20
    amount_table["total_evidence"] = anchor("총점 20점")
    return amount_table, source


def test_busan_hwp_duplicate_summary_headers_bind_only_detailed_criteria() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert [item.criterion_literal for item in profile.available_candidates] == [
        "1) 용역수행 실적(금액, 6점)",
        "2) 용역수행 실적(건수, 4점)",
        "❍ 제안업체 경영상태 (10점)",
    ]
    assert [len(item.cases) for item in profile.available_candidates] == [3, 2, 4]


def test_exact_sourcewide_rebind_clears_fully_resolved_model_table_ambiguity() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "요약 배점과 상세 배점 중 적용 표를 확인해야 함"

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert "AMBIGUOUS_TABLE" not in issue_codes(profile)
    assert not any(
        code.startswith("SOURCEWIDE_AMBIGUITY_") for code in issue_codes(profile)
    )
    assert [item.criterion_literal for item in profile.available_candidates] == [
        "1) 용역수행 실적(금액, 6점)",
        "2) 용역수행 실적(건수, 4점)",
        "❍ 제안업체 경영상태 (10점)",
    ]


def generic_performance_summary_fixture() -> tuple[dict, str]:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "❍ 정량적 평가(10점): 사업부서 평가",
            "2. 정량적 평가 세부 기준",
            "1) 유사용역 수행실적(금액, 5점)",
            "A. 1억 원 이상",
            "5",
            "B. 5천만 원 이상",
            "3",
            "2) 유사용역 수행실적(건수, 5점)",
            "A. 3건 이상",
            "5",
            "B. 2건",
            "3",
        ]
    )
    table = split_cell_case_table(
        metric="PERFORMANCE_AMOUNT",
        unit="원",
        max_points=5,
        cases=[
            split_case(
                "A. 1억 원 이상",
                operator="GTE",
                comparison_value=100_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=5,
                row_order=1,
            ),
            split_case(
                "B. 5천만 원 이상",
                operator="GTE",
                comparison_value=50_000_000,
                category_values=[],
                award_kind="POINTS",
                award_value=3,
                row_order=2,
            ),
        ],
    )
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_id": "GENERIC-PERFORMANCE-AMOUNT",
            "label": "유사용역 수행실적(금액)",
            "required_evidence": ["company.performance.amount"],
        }
    )
    count = json.loads(json.dumps(amount))
    count.update(
        {
            "criterion_id": "GENERIC-PERFORMANCE-COUNT",
            "label": "유사용역 수행실적(건수)",
            "metric": "PERFORMANCE_COUNT",
            "unit": "건",
            "required_evidence": ["company.performance.count"],
            "cases": [
                split_case(
                    "A. 3건 이상",
                    operator="GTE",
                    comparison_value=3,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=5,
                    row_order=1,
                ),
                split_case(
                    "B. 2건",
                    operator="EQ",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=3,
                    row_order=2,
                ),
            ],
        }
    )
    table["criteria"] = [amount, count]
    table["total_points"] = 10
    table["total_evidence"] = anchor("❍ 정량적 평가(10점): 사업부서 평가")
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"
    return table, source


def generic_duplicate_metric_fixture() -> tuple[dict, str]:
    table, source = generic_performance_summary_fixture()
    old_summary = "❍ 정량적 평가(10점): 사업부서 평가"
    new_summary = "❍ 정량적 평가(11점): 사업부서 평가"
    source = source.replace(old_summary, new_summary).replace(
        "1) 유사용역 수행실적(금액, 5점)\n"
        "A. 1억 원 이상\n5\nB. 5천만 원 이상\n3",
        "1) 일반용역 수행실적(건수, 6점)\n"
        "A. 5건 이상\n6\nB. 4건\n3",
    )
    first = table["criteria"][0]
    first.update(
        {
            "metric": "PERFORMANCE_COUNT",
            "unit": "건",
            "max_points": 6,
            "required_evidence": ["company.performance.count"],
            "cases": [
                split_case(
                    "A. 5건 이상",
                    operator="GTE",
                    comparison_value=5,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=6,
                    row_order=1,
                ),
                split_case(
                    "B. 4건",
                    operator="EQ",
                    comparison_value=4,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=3,
                    row_order=2,
                ),
            ],
        }
    )
    table["total_points"] = 11
    table["total_evidence"] = anchor(new_summary)
    return table, source


def test_sourcewide_rebind_clears_non_busan_performance_table_ambiguity() -> None:
    table, source = generic_performance_summary_fixture()

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert "AMBIGUOUS_TABLE" not in issue_codes(profile)
    assert not any(
        code.startswith("SOURCEWIDE_AMBIGUITY_") for code in issue_codes(profile)
    )
    assert [item.criterion_literal for item in profile.available_candidates] == [
        "1) 유사용역 수행실적(금액, 5점)",
        "2) 유사용역 수행실적(건수, 5점)",
    ]


def test_generic_sourcewide_rebind_rejects_duplicate_metric() -> None:
    table, source = generic_duplicate_metric_fixture()

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "SOURCEWIDE_AMBIGUITY_SIGNATURE_UNSUPPORTED" in issue_codes(profile)


def test_generic_sourcewide_rebind_rejects_unsupported_metric() -> None:
    table, source = generic_performance_summary_fixture()
    count = table["criteria"][1]
    count.update(
        {
            "metric": "PERSONNEL_COUNT",
            "unit": "명",
            "required_evidence": ["company.personnel.count"],
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_generic_sourcewide_rebind_rejects_omitted_case_row() -> None:
    table, source = generic_performance_summary_fixture()
    table["criteria"][1]["cases"] = table["criteria"][1]["cases"][:1]

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "SOURCEWIDE_AMBIGUITY_CASE_CENSUS_MISMATCH" in issue_codes(profile)


def busan_hwp_summary_before_detail_fixture() -> tuple[dict, str]:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"
    summary = "❍ 정량적 평가(20점): 부산광역시교육청 사업부서 평가"
    table["total_evidence"] = anchor(summary)
    source = source.replace(
        "[HWP SECTION 0]\n",
        "[HWP SECTION 0]\n참고 수행능력 (3점)\n[HWP SECTION 1]\n",
        1,
    ).replace(
        "정량적 평가 요약",
        f"정량적 평가 요약\n{summary}",
        1,
    ).replace(
        "1) 용역수행 실적(금액, 6점)",
        "나. 정량적 평가 세부기준\n1) 용역수행 실적(금액, 6점)",
        1,
    ).replace("\n총점 20점", "", 1)
    source = (
        f"{source}\n"
        f"{'\n'.join(str(index) for index in range(1, 22))}\n"
        "10점\n20점\n"
        "3. 제안서 평가\n"
        "❍ 총점 100점 만점으로 정량적 평가(20점) 및 "
        "정성적 평가(80점)를 실시한다."
    )
    return table, source


def test_busan_summary_total_before_detail_proves_same_source_table() -> None:
    table, source = busan_hwp_summary_before_detail_fixture()
    table["criteria"][2]["unit"] = "점"

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert "AMBIGUOUS_TABLE" not in issue_codes(profile)
    credit = next(
        item
        for item in profile.available_candidates
        if item.metric == "CREDIT_RATING"
    )
    assert credit.unit == "등급"
    assert not any(
        code.startswith("SOURCEWIDE_AMBIGUITY_") for code in issue_codes(profile)
    )


def test_busan_source_conditions_canonicalize_short_same_cell_evidence() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    full_condition = "①‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
    short_literal = "‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
    for candidate in table["criteria"][:2]:
        candidate.setdefault("recognition_conditions", []).append(
            {
                "literal": short_literal,
                "evidence": anchor(full_condition),
            }
        )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" not in issue_codes(profile)
    for candidate in profile.available_candidates[:2]:
        matching = [
            condition
            for condition in candidate.recognition_conditions
            if condition.literal == full_condition
        ]
        assert len(matching) == 1
        assert matching[0].evidence.quote == full_condition


def test_busan_source_conditions_canonicalize_repeated_short_scope_in_owned_cell() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    short_condition = "최근 3년간 지자체, 공공기관 등"
    full_condition = (
        "최근 3년간 지자체, 공공기관 등\n"
        "(교육, 취업, 행사) 용역 수행완료 실적 (6점)"
    )
    table["criteria"][0].setdefault("recognition_conditions", []).append(
        {
            "literal": short_condition,
            "evidence": anchor(short_condition),
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" not in issue_codes(profile)
    amount = next(
        candidate
        for candidate in profile.available_candidates
        if candidate.metric == "PERFORMANCE_AMOUNT"
    )
    assert short_condition not in {
        condition.literal for condition in amount.recognition_conditions
    }
    matching = [
        condition
        for condition in amount.recognition_conditions
        if condition.literal == full_condition
    ]
    assert len(matching) == 1
    assert matching[0].evidence.quote == full_condition


def test_busan_source_conditions_canonicalize_same_short_scope_per_exact_criterion() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    table["ambiguity_reason"] = None
    exact_headers = (
        "1) 용역수행 실적(금액)\n(6점)",
        "2) 용역수행 실적(건수)\n4점",
        "❍ 제안업체 경영상태 (10점)",
    )
    for candidate, header in zip(
        table["criteria"],
        exact_headers,
        strict=True,
    ):
        candidate["criterion_literal"] = header
        candidate["evidence"] = anchor(header)
    short_condition = "최근 3년간 지자체, 공공기관 등"
    full_conditions = (
        (
            "최근 3년간 지자체, 공공기관 등\n"
            "(교육, 취업, 행사) 용역 수행완료 실적 (6점)"
        ),
        (
            "최근 3년간 지자체, 공공기관 등\n"
            "(교육,취업,행사)용역\n수행완료 실적 (4점)"
        ),
    )
    for candidate in table["criteria"][:2]:
        candidate.setdefault("recognition_conditions", []).append(
            {
                "literal": short_condition,
                "evidence": anchor(short_condition),
            }
        )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    for candidate, full_condition in zip(
        profile.available_candidates[:2],
        full_conditions,
        strict=True,
    ):
        assert short_condition not in {
            condition.literal for condition in candidate.recognition_conditions
        }
        matching = [
            condition
            for condition in candidate.recognition_conditions
            if condition.literal == full_condition
        ]
        assert len(matching) == 1
        assert matching[0].evidence.quote == full_condition


def test_busan_source_conditions_preserve_repeated_short_model_duplicates() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    short_condition = "최근 3년간 지자체, 공공기관 등"
    condition = {
        "literal": short_condition,
        "evidence": anchor(short_condition),
    }
    table["criteria"][0].setdefault("recognition_conditions", []).extend(
        [condition, json.loads(json.dumps(condition))]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "RECOGNITION_CONDITION_DUPLICATE" in issue_codes(profile)


@pytest.mark.parametrize("structural_claim", ("foreign-header", "foreign-case"))
def test_busan_source_conditions_never_canonicalize_structural_claims(
    structural_claim: str,
) -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    exact_headers = (
        "1) 용역수행 실적(금액)\n(6점)",
        "2) 용역수행 실적(건수)\n4점",
        "❍ 제안업체 경영상태 (10점)",
    )
    for candidate, header in zip(
        table["criteria"],
        exact_headers,
        strict=True,
    ):
        candidate["criterion_literal"] = header
        candidate["evidence"] = anchor(header)
    if structural_claim == "foreign-header":
        owner = table["criteria"][0]
        literal = exact_headers[1]
    else:
        for case in table["criteria"][0]["cases"]:
            score = f"{float(case['award_value']):g}"
            literal = f"{case['literal']}\n{score}"
            case["literal"] = literal
            case["evidence"] = anchor(literal)
        owner = table["criteria"][2]
        literal = "A. 2억 원 이상\n6"
    owner.setdefault("recognition_conditions", []).append(
        {
            "literal": literal,
            "evidence": anchor(literal),
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)
    assert len(profile.available_candidates) == 3
    assert profile.review_candidates == ()


def test_busan_source_conditions_do_not_hide_repeated_header_score_literal() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    table["ambiguity_reason"] = None
    exact_headers = (
        "1) 용역수행 실적(금액)\n(6점)",
        "2) 용역수행 실적(건수)\n4점",
        "❍ 제안업체 경영상태 (10점)",
    )
    for candidate, header in zip(
        table["criteria"],
        exact_headers,
        strict=True,
    ):
        candidate["criterion_literal"] = header
        candidate["evidence"] = anchor(header)
    table["criteria"][1].setdefault("recognition_conditions", []).append(
        {
            "literal": "4점",
            "evidence": anchor("4점"),
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)
    assert len(profile.available_candidates) == 3
    assert profile.review_candidates == ()


def test_busan_source_conditions_do_not_hide_repeated_case_row_literal() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    table["ambiguity_reason"] = None
    exact_headers = (
        "1) 용역수행 실적(금액)\n(6점)",
        "2) 용역수행 실적(건수)\n4점",
        "❍ 제안업체 경영상태 (10점)",
    )
    for candidate, header in zip(
        table["criteria"],
        exact_headers,
        strict=True,
    ):
        candidate["criterion_literal"] = header
        candidate["evidence"] = anchor(header)
    original_row = "A. 2억 원 이상\n6"
    structural_row = "A. 2억 원 이상 지자체, 공공기관 등\n6"
    source = source.replace(original_row, structural_row, 1)
    first_case = table["criteria"][0]["cases"][0]
    first_case["literal"] = structural_row
    first_case["evidence"] = anchor(structural_row)
    table["criteria"][0].setdefault("recognition_conditions", []).append(
        {
            "literal": "지자체, 공공기관 등",
            "evidence": anchor("지자체, 공공기관 등"),
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)
    assert len(profile.available_candidates) == 3
    assert profile.review_candidates == ()


def test_busan_source_conditions_never_replace_cross_attachment_anchor() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    full_condition = "①‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
    evidence = anchor(full_condition)
    evidence["attachment_id"] = "ATT-OTHER"
    table["criteria"][0].setdefault("recognition_conditions", []).append(
        {
            "literal": "‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다.",
            "evidence": evidence,
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "UNKNOWN_ATTACHMENT" in issue_codes(profile)


def test_busan_source_conditions_preserve_duplicate_model_claims() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    full_condition = "①‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다."
    condition = {
        "literal": "‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다.",
        "evidence": anchor(full_condition),
    }
    table["criteria"][0].setdefault("recognition_conditions", []).extend(
        [condition, json.loads(json.dumps(condition))]
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "RECOGNITION_CONDITION_DUPLICATE" in issue_codes(profile)


def test_busan_source_conditions_do_not_collapse_multi_cell_claim() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    combined = (
        "①‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다.\n"
        "② 증빙서류로 용역수행실적 총괄표(별지서식5호), "
        "용역실적증명서(별지서식6호)를 첨부한다."
    )
    table["criteria"][0].setdefault("recognition_conditions", []).append(
        {
            "literal": combined,
            "evidence": anchor(combined),
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)


def test_busan_source_conditions_do_not_hide_disjoint_literal_evidence() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    table["criteria"][0].setdefault("recognition_conditions", []).append(
        {
            "literal": "(교육, 취업, 행사) 용역 수행완료 실적 (6점)",
            "evidence": anchor("A. 2억 원 이상"),
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)
    assert "RECOGNITION_CONDITION_LITERAL_MISMATCH" in issue_codes(profile)


@pytest.mark.parametrize(
    "footnote_mutation",
    ("missing", "duplicate", "extra-score", "later-section"),
)
def test_busan_credit_source_footnote_boundary_fails_closed(
    footnote_mutation: str,
) -> None:
    table, source = busan_hwp_summary_before_detail_fixture()
    table["criteria"][2]["unit"] = "점"
    footnote = (
        "* 등급별 평점이 소수점 이하의 숫자가 있는 경우 "
        "소수점 다섯째자리에서 반올림 함"
    )
    if footnote_mutation == "missing":
        source = source.replace(f"\n{footnote}", "", 1)
    elif footnote_mutation == "duplicate":
        source = source.replace(footnote, f"{footnote}\n{footnote}", 1)
    elif footnote_mutation == "extra-score":
        source = source.replace(footnote, f"0점\n{footnote}", 1)
    else:
        source = source.replace(f"\n{footnote}", "", 1)
        source = f"{source}\n[HWP SECTION 2]\n{footnote}"

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CASE_CENSUS_MISMATCH" in issue_codes(
        profile
    )
    credit = next(
        item
        for item in profile.available_candidates
        if item.metric == "CREDIT_RATING"
    )
    assert credit.unit == "점"


def test_sourcewide_rebind_keeps_ambiguity_when_total_is_outside_table_section() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"
    source = source.replace("\n총점 20점", "", 1)
    source = f"{source}\n[HWP SECTION 1]\n총점 20점"

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert (
        "SOURCEWIDE_AMBIGUITY_TOTAL_PROVENANCE_UNPROVEN"
        in issue_codes(profile)
    )


def test_sourcewide_rebind_keeps_ambiguity_for_second_source_only_table() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "별도 정량평가표 적용 여부 확인 필요"
    source = (
        f"{source}\n[HWP SECTION 1]\n정량적 평가표\n"
        "가격경쟁력 30점\n총점 30점"
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_sourcewide_rebind_keeps_ambiguity_for_ordinal_competing_table() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"
    source = (
        f"{source}\n[HWP SECTION 1]\n다. 정량적 평가 세부기준\n"
        "가격경쟁력 (30점)\n합계 : 30점"
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


@pytest.mark.parametrize(
    "ambiguity_reason",
    (
        "외부 별도지침 적용 여부를 확인해야 함",
        "외부 별도지침과 요약 배점 및 상세 배점을 함께 확인해야 함",
        "HWP 셀 구조상 행 연결 검토 필요. 원문 외 사유로 적용 표를 확인해야 함",
    ),
)
def test_sourcewide_rebind_does_not_clear_unrelated_model_ambiguity(
    ambiguity_reason: str,
) -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = ambiguity_reason

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_sourcewide_rebind_requires_every_owned_source_case_row() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"
    table["criteria"][1]["cases"] = table["criteria"][1]["cases"][:1]

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CASE_CENSUS_MISMATCH" in issue_codes(profile)


def test_sourcewide_ambiguity_blocker_is_enum_only_and_omits_model_prose() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    model_prose = "외부 별도지침 적용 여부를 확인해야 함"
    table["ambiguity_reason"] = model_prose

    profile = build(payload_with_table(table), source=source)

    blockers = [
        item
        for item in profile.issues
        if item.code.startswith("SOURCEWIDE_AMBIGUITY_")
    ]
    assert [(item.code, item.disposition) for item in blockers] == [
        ("SOURCEWIDE_AMBIGUITY_REASON_NOT_STRUCTURAL", "REVIEW")
    ]
    assert all(
        re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", item.code)
        for item in blockers
    )
    assert model_prose not in profile.model_dump_json()
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256="b" * 64,
        manifest_sha256="a" * 64,
    )
    assert "SOURCEWIDE_AMBIGUITY_REASON_NOT_STRUCTURAL" in {
        item.code for item in record.issues
    }
    assert model_prose not in record.model_dump_json()


def test_non_hwp_structural_ambiguity_reports_scope_blocker_code() -> None:
    table = valid_table()
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"

    profile = build(payload_with_table(table))

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_SCOPE_UNSUPPORTED" in issue_codes(profile)


def test_sourcewide_rebind_rejects_unmodeled_credit_zero_point_row() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"
    source = source.replace(
        "* 등급별 평점이",
        "DDD 이하\n0점\n* 등급별 평점이",
        1,
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_exact_model_headers_do_not_clear_model_table_ambiguity() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    for candidate, literal in zip(
        table["criteria"],
        (
            "1) 용역수행 실적(금액, 6점)",
            "2) 용역수행 실적(건수, 4점)",
            "❍ 제안업체 경영상태 (10점)",
        ),
        strict=True,
    ):
        candidate["criterion_literal"] = literal
        candidate["evidence"] = anchor(literal)
    table["ambiguity_reason"] = "원문 외부 사유로 적용 표를 확인해야 함"

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_partial_sourcewide_rebind_does_not_clear_model_table_ambiguity() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount["criterion_literal"] = "1) 용역수행 실적(금액, 6점)"
    amount["evidence"] = anchor("1) 용역수행 실적(금액, 6점)")
    table["ambiguity_reason"] = "모든 평가항목의 상세 배점을 확인해야 함"

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "REVIEW"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_sourcewide_rebind_never_clears_ambiguity_for_two_tables() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "복수 평가표 중 적용 대상을 확인해야 함"
    second = json.loads(json.dumps(table))
    second["table_id"] = "BUSAN-DUPLICATE-TABLE-2"
    for candidate in second["criteria"]:
        candidate["criterion_id"] = f"{candidate['criterion_id']}-2"
    payload = payload_with_table(table).model_copy(
        update={
            "quantitative_tables": [
                ExtractionPayload.model_validate(
                    {
                        **payload_with_table(table).model_dump(),
                        "quantitative_tables": [second],
                    }
                ).quantitative_tables[0],
                *payload_with_table(table).quantitative_tables,
            ]
        }
    )

    profile = build(payload, source=source)

    assert profile.status != "AVAILABLE"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


@pytest.mark.parametrize(
    "payload_mutation",
    ("source-gap", "not-applicable", "duplicate-total", "total-mismatch"),
)
def test_sourcewide_rebind_requires_complete_unique_consistent_table_proof(
    payload_mutation: str,
) -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    table["ambiguity_reason"] = "상세 평가표 완전성을 확인해야 함"
    payload_data = payload_with_table(table).model_dump()
    if payload_mutation == "source-gap":
        payload_data["missing_or_unreadable"] = ["평가표 일부 행 판독 불가"]
    elif payload_mutation == "not-applicable":
        source = f"{source}\n이 공고에는 정량평가표가 적용되지 않음"
        payload_data["quantitative_table_not_applicable"] = {
            "reason_literal": "이 공고에는 정량평가표가 적용되지 않음",
            "evidence": anchor("이 공고에는 정량평가표가 적용되지 않음"),
        }
    elif payload_mutation == "duplicate-total":
        source = source.replace("총점 20점", "총점 20점\n총점 20점")
    else:
        table["total_points"] = 21
        table["total_evidence"] = anchor("총점 21점")
        source = source.replace("총점 20점", "총점 21점")
        payload_data = payload_with_table(table).model_dump()

    profile = build(ExtractionPayload.model_validate(payload_data), source=source)

    assert profile.status != "AVAILABLE"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def busan_hwp_production_partial_anchor_fixture() -> tuple[dict, str, str]:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount, count, credit = table["criteria"]
    table["ambiguity_reason"] = "HWP 셀 구조상 행 연결 검토 필요"

    # Production diagnostics showed three different partial model shapes:
    # amount evidence had only the 6-point cell, count evidence had only the
    # metric words, and the credit literal omitted its 10-point maximum.
    amount.update(
        {
            "criterion_literal": "용역수행 실적(금액, 6점)",
            "evidence": anchor("6점"),
        }
    )
    count.update(
        {
            "criterion_literal": "용역수행 실적(건수, 4점)",
            "evidence": anchor("용역수행 실적(건수)"),
            "unit": None,
            "cases": [
                split_case(
                    "A. 5건 이상",
                    operator="GTE",
                    comparison_value=5,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                ),
                split_case(
                    "B. 4건",
                    operator="EQ",
                    comparison_value=4,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=3.7,
                    row_order=2,
                ),
                split_case(
                    "C. 3건",
                    operator="EQ",
                    comparison_value=3,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=3.4,
                    row_order=3,
                ),
                split_case(
                    "D. 2건",
                    operator="EQ",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=3.1,
                    row_order=4,
                ),
                split_case(
                    "E. 1건",
                    operator="EQ",
                    comparison_value=1,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=2.8,
                    row_order=5,
                ),
            ],
        }
    )
    credit.update(
        {
            "criterion_literal": "제안업체 경영상태",
            "evidence": anchor("제안업체 경영상태 요약 (10점)"),
            "unit": "점",
        }
    )
    footnote_block = (
        "※ 실적인정 기준\n"
        "①‘최근 3년간’이라 함은 입찰공고일을 기준으로 한다.\n"
        "② 증빙서류로 용역수행실적 총괄표(별지서식5호), "
        "용역실적증명서(별지서식6호)를 첨부한다.\n"
        "③ 공동계약으로 참여한 실적의 경우 공동계약 참여 비율에 "
        "따른 금액의 실적\n"
        "④ 자체 합산표 및 증빙서류를 제출하지 않은 경우에는 "
        "최저점으로 처리한다."
    )
    source = source.replace(
        "1) 용역수행 실적(금액, 6점)",
        (
            "1) 용역수행 실적(금액)\n(6점)\n"
            "세부 항목\n평가요소\n배점\n등급\n배점(점)\n"
            "최근 3년간 지자체, 공공기관 등\n"
            "(교육, 취업, 행사) 용역 수행완료 실적 (6점)\n"
            "단일용역\n최고금액\n(1건)\n6"
        ),
    ).replace(
        "B. 4건\n3.7",
        "B. 4건\n3.7\nC. 3건\n3.4\nD. 2건\n3.1\nE. 1건\n2.8",
    ).replace(
        "2) 용역수행 실적(건수, 4점)",
        (
            "2) 용역수행 실적(건수)\n4점\n"
            "세부 항목\n평가요소\n배점\n등급\n배점(점)\n"
            "최근 3년간 지자체, 공공기관 등\n"
            "(교육,취업,행사)용역\n수행완료 실적 (4점)\n"
            "실적건수\n(0.2억원\n이상)\n4"
        ),
    ).replace(
        "E. 1건\n2.8\n❍ 제안업체 경영상태",
        (
            f"E. 1건\n2.8\n{footnote_block}\n"
            "❍ 제안업체 경영상태"
        ),
    )
    return table, source, footnote_block


def busan_hwp_production_raw_performance_conditions(
    footnote_block: str,
) -> list[dict[str, object]]:
    """Mirror the bounded literal/evidence shapes observed in production."""

    conditions: list[dict[str, object]] = []
    for line in footnote_block.splitlines()[1:]:
        literal = line[2:] if len(line) > 1 and line[1] == " " else line[1:]
        evidence_literal = literal[:-1] if literal.endswith(".") else literal
        conditions.append(
            {
                "literal": literal,
                "evidence": anchor(evidence_literal),
            }
        )
    return conditions


def test_busan_hwp_removes_only_count_share_owned_by_amount() -> None:
    table, source, footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    raw_conditions = busan_hwp_production_raw_performance_conditions(
        footnote_block
    )
    for candidate in table["criteria"][:2]:
        candidate["recognition_conditions"] = json.loads(
            json.dumps(raw_conditions)
        )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" not in issue_codes(profile)
    assert [
        len(item.recognition_conditions)
        for item in profile.available_candidates
    ] == [6, 5, 0]

    manifest_sha = "a" * 64
    document_sha = "b" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )
    runtime_profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )
    request = quantitative_request_from_candidate_profile(runtime_profile)

    assert record.status == "AVAILABLE", record.issues
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    assert request.activation_reasons == []
    criteria = {item.metric_key: item for item in request.criteria}
    amount_scope = criteria["company.performance.amount"].performance_scope
    count_scope = criteria["company.performance.count"].performance_scope
    assert amount_scope is not None
    assert count_scope is not None
    assert amount_scope.consortium_share_rule == "APPLY_SHARE"
    assert count_scope.consortium_share_rule == "UNSPECIFIED"


def test_busan_hwp_preserves_duplicate_count_share_claims() -> None:
    table, source, footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    raw_conditions = busan_hwp_production_raw_performance_conditions(
        footnote_block
    )
    for candidate in table["criteria"][:2]:
        candidate["recognition_conditions"] = json.loads(
            json.dumps(raw_conditions)
        )
    table["criteria"][1]["recognition_conditions"].append(
        json.loads(json.dumps(raw_conditions[2]))
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "RECOGNITION_CONDITION_DUPLICATE" in issue_codes(profile)


def test_busan_hwp_preserves_cross_attachment_count_share_claim() -> None:
    table, source, footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    raw_conditions = busan_hwp_production_raw_performance_conditions(
        footnote_block
    )
    for candidate in table["criteria"][:2]:
        candidate["recognition_conditions"] = json.loads(
            json.dumps(raw_conditions)
        )
    table["criteria"][1]["recognition_conditions"][2]["evidence"][
        "attachment_id"
    ] = "ATT-OTHER"

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "UNKNOWN_ATTACHMENT" in issue_codes(profile)


@pytest.mark.parametrize("evidence_shape", ("broad", "disjoint"))
def test_busan_hwp_preserves_unbounded_count_share_claim(
    evidence_shape: str,
) -> None:
    table, source, footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    raw_conditions = busan_hwp_production_raw_performance_conditions(
        footnote_block
    )
    for candidate in table["criteria"][:2]:
        candidate["recognition_conditions"] = json.loads(
            json.dumps(raw_conditions)
        )
    count_share = table["criteria"][1]["recognition_conditions"][2]
    if evidence_shape == "broad":
        count_share["evidence"] = anchor(
            "\n".join(footnote_block.splitlines()[3:5])
        )
    else:
        count_share["evidence"] = anchor(raw_conditions[0]["literal"])

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)
    assert "RECOGNITION_CONDITION_LITERAL_MISMATCH" in issue_codes(profile)


@pytest.mark.parametrize("owner_mutation", ("ambiguous", "invalid-row-order"))
def test_busan_hwp_preserves_count_share_without_unique_valid_amount_owner(
    owner_mutation: str,
) -> None:
    table, source, footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    raw_conditions = busan_hwp_production_raw_performance_conditions(
        footnote_block
    )
    for candidate in table["criteria"][:2]:
        candidate["recognition_conditions"] = json.loads(
            json.dumps(raw_conditions)
        )
    amount = table["criteria"][0]
    if owner_mutation == "ambiguous":
        amount["ambiguity_reason"] = "금액 기준 소유자 확인 필요"
    else:
        amount["cases"][1]["row_order"] = 3

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "SOURCEWIDE_AMBIGUITY_CLAIM_COLLISION" in issue_codes(profile)


def test_busan_hwp_sourcewide_headers_repair_production_partial_anchors() -> None:
    table, source, footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    assert [item.criterion_literal for item in profile.available_candidates] == [
        "1) 용역수행 실적(금액)\n(6점)",
        "2) 용역수행 실적(건수)\n4점",
        "❍ 제안업체 경영상태 (10점)",
    ]
    assert [item.max_points for item in profile.available_candidates] == [6, 4, 10]
    assert [item.metric for item in profile.available_candidates] == [
        "PERFORMANCE_AMOUNT",
        "PERFORMANCE_COUNT",
        "CREDIT_RATING",
    ]
    assert [item.unit for item in profile.available_candidates] == [
        "원",
        "건",
        "등급",
    ]
    assert [len(item.cases) for item in profile.available_candidates] == [3, 5, 4]
    assert [
        len(item.recognition_conditions)
        for item in profile.available_candidates
    ] == [5, 4, 0]
    assert [item.evidence.page for item in profile.available_candidates] == [
        None,
        None,
        None,
    ]
    assert [item.evidence.section for item in profile.available_candidates] == [
        "HWP SECTION 0",
        "HWP SECTION 0",
        "HWP SECTION 0",
    ]

    manifest_sha = "a" * 64
    document_sha = "b" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )
    runtime_profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )
    request = quantitative_request_from_candidate_profile(runtime_profile)

    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    assert request.activation_reasons == []
    criteria = {item.metric_key: item for item in request.criteria}
    amount_scope = criteria["company.performance.amount"].performance_scope
    count_scope = criteria["company.performance.count"].performance_scope
    assert amount_scope is not None
    assert amount_scope.lookback_years == 3
    assert amount_scope.lookback_anchor_basis == "BID_NOTICE_DATE"
    assert amount_scope.aggregation == "MAX_SINGLE_AMOUNT"
    assert amount_scope.consortium_share_rule == "APPLY_SHARE"
    assert amount_scope.certificate_required is True
    assert set(amount_scope.similarity_keywords) == {"교육", "취업", "행사"}
    assert count_scope is not None
    assert count_scope.lookback_years == 3
    assert count_scope.lookback_anchor_basis == "BID_NOTICE_DATE"
    assert count_scope.aggregation == "COUNT"
    assert count_scope.minimum_single_contract_amount_krw == 20_000_000
    assert count_scope.certificate_required is True

    footer_after_credit = source.replace(
        f"{footnote_block}\n❍ 제안업체 경영상태",
        "❍ 제안업체 경영상태",
        1,
    ).replace(
        "\n총점 20점",
        f"\n{footnote_block}\n총점 20점",
        1,
    )
    later_profile = build(
        payload_with_table(table),
        source=footer_after_credit,
    )
    assert later_profile.status == "AVAILABLE", issue_codes(later_profile)
    assert [
        len(item.recognition_conditions)
        for item in later_profile.available_candidates
    ] == [2, 2, 0]

    footer_after_unmodeled_boundary = source.replace(
        "※ 실적인정 기준",
        "9) 후속 별도 실적인정 기준 (1점)\n※ 실적인정 기준",
        1,
    )
    bounded_profile = build(
        payload_with_table(table),
        source=footer_after_unmodeled_boundary,
    )
    assert bounded_profile.status == "AVAILABLE", issue_codes(bounded_profile)
    assert [
        len(item.recognition_conditions)
        for item in bounded_profile.available_candidates
    ] == [2, 2, 0]


@pytest.mark.parametrize(
    ("values", "expected_unit"),
    (
        ((2, 1.5, 1), "억원"),
        ((200_000_000, 150_000_000, 100_000_000), "원"),
    ),
    ids=("source-scale-values", "canonical-values"),
)
def test_busan_hwp_source_bound_unitless_amount_rows_keep_extracted_values(
    values: tuple[float, float, float],
    expected_unit: str,
) -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    amount = table["criteria"][0]
    amount["unit"] = None
    for case, value in zip(amount["cases"], values, strict=True):
        case["comparison_value"] = value

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    repaired_amount = next(
        item
        for item in profile.available_candidates
        if item.metric == "PERFORMANCE_AMOUNT"
    )
    assert repaired_amount.unit == expected_unit
    assert [case.comparison_value for case in repaired_amount.cases] == list(values)
    assert [case.award_value for case in repaired_amount.cases] == [6, 5.5, 5]
    assert [case.operator for case in repaired_amount.cases] == [
        "GTE",
        "GTE",
        "GTE",
    ]
    assert [case.literal for case in repaired_amount.cases] == [
        "A. 2억 원 이상\n6",
        "B. 1.5억 원 이상\n5.5",
        "C. 1억 원 이상\n5",
    ]


def test_source_bound_unitless_amount_repair_accepts_zero_after_nonzero_scale() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    amount = table["criteria"][0]
    amount["unit"] = None
    for case, value in zip(amount["cases"], (2, 1.5, 0), strict=True):
        case["comparison_value"] = value
    source = source.replace("C. 1억 원 이상", "C. 0억 원 이상", 1)
    amount["cases"][2]["literal"] = "C. 0억 원 이상"
    amount["cases"][2]["evidence"] = anchor("C. 0억 원 이상")

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    repaired_amount = next(
        item
        for item in profile.available_candidates
        if item.metric == "PERFORMANCE_AMOUNT"
    )
    assert repaired_amount.unit == "억원"
    assert [case.comparison_value for case in repaired_amount.cases] == [
        2,
        1.5,
        0,
    ]


def test_source_bound_unitless_amount_repair_rejects_omitted_source_row() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    table["ambiguity_reason"] = None
    amount = table["criteria"][0]
    amount["unit"] = None
    for case, value in zip(amount["cases"], (2, 1.5, 1), strict=True):
        case["comparison_value"] = value
    amount["cases"].pop(1)
    amount["cases"][1]["row_order"] = 2

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "AMBIGUOUS_RULE" in amount_review.issue_codes
    assert "CASE_COMPARATOR_MISMATCH" in amount_review.issue_codes
    assert "CASE_NUMBER_MISMATCH" in amount_review.issue_codes


def test_late_unit_repair_rejects_omitted_source_count_row() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    table["ambiguity_reason"] = None
    count = table["criteria"][1]
    count["unit"] = None
    count["cases"].pop(1)
    for row_order, case in enumerate(count["cases"], start=1):
        case["row_order"] = row_order
    count["cases"][-1]["literal"] = "2.8"
    count["cases"][-1]["evidence"] = anchor("E. 1건")

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    count_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-COUNT"
    )
    assert "AMBIGUOUS_RULE" in count_review.issue_codes


@pytest.mark.parametrize(
    "mutation",
    ("converted-value", "mixed-source-unit", "disjoint-evidence", "duplicate-row"),
)
def test_source_bound_unitless_amount_repair_fails_closed_without_unique_rows(
    mutation: str,
) -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    amount = table["criteria"][0]
    amount["unit"] = None
    for case, value in zip(amount["cases"], (2, 1.5, 1), strict=True):
        case["comparison_value"] = value
    if mutation == "converted-value":
        amount["cases"][0]["comparison_value"] = 200_000_000
    elif mutation == "mixed-source-unit":
        source = source.replace("B. 1.5억 원 이상", "B. 150백만 원 이상", 1)
        amount["cases"][1]["literal"] = "B. 150백만 원 이상"
        amount["cases"][1]["evidence"] = anchor("B. 150백만 원 이상")
        amount["cases"][1]["comparison_value"] = 150
    elif mutation == "disjoint-evidence":
        amount["cases"][1]["evidence"] = anchor("C. 1억 원 이상")
    else:
        source = source.replace(
            "B. 1.5억 원 이상\n5.5",
            "B. 1.5억 원 이상\n5.5\nB. 1.5억 원 이상\n5.5",
            1,
        )

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "CASE_COMPARATOR_MISMATCH" in issue_codes(profile)
    assert "CASE_NUMBER_MISMATCH" in issue_codes(profile)


def test_busan_hwp_production_shape_rebinds_without_mutating_rule_values() -> None:
    table, source, footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    amount, count, credit = table["criteria"]
    amount["unit"] = None
    for case, value in zip(amount["cases"], (2, 1.5, 1), strict=True):
        case["comparison_value"] = value
    count["unit"] = None
    shared_conditions = busan_hwp_production_raw_performance_conditions(
        footnote_block
    )
    for candidate in (amount, count):
        candidate["recognition_conditions"] = json.loads(
            json.dumps(shared_conditions)
        )

    # The live diagnostic reports one category expression per enterprise-credit
    # row.  Keep those extracted expressions byte-for-byte: source-bound repair
    # may rebind a split literal/evidence span, but must not split or expand a
    # persisted category into grades that the model did not return.
    credit_category_expressions = [
        "AAA, AA+, AA0, AA-, A+, A0, A-, BBB+, BBB0",
        "BBB-, BB+, BB0, BB-",
        "B+, B0, B-",
        "CCC+ 이하",
    ]
    for case, category in zip(
        credit["cases"], credit_category_expressions, strict=True
    ):
        case["category_values"] = [category]

    profile = build(payload_with_table(table), source=source)

    numeric_issue_codes = {
        issue.code
        for issue in profile.issues
        if issue.criterion_id in {amount["criterion_id"], count["criterion_id"]}
    }
    assert "CASE_COMPARATOR_MISMATCH" not in numeric_issue_codes
    assert "CASE_NUMBER_MISMATCH" not in numeric_issue_codes
    assert profile.status == "AVAILABLE", issue_codes(profile)
    candidates = {item.metric: item for item in profile.available_candidates}
    assert [case.category_values for case in candidates["CREDIT_RATING"].cases] == [
        (category,) for category in credit_category_expressions
    ]
    assert [case.comparison_value for case in candidates["PERFORMANCE_AMOUNT"].cases] == [
        2,
        1.5,
        1,
    ]
    assert [case.award_value for case in candidates["PERFORMANCE_AMOUNT"].cases] == [
        6,
        5.5,
        5,
    ]
    assert [case.operator for case in candidates["PERFORMANCE_COUNT"].cases] == [
        "GTE",
        "EQ",
        "EQ",
        "EQ",
        "EQ",
    ]

    manifest_sha = "a" * 64
    document_sha = "b" * 64
    record = validate_quantitative_attachment_extraction(
        payload_with_table(table),
        source_text=source,
        attachment_id=ATTACHMENT_ID,
        document_sha256=document_sha,
        manifest_sha256=manifest_sha,
    )
    runtime_profile = merge_validated_quantitative_records(
        [record],
        expected_documents={ATTACHMENT_ID: document_sha},
        manifest_sha256=manifest_sha,
    )
    request = quantitative_request_from_candidate_profile(runtime_profile)

    assert record.status == "AVAILABLE", record.issues
    assert request.activation_status == "AUTO_ACTIVE", request.activation_reasons
    criteria = {item.metric_key: item for item in request.criteria}
    credit_table = criteria["company.credit_rating"].case_table
    assert credit_table is not None
    assert tuple(
        value
        for row in credit_table.rows
        for value in row.category_values
    ) == CREDIT_RATING_ORDER
    facts = [
        QuantitativeFact(
            metric_key="company.performance.amount",
            status="ESTIMATED",
            value=200_000_000,
            lower_value=200_000_000,
            upper_value=200_000_000,
            evidence_key="company.performance.amount",
            fact_binding_sha256=criteria[
                "company.performance.amount"
            ].fact_binding_sha256,
            confidence=0.9,
            rationale="검증된 단일 최고 수행실적",
        ),
        QuantitativeFact(
            metric_key="company.performance.count",
            status="ESTIMATED",
            value=5,
            lower_value=5,
            upper_value=5,
            evidence_key="company.performance.count",
            fact_binding_sha256=criteria[
                "company.performance.count"
            ].fact_binding_sha256,
            confidence=0.9,
            rationale="검증된 수행실적 건수",
        ),
        QuantitativeFact(
            metric_key="company.credit_rating",
            status="CONFIRMED",
            value="A0",
            evidence_key="company.credit_rating",
            fact_binding_sha256=criteria[
                "company.credit_rating"
            ].fact_binding_sha256,
            confidence=1,
            rationale="유효 신용평가등급",
        ),
    ]

    result = estimate_quantitative_score(
        request.model_copy(update={"facts": facts})
    )

    assert (result.total_max_points, result.estimated_points) == (20, 20)
    assert {item.category: item.estimated_points for item in result.criteria} == {
        "PERFORMANCE_AMOUNT": 6,
        "PERFORMANCE_COUNT": 4,
        "CREDIT_RATING": 10,
    }


def test_source_bound_single_expression_credit_census_requires_full_registry() -> None:
    table, source, _footnote_block = (
        busan_hwp_production_partial_anchor_fixture()
    )
    credit = table["criteria"][2]
    expressions = [
        "AAA, AA+, AA0, AA-, A+, A0, A-, BBB+",
        "BBB-, BB+, BB0, BB-",
        "B+, B0, B-",
        "CCC+ 이하",
    ]
    source = source.replace(
        "A+, A0, A-, BBB+, BBB0\n배점의 100%",
        "A+, A0, A-, BBB+\n배점의 100%",
        1,
    )
    credit["cases"][0]["literal"] = (
        "AAA, AA+, AA0, AA-\nA+, A0, A-, BBB+"
    )
    credit["cases"][0]["evidence"] = anchor(
        credit["cases"][0]["literal"]
    )
    for case, expression in zip(credit["cases"], expressions, strict=True):
        case["category_values"] = [expression]

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "CASE_TABLE_NOT_DETERMINISTIC" in issue_codes(profile)
    assert [case["category_values"] for case in credit["cases"]] == [
        [expression] for expression in expressions
    ]


@pytest.mark.parametrize(
    ("replacement", "mutate_table"),
    [
        ("1) 용역수행 실적(건수, 6점)", None),
        ("1) 용역수행 실적(금액, 7점)", None),
        ("1) 용역수행 실적(금액, 6점 / 총 20점)", None),
        (
            "1) 용역수행 실적(금액, 6점)",
            lambda table: table["criteria"][0]["cases"][1].update(
                {"row_order": 3}
            ),
        ),
    ],
    ids=("metric", "maximum", "multiple-points", "row-order"),
)
def test_busan_hwp_sourcewide_header_mismatch_fails_closed(
    replacement: str,
    mutate_table,
) -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_literal": "모델이 재작성한 금액 기준 6점",
            "evidence": anchor("6점"),
        }
    )
    if mutate_table is not None:
        mutate_table(table)
    source = source.replace("1) 용역수행 실적(금액, 6점)", replacement)

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


def test_busan_hwp_sourcewide_header_cannot_borrow_rows_across_section() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_literal": "모델이 재작성한 금액 기준 6점",
            "evidence": anchor("6점"),
        }
    )
    source = source.replace(
        "1) 용역수행 실적(금액, 6점)\nA. 2억 원 이상",
        "1) 용역수행 실적(금액, 6점)\n[HWP SECTION 1]\nA. 2억 원 이상",
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


@pytest.mark.parametrize(
    "unmodeled_boundary",
    [
        "3) 기술인력 보유현황 (5점)",
        "기술인력 보유현황 (5점)",
        "최근 3년간 기술인력 보유현황 (5점)",
        "ISO 인증 보유현황 (5점)",
        "9) 용역수행 실적(건수, 5점)",
    ],
    ids=(
        "explicit-criterion",
        "numberless-criterion",
        "numberless-year-label",
        "numberless-ascii-label",
        "supported-metric",
    ),
)
def test_busan_hwp_sourcewide_header_cannot_borrow_rows_across_unmodeled_table(
    unmodeled_boundary: str,
) -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_literal": "모델이 재작성한 금액 기준 6점",
            "evidence": anchor("6점"),
        }
    )
    source = source.replace(
        "1) 용역수행 실적(금액, 6점)\nA. 2억 원 이상",
        (
            "1) 용역수행 실적(금액, 6점)\n"
            f"{unmodeled_boundary}\n"
            "A. 2억 원 이상"
        ),
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


def test_busan_hwp_column_detail_with_different_points_remains_boundary() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_literal": "모델이 재작성한 금액 기준 6점",
            "evidence": anchor("6점"),
        }
    )
    source = source.replace(
        "1) 용역수행 실적(금액, 6점)\nA. 2억 원 이상",
        (
            "1) 용역수행 실적(금액, 6점)\n"
            "세부 항목\n평가요소\n배점\n등급\n배점(점)\n"
            "최근 3년간 지자체, 공공기관 등\n"
            "(교육, 취업, 행사) 용역 수행완료 실적 (5점)\n"
            "A. 2억 원 이상"
        ),
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


@pytest.mark.parametrize(
    "independent_same_point_detail",
    [
        (
            "3) 최근 3년간 지자체, 공공기관 등 "
            "(교육, 취업, 행사) 용역 수행완료 실적 (6점)"
        ),
        "최근 3년간 타 기관 IT 용역 실적 (6점)",
    ],
    ids=("numbered", "different-scope"),
)
def test_busan_hwp_column_cluster_does_not_hide_same_point_criterion(
    independent_same_point_detail: str,
) -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_literal": "모델이 재작성한 금액 기준 6점",
            "evidence": anchor("6점"),
        }
    )
    source = source.replace(
        "1) 용역수행 실적(금액, 6점)\nA. 2억 원 이상",
        (
            "1) 용역수행 실적(금액, 6점)\n"
            "세부 항목\n평가요소\n배점\n등급\n배점(점)\n"
            f"{independent_same_point_detail}\n"
            "A. 2억 원 이상"
        ),
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


def test_sourcewide_boundary_does_not_mistake_korean_eq_case_for_header() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "용역수행 실적(건수, 4점)",
            "가. 5건 이상 4점",
            "나. 4건 3.7점",
            "다. 3건 3.4점",
            "정량평가 총점 4점",
        ]
    )
    table = split_cell_case_table(
        metric="PERFORMANCE_COUNT",
        unit="건",
        max_points=4,
        cases=[
            split_case(
                "가. 5건 이상 4점",
                operator="GTE",
                comparison_value=5,
                category_values=[],
                award_kind="POINTS",
                award_value=4,
                row_order=1,
            ),
            split_case(
                "나. 4건 3.7점",
                operator="EQ",
                comparison_value=4,
                category_values=[],
                award_kind="POINTS",
                award_value=3.7,
                row_order=2,
            ),
            split_case(
                "다. 3건 3.4점",
                operator="EQ",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=3.4,
                row_order=3,
            ),
        ],
    )
    criterion = table["criteria"][0]
    criterion.update(
        {
            "criterion_literal": "모델이 재작성한 건수 기준",
            "evidence": anchor("4점"),
        }
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    candidate = profile.available_candidates[0]
    assert candidate.criterion_literal == "용역수행 실적(건수, 4점)"
    assert [case.row_order for case in candidate.cases] == [1, 2, 3]


def test_busan_hwp_sourcewide_header_without_owning_section_fails_closed() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_literal": "모델이 재작성한 금액 기준 6점",
            "evidence": anchor("6점", page=99),
        }
    )
    source = source.replace("[HWP SECTION 0]\n", "").replace(
        "총점 20점",
        "총점 20점\n[HWP SECTION 1]\n후속 부록",
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


def test_busan_hwp_repeated_sourcewide_headers_exceeding_cap_fail_closed() -> None:
    table, source = busan_hwp_duplicate_summary_fixture()
    amount = table["criteria"][0]
    amount.update(
        {
            "criterion_literal": "모델이 재작성한 금액 기준 6점",
            "evidence": anchor("6점"),
        }
    )
    repeated = "\n".join(
        f"{index}) 용역수행 실적(금액, 6점)"
        for index in range(1, 67)
    )
    source = source.replace(
        "1) 용역수행 실적(금액, 6점)",
        repeated,
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


def test_busan_hwp_two_complete_detailed_headers_remain_ambiguous() -> None:
    table, source = busan_hwp_duplicate_summary_fixture(
        duplicate_amount_detail=True
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


def test_busan_hwp_repeated_quote_inside_one_header_remains_ambiguous() -> None:
    table, source = busan_hwp_duplicate_summary_fixture(
        repeated_amount_header_quote=True
    )

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "INCOMPLETE"
    amount_review = next(
        item
        for item in profile.review_candidates
        if item.criterion_id == "BUSAN-DUPLICATE-AMOUNT"
    )
    assert "CRITERION_LITERAL_MISMATCH" in amount_review.issue_codes


def test_split_hwp_explicit_table_footnote_can_bind_to_an_earlier_criterion() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 6점",
            "3건 이상 6점",
            "기준 B 4점",
            "2건 이상 4점",
            "③ 공동계약으로 참여한 실적의 경우",
            "참여 비율에 따른 금액의 실적",
            "정량평가 총점 10점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A 6점",
            "evidence": anchor("기준 A 6점"),
            "recognition_conditions": [
                {
                    "literal": (
                        "공동계약으로 참여한 실적의 경우 "
                        "참여 비율에 따른 금액의 실적"
                    ),
                    "evidence": anchor("공동계약으로 참여한 실적의 경우"),
                }
            ],
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            "criterion_literal": "기준 B 4점",
            "max_points": 4,
            "evidence": anchor("기준 B 4점"),
            "cases": [
                split_case(
                    "2건 이상 4점",
                    operator="GTE",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                )
            ],
            "recognition_conditions": [],
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 10
    table["total_evidence"] = anchor("정량평가 총점 10점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status == "AVAILABLE", issue_codes(profile)
    condition = next(
        item
        for item in profile.available_candidates
        if item.criterion_id == "SPLIT-CELL-CASE-A"
    ).recognition_conditions[0]
    assert condition.evidence.quote == (
        "③ 공동계약으로 참여한 실적의 경우\n참여 비율에 따른 금액의 실적"
    )


@pytest.mark.parametrize(
    "external_line",
    [
        "최근 3년 이내 완료 실적만 인정",
        "1. 최근 3년 이내 완료 실적만 인정",
    ],
)
def test_split_hwp_plain_external_recognition_is_not_assigned_across_criteria(
    external_line: str,
) -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 6점",
            "3건 이상 6점",
            "기준 B 4점",
            "2건 이상 4점",
            external_line,
            "정량평가 총점 10점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A 6점",
            "evidence": anchor("기준 A 6점"),
            "recognition_conditions": [
                {
                    "literal": external_line,
                    "evidence": anchor(external_line),
                }
            ],
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            "criterion_literal": "기준 B 4점",
            "max_points": 4,
            "evidence": anchor("기준 B 4점"),
            "cases": [
                split_case(
                    "2건 이상 4점",
                    operator="GTE",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                )
            ],
            "recognition_conditions": [],
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 10
    table["total_evidence"] = anchor("정량평가 총점 10점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_split_hwp_shared_recognition_cannot_reuse_a_structural_header() -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 6점",
            "3건 이상 6점",
            "기준 B 4점",
            "2건 이상 4점",
            "정량평가 총점 10점",
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    shared_condition = {
        "literal": "기준 B 4점",
        "evidence": anchor("기준 B 4점"),
    }
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A 6점",
            "evidence": anchor("기준 A 6점"),
            "recognition_conditions": [shared_condition],
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            "criterion_literal": "기준 B 4점",
            "max_points": 4,
            "evidence": anchor("기준 B 4점"),
            "cases": [
                split_case(
                    "2건 이상 4점",
                    operator="GTE",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                )
            ],
            "recognition_conditions": [shared_condition],
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 10
    table["total_evidence"] = anchor("정량평가 총점 10점")

    profile = build(payload_with_table(table), source=source)

    assert profile.status != "AVAILABLE"
    assert "AMBIGUOUS_TABLE" in issue_codes(profile)


@pytest.mark.parametrize(
    ("footer_lines", "literal", "quote", "expected_available"),
    [
        (
            ["최근 3년 이내 완료 실적만 인정"],
            "최근 3년 이내 완료 실적만 인정",
            "최근 3년 이내 완료 실적만 인정",
            False,
        ),
        (
            ["① 최근 3년 이내", "완료 실적만 인정"],
            "최근 3년 이내 완료 실적만 인정",
            "최근 3년 이내",
            True,
        ),
    ],
)
def test_split_hwp_shared_recognition_after_total_requires_explicit_footnote(
    footer_lines: list[str],
    literal: str,
    quote: str,
    expected_available: bool,
) -> None:
    source = "\n".join(
        [
            "[HWP SECTION 0]",
            "기준 A 6점",
            "3건 이상 6점",
            "기준 B 4점",
            "2건 이상 4점",
            "정량평가 총점 10점",
            *footer_lines,
        ]
    )
    table = split_cell_case_table(
        cases=[
            split_case(
                "3건 이상 6점",
                operator="GTE",
                comparison_value=3,
                category_values=[],
                award_kind="POINTS",
                award_value=6,
                row_order=1,
            )
        ]
    )
    shared_condition = {
        "literal": literal,
        "evidence": anchor(quote),
    }
    table["criteria"][0].update(
        {
            "criterion_id": "SPLIT-CELL-CASE-A",
            "label": "기준 A",
            "criterion_literal": "기준 A 6점",
            "evidence": anchor("기준 A 6점"),
            "recognition_conditions": [shared_condition],
        }
    )
    second = json.loads(json.dumps(table["criteria"][0]))
    second.update(
        {
            "criterion_id": "SPLIT-CELL-CASE-B",
            "label": "기준 B",
            "criterion_literal": "기준 B 4점",
            "max_points": 4,
            "evidence": anchor("기준 B 4점"),
            "cases": [
                split_case(
                    "2건 이상 4점",
                    operator="GTE",
                    comparison_value=2,
                    category_values=[],
                    award_kind="POINTS",
                    award_value=4,
                    row_order=1,
                )
            ],
            "recognition_conditions": [shared_condition],
        }
    )
    table["criteria"].append(second)
    table["total_points"] = 10
    table["total_evidence"] = anchor("정량평가 총점 10점")

    profile = build(payload_with_table(table), source=source)

    if expected_available:
        assert profile.status == "AVAILABLE", issue_codes(profile)
        assert {
            item.recognition_conditions[0].evidence.quote
            for item in profile.available_candidates
        } == {"\n".join(footer_lines)}
    else:
        assert profile.status != "AVAILABLE"
        assert "AMBIGUOUS_TABLE" in issue_codes(profile)


def test_previous_split_cell_validator_record_is_rejected_as_stale() -> None:
    record = validate_quantitative_attachment_extraction(
        payload_with_table(),
        source_text=VALID_SOURCE,
        attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64,
        manifest_sha256="b" * 64,
    )
    stale = record.model_copy(
        update={
            "validator_version": "pai-loop-quantitative-attachment-validator-0.6.12"
        }
    )
    stale = stale.model_copy(
        update={
            "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(
                stale
            )
        }
    )

    profile = merge_validated_quantitative_records(
        [stale],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
    )

    assert profile.status == "INCOMPLETE"
    assert "VALIDATOR_VERSION_MISMATCH" in issue_codes(profile)
