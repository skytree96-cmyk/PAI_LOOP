"""SYN read-only fallback: identical inputs, narrow numeric loss, no history edits."""
from copy import deepcopy

import pytest

from pai_loop.models import NoticeVersion
from pai_loop.pps_enrichment import (
    _current_manifest_attempts, preserved_quantitative_read_proof,
    quantitative_record_proves_available,
)
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord, validated_quantitative_record_fingerprint,
)
from pai_loop.quantitative_scoring import _current_dynamic_quantitative_profile
from test_previous_processing_contract import (
    CURRENT, EXTRACTION, PROCESSING, LEGACY, PREVIOUS,
    demoted_same_generation_attempt, modern_range_notice,
)


def _copy_attempt(notice, old, *, number=3):
    return NoticeVersion(
        id=f"SYN-COPY-{number}", notice=notice, version_no=number,
        file_sha256=old.file_sha256, document_complete=True,
        extraction_status="ACCEPTED", extraction_confidence=1,
        source_payload=deepcopy(old.source_payload),
    )


def _record_update(version, **changes):
    record = ValidatedQuantitativeAttachmentRecord.model_validate(
        version.source_payload["quantitative_validation_record"]
    ).model_copy(update=changes)
    record = record.model_copy(update={
        "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(record),
    })
    version.source_payload["quantitative_validation_record"] = record.model_dump(mode="json")


def _read_attempt(notice, *, validate=True):
    values = _current_manifest_attempts(
        notice.versions, validate_accepted=validate, preserve_quantitative_proof=True,
    )[2]
    return list(values.values())


@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
@pytest.mark.parametrize("loss", ["numeric", "fingerprint"])
def test_same_input_read_preserves_proof_but_not_processing_history(contract, loss):
    notice, _, old, _ = modern_range_notice(contract)
    if loss == "numeric":
        newer = demoted_same_generation_attempt(notice, old, contract)
    else:
        newer = _copy_attempt(notice, old)
        newer.source_payload["quantitative_validation_record"]["validation_fingerprint_sha256"] = "f" * 64
    before = [deepcopy(version.source_payload) for version in notice.versions]
    assert list(_current_manifest_attempts(notice.versions, validate_accepted=False)[2].values()) == [newer]
    assert _read_attempt(notice) == _read_attempt(notice, validate=False) == [old]
    profile = _current_dynamic_quantitative_profile(notice)
    assert profile.status == "AVAILABLE"
    assert len(profile.available_candidates) == 1
    assert profile.document_bindings[0].document_sha256 == old.file_sha256
    assert [version.source_payload for version in notice.versions] == before


@pytest.mark.parametrize("change", [
    "record_missing", "source_hash_missing", "input_hash_missing", "invalid_digest",
    "file_bytes", "document_digest", "record_digest", "source_text", "analysis_input",
    "unknown_validator", "read_incomplete", "input_incomplete", "unaccepted", "version_incomplete",
])
def test_unproven_or_changed_input_cannot_restore_old_proof(change):
    notice, _, old, _ = modern_range_notice(CURRENT)
    newer = demoted_same_generation_attempt(notice, old, CURRENT)
    payload = newer.source_payload
    processing = payload["document_processing"]
    if change == "record_missing":
        payload["quantitative_validation_record"] = None
    elif change == "source_hash_missing":
        processing.pop("source_text_sha256")
    elif change == "input_hash_missing":
        processing.pop("analysis_input_sha256")
    elif change == "invalid_digest":
        processing["analysis_input_sha256"] = "SYN-not-a-digest"
    elif change == "file_bytes":
        newer.file_sha256 = payload["document_sha256"] = "e" * 64
        _record_update(newer, document_sha256="e" * 64)
    elif change == "document_digest":
        payload["document_sha256"] = "e" * 64
    elif change == "record_digest":
        _record_update(newer, document_sha256="e" * 64)
    elif change in {"source_text", "analysis_input"}:
        processing[change + "_sha256"] = "e" * 64
    elif change == "unknown_validator":
        payload["quantitative_validation_record"]["validator_version"] = "SYN-unreleased-validator"
    elif change in {"read_incomplete", "input_incomplete"}:
        processing["source_read_complete" if change == "read_incomplete" else "analysis_input_complete"] = False
    elif change == "unaccepted":
        payload["status"] = newer.extraction_status = "REVIEW"
    else:
        newer.document_complete = False
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert old not in _read_attempt(notice)
    assert not _current_dynamic_quantitative_profile(notice).available_candidates


