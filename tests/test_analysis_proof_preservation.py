"""Analysis and quantitative reads preserve only the same synthetic input proof."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from pai_loop.analysis_pipeline import _select_source_versions
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.extraction_contracts import (
    CURRENT_EXTRACTION_CONTRACT as CURRENT,
    PREVIOUS_EXTRACTION_CONTRACT as EXTRACTION,
    PREVIOUS_PROCESSING_CONTRACT as PROCESSING,
)
from pai_loop.integrations.openai_extraction import ExtractionPayload, PROMPT_VERSION
from pai_loop.models import NoticeVersion
from pai_loop.quantitative_rule_extraction import (
    validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint,
)
from test_extraction_contract_compatibility import notice_fixture, source_payload
from test_previous_processing_contract import newer_attempt


def _reread(notice, old, contract, *, version_no=3, mode="demoted"):
    payload = deepcopy(old.source_payload)
    raw = deepcopy(payload["result"])
    _, source = source_payload(payload["attachment_id"])
    if mode == "no_table":
        raw["quantitative_tables"] = []
    elif mode != "available":
        raw["quantitative_tables"][0]["criteria"][0]["cases"][0]["award_value"] = 99
    if mode == "manual_scope":
        raw["quantitative_tables"][0]["criteria"][0]["ambiguity_reason"] = "SYN scope needs review"
    record = validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(raw), source_text=source,
        attachment_id=payload["attachment_id"], document_sha256=old.file_sha256,
        manifest_sha256=payload["current_manifest_sha256"],
    )
    record = record.model_copy(update={
        "prompt_version": contract.prompt,
        "extraction_schema_version": contract.schema,
        "validator_version": contract.validator,
    })
    record = record.model_copy(update={
        "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(record),
    })
    payload.update(
        prompt_version=contract.prompt, schema_version=contract.schema,
        processing_version=contract.processing, result=raw,
        quantitative_validation_record=record.model_dump(mode="json"),
    )
    return NoticeVersion(
        id=f"SYN-REREAD-{version_no}", notice=notice, version_no=version_no,
        file_sha256=old.file_sha256, document_complete=True,
        extraction_status="ACCEPTED", extraction_confidence=0.99,
        source_payload=payload, created_at=datetime(2026, 2, version_no, tzinfo=timezone.utc),
    )


def _selected(notice, *, source_version_ids=None):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with build_session_factory(engine)() as session:
        session.add(notice)
        session.commit()
        selected = _select_source_versions(
            session, notice_id=notice.id, prompt_version=PROMPT_VERSION,
            source_version_ids=source_version_ids,
        )
        result = [version.id for version in selected]
    engine.dispose()
    return result


@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
def test_analysis_preserves_proof_only_for_same_input_numeric_demotion(contract):
    notice, _, old, _ = notice_fixture(contract=contract)
    newest = _reread(notice, old, contract)
    before = deepcopy((old.source_payload, newest.source_payload))
    assert _selected(notice) == [old.id]
    assert (old.source_payload, newest.source_payload) == before


@pytest.mark.parametrize("changed", [
    "file_sha256", "document_sha256", "source_text_sha256", "analysis_input_sha256",
    "missing_source_hash", "missing_input_hash", "source_incomplete", "input_incomplete",
    "document_incomplete", "scope_warning", "processing_missing",
])
@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
def test_analysis_never_preserves_across_changed_or_unproven_input(changed, contract):
    notice, _, old, _ = notice_fixture(contract=contract)
    newest = _reread(notice, old, contract)
    payload = newest.source_payload
    processing = payload["document_processing"]
    if changed == "file_sha256":
        newest.file_sha256 = "e" * 64
    elif changed == "document_sha256":
        payload["document_sha256"] = "e" * 64
    elif changed in {"source_text_sha256", "analysis_input_sha256"}:
        processing[changed] = "e" * 64
    elif changed == "missing_source_hash":
        processing.pop("source_text_sha256")
    elif changed == "missing_input_hash":
        processing.pop("analysis_input_sha256")
    elif changed == "source_incomplete":
        processing["source_read_complete"] = False
    elif changed == "input_incomplete":
        processing["analysis_input_complete"] = False
    elif changed == "document_incomplete":
        newest.document_complete = False
    elif changed == "scope_warning":
        payload["result"]["missing_or_unreadable"] = ["SYN source condition missing"]
    else:
        payload.pop("document_processing")
    assert old.id not in _selected(notice)


@pytest.mark.parametrize("mode", ["no_table", "manual_scope", "available"])
@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
def test_no_table_manual_scope_or_new_available_rule_is_not_a_numeric_demotion(mode, contract):
    notice, _, old, _ = notice_fixture(contract=contract)
    newest = _reread(notice, old, contract, mode=mode)
    assert _selected(notice) == [newest.id]


@pytest.mark.parametrize("old_contract,new_contract", [
    (PROCESSING, EXTRACTION), (PROCESSING, CURRENT), (EXTRACTION, CURRENT),
])
def test_new_released_generation_keeps_its_boundary(old_contract, new_contract):
    notice, _, old, _ = notice_fixture(contract=old_contract)
    newest = _reread(notice, old, new_contract)
    assert _selected(notice) == [newest.id]


@pytest.mark.parametrize("barrier", ["source_hash", "input_hash", "unknown_contract", "review", "missing_record"])
def test_intermediate_history_barrier_prevents_same_input_aba_fallback(barrier):
    notice, _, old, _ = notice_fixture(contract=CURRENT)
    middle = _reread(notice, old, CURRENT)
    newest = _reread(notice, old, CURRENT, version_no=4)
    if barrier == "source_hash":
        middle.source_payload["document_processing"]["source_text_sha256"] = "e" * 64
    elif barrier == "input_hash":
        middle.source_payload["document_processing"]["analysis_input_sha256"] = "e" * 64
    elif barrier == "unknown_contract":
        middle.source_payload["prompt_version"] = "SYN unknown contract"
    elif barrier == "review":
        middle.source_payload["status"] = "REVIEW"
        middle.extraction_status = "REVIEW"
    else:
        middle.source_payload["quantitative_validation_record"] = None
    assert _selected(notice) == [newest.id]


def test_changed_authoritative_manifest_excludes_old_proof():
    notice, metadata, old, _ = notice_fixture(contract=CURRENT)
    _reread(notice, old, CURRENT)
    metadata.source_payload["attachment_manifest"][0]["file_name"] = "SYN replacement.pdf"
    assert _selected(notice) == []


def test_explicit_old_only_audit_selection_does_not_expand_to_newer_history():
    notice, _, old, _ = notice_fixture(contract=CURRENT)
    newer = _reread(notice, old, CURRENT)
    newer.source_payload["document_processing"]["source_text_sha256"] = "e" * 64
    assert _selected(notice, source_version_ids=[old.id]) == [old.id]


@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
def test_unknown_latest_contract_blocks_all_older_automatic_sources(contract):
    notice, _, old, _ = notice_fixture(contract=contract)
    newest = _reread(notice, old, contract)
    newest.source_payload["prompt_version"] = "SYN future unsupported contract"
    assert _selected(notice) == []


def test_explicit_old_only_audit_selection_keeps_its_contract_override():
    notice, _, old, _ = notice_fixture(contract=CURRENT)
    newest = _reread(notice, old, CURRENT)
    newest.source_payload["prompt_version"] = "SYN future unsupported contract"
    assert _selected(notice, source_version_ids=[old.id]) == [old.id]


def test_unbound_other_attachment_cannot_create_a_generation_barrier():
    notice, _, old, _ = notice_fixture(contract=CURRENT)
    newest = _reread(notice, old, CURRENT)
    newest.source_payload["prompt_version"] = "SYN future unsupported contract"
    newest.source_payload["attachment_id"] = "SYN-OTHER-ATTACHMENT"
    assert _selected(notice) == [old.id]


def test_deployment_flag_off_keeps_latest_attempt(monkeypatch):
    monkeypatch.setenv("PAI_LOOP_EXTRACTION_DEMOTION_GUARD", "off")
    notice, _, old, _ = notice_fixture(contract=CURRENT)
    newest = _reread(notice, old, CURRENT)
    assert _selected(notice) == [newest.id]


@pytest.mark.parametrize("contract", [EXTRACTION, PROCESSING])
def test_same_native_document_invalid_record_fallback_is_unchanged(contract):
    notice, _, old, _ = notice_fixture(contract=contract)
    newer_attempt(notice, old, contract, "INVALID_ACCEPTED")
    assert _selected(notice) == [old.id]


@pytest.mark.parametrize("digest", ["", "SYN-invalid-digest", "f" * 64])
@pytest.mark.parametrize("contract", [EXTRACTION, PROCESSING])
def test_invalid_newer_record_never_falls_back_to_a_different_or_unknown_native_file(digest, contract):
    notice, _, old, _ = notice_fixture(contract=contract)
    newest = newer_attempt(notice, old, contract, "INVALID_ACCEPTED")
    newest.file_sha256 = digest
    assert old.id not in _selected(notice)
