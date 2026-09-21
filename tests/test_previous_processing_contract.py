"""Synthetic exact processing-predecessor proofs; no real sources or network."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from itertools import product

import pytest
from sqlalchemy import select, func

from pai_loop.extraction_contracts import (
    CURRENT_EXTRACTION_CONTRACT as CURRENT,
    PREVIOUS_EXTRACTION_CONTRACT as EXTRACTION,
    PREVIOUS_PROCESSING_CONTRACT as PROCESSING,
    PREVIOUS_CASE_CONTRACT as PREVIOUS,
    LEGACY_CASE_CONTRACT as LEGACY,
    EXTRACTION_READ_POLICY_VERSION,
    classify_attempt_header, classify_record_contract,
)
from pai_loop.integrations.openai_extraction import ExtractionPayload, ExtractionOutcome, PROMPT_VERSION
from pai_loop.quantitative_rule_extraction import ValidatedQuantitativeAttachmentRecord, validate_quantitative_attachment_extraction, validated_quantitative_record_fingerprint
from pai_loop.quantitative_scoring import _current_dynamic_quantitative_profile, quantitative_request_from_candidate_profile
from pai_loop.pps_enrichment import (
    _current_manifest_attempts, public_analysis_reason, safe_public_bound_extraction,
    safe_public_live_extraction, _persist_extraction_version, _stored_attachment_result,
    _accepted_outcome_for_duplicate_content, enrich_notice_from_pps,
)
from pai_loop.analysis_pipeline import _parse_source, _select_source_versions
from pai_loop.recovery_diagnostics import _attachment_projection
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import NoticeVersion
from test_extraction_contract_compatibility import notice_fixture, usable, merge
from test_quantitative_count_ranges import fixture as range_fixture


CONTRACTS = (CURRENT, EXTRACTION, PROCESSING, PREVIOUS, LEGACY)
KINDS = ('CURRENT', 'EXACT_PREVIOUS_EXTRACTION', 'EXACT_PREVIOUS_PROCESSING', 'LEGACY_CASE_V2', 'LEGACY_CASE_V1')


@pytest.mark.parametrize('parts', product(range(len(CONTRACTS)), repeat=4))
def test_only_exact_released_contract_tuples_are_readable(parts):
    values = tuple(getattr(CONTRACTS[index], field) for index, field in zip(parts, ('prompt', 'schema', 'validator', 'processing')))
    payload = dict(prompt_version=values[0], schema_version=values[1], processing_version=values[3])
    record = dict(prompt_version=values[0], extraction_schema_version=values[1], validator_version=values[2])
    expected = next((kind for contract, kind in zip(CONTRACTS, KINDS) if tuple(contract) == values), 'UNSUPPORTED')
    assert classify_record_contract(payload, record) == expected


def modern_range_notice(contract=PROCESSING):
    notice, metadata, attempt, _ = notice_fixture(contract=contract)
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
    # Synthetic historical proof: reads must retain this exact old fingerprint.
    record = record.model_copy(update=dict(prompt_version=contract.prompt,
        extraction_schema_version=contract.schema, validator_version=contract.validator))
    record = record.model_copy(update=dict(
        validation_fingerprint_sha256=validated_quantitative_record_fingerprint(record)))
    attempt.file_sha256 = source_sha
    attempt.source_payload.update(document_sha256=source_sha, result=raw,
        quantitative_validation_record=record.model_dump(mode='json'))
    attempt.source_payload['document_processing'].update(source_text_sha256=source_sha, analysis_input_sha256=source_sha)
    return notice, metadata, attempt, record


@pytest.mark.parametrize('contract,kind', [
    (EXTRACTION, 'EXACT_PREVIOUS_EXTRACTION'),
    (PROCESSING, 'EXACT_PREVIOUS_PROCESSING'),
])
def test_modern_predecessors_retain_ranges_submission_and_original_fingerprint(contract, kind):
    notice, metadata, attempt, record = modern_range_notice(contract)
    original = deepcopy(attempt.source_payload)
    assert CURRENT.processing == 'pps-document-processing-0.5.2'
    assert PROCESSING.processing == 'pps-document-processing-0.5.1'
    assert EXTRACTION[:3] == PROCESSING[:3]
    assert CURRENT.prompt == 'pai-loop-extraction-0.5.8'
    assert CURRENT.validator == 'pai-loop-quantitative-attachment-validator-0.6.21'
    assert EXTRACTION_READ_POLICY_VERSION == 'exact-case-contract-read-v4'
    assert classify_attempt_header(original) == kind
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
    assert diagnostic.attempt_contract == kind
    assert _stored_attachment_result(attempt, attachments_discovered=1).openai_calls == 0
    assert attempt.source_payload == original


@pytest.mark.parametrize('options', [{'gap': True}, {'neutral': True}])
@pytest.mark.parametrize('contract', [EXTRACTION, PROCESSING])
def test_modern_predecessors_do_not_promote_review_or_no_table(options, contract):
    notice, _, attempt, record = notice_fixture(contract=contract, **options)
    original = deepcopy(attempt.source_payload)
    assert usable(attempt, record)
    assert _current_manifest_attempts(notice.versions)[2][record.attachment_id] is attempt
    assert merge(attempt, record).status != 'AVAILABLE'
    assert attempt.source_payload == original


@pytest.mark.parametrize('contract', [EXTRACTION, PROCESSING, PREVIOUS, LEGACY])
@pytest.mark.parametrize('options', [{}, {'gap': True}, {'neutral': True}])
def test_ordinary_enrichment_reuses_predecessor_without_download_model_or_new_row(monkeypatch, contract, options):
    import pai_loop.pps_enrichment as enrichment

    def forbidden(*args, **kwargs):
        pytest.fail('a readable predecessor must not download or invoke a model')

    monkeypatch.setattr(enrichment, 'download_public_attachment', forbidden)
    notice, _, attempt, _ = notice_fixture(contract=contract, **options)
    original = deepcopy(attempt.source_payload)
    notice_id, attempt_id = notice.id, attempt.id
    engine = build_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        result = enrich_notice_from_pps(session, notice_id=notice_id,
            openai_api_key=None, openai_model='SYN-no-model',
            openai_client_factory=forbidden)
        assert result.openai_calls == 0
        assert result.version_id == attempt_id
        assert session.scalar(select(func.count()).select_from(NoticeVersion)) == 2
        assert session.get(NoticeVersion, attempt_id).source_payload == original
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize('mutation', ['fingerprint', 'file_digest', 'manifest', 'unknown_schema', 'unsupported_055'])
@pytest.mark.parametrize('contract', [EXTRACTION, PROCESSING])
def test_modern_predecessors_never_accept_tampered_or_mixed_proof(mutation, contract):
    notice, metadata, attempt, record = modern_range_notice(contract)
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
@pytest.mark.parametrize('old_contract,new_contract', [
    (PROCESSING, CURRENT), (PROCESSING, EXTRACTION), (EXTRACTION, CURRENT),
])
def test_new_generation_does_not_resurrect_previous_source(status, old_contract, new_contract):
    notice, _, old, _ = modern_range_notice(old_contract)
    newer = newer_attempt(notice, old, new_contract, status)
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


@pytest.mark.parametrize('contract', [EXTRACTION, PROCESSING])
def test_modern_predecessor_keeps_same_generation_valid_fallback(contract):
    notice, _, old, _ = modern_range_notice(contract)
    newer_attempt(notice, old, contract, 'INVALID_ACCEPTED')
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
@pytest.mark.parametrize('contract', [CURRENT, EXTRACTION, PROCESSING])
def test_duplicate_reuse_requires_current_prompt_and_identical_bytes_source_and_input(changed_hash, contract):
    notice, _, attempt, record = modern_range_notice(contract)
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
        if changed_hash or contract != CURRENT:
            assert reused is None
        else:
            assert reused is not None and reused.api_calls == 0
            assert reused.data.model_dump(mode='json') == original['result']
        assert attempt.source_payload == original
        assert session.scalar(select(func.count()).select_from(NoticeVersion)) == 2
    finally:
        session.close()
        engine.dispose()


def demoted_same_generation_attempt(notice, old, contract):
    """A second pass of the SAME generation that kept nothing activatable.

    The document extraction still succeeded and its record still binds, so
    none of the invalid-attempt fallbacks apply; only the activatable rules
    are gone.
    """

    payload = deepcopy(old.source_payload)
    aid = payload['attachment_id']
    # Build the losing re-read through the real validator rather than by hand:
    # one award number no longer matches its own literal, which is the exact
    # shape production produced (CASE_NUMBER_MISMATCH on a rating row).
    raw, source = range_fixture(inline=True)

    def bind(value):
        if isinstance(value, dict):
            return {k: aid if k == 'attachment_id' else bind(v) for k, v in value.items()}
        if isinstance(value, list):
            return [bind(item) for item in value]
        return value

    raw = bind(raw)
    raw['quantitative_tables'][0]['criteria'][0]['cases'][0]['award_value'] = 99
    source_sha = hashlib.sha256(source.encode()).hexdigest()
    record = validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(raw), source_text=source, attachment_id=aid,
        document_sha256=source_sha,
        manifest_sha256=payload['current_manifest_sha256'],
    )
    record = record.model_copy(update=dict(
        prompt_version=contract.prompt, extraction_schema_version=contract.schema,
        validator_version=contract.validator,
    ))
    record = record.model_copy(update=dict(
        validation_fingerprint_sha256=validated_quantitative_record_fingerprint(record)))
    assert not record.available_candidates
    record = record.model_dump(mode='json')
    payload.update(
        prompt_version=contract.prompt, schema_version=contract.schema,
        processing_version=contract.processing, status='ACCEPTED',
        quantitative_validation_record=record, document_sha256=old.file_sha256, result=None,
    )
    return NoticeVersion(
        id='SYN-DEMOTED-SAME-GENERATION', notice=notice, version_no=3,
        file_sha256=old.file_sha256, document_complete=True, extraction_status='ACCEPTED',
        extraction_confidence=0, source_payload=payload,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize('contract', [EXTRACTION, PROCESSING])
def test_same_generation_rerun_does_not_drop_a_proven_rule(contract):
    """Re-reading one document is not deterministic; a lost rule is not news.

    The earlier pass proved an activatable rule from the same bytes and the
    same generation. Letting the later pass replace it would discard evidence
    we already paid for, with no new information to justify it.
    """

    notice, _, old, _ = modern_range_notice(contract)
    demoted = demoted_same_generation_attempt(notice, old, contract)

    # Known gap: _current_manifest_attempts still answers with the newest
    # attempt, so the stored profile keeps reading the demoted one. Only the
    # analysis source selection is guarded here.
    assert list(_current_manifest_attempts(notice.versions)[2].values()) == [demoted]
    engine = build_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        selected = _select_source_versions(
            session, notice_id=notice.id, prompt_version=PROMPT_VERSION,
            source_version_ids=None,
        )
        assert old.id in [row.id for row in selected]
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize('contract', [EXTRACTION, PROCESSING])
def test_demotion_guard_can_be_disabled_by_a_deployment_flag(contract, monkeypatch):
    monkeypatch.setenv('PAI_LOOP_EXTRACTION_DEMOTION_GUARD', 'off')
    notice, _, old, _ = modern_range_notice(contract)
    demoted = demoted_same_generation_attempt(notice, old, contract)

    engine = build_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        session.add(notice)
        session.commit()
        selected = _select_source_versions(
            session, notice_id=notice.id, prompt_version=PROMPT_VERSION,
            source_version_ids=None,
        )
        assert demoted.id in [row.id for row in selected]
        assert old.id not in [row.id for row in selected]
    finally:
        session.close()
        engine.dispose()
