"""SYN: a retry that never re-read the document cannot erase its accepted read."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from pai_loop.analysis_pipeline import _select_source_versions
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.integrations.openai_extraction import PROMPT_VERSION
from pai_loop.models import NoticeVersion
from pai_loop.pps_enrichment import (
    _current_manifest_attempts, preserved_quantitative_read_proof,
)
from pai_loop.quantitative_scoring import _current_dynamic_quantitative_profile
from test_previous_processing_contract import CURRENT, EXTRACTION, PROCESSING, modern_range_notice


def _failed(notice, old, *, code="ATTACHMENT_NETWORK_ERROR", number=3, download=True,
            contract=None, file_sha256=None):
    payload = deepcopy(old.source_payload)
    marker = "d" * 64
    sha = file_sha256 or (marker if download else old.file_sha256)
    payload.update(status="REVIEW", review_code="R07", error_code=code, result=None,
                   document_sha256=sha, quantitative_validation_record=None)
    if contract is not None:
        payload.update(prompt_version=contract.prompt, schema_version=contract.schema,
                       processing_version=contract.processing)
    payload["document_processing"] = {
        **payload["document_processing"],
        "document_digest_basis": "FAILED_DOWNLOAD_MARKER" if download else "DOWNLOADED_BYTES",
        "source_read_complete": False, "analysis_input_complete": False,
    }
    return NoticeVersion(
        id=f"SYN-FAILED-{number}", notice=notice, version_no=number, file_sha256=sha,
        document_complete=False, extraction_status="REVIEW", extraction_confidence=0,
        source_payload=payload, created_at=datetime(2026, 10, 1, number, tzinfo=timezone.utc),
    )


def _read(notice):
    return list(_current_manifest_attempts(
        notice.versions, validate_accepted=False, preserve_quantitative_proof=True,
    )[2].values())


def _selected(notice):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with build_session_factory(engine)() as session:
        session.add(notice)
        session.commit()
        ids = [row.id for row in _select_source_versions(
            session, notice_id=notice.id, prompt_version=PROMPT_VERSION, source_version_ids=None,
        )]
    engine.dispose()
    return ids


@pytest.mark.parametrize("code,download", [
    ("ATTACHMENT_NETWORK_ERROR", True), ("ATTACHMENT_TOO_LARGE", True),
    ("HTTP_ERROR", False), ("SCHEMA_VALIDATION_ERROR", False), ("UNVERIFIED_QUOTE", False),
    ("INTERNAL_ENRICHMENT_ERROR", False),
])
@pytest.mark.parametrize("contract", [CURRENT, EXTRACTION, PROCESSING])
def test_evidence_free_failures_keep_the_accepted_read(code, download, contract):
    notice, _, old, _ = modern_range_notice(contract)
    first = _failed(notice, old, code=code, download=download)
    second = _failed(notice, old, code="ATTACHMENT_NETWORK_ERROR", number=4)
    before = [deepcopy(version.source_payload) for version in notice.versions]
    assert preserved_quantitative_read_proof([old, first, second]) is old
    assert _read(notice) == [old]
    assert _current_dynamic_quantitative_profile(notice).status == "AVAILABLE"
    assert old.id in _selected(notice)
    assert [version.source_payload for version in notice.versions] == before


@pytest.mark.parametrize("barrier", [
    "content_failure", "other_bytes", "new_generation", "unknown_contract",
    "review_without_code", "unlisted_code",
])
def test_failures_that_carry_document_evidence_remain_barriers(barrier):
    notice, _, old, _ = modern_range_notice(PROCESSING)
    if barrier == "content_failure":
        newer = _failed(notice, old, code="DOCUMENT_TEXT_EMPTY", download=False)
    elif barrier == "other_bytes":
        newer = _failed(notice, old, code="HTTP_ERROR", download=False, file_sha256="e" * 64)
    elif barrier == "new_generation":
        newer = _failed(notice, old, code="ATTACHMENT_NETWORK_ERROR", contract=CURRENT)
    elif barrier == "unknown_contract":
        newer = _failed(notice, old)
        newer.source_payload["prompt_version"] = "SYN-unsupported"
    elif barrier == "review_without_code":
        newer = _failed(notice, old, download=False)
        newer.source_payload["error_code"] = None
    else:
        newer = _failed(notice, old, code="XLS_PARSE_FAILED", download=False)
    assert preserved_quantitative_read_proof([old, newer]) is None
    assert old not in _read(notice)
    assert _current_dynamic_quantitative_profile(notice).status != "AVAILABLE"


def test_failure_behind_a_newer_accepted_read_cannot_jump_to_the_older_one():
    notice, _, old, _ = modern_range_notice(CURRENT)
    middle = _failed(notice, old, code="DOCUMENT_TEXT_EMPTY", download=False)
    latest = _failed(notice, old, number=4)
    assert preserved_quantitative_read_proof([old, middle, latest]) is None
    assert old not in _read(notice)


def test_guard_off_keeps_the_latest_failure(monkeypatch):
    monkeypatch.setenv("PAI_LOOP_EXTRACTION_DEMOTION_GUARD", "off")
    notice, _, old, _ = modern_range_notice(CURRENT)
    newer = _failed(notice, old)
    assert _read(notice) == [newer]