@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
def test_guard_off_does_not_preserve_an_older_score(contract, monkeypatch):
    monkeypatch.setenv("PAI_LOOP_EXTRACTION_DEMOTION_GUARD", "off")
    notice, _, old, _ = modern_range_notice(contract)
    newer = demoted_same_generation_attempt(notice, old, contract)
    assert _read_attempt(notice) == [newer]
    assert not _current_dynamic_quantitative_profile(notice).available_candidates


@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
def test_unknown_latest_contract_is_a_barrier_even_before_current(contract):
    notice, _, old, _ = modern_range_notice(contract)
    newer = _copy_attempt(notice, old)
    newer.source_payload["prompt_version"] = "SYN-unsupported-new-contract"
    assert not _read_attempt(notice)
    assert not _current_dynamic_quantitative_profile(notice).available_candidates


@pytest.mark.parametrize("old_contract,new_contract", [
    (PROCESSING, EXTRACTION), (PROCESSING, CURRENT), (EXTRACTION, CURRENT),
])
def test_new_generation_demotion_does_not_restore_previous_generation(old_contract, new_contract):
    notice, _, old, _ = modern_range_notice(old_contract)
    newer = demoted_same_generation_attempt(notice, old, new_contract)
    assert _read_attempt(notice) == [newer]
    assert not _current_dynamic_quantitative_profile(notice).available_candidates


@pytest.mark.parametrize("barrier", ["review", "unsupported", "file_bytes", "analysis_input", "generation"])
def test_aba_history_cannot_jump_over_an_intermediate_barrier(barrier):
    notice, _, old, _ = modern_range_notice(CURRENT)
    middle = _copy_attempt(notice, old)
    latest = demoted_same_generation_attempt(notice, old, CURRENT)
    latest.version_no = 4
    if barrier == "review":
        middle.source_payload["status"] = middle.extraction_status = "REVIEW"
    elif barrier == "unsupported":
        middle.source_payload["prompt_version"] = "SYN-unsupported"
    elif barrier == "file_bytes":
        middle.file_sha256 = middle.source_payload["document_sha256"] = "e" * 64
        _record_update(middle, document_sha256="e" * 64)
    elif barrier == "analysis_input":
        middle.source_payload["document_processing"]["analysis_input_sha256"] = "e" * 64
    else:
        middle.source_payload.update(prompt_version=EXTRACTION.prompt, schema_version=EXTRACTION.schema,
                                     processing_version=EXTRACTION.processing)
        _record_update(middle, prompt_version=EXTRACTION.prompt, extraction_schema_version=EXTRACTION.schema,
                       validator_version=EXTRACTION.validator)
    assert preserved_quantitative_read_proof([old, latest, middle]) is None
    assert _read_attempt(notice) == [latest]


@pytest.mark.parametrize("state", ["NO_TABLE", "NOT_APPLICABLE", "source_gap", "changed_scope"])
def test_new_scope_information_is_not_discarded_as_numeric_demotion(state):
    notice, _, old, record = modern_range_notice(CURRENT)
    newer = demoted_same_generation_attempt(notice, old, CURRENT)
    if state in {"NO_TABLE", "NOT_APPLICABLE"}:
        _record_update(newer, status=state, tables=(), available_candidates=(), review_candidates=(), issues=(),
                       not_applicable_evidence=(record.available_candidates[0].evidence,) if state == "NOT_APPLICABLE" else ())
    elif state == "source_gap":
        newer.source_payload["result"] = {"missing_or_unreadable": ["SYN additional score table unreadable"]}
    else:
        raw = ValidatedQuantitativeAttachmentRecord.model_validate(newer.source_payload["quantitative_validation_record"])
        _record_update(newer, review_candidates=tuple(
            item.model_copy(update={"label": "SYN changed population"}) for item in raw.review_candidates
        ))
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert old not in _read_attempt(notice)
    assert not _current_dynamic_quantitative_profile(notice).available_candidates


