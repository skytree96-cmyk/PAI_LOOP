from __future__ import annotations

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord,
    build_quantitative_candidate_profile,
    merge_validated_quantitative_records,
    validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint,
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

    tampered = record.model_copy(update={"status": "REVIEW"})
    altered = merge_validated_quantitative_records(
        [tampered],
        expected_documents={ATTACHMENT_ID: "a" * 64},
        manifest_sha256="b" * 64,
    )
    assert altered.status == "INCOMPLETE"
    assert "VALIDATION_FINGERPRINT_MISMATCH" in issue_codes(altered)
