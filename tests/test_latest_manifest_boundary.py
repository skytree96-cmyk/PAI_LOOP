from __future__ import annotations

from datetime import datetime, timezone
import copy
from dataclasses import asdict
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import Notice, NoticeVersion
from pai_loop import pps_enrichment as pps
from pai_loop.quantitative_scoring import (
    _current_authoritative_document_state,
    _current_dynamic_quantitative_profile,
    estimate_for_notice,
)
import test_pps_enrichment as pps_fixtures
import test_quantitative_rule_extraction as rule_fixtures

MISSING = "__MISSING_SYNTHETIC_FIELD__"
MALFORMED = [None, {}, "synthetic-invalid-container", 7, True, MISSING]


def _metadata(version_no: int, value, *, notice_id=None):
    payload = {"kind": pps.PPS_METADATA_KIND, "schema_version": pps.PPS_METADATA_SCHEMA}
    if value != MISSING:
        payload["attachment_manifest"] = copy.deepcopy(value)
    return NoticeVersion(
        notice_id=notice_id, version_no=version_no, file_sha256="f" * 64,
        document_complete=False, extraction_status="METADATA", extraction_confidence=1,
        source_payload=payload,
    )


def _version_state(versions):
    return [(v.version_no, v.file_sha256, copy.deepcopy(v.source_payload)) for v in versions]


@pytest.mark.parametrize("malformed", MALFORMED)
@pytest.mark.parametrize("later_materialisation", [False, True])
def test_current_readers_reject_newest_malformed_manifest(malformed, later_materialisation):
    versions = pps_fixtures._analysis_versions(".pdf", status="ACCEPTED")
    baseline = pps.pps_attachment_coverage(versions)
    assert baseline.complete and baseline.accepted == 1
    versions.append(_metadata(3, malformed, notice_id="notice"))
    if later_materialisation:
        versions.append(NoticeVersion(version_no=4, file_sha256="e" * 64,
            source_payload={"kind": "SYNTHETIC_ANALYSIS_MATERIALISATION"}))
    before = _version_state(versions)
    attachments, invalid, attempts = pps._current_manifest_attempts(versions)
    assert attachments == [] and invalid == 1 and attempts == {}
    assert pps._current_manifest_attempts(versions, validate_accepted=False) == ([], 1, {})
    assert pps.current_retryable_review_version_ids(versions) == frozenset()
    assert pps.pps_recorded_attachment_attempt_count(versions) == 0
    coverage = pps.pps_attachment_coverage(versions)
    assert asdict(coverage) == asdict(pps.PpsAttachmentCoverage())
    for evaluated in (False, True):
        reason = pps.public_analysis_reason(versions, evaluated=evaluated)
        assert reason.state == "REVIEW"
        assert reason.reason_code == "ATTACHMENT_COVERAGE_INCOMPLETE"
        assert reason.attachment_count == 0
        assert reason.attempted is False
    assert _version_state(versions) == before


def _dynamic_notice():
    captured = []
    real_notice = rule_fixtures.Notice
    def capture(*args, **kwargs):
        notice = real_notice(*args, **kwargs)
        captured.append(notice)
        return notice
    with patch.object(rule_fixtures, "Notice", side_effect=capture):
        rule_fixtures.test_current_persisted_manifest_recovers_busan_sibling_table()
    return captured[0]


@pytest.mark.parametrize("malformed", MALFORMED)
def test_dynamic_and_curated_source_gates_do_not_resurrect_previous_profile(malformed):
    notice = _dynamic_notice()
    old = _current_dynamic_quantitative_profile(notice)
    assert old.status == "AVAILABLE"
    notice.versions.append(_metadata(4, malformed))
    before = _version_state(notice.versions)
    profile = _current_dynamic_quantitative_profile(notice)
    assert profile is not None and profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    assert profile.document_bindings == ()
    assert "ATTACHMENT_INCOMPLETE" in {issue.code for issue in profile.issues}
    assert _current_authoritative_document_state(notice)[0] == set()
    assert _current_authoritative_document_state(notice)[1] is not None
    estimate = estimate_for_notice(notice)
    assert estimate.activation_status == "REVIEW_REQUIRED"
    assert estimate.source_validation_status == "INCOMPLETE"
    assert estimate.criteria == [] and estimate.estimated_points is None
    assert estimate.ruleset_version.startswith("dynamic-quantitative-rules-")
    assert _version_state(notice.versions) == before