@pytest.mark.parametrize("change", [
    "missing_result", "condition_added", "condition_dropped", "criterion_literal", "required_evidence",
])
def test_raw_recognition_and_source_context_cannot_be_discarded(change):
    notice, _, old, _ = modern_range_notice(CURRENT)
    newer = demoted_same_generation_attempt(notice, old, CURRENT)
    raw = newer.source_payload["result"]
    criterion = raw["quantitative_tables"][0]["criteria"][0]
    if change == "missing_result":
        newer.source_payload["result"] = None
    elif change == "condition_added":
        condition = deepcopy(criterion["recognition_conditions"][0])
        condition["literal"] = condition["evidence"]["quote"] = "SYN additional recognition condition"
        criterion["recognition_conditions"].append(condition)
    elif change == "condition_dropped":
        assert criterion["recognition_conditions"]
        criterion["recognition_conditions"] = []
    elif change == "criterion_literal":
        criterion["criterion_literal"] += " SYN narrower population"
    else:
        criterion["required_evidence"] = ["company.personnel.count"]
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert _read_attempt(notice) == [newer]
    assert not _current_dynamic_quantitative_profile(notice).available_candidates


def test_fingerprint_only_fallback_requires_identical_raw_extraction():
    notice, _, old, _ = modern_range_notice(CURRENT)
    newer = _copy_attempt(notice, old)
    newer.source_payload["quantitative_validation_record"]["validation_fingerprint_sha256"] = "f" * 64
    newer.source_payload["result"]["summary"] = "SYN changed source interpretation"
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert old not in _read_attempt(notice)
    assert not _current_dynamic_quantitative_profile(notice).available_candidates


def test_generic_case_ambiguity_without_numeric_mismatch_is_not_preserved():
    notice, _, old, _ = modern_range_notice(CURRENT)
    newer = demoted_same_generation_attempt(notice, old, CURRENT)
    record = ValidatedQuantitativeAttachmentRecord.model_validate(
        newer.source_payload["quantitative_validation_record"]
    )
    generic = "CASE_TABLE_NOT_DETERMINISTIC"
    issues = tuple(issue for issue in record.issues if issue.code == generic)
    assert issues
    _record_update(newer, issues=issues, review_candidates=tuple(
        item.model_copy(update={"issue_codes": (generic,)}) for item in record.review_candidates
    ))
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert old not in _read_attempt(notice)


def test_latest_available_candidate_remains_authoritative():
    notice, _, old, _ = modern_range_notice(CURRENT)
    newer = _copy_attempt(notice, old)
    assert quantitative_record_proves_available(
        newer, attachment_id=newer.source_payload["attachment_id"],
        current_manifest_sha256=newer.source_payload["current_manifest_sha256"],
    )
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert _read_attempt(notice) == [newer]


@pytest.mark.parametrize("contract", [LEGACY, PREVIOUS])
def test_legacy_records_never_opt_in_to_preservation(contract):
    notice, _, old, _ = modern_range_notice(contract)
    newer = demoted_same_generation_attempt(notice, old, contract)
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert old not in _read_attempt(notice)


def test_replaced_manifest_cannot_restore_old_binding():
    notice, metadata, old, _ = modern_range_notice(CURRENT)
    demoted_same_generation_attempt(notice, old, CURRENT)
    metadata.source_payload["attachment_manifest"][0]["file_name"] = "SYN replacement.pdf"
    assert not _read_attempt(notice)
    assert not _current_dynamic_quantitative_profile(notice).available_candidates
