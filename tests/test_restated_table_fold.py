"""Only source-equivalent complete table programs are duplicate representations."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from conftest import login_department_reader
from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.models import AnalysisRun, Notice, NoticeVersion, ScoreSnapshot
from pai_loop.pps_enrichment import _digest
from pai_loop.quantitative_rule_extraction import (
    AttachmentDocumentBinding, QuantitativeCandidateProfile, QuantitativeValidationIssue,
    validate_quantitative_attachment_extraction,
)
from pai_loop import quantitative_scoring as qs
from test_personnel_public_roundtrip import _stored_personnel_source
from test_quantitative_personnel_binding import AS_OF, _fact as _roster_fact
from test_quantitative_auto_activation import _validated_profile


def _rebind(value, old, new):
    if isinstance(value, dict):
        return {key: _rebind(item, old, new) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rebind(item, old, new) for item in value]
    return new if value == old else value


def _profile(*, metric="BUSINESS_YEARS", label="SYN 업력", unit="년", fact_key="company.business.years"):
    base = _validated_profile(metric=metric, label=label, unit=unit, fact_key=fact_key)
    copies = []
    for attachment_id in ("SYN-A", "SYN-B"):
        copies.append(QuantitativeCandidateProfile.model_validate(
            _rebind(base.model_dump(mode="json"), "ATT-AUTO-ACTIVE", attachment_id)
        ))
    return base.model_copy(update={
        "status": "INCOMPLETE", "expected_attachment_ids": ("SYN-A", "SYN-B", "SYN-UNREAD"),
        "processed_attachment_ids": ("SYN-A", "SYN-B"),
        "document_bindings": tuple(AttachmentDocumentBinding(
            attachment_id=attachment_id, document_sha256=digest * 64,
            document_type="RFP", source_label=f"SYN 제안요청서.{extension}",
        ) for attachment_id, digest, extension in (("SYN-A", "a", "hwp"), ("SYN-B", "b", "pdf"))),
        "tables": tuple(table for copy in copies for table in copy.tables),
        "available_candidates": tuple(item for copy in copies for item in copy.available_candidates),
        "issues": (QuantitativeValidationIssue(code="ATTACHMENT_UNRESOLVED", disposition="INCOMPLETE", attachment_id="SYN-UNREAD", message="SYN unread attachment"),),
    })


def _request(profile):
    return qs.quantitative_request_from_candidate_profile(profile, allow_partial_source=True)


def test_complete_alternate_tables_contribute_one_partial_subtotal_and_keep_binding():
    profile = _profile()
    first = _request(profile)
    reversed_profile = profile.model_copy(update={
        "tables": tuple(reversed(profile.tables)),
        "available_candidates": tuple(reversed(profile.available_candidates)),
        "document_bindings": tuple(reversed(profile.document_bindings)),
    })
    second = _request(reversed_profile)
    assert len(first.criteria) == len(second.criteria) == 1
    assert first.criteria[0] == second.criteria[0]
    selected = profile.available_candidates[0]
    assert first.criteria[0].fact_binding_sha256 == qs._candidate_fact_binding_sha256(
        selected, document_sha256="a" * 64,
    )
    assert first.criteria[0].source_anchor.document_label == "SYN-A"
    result = qs.estimate_quantitative_score(first)
    assert result.total_max_points == 20
    assert result.overall_status == "REVIEW"
    assert result.minimum_score is result.meets_minimum is None


@pytest.mark.parametrize("change", [
    "candidate_label", "candidate_section", "candidate_quote", "recognition", "required_evidence",
    "table_label", "table_section", "table_total", "minimum", "review_table", "missing_total",
    "same_format_revision", "different_stem", "different_role", "missing_role", "missing_binding",
    "unprocessed", "missing_manifest", "own_attachment_issue", "unlocated_issue", "foreign_anchor",
    "unlinked_row", "table_limit",
])
def test_similar_tables_with_different_scope_or_unproven_identity_do_not_fold(change):
    profile = _profile()
    candidate, table, binding = profile.available_candidates[1], profile.tables[1], profile.document_bindings[1]
    if change == "candidate_label":
        candidate = candidate.model_copy(update={"label": "SYN 별도 항목"})
    elif change in {"candidate_section", "candidate_quote"}:
        key = "section" if change.endswith("section") else "quote"
        candidate = candidate.model_copy(update={"evidence": candidate.evidence.model_copy(update={key: "SYN 다른 구역/원문"})})
    elif change == "recognition":
        from pai_loop.quantitative_rule_extraction import ImmutableQuantitativeRecognitionCondition
        candidate = candidate.model_copy(update={"recognition_conditions": (
            ImmutableQuantitativeRecognitionCondition(literal="SYN 별도 인정조건", evidence=candidate.evidence),
        )})
    elif change == "required_evidence":
        candidate = candidate.model_copy(update={"required_evidence": ("SYN-extra-proof",)})
    elif change == "table_label":
        table = table.model_copy(update={"label": "SYN 제2구역 평가"})
    elif change == "table_section":
        table = table.model_copy(update={"total_evidence": table.total_evidence.model_copy(update={"section": "SYN 제2구역"})})
    elif change == "table_total":
        table = table.model_copy(update={"total_points": 40})
    elif change == "minimum":
        table = table.model_copy(update={"minimum_score": 15})
    elif change == "review_table":
        table = table.model_copy(update={"status": "REVIEW"})
    elif change == "missing_total":
        table = table.model_copy(update={"total_evidence": None})
    elif change == "same_format_revision":
        binding = binding.model_copy(update={"source_label": "SYN 제안요청서.hwp"})
    elif change == "different_stem":
        binding = binding.model_copy(update={"source_label": "SYN 다른 제안요청서.pdf"})
    elif change == "different_role":
        binding = binding.model_copy(update={"document_type": "NOTICE", "source_label": "SYN 공고문.pdf"})
    elif change == "missing_role":
        binding = binding.model_copy(update={"document_type": None, "source_label": "SYN.pdf"})
    elif change == "missing_binding":
        profile = profile.model_copy(update={"document_bindings": profile.document_bindings[:1]})
    elif change == "unprocessed":
        profile = profile.model_copy(update={"processed_attachment_ids": ("SYN-A",)})
    elif change == "missing_manifest":
        profile = profile.model_copy(update={"manifest_sha256": None})
    elif change in {"own_attachment_issue", "unlocated_issue"}:
        issue = profile.issues[0].model_copy(update={
            "attachment_id": "SYN-B" if change == "own_attachment_issue" else None,
        })
        profile = profile.model_copy(update={"issues": (issue,)})
    elif change == "foreign_anchor":
        candidate = candidate.model_copy(update={"evidence": candidate.evidence.model_copy(update={"attachment_id": "SYN-OTHER"})})
    elif change == "unlinked_row":
        candidate = candidate.model_copy(update={"table_id": "SYN-UNLINKED"})
    profile = profile.model_copy(update={
        "tables": (profile.tables[0], table),
        "available_candidates": (profile.available_candidates[0], candidate),
        "document_bindings": (profile.document_bindings[0], binding) if change != "missing_binding" else profile.document_bindings,
    })
    if change == "table_limit":
        profile = profile.model_copy(update={"tables": profile.tables * qs._MAX_LOGICAL_PROGRAM_TABLES})
    assert len(qs._fold_restated_table_candidates(profile, profile.available_candidates)) == 2


def test_personnel_label_that_changes_scope_is_never_discarded():
    profile = _profile(metric="PERSONNEL_COUNT", label="SYN 학사학위이상 보유인력", unit="명", fact_key="company.personnel.count")
    assigned = profile.available_candidates[1].model_copy(update={"label": "SYN 학사학위이상 참여인력"})
    profile = profile.model_copy(update={"available_candidates": (profile.available_candidates[0], assigned)})
    request = _request(profile)
    assert len(request.criteria) == 2
    retained, capacity = (row.personnel_scope for row in request.criteria)
    assert retained is not None and retained.population_basis == "RETAINED"
    # The assignment row keeps its own scope; it is a capacity forecast only.
    assert capacity is not None and capacity.population_basis == "ASSIGNED_CAPACITY"
    # A verified roster without the explicit availability assumption never
    # scores the assignment row.
    facts = qs.resolve_personnel_register_facts(request.criteria, [_roster_fact()], as_of=AS_OF)
    assert facts[1].status == "REVIEW" and facts[1].value is None
    assert "사용자 승인" in facts[1].rationale


def test_repeated_identical_rows_in_one_physical_table_remain_two_rows():
    profile = _profile()
    candidates, tables = [], []
    for original, table in zip(profile.available_candidates, profile.tables):
        second = original.model_copy(update={"criterion_id": "SYN-SECOND"})
        candidates.extend((original, second))
        tables.append(table.model_copy(update={
            "total_points": 40,
            "criterion_ids": (original.criterion_id, second.criterion_id),
            "available_criterion_ids": (original.criterion_id, second.criterion_id),
            "total_evidence": table.total_evidence.model_copy(update={"quote": "SYN 합계 40점"}),
        }))
    profile = profile.model_copy(update={"tables": tuple(tables), "available_candidates": tuple(candidates)})
    assert len(_request(profile).criteria) == 2


def test_same_attachment_tables_and_different_full_programs_never_fold():
    profile = _profile()
    extra_table = profile.tables[0].model_copy(update={"table_id": "SYN-SECOND-TABLE"})
    extra_candidate = profile.available_candidates[0].model_copy(update={"table_id": "SYN-SECOND-TABLE"})
    profile = profile.model_copy(update={
        "tables": (*profile.tables, extra_table),
        "available_candidates": (*profile.available_candidates, extra_candidate),
    })
    assert len(_request(profile).criteria) == 3
    profile = profile.model_copy(update={"tables": (*profile.tables[:2], extra_table.model_copy(update={"label": "SYN 다른 표"}))})
    assert len(_request(profile).criteria) == 3


def test_identical_document_digest_does_not_require_matching_filenames():
    profile = _profile()
    second = profile.document_bindings[1].model_copy(update={"document_sha256": "a" * 64, "source_label": "SYN 복사본.pdf"})
    profile = profile.model_copy(update={"document_bindings": (profile.document_bindings[0], second)})
    assert len(_request(profile).criteria) == 1


def _store_duplicate_native_source(session):
    key, _roster_id, document_sha = _stored_personnel_source(session, projected=False, incomplete=True)
    notice = session.scalar(select(Notice).where(Notice.notice_key == key))
    metadata, accepted = sorted(notice.versions, key=lambda item: item.version_no)
    manifest = deepcopy(metadata.source_payload["attachment_manifest"])
    old_id = manifest[0]["attachment_id"]
    new_id = "PPS-ATT-" + "e" * 24
    duplicate = {**manifest[0], "attachment_id": new_id, "slot": 3,
                 "url": manifest[0]["url"].replace("fileSeq=1", "fileSeq=3")}
    manifest.append(duplicate)
    metadata.source_payload = {**metadata.source_payload, "attachment_manifest": manifest}
    original = deepcopy(accepted.source_payload)
    for attachment, version in ((manifest[0], accepted), (duplicate, None)):
        attachment_id = attachment["attachment_id"]
        payload = _rebind(deepcopy(original), old_id, attachment_id)
        raw = ExtractionPayload.model_validate(payload["result"])
        table = raw.quantitative_tables[0]
        source = "\n".join((table.criteria[0].criterion_literal, table.criteria[0].threshold.literal, table.total_evidence.quote))
        record = validate_quantitative_attachment_extraction(
            raw, source_text=source, attachment_id=attachment_id,
            document_sha256=document_sha, manifest_sha256=_digest(manifest),
        )
        assert record.status == "AVAILABLE"
        payload.update(manifest_sha256=_digest(attachment), current_manifest_sha256=_digest(manifest),
                       quantitative_validation_record=record.model_dump(mode="json"))
        if version is None:
            notice.versions.append(NoticeVersion(version_no=3, file_sha256=document_sha,
                extraction_status="ACCEPTED", document_complete=True, extraction_confidence=1, source_payload=payload))
        else:
            version.source_payload = payload
    session.commit()
    return key


@pytest.mark.parametrize("obsolete_engine", [False, True])
def test_native_duplicate_subtotal_persists_once_and_public_cache_rejects_old_engine(client, monkeypatch, obsolete_engine):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("SYN provider/public recomputation forbidden")
    monkeypatch.setattr("pai_loop.analysis_api._enrich_one_notice", forbidden)
    with client.app.state.session_factory() as session:
        key = _store_duplicate_native_source(session)
    path = f"/api/v1/notices/{key}/quantitative-estimate"
    response = client.get(path)
    assert response.status_code == 200, response.text
    internal = response.json()
    assert internal["activation_status"] == "PARTIAL_SOURCE"
    assert internal["lower_points"] == internal["upper_points"] == internal["total_max_points"] == 20
    assert len(internal["criteria"]) == 1
    assert internal["criteria"][0]["status"] == "ESTIMATED"
    batch = client.post("/api/v1/notices/analysis/batch", json={"notice_keys": [key], "enrich_missing": False, "dry_run": False})
    assert batch.status_code == 200, batch.text
    assert batch.json()["failed"] == batch.json()["openai_calls"] == 0
    with client.app.state.session_factory() as session:
        score = session.scalar(select(ScoreSnapshot).where(ScoreSnapshot.score_key == "quantitative.total"))
        assert score.basis_json["total_max_points"] == 20
        assert len(score.basis_json["public_criteria"]["items"]) == 1
        assert score.method_version == qs.QUANTITATIVE_ENGINE_VERSION
        if obsolete_engine:
            score.method_version = "pai-loop-quantitative-engine-1.8.8"
            run = session.scalar(select(AnalysisRun))
            run.basis_versions = {**run.basis_versions, "quantitative_engine": score.method_version}
            session.commit()
    original_estimate = qs.estimate_for_notice
    recalculations = []

    def public_fallback(notice, facts=(), performance_records=()):
        assert not facts and not performance_records
        recalculations.append(notice.notice_key)
        return original_estimate(notice, facts, performance_records)

    monkeypatch.setattr("pai_loop.quantitative_scoring.estimate_for_notice", public_fallback if obsolete_engine else forbidden)
    client.app.state.settings = replace(client.app.state.settings, public_read_only=True)
    login_department_reader(client)
    response = client.get(path)
    assert response.status_code == 200, response.text
    public = response.json()
    if obsolete_engine:
        assert recalculations == [key]
        assert not any(item["status"] == "ESTIMATED" for item in public["criteria"])
        assert public["lower_points"] != 20
    else:
        assert public["lower_points"] == public["upper_points"] == public["total_max_points"] == 20
        assert len(public["criteria"]) == 1
        assert public["criteria"][0]["status"] == "ESTIMATED"
        assert public["criteria"][0]["fact_binding_sha256"] is None
        assert public["overall_status"] == "REVIEW"
        assert public["estimated_points"] is public["minimum_score"] is public["meets_minimum"] is None
    assert "SYN-PRIVATE" not in json.dumps(public)
