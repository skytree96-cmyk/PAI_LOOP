"""Synthetic exact processing-predecessor proofs; no real sources or network."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from itertools import product

import pytest
from sqlalchemy import select, func

from pai_loop.extraction_contracts import (
    CURRENT_EXTRACTION_CONTRACT as CURRENT,
    PREVIOUS_PROCESSING_CONTRACT as PROCESSING,
    PREVIOUS_CASE_CONTRACT as PREVIOUS,
    LEGACY_CASE_CONTRACT as LEGACY,
    EXTRACTION_READ_POLICY_VERSION,
    classify_attempt_header, classify_record_contract,
)
from pai_loop.integrations.openai_extraction import ExtractionPayload, ExtractionOutcome, PROMPT_VERSION
from pai_loop.quantitative_rule_extraction import validate_quantitative_attachment_extraction, validated_quantitative_record_fingerprint
from pai_loop.quantitative_scoring import _current_dynamic_quantitative_profile, quantitative_request_from_candidate_profile
from pai_loop.pps_enrichment import (
    _current_manifest_attempts, public_analysis_reason, safe_public_bound_extraction,
    safe_public_live_extraction, _persist_extraction_version, _stored_attachment_result,
    _accepted_outcome_for_duplicate_content,
)
from pai_loop.analysis_pipeline import _parse_source, _select_source_versions
from pai_loop.recovery_diagnostics import _attachment_projection
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import NoticeVersion
from test_extraction_contract_compatibility import notice_fixture, usable, merge
from test_quantitative_count_ranges import fixture as range_fixture


CONTRACTS = (CURRENT, PROCESSING, PREVIOUS, LEGACY)
KINDS = ('CURRENT', 'EXACT_PREVIOUS_PROCESSING', 'LEGACY_CASE_V2', 'LEGACY_CASE_V1')


@pytest.mark.parametrize('parts', product(range(4), repeat=4))
def test_only_four_exact_released_contract_tuples_are_readable(parts):
    values = tuple(getattr(CONTRACTS[index], field) for index, field in zip(parts, ('prompt', 'schema', 'validator', 'processing')))
    payload = dict(prompt_version=values[0], schema_version=values[1], processing_version=values[3])
    record = dict(prompt_version=values[0], extraction_schema_version=values[1], validator_version=values[2])
    expected = next((kind for contract, kind in zip(CONTRACTS, KINDS) if tuple(contract) == values), 'UNSUPPORTED')
    assert classify_record_contract(payload, record) == expected


def modern_range_notice():
    notice, metadata, attempt, _ = notice_fixture(contract=PROCESSING)
    aid = attempt.source_payload['attachment_id']
    raw, source = range_fixture(inline=True)
    def bind(value):
        if isinstance(value, dict):
            return {key: aid if key == 'attachment_id' else bind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [bind(item) for item in value]
        return value
    raw = bind(raw)
    source_sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(ExtractionPayload.model_validate(raw),
        source_text=source, attachment_id=aid, document_sha256=source_sha,
        manifest_sha256=attempt.source_payload['current_manifest_sha256'])
    attempt.file_sha256 = source_sha
    attempt.source_payload.update(document_sha256=source_sha, result=raw,
        quantitative_validation_record=record.model_dump(mode='json'))
    attempt.source_payload['document_processing'].update(source_text_sha256=source_sha, analysis_input_sha256=source_sha)
    return notice, metadata, attempt, record


def test_previous_processing_retains_modern_ranges_submission_and_original_fingerprint():
    notice, metadata, attempt, record = modern_range_notice()
    original = deepcopy(attempt.source_payload)
    assert CURRENT.processing == 'pps-document-processing-0.5.2'
    assert PROCESSING.processing == 'pps-document-processing-0.5.1'
    assert CURRENT[:3] == PROCESSING[:3]
    assert EXTRACTION_READ_POLICY_VERSION == 'exact-case-contract-read-v3'
    assert classify_attempt_header(original) == 'EXACT_PREVIOUS_PROCESSING'
    assert record.status == 'AVAILABLE'
    assert usable(attempt, record)
    assert validated_quantitative_record_fingerprint(record) == record.validation_fingerprint_sha256
    profile = merge(attempt, record)
    assert [row.operator for row in profile.available_candidates[0].cases] == ['GTE', 'BETWEEN', 'BETWEEN', 'BETWEEN', 'NOT_SUBMITTED']
    assert _current_manifest_attempts(notice.versions)[2][record.attachment_id] is attempt
    assert _current_dynamic_quantitative_profile(notice).status == 'AVAILABLE'
    assert quantitative_request_from_candidate_profile(profile).activation_status == 'AUTO_ACTIVE'
    assert _parse_source(attempt, prompt_version=PROMPT_VERSION, allow_compatible_pps=True).materializable
    assert public_analysis_reason(notice.versions).state == 'ANALYZED'
    assert safe_public_live_extraction(attempt.source_payload) is None
    assert safe_public_bound_extraction(attempt.source_payload, notice.versions) is not None
    diagnostic = _attachment_projection(1, metadata.source_payload['attachment_manifest'][0], attempt,
        {'state': 'ANALYZED', 'reason_code': 'ANALYZED'})
    assert diagnostic.attempt_contract == 'EXACT_PREVIOUS_PROCESSING'
    assert _stored_attachment_result(attempt, attachments_discovered=1).openai_calls == 0
    assert attempt.source_payload == original


@pytest.mark.parametrize('options', [{'gap': True}, {'neutral': True}])
def test_previous_processing_does_not_promote_review_or_no_table(options):
    notice, _, attempt, record = notice_fixture(contract=PROCESSING, **options)
    original = deepcopy(attempt.source_payload)
    assert usable(attempt, record)
    assert _current_manifest_attempts(notice.versions)[2][record.attachment_id] is attempt
    assert merge(attempt, record).status != 'AVAILABLE'
    assert attempt.source_payload == original


@pytest.mark.parametrize('mutation', ['fingerprint', 'file_digest', 'manifest', 'unknown_schema', 'unsupported_055'])
def test_previous_processing_never_accepts_tampered_or_mixed_proof(mutation):
    notice, metadata, attempt, record = modern_range_notice()
    if mutation == 'fingerprint':
        attempt.source_payload['quantitative_validation_record']['validation_fingerprint_sha256'] = '0' * 64
    elif mutation == 'file_digest':
        attempt.file_sha256 = '0' * 64
    elif mutation == 'manifest':
        metadata.source_payload['attachment_manifest'][0]['file_name'] = 'SYN 다른 원문.pdf'
    elif mutation == 'unknown_schema':
        attempt.source_payload['schema_version'] = 'SYN-unknown-schema'
    else:
        attempt.source_payload['prompt_version'] = 'pai-loop-extraction-0.5.5'
    assert not _current_manifest_attempts(notice.versions)[2]
    assert safe_public_bound_extraction(attempt.source_payload, notice.versions) is None


def newer_attempt(notice, old, contract, status):
    payload = deepcopy(old.source_payload)
    payload.update(prompt_version=contract.prompt, schema_version=contract.schema, processing_version=contract.processing,
        status='REVIEW' if status == 'REVIEW' else 'ACCEPTED', error_code='SCHEMA_VALIDATION_ERROR',
        result=None, quantitative_validation_record=None)
    if status == 'UNKNOWN_HEADER': payload['prompt_version'] = 'SYN-unknown-new-generation'
    return NoticeVersion(id='SYN-NEW-PROCESSING', notice=notice, version_no=3,
        file_sha256=old.file_sha256, document_complete=False, extraction_status=payload['status'],
        extraction_confidence=0, source_payload=payload, created_at=datetime.now(timezone.utc))


@pytest.mark.parametrize('status', ['REVIEW', 'INVALID_ACCEPTED', 'UNKNOWN_HEADER'])
def test_new_processing_generation_does_not_resurrect_previous_source(status):
    notice, _, old, _ = modern_range_notice()
    newer = newer_attempt(notice, old, CURRENT, status)
    attempts = _current_manifest_attempts(notice.versions)[2]
    assert old not in attempts.values()
    assert _current_dynamic_quantitative_profile(notice).status != 'AVAILABLE'
    engine = build_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        selected = _select_source_versions(session, notice_id=notice.id, prompt_version=PROMPT_VERSION, source_version_ids=None)
        assert old.id not in [row.id for row in selected]
    finally:
        session.close()
        engine.dispose()


def test_previous_processing_keeps_same_generation_valid_fallback():
    notice, _, old, _ = modern_range_notice()
    newer_attempt(notice, old, PROCESSING, 'INVALID_ACCEPTED')
    assert list(_current_manifest_attempts(notice.versions)[2].values()) == [old]
    engine = build_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        selected = _select_source_versions(session, notice_id=notice.id, prompt_version=PROMPT_VERSION, source_version_ids=None)
        assert [row.id for row in selected] == [old.id]
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize('contract', [LEGACY, PREVIOUS])
def test_previous_processing_invalid_attempt_still_blocks_old_case_generations(contract):
    notice, _, old, _ = notice_fixture(contract=contract)
    newer_attempt(notice, old, PROCESSING, 'INVALID_ACCEPTED')
    assert not _current_manifest_attempts(notice.versions)[2]


def test_new_writes_use_only_new_processing_without_mutating_previous_review():
    notice, metadata, old, _ = notice_fixture(contract=PROCESSING)
    old.source_payload.update(status='REVIEW', error_code='UNSUPPORTED_ATTACHMENT_TYPE', result=None, quantitative_validation_record=None)
    old.document_complete = False
    old.extraction_status = 'REVIEW'
    original = deepcopy(old.source_payload)
    engine = build_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        outcome = ExtractionOutcome(status='REVIEW', message='SYN deterministic review', review_code='R07',
            error_code='UNSUPPORTED_ATTACHMENT_TYPE', api_calls=0)
        params = dict(notice_id=notice.id, attachment=metadata.source_payload['attachment_manifest'][0],
            manifest_sha256=old.source_payload['manifest_sha256'], current_manifest_sha256=old.source_payload['current_manifest_sha256'],
            document_sha256=old.file_sha256, outcome=outcome, error_code=None)
        current = _persist_extraction_version(session, **params)
        assert current.id != old.id
        assert current.source_payload['processing_version'] == CURRENT.processing
        assert classify_attempt_header(current.source_payload) == 'CURRENT'
        assert _persist_extraction_version(session, **params).id == current.id
        assert old.source_payload == original
        assert session.scalar(select(func.count()).select_from(NoticeVersion)) == 3
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize('changed_hash', [None, 'document_sha256', 'source_text_sha256', 'analysis_input_sha256'])
def test_processing_upgrade_reuse_requires_identical_bytes_source_and_model_input(changed_hash):
    notice, _, attempt, record = modern_range_notice()
    original = deepcopy(attempt.source_payload)
    engine = build_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        params = dict(notice_id=notice.id, attachment_id=record.attachment_id,
            document_sha256=record.document_sha256, source_text_sha256=record.document_sha256,
            analysis_input_sha256=record.document_sha256)
        if changed_hash:
            params[changed_hash] = '0' * 64
        reused = _accepted_outcome_for_duplicate_content(session, **params)
        if changed_hash:
            assert reused is None
        else:
            assert reused is not None and reused.api_calls == 0
            assert reused.data.model_dump(mode='json') == original['result']
        assert attempt.source_payload == original
        assert session.scalar(select(func.count()).select_from(NoticeVersion)) == 2
    finally:
        session.close()
        engine.dispose()