@pytest.mark.parametrize("malformed", MALFORMED)
@pytest.mark.parametrize("later_materialisation", [False, True])
def test_enrichment_and_write_guards_reject_invalid_latest_without_provider_or_mutation(monkeypatch, malformed, later_materialisation):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid latest metadata must not reach attachment work")
    monkeypatch.setattr(pps, "_enrich_selected_pps_attachment", forbidden)
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    versions = pps_fixtures._analysis_versions(".pdf", status="ACCEPTED")
    old_manifest = copy.deepcopy(versions[0].source_payload["attachment_manifest"])
    attachment = old_manifest[0]
    descriptor = pps._digest(attachment)
    manifest_digest = pps._digest(old_manifest)
    versions.append(_metadata(3, malformed, notice_id="notice"))
    if later_materialisation:
        versions.append(NoticeVersion(notice_id="notice", version_no=4,
            file_sha256="e" * 64, document_complete=True, extraction_status="COMPLETE",
            extraction_confidence=1, source_payload={"kind": "SYNTHETIC_MATERIALISATION"}))
    with factory() as session:
        session.add(Notice(id="notice", notice_key="PPS-SYN-LATEST-BOUNDARY",
            bid_notice_no="SYN-LATEST", revision_no="000", title="합성 원문 경계 검증",
            agency="합성 기관", deadline=datetime(2027, 1, 1, tzinfo=timezone.utc), status="OPEN"))
        session.add_all(versions)
        session.commit()
    with factory() as session:
        assert pps.has_current_accepted_pps_extraction(session, "notice") is False
        assert pps._manifest_binding_is_current(session, notice_id="notice", attachment=attachment,
            manifest_sha256=descriptor, current_manifest_sha256=manifest_digest) is False
        session.rollback()
        result = pps.enrich_notice_from_pps(session, notice_id="notice", openai_api_key=None, openai_model="unused")
        assert result.status == "REVIEW"
        assert result.openai_calls == 0
        assert result.attachments_discovered == 0
        assert result.warnings == ["INVALID_ATTACHMENT_MANIFEST", "ATTACHMENT_COVERAGE_INCOMPLETE"]
        assert not session.in_transaction()
        marker = pps.record_internal_pps_enrichment_failure(session, notice_id="notice", attachment=attachment,
            manifest_sha256=descriptor, current_manifest_sha256=manifest_digest, attachments_discovered=1)
        assert marker.status == "REVIEW"
        assert "PPS_MANIFEST_CHANGED_DURING_ENRICHMENT" in marker.warnings
        assert session.scalar(select(func.count()).select_from(NoticeVersion)) == len(versions)
    engine.dispose()


def test_valid_latest_manifest_and_no_metadata_legacy_behaviour_stay_unchanged():
    versions = pps_fixtures._analysis_versions(".pdf", status="ACCEPTED")
    before = _version_state(versions)
    coverage = pps.pps_attachment_coverage(versions)
    assert coverage.complete and coverage.discovered == coverage.audited == coverage.accepted == 1
    assert pps.public_analysis_reason(versions).reason_code == "ANALYZED"
    assert pps.pps_recorded_attachment_attempt_count(versions) == 1
    assert _version_state(versions) == before
    assert pps.public_analysis_reason([], evaluated=True).reason_code == "ANALYZED"
    assert pps.public_analysis_reason([], source_kind="MANUAL", evaluated=True).reason_code == "ANALYZED"
    assert pps._current_manifest_attempts([]) == ([], 0, {})


@pytest.mark.parametrize("manifest", [[], ["synthetic-invalid-slot"]])
def test_empty_or_invalid_latest_list_does_not_fall_back(manifest):
    notice = _dynamic_notice()
    notice.versions.append(_metadata(4, manifest))
    profile = _current_dynamic_quantitative_profile(notice)
    assert profile.status == "INCOMPLETE"
    assert profile.available_candidates == ()
    coverage = pps.pps_attachment_coverage(notice.versions)
    assert not coverage.complete and coverage.accepted == 0
    assert coverage.discovered == len(manifest)


def test_newest_stale_list_schema_does_not_fall_back():
    pps_fixtures.test_newest_stale_metadata_never_falls_back_to_prior_current_manifest()
