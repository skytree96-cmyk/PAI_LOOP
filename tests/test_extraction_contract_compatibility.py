from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from itertools import product

import pytest
from pydantic import ValidationError
from sqlalchemy import select, func

from pai_loop.extraction_contracts import (
    CURRENT_EXTRACTION_CONTRACT as CURRENT, LEGACY_CASE_CONTRACT as LEGACY,
    PREVIOUS_CASE_CONTRACT as PREVIOUS,
    PREVIOUS_EXTRACTION_CONTRACT as PREVIOUS_EXTRACTION,
    PREVIOUS_PROCESSING_CONTRACT as PREVIOUS_PROCESSING,
    classify_attempt_header, classify_record_contract,
)
from pai_loop.integrations.openai_extraction import (
    ExtractionOutcome, ExtractionPayload, QuantitativeCaseLiteral, PROMPT_VERSION,
)
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord, validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint, quantitative_record_contract_is_usable,
    merge_validated_quantitative_records,
)
from pai_loop.quantitative_scoring import (
    _current_dynamic_quantitative_profile, quantitative_request_from_candidate_profile,
    public_quantitative_snapshot_projection, QUANTITATIVE_ENGINE_VERSION,
)
from pai_loop.pps_enrichment import (
    PPS_METADATA_SCHEMA, build_attachment_manifest, _current_manifest_attempts,
    public_analysis_reason, safe_public_live_extraction, safe_public_bound_extraction,
    _matching_extraction_version, current_retryable_review_version_ids,
    _accepted_quantitative_review_retry_boundary, _stored_attachment_result,
    _accepted_outcome_for_duplicate_content, _persist_extraction_version, PpsEnrichmentError,
)
from pai_loop.analysis_pipeline import (
    _parse_source, _current_pps_manifest_basis, _source_supplies_quantitative_table,
    _select_source_versions, run_analysis_pipeline,
)
from pai_loop.api import _public_document_analyses
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.models import Notice, NoticeVersion, AnalysisRun, ScoreSnapshot
from pai_loop.notice_freshness import analysis_version_refresh_required
from pai_loop.analysis_pipeline import PIPELINE_VERSION
from pai_loop.eligibility_policy import POLICY_VERSION


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def source_payload(attachment_id, *, tail="EQ", gap=False):
    def anchor(quote):
        return dict(attachment_id=attachment_id, page=1, section="SYN 평가표",
                    quote=quote, confidence=0.99)
    cases = []
    for index, (operator, value, points) in enumerate([
        ("GTE", 5, 5), ("EQ", 4, 4), ("EQ", 3, 3), ("EQ", 2, 2),
        (tail, 2 if tail == "LT" else 1, 1),
    ], 1):
        word = {"GTE": " 이상", "LTE": " 이하", "LT": " 미만", "EQ": ""}[operator]
        literal = f"{value}건{word} {points}점"
        cases.append(dict(literal=literal, operator=operator, comparison_value=value,
                          category_values=[], award_kind="POINTS", award_value=points,
                          row_order=index, evidence=anchor(literal)))
    criterion = dict(criterion_id="SYN-COUNT", label="수행실적", criterion_literal="수행실적 5점",
                     max_points=5, scoring_method="CASE_TABLE", metric="PERFORMANCE_COUNT",
                     unit="건", brackets=[], threshold=None, formula_literal=None,
                     cases=cases, recognition_conditions=[],
                     required_evidence=["company.performance.count"],
                     evidence=anchor("수행실적 5점"),
                     ambiguity_reason="SYN 해석 확인 필요" if gap else None)
    table = dict(table_id="SYN-TABLE", label="SYN 평가표", criteria=[criterion],
                 total_points=5, total_evidence=anchor("정량평가 총점 5점"),
                 minimum_score=None, minimum_evidence=None, ambiguity_reason=None)
    source = "\n".join(["SYN 평가표", "수행실적 5점",
                         *(case["literal"] for case in cases), "정량평가 총점 5점"])
    result = ExtractionPayload(document_type="RFP", summary="SYN 정량 근거",
        requirements=[], quantitative_tables=[table], quantitative_table_not_applicable=None,
        # Names a scoring artifact, so the gap gate holds this record open.
        # ``증빙 미제공`` alone would not: a missing submission document is a
        # procedural note, not a rule the score reads.
        missing_or_unreadable=["SYN 배점표 미제공"] if gap else [])
    return result, source


def notice_fixture(*, legacy=True, tail="EQ", gap=False, neutral=False, contract=None):
    manifest = build_attachment_manifest(dict(
        bidNtceNo="SYN-CASE", bidNtceOrd="000", ntceSpecFileNm1="SYN 제안요청서.pdf",
        ntceSpecDocUrl1="https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-CASE&bidPbancOrd=000&fileSeq=1&fileType=1&prcmBsneSeCd=01",
    ))
    attachment = manifest[0]
    assert "attachment_id" in attachment
    aid = attachment["attachment_id"]
    result, source = source_payload(aid, tail=tail, gap=gap)
    if neutral:
        result = result.model_copy(update={"quantitative_tables": []})
        source = "SYN 별도 안내문"
    file_sha = hashlib.sha256(source.encode()).hexdigest()
    manifest_sha = digest(manifest)
    record = validate_quantitative_attachment_extraction(result, source_text=source,
        attachment_id=aid, document_sha256=file_sha, manifest_sha256=manifest_sha)
    contract = contract or (LEGACY if legacy else CURRENT)
    if contract != CURRENT:
        record = record.model_copy(update=dict(prompt_version=contract.prompt,
            extraction_schema_version=contract.schema, validator_version=contract.validator))
        record = record.model_copy(update=dict(
            validation_fingerprint_sha256=validated_quantitative_record_fingerprint(record)))
    payload = dict(kind="OPENAI_REQUIREMENT_EXTRACTION", source_kind="PPS_PUBLIC_ATTACHMENT",
        attachment_id=aid, source_label=attachment["file_name"], manifest_sha256=digest(attachment),
        current_manifest_sha256=manifest_sha, document_sha256=file_sha,
        prompt_version=contract.prompt, schema_version=contract.schema,
        processing_version=contract.processing, status="ACCEPTED", error_code=None,
        document_processing=dict(source_read_complete=True, analysis_input_complete=True,
                                 source_characters=len(source), source_text_sha256=file_sha,
                                 analysis_input_sha256=file_sha),
        quantitative_validation_record=record.model_dump(mode="json"),
        result=result.model_dump(mode="json"))
    notice = Notice(id="SYN-NOTICE", notice_key="PPS-SYN-CASE", bid_notice_no="SYN-CASE",
        title="SYN 정량 계약", agency="SYN 기관", status="OPEN", estimated_amount=1000000,
        deadline=datetime(2030, 1, 1, tzinfo=timezone.utc), category="SYN 용역",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc), risk_dimensions=None)
    metadata = NoticeVersion(id="SYN-METADATA", notice=notice, version_no=1,
        file_sha256="0"*64, document_complete=False, extraction_status="METADATA",
        extraction_confidence=1, source_payload=dict(kind="PPS_NOTICE_METADATA",
            schema_version=PPS_METADATA_SCHEMA, attachment_manifest=manifest))
    attempt = NoticeVersion(id="SYN-ATTEMPT", notice=notice, version_no=2,
        file_sha256=file_sha, document_complete=True, extraction_status="ACCEPTED",
        extraction_confidence=0.99, source_payload=payload,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return notice, metadata, attempt, record


def usable(attempt, record):
    return quantitative_record_contract_is_usable(record,
        source_payload=attempt.source_payload, attachment_id=record.attachment_id,
        document_sha256=attempt.file_sha256,
        manifest_sha256=attempt.source_payload["current_manifest_sha256"])


def merge(attempt, record, *, include_payload=True):
    payload = attempt.source_payload
    return merge_validated_quantitative_records([record],
        expected_documents={record.attachment_id: attempt.file_sha256},
        manifest_sha256=payload["current_manifest_sha256"],
        attachment_profiles={record.attachment_id: dict(document_type="RFP",
            source_label=payload["source_label"],
            missing_or_unreadable=payload["result"]["missing_or_unreadable"])},
        source_payloads={record.attachment_id: payload} if include_payload else None)


@pytest.mark.parametrize("parts", list(product((False, True), repeat=4)))
def test_only_released_tuple_combinations_are_readable(parts):
    contracts = [CURRENT if current else LEGACY for current in parts]
    payload = dict(prompt_version=contracts[0].prompt, schema_version=contracts[1].schema,
                   processing_version=contracts[3].processing)
    raw = dict(prompt_version=payload["prompt_version"],
               extraction_schema_version=payload["schema_version"], validator_version=contracts[2].validator)
    expected = "CURRENT" if all(parts) else "LEGACY_CASE_V1" if not any(parts) else "UNSUPPORTED"
    assert classify_record_contract(payload, raw) == expected


@pytest.mark.parametrize("field", ["prompt_version", "schema_version", "processing_version"])
@pytest.mark.parametrize("value", [None, "SYN-unsupported-version"])
def test_missing_and_unknown_attempt_contract_is_not_a_fallback(field, value):
    _, _, attempt, _ = notice_fixture()
    attempt.source_payload[field] = value
    assert classify_attempt_header(attempt.source_payload) == "UNSUPPORTED"


@pytest.mark.parametrize("legacy", [False, True])
def test_current_and_exact_legacy_manifest_evidence_remain_active(legacy):
    notice, _, attempt, record = notice_fixture(legacy=legacy)
    assert record.status == "AVAILABLE"
    assert usable(attempt, record)
    assert merge(attempt, record).status == "AVAILABLE"
    assert public_analysis_reason(notice.versions).state == "ANALYZED"
    profile = _current_dynamic_quantitative_profile(notice)
    assert profile.status == "AVAILABLE"
    assert quantitative_request_from_candidate_profile(profile).source_validation_status == "SOURCE_VALIDATED"
    assert _current_pps_manifest_basis(notice.versions, prompt_version=PROMPT_VERSION)["accepted_attachment_ids"] == [record.attachment_id]
    assert _public_document_analyses(notice.versions)[0]["summary"] == "SYN 정량 근거"
    assert not legacy or safe_public_live_extraction(attempt.source_payload) is None
    parsed = _parse_source(attempt, prompt_version=PROMPT_VERSION, allow_compatible_pps=True)
    assert parsed.materializable
    assert parsed.prompt_version == (LEGACY if legacy else CURRENT).prompt
    assert _source_supplies_quantitative_table(parsed)


@pytest.mark.parametrize("tail", ["LTE", "LT"])
def test_new_count_tail_survives_extraction_record_merge_and_diagnostics(tail):
    from pai_loop.manual_analysis import QuantitativeDiagnosticCaseShape
    notice, _, attempt, record = notice_fixture(legacy=False, tail=tail)
    assert record.status == "AVAILABLE", [i.code for i in record.issues]
    assert merge(attempt, record).status == "AVAILABLE"
    assert _current_dynamic_quantitative_profile(notice).available_candidates[0].cases[-1].operator == tail
    assert tail in QuantitativeCaseLiteral.model_json_schema()["properties"]["operator"]["enum"]
    assert tail in QuantitativeDiagnosticCaseShape.model_json_schema()["properties"]["operator"]["enum"]


@pytest.mark.parametrize("tail", ["LTE", "LT"])
def test_legacy_label_cannot_claim_new_case_vocabulary(tail):
    _, _, attempt, record = notice_fixture(legacy=True, tail=tail)
    assert record.status == "AVAILABLE"
    assert not usable(attempt, record)
    assert merge(attempt, record).status != "AVAILABLE"


@pytest.mark.parametrize("options", [{}, {"tail": "LT"}, {"gap": True}, {"neutral": True}])
def test_exact_previous_contract_keeps_its_original_proof_and_status(options):
    notice, _, attempt, record = notice_fixture(contract=PREVIOUS, **options)
    assert classify_attempt_header(attempt.source_payload) == "LEGACY_CASE_V2"
    assert usable(attempt, record)
    assert _current_pps_manifest_basis(notice.versions, prompt_version=PROMPT_VERSION)["selected_attempt_ids"] == [attempt.id]
    assert _parse_source(attempt, prompt_version=PROMPT_VERSION, allow_compatible_pps=True).materializable
    assert bool(merge(attempt, record).available_candidates) == bool(record.available_candidates)
    # Recreate pre-upgrade JSON: the new optional field was absent, not null.
    raw = record.model_dump(mode="json")
    for candidate in raw["available_candidates"]:
        for case in candidate["cases"]:
            case.pop("comparison_upper_value", None)
    restored = ValidatedQuantitativeAttachmentRecord.model_validate(raw)
    assert restored.validation_fingerprint_sha256 == validated_quantitative_record_fingerprint(restored)
    assert usable(attempt, restored)


@pytest.mark.parametrize("contract", [LEGACY, PREVIOUS])
@pytest.mark.parametrize("gap", [False, True])
@pytest.mark.parametrize("mutation", ["upper", "between", "not_submitted"])
def test_predecessors_cannot_smuggle_new_case_semantics(contract, gap, mutation):
    _, _, attempt, record = notice_fixture(contract=contract, gap=gap)
    case = attempt.source_payload["result"]["quantitative_tables"][0]["criteria"][0]["cases"][0]
    if mutation == "upper":
        case["comparison_upper_value"] = 6
    elif mutation == "between":
        case.update(operator="BETWEEN", comparison_value=5, comparison_upper_value=6)
    else:
        case.update(operator="NOT_SUBMITTED", comparison_value=None, award_value=0)
    assert not usable(attempt, record)


@pytest.mark.parametrize("field", ["prompt", "schema", "validator", "processing"])
def test_previous_tuple_cannot_mix_with_current_contract(field):
    parts = PREVIOUS._asdict()
    parts[field] = getattr(CURRENT, field)
    payload = dict(prompt_version=parts["prompt"], schema_version=parts["schema"], processing_version=parts["processing"])
    record = dict(prompt_version=parts["prompt"], extraction_schema_version=parts["schema"], validator_version=parts["validator"])
    assert classify_record_contract(payload, record) == "UNSUPPORTED"


def test_additive_empty_upper_bound_preserves_existing_company_fact_identity():
    from pai_loop.quantitative_performance import parse_performance_recognition_scope
    from pai_loop.quantitative_scoring import (
        _candidate_fact_binding_sha256, _canonical_digest, _performance_scope_literal,
    )
    _, _, _, record = notice_fixture(contract=PREVIOUS)
    candidate = record.available_candidates[0]
    legacy_json = candidate.model_dump(mode="json")
    for case in legacy_json["cases"]:
        case.pop("comparison_upper_value", None)
    payload = {"binding_schema": "pai-loop-quantitative-fact-binding-1.0.0",
        "document_sha256": record.document_sha256, "candidate": legacy_json}
    legacy_digest = _canonical_digest(payload)
    # Since engine 1.8.5 a performance binding also carries the current
    # recognition semantics: a deliberate one-time re-binding so that a fact
    # attested under an older reading cannot satisfy a re-interpreted rule.
    # The additive empty upper bound itself must still be inert.
    assert candidate.metric == "PERFORMANCE_COUNT"
    scope = parse_performance_recognition_scope(
        _performance_scope_literal(candidate), metric_key="company.performance.count")
    payload["performance_recognition_contract"] = "performance-recognition-2"
    payload["performance_scope"] = (
        scope.model_dump(mode="json", exclude={"source_literal"}) if scope else None)
    current = _candidate_fact_binding_sha256(candidate, document_sha256=record.document_sha256)
    assert current == _canonical_digest(payload)
    assert current != legacy_digest
    changed = candidate.model_copy(update={"cases": (
        candidate.cases[0].model_copy(update={"operator": "BETWEEN", "comparison_upper_value": 6}),
        *candidate.cases[1:],
    )})
    assert _candidate_fact_binding_sha256(changed, document_sha256=record.document_sha256) != current


def test_legacy_review_original_case_rows_cannot_hide_new_vocabulary():
    _, _, attempt, record = notice_fixture(gap=True)
    assert record.status != "AVAILABLE"
    assert record.review_candidates
    row = attempt.source_payload["result"]["quantitative_tables"][0]["criteria"][0]["cases"][-1]
    row["operator"] = "LTE"
    assert not usable(attempt, record)


def test_legacy_review_and_no_table_records_keep_their_status():
    for options, expected in [(dict(gap=True), "INCOMPLETE"), (dict(neutral=True), "NO_TABLE")]:
        notice, _, attempt, record = notice_fixture(**options)
        assert record.status == expected
        assert usable(attempt, record)
        assert public_analysis_reason(notice.versions).state in {"ANALYZED", "REVIEW"}
        assert merge(attempt, record).status != "AVAILABLE"


def test_legacy_record_without_original_payload_never_merges():
    _, _, attempt, record = notice_fixture()
    assert merge(attempt, record, include_payload=False).status == "INCOMPLETE"
    attempt.source_payload.pop("result")
    assert not usable(attempt, record)


@pytest.mark.parametrize("field", ["document_sha256", "current_manifest_sha256", "attachment_id"])
def test_legacy_payload_binding_changes_invalidate_proof(field):
    _, _, attempt, record = notice_fixture()
    attempt.source_payload[field] = "SYN-wrong"
    assert not usable(attempt, record)


def test_legacy_payload_criterion_and_anchor_substitution_fail_closed():
    for mutation in ("criterion", "anchor", "award", "extra"):
        _, _, attempt, record = notice_fixture()
        tables = attempt.source_payload["result"]["quantitative_tables"]
        criterion = tables[0]["criteria"][0]
        if mutation == "criterion": criterion["criterion_id"] = "SYN-OTHER"
        if mutation == "anchor": criterion["cases"][0]["evidence"]["attachment_id"] = "SYN-OTHER"
        if mutation == "award": criterion["cases"][0]["award_value"] = 4
        if mutation == "extra": tables[0]["criteria"].append(deepcopy(criterion))
        assert not usable(attempt, record), mutation


@pytest.mark.parametrize("status", ["REVIEW", "INVALID_ACCEPTED", "UNKNOWN_HEADER"])
def test_new_generation_never_resurrects_old_available(status):
    notice, _, old, record = notice_fixture()
    payload = deepcopy(old.source_payload)
    payload.update(prompt_version=CURRENT.prompt, schema_version=CURRENT.schema,
                   processing_version=CURRENT.processing,
                   status="REVIEW" if status == "REVIEW" else "ACCEPTED",
                   error_code="SCHEMA_VALIDATION_ERROR", result=None,
                   quantitative_validation_record=None)
    if status == "UNKNOWN_HEADER":
        payload["prompt_version"] = "SYN-unrecognized-new-contract"
    newer = NoticeVersion(id="SYN-NEW", notice=notice, version_no=3,
        file_sha256=old.file_sha256, document_complete=False,
        extraction_status=payload["status"], extraction_confidence=0, source_payload=payload)
    _, _, attempts = _current_manifest_attempts(notice.versions)
    assert old not in attempts.values()
    assert public_analysis_reason(notice.versions).state != "ANALYZED"
    assert _current_dynamic_quantitative_profile(notice).status != "AVAILABLE"
    assert _public_document_analyses(notice.versions) == []
    basis = _current_pps_manifest_basis(notice.versions, prompt_version=PROMPT_VERSION)
    assert basis["accepted_attachment_ids"] == []
    assert basis["selected_attempt_ids"] == ([] if status == "UNKNOWN_HEADER" else [newer.id])


def test_changed_manifest_and_latest_invalid_metadata_never_reuse_legacy():
    notice, metadata, attempt, record = notice_fixture()
    metadata.source_payload["attachment_manifest"][0]["file_name"] = "SYN 다른 제안요청서.pdf"
    assert public_analysis_reason(notice.versions).state != "ANALYZED"
    assert safe_public_bound_extraction(attempt.source_payload, notice.versions) is None
    metadata.source_payload["schema_version"] = "SYN-future-metadata"
    assert _current_dynamic_quantitative_profile(notice).status != "AVAILABLE"


def test_legacy_read_reuse_returns_existing_row_without_restamping_contract():
    _, _, attempt, record = notice_fixture()
    original = deepcopy(attempt.source_payload)
    reused = _stored_attachment_result(attempt, attachments_discovered=1)
    assert reused.status == "REUSED" and reused.openai_calls == 0
    assert reused.version_id == attempt.id
    assert attempt.source_payload == original
    assert _matching_extraction_version([attempt], attachment_id=record.attachment_id,
        manifest_sha256=attempt.source_payload["manifest_sha256"],
        current_manifest_sha256=record.manifest_sha256,
        document_sha256=record.document_sha256) is None


def test_legacy_review_retry_snapshot_preserves_one_new_generation_boundary():
    notice, _, attempt, record = notice_fixture(gap=True)
    retry_ids = current_retryable_review_version_ids(notice.versions)
    assert retry_ids == frozenset({attempt.id})
    boundary = _accepted_quantitative_review_retry_boundary(notice.versions,
        attachment_id=record.attachment_id, manifest_sha256=attempt.source_payload["manifest_sha256"],
        current_manifest_sha256=record.manifest_sha256, retry_reviewed_version_ids=retry_ids)
    assert boundary == attempt.version_no
    assert _stored_attachment_result(attempt, attachments_discovered=1,
                                     retry_reviewed_version_ids=retry_ids) is None


@pytest.mark.parametrize("contract", [LEGACY, PREVIOUS, PREVIOUS_PROCESSING, PREVIOUS_EXTRACTION])
def test_pipeline_refresh_preserves_legacy_sources_and_is_idempotent(monkeypatch, contract):
    import pai_loop.analysis_pipeline as pipeline
    import pai_loop.quantitative_scoring as scoring
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        notice, _, attempt, record = notice_fixture(contract=contract)
        original = deepcopy(attempt.source_payload)
        notice_id = notice.id
        session.add(notice)
        session.commit()
        selected = _select_source_versions(session, notice_id=notice_id,
            prompt_version=PROMPT_VERSION, source_version_ids=None)
        assert [row.id for row in selected] == [attempt.id]
        session.rollback()
        old_engine = "pai-loop-quantitative-engine-1.7.1"
        monkeypatch.setattr(pipeline, "QUANTITATIVE_ENGINE_VERSION", old_engine)
        monkeypatch.setattr(scoring, "QUANTITATIVE_ENGINE_VERSION", old_engine)
        old_result = run_analysis_pipeline(session, notice_id=notice_id)
        monkeypatch.setattr(pipeline, "QUANTITATIVE_ENGINE_VERSION", QUANTITATIVE_ENGINE_VERSION)
        monkeypatch.setattr(scoring, "QUANTITATIVE_ENGINE_VERSION", QUANTITATIVE_ENGINE_VERSION)
        session.expire(notice, ["analysis_runs", "versions"])
        assert analysis_version_refresh_required(notice, pipeline_version=PIPELINE_VERSION,
            policy_version=POLICY_VERSION, quantitative_engine_version=QUANTITATIVE_ENGINE_VERSION)
        old_run = session.get(AnalysisRun, old_result.analysis_run_id)
        old_score = session.scalar(select(ScoreSnapshot).where(
            ScoreSnapshot.analysis_run_id == old_run.id, ScoreSnapshot.score_key == "quantitative.total"))
        assert public_quantitative_snapshot_projection(old_run, old_score) is None
        session.rollback()
        first = run_analysis_pipeline(session, notice_id=notice_id)
        second = run_analysis_pipeline(session, notice_id=notice_id)
        assert first.analysis_run_id != old_result.analysis_run_id
        assert second.reused and first.analysis_run_id == second.analysis_run_id
        assert attempt.source_payload == original
        run = session.get(AnalysisRun, first.analysis_run_id)
        score = session.scalar(select(ScoreSnapshot).where(
            ScoreSnapshot.analysis_run_id == run.id, ScoreSnapshot.score_key == "quantitative.total"))
        assert run.basis_versions["quantitative_engine"] == QUANTITATIVE_ENGINE_VERSION
        assert [list(item) for item in run.basis_versions["source_extraction_contracts"]] == [[contract.prompt, contract.schema]]
        assert public_quantitative_snapshot_projection(run, score) is not None
        assert session.scalar(select(func.count()).select_from(AnalysisRun)) == 2
        session.rollback()
        duplicate = _accepted_outcome_for_duplicate_content(session, notice_id=notice_id,
            attachment_id=record.attachment_id, document_sha256=record.document_sha256,
            source_text_sha256=record.document_sha256,
            analysis_input_sha256=record.document_sha256)
        # A predecessor stays readable under its original proof; it cannot
        # be cloned and restamped as if the new prompt produced its output.
        assert duplicate is None
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("tail", ["LTE", "LT"])
@pytest.mark.parametrize("field,value", [("comparison_value", True), ("award_value", False),
    ("comparison_value", None), ("comparison_value", float("inf")),
    ("category_values", ["SYN invalid numeric category"])])
def test_new_extraction_count_tail_keeps_strict_numeric_shape(tail, field, value):
    _, _, attempt, _ = notice_fixture(legacy=False, tail=tail)
    row = attempt.source_payload["result"]["quantitative_tables"][0]["criteria"][0]["cases"][-1]
    row[field] = value
    with pytest.raises(ValidationError):
        QuantitativeCaseLiteral.model_validate(row)


@pytest.mark.parametrize("tail,source_operator", [("LTE", "미만"), ("LT", "이하")])
def test_count_tail_never_inverts_the_source_comparator(tail, source_operator):
    notice, _, attempt, record = notice_fixture(legacy=False, tail=tail)
    payload, source = source_payload(record.attachment_id, tail=tail)
    raw = payload.model_dump(mode="json")
    case = raw["quantitative_tables"][0]["criteria"][0]["cases"][-1]
    prior_word = "이하" if tail == "LTE" else "미만"
    source = source.replace(prior_word, source_operator)
    case["literal"] = case["literal"].replace(prior_word, source_operator)
    case["evidence"]["quote"] = case["literal"]
    invalid = validate_quantitative_attachment_extraction(ExtractionPayload.model_validate(raw),
        source_text=source, attachment_id=record.attachment_id,
        document_sha256=record.document_sha256, manifest_sha256=record.manifest_sha256)
    assert invalid.status == "INCOMPLETE"
    assert "CASE_COMPARATOR_MISMATCH" in {issue.code for issue in invalid.issues}


def test_mixed_legacy_current_attachment_contracts_share_one_current_manifest():
    notice, metadata, attempt, record = notice_fixture()
    second_attachment = {
        **metadata.source_payload["attachment_manifest"][0],
        "attachment_id": "PPS-ATT-" + "b"*24, "file_name": "SYN 공고문.pdf", "slot": 2,
    }
    metadata.source_payload["attachment_manifest"].append(second_attachment)
    new_manifest = digest(metadata.source_payload["attachment_manifest"])
    record = record.model_copy(update={"manifest_sha256": new_manifest})
    record = record.model_copy(update={"validation_fingerprint_sha256":
                                      validated_quantitative_record_fingerprint(record)})
    attempt.source_payload.update(current_manifest_sha256=new_manifest,
                                 quantitative_validation_record=record.model_dump(mode="json"))
    empty_result = ExtractionPayload(document_type="NOTICE", summary="SYN 안내",
        requirements=[], missing_or_unreadable=[], quantitative_tables=[],
        quantitative_table_not_applicable=None)
    second_record = validate_quantitative_attachment_extraction(empty_result,
        source_text="SYN 안내", attachment_id=second_attachment["attachment_id"],
        document_sha256="c"*64, manifest_sha256=new_manifest)
    second_payload = {**deepcopy(attempt.source_payload),
        "attachment_id": second_attachment["attachment_id"], "source_label": "SYN 공고문.pdf",
        "document_sha256": "c"*64, "manifest_sha256": digest(second_attachment),
        "prompt_version": CURRENT.prompt, "schema_version": CURRENT.schema,
        "processing_version": CURRENT.processing,
        "result": empty_result.model_dump(mode="json"),
        "quantitative_validation_record": second_record.model_dump(mode="json")}
    NoticeVersion(id="SYN-SECOND", notice=notice, version_no=3, file_sha256="c"*64,
        document_complete=True, extraction_status="ACCEPTED", extraction_confidence=0.99,
        source_payload=second_payload)
    assert public_analysis_reason(notice.versions).state == "ANALYZED"
    assert _current_dynamic_quantitative_profile(notice).status == "AVAILABLE"
    assert len(_public_document_analyses(notice.versions)) == 2
    assert len(_current_pps_manifest_basis(notice.versions,
        prompt_version=PROMPT_VERSION)["accepted_attachment_ids"]) == 2


@pytest.mark.parametrize("field,value", [("prompt_version", LEGACY.prompt),
    ("extraction_schema_version", LEGACY.schema)])
def test_new_validation_cannot_write_a_predecessor_extraction_contract(field, value):
    payload, source = source_payload("SYN-ATTACHMENT")
    with pytest.raises(ValueError, match="current extraction contract"):
        validate_quantitative_attachment_extraction(payload, source_text=source,
            attachment_id="SYN-ATTACHMENT", document_sha256="a"*64,
            manifest_sha256="b"*64, **{field: value})


def test_legacy_review_retry_reuses_the_new_current_review_on_continuation():
    notice, _, old, record = notice_fixture(gap=True)
    retry_ids = current_retryable_review_version_ids(notice.versions)
    payload = {**deepcopy(old.source_payload), "prompt_version": CURRENT.prompt,
        "schema_version": CURRENT.schema, "processing_version": CURRENT.processing, "status": "REVIEW",
        "error_code": "SCHEMA_VALIDATION_ERROR", "result": None,
        "quantitative_validation_record": None}
    new = NoticeVersion(id="SYN-RETRY-RESULT", notice=notice, version_no=3,
        file_sha256=old.file_sha256, document_complete=False, extraction_status="REVIEW",
        extraction_confidence=0, source_payload=payload, created_at=datetime.now(timezone.utc))
    assert _matching_extraction_version([new, old], attachment_id=record.attachment_id,
        manifest_sha256=payload["manifest_sha256"], current_manifest_sha256=record.manifest_sha256,
        document_sha256=record.document_sha256, retry_reviewed_version_ids=retry_ids) is new
    projected = _stored_attachment_result(new, attachments_discovered=1,
                                         retry_reviewed_version_ids=retry_ids)
    assert projected.status == "REVIEW" and projected.openai_calls == 0


@pytest.mark.parametrize("mutation", ["raw_new_operator", "bad_fingerprint", "mixed_tuple"])
def test_source_selection_cannot_publish_legacy_requirements_without_record_proof(mutation):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        notice, _, attempt, _ = notice_fixture()
        if mutation == "raw_new_operator":
            attempt.source_payload["result"]["quantitative_tables"][0]["criteria"][0]["cases"][-1]["operator"] = "LTE"
        elif mutation == "bad_fingerprint":
            attempt.source_payload["quantitative_validation_record"]["validation_fingerprint_sha256"] = "0"*64
        else:
            attempt.source_payload["schema_version"] = CURRENT.schema
        notice_id = notice.id
        session.add(notice)
        session.commit()
        assert _select_source_versions(session, notice_id=notice_id,
            prompt_version=PROMPT_VERSION, source_version_ids=None) == []
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("status", ["ACCEPTED", "REVIEW"])
@pytest.mark.parametrize("target,field,value", [
    ("outcome", "prompt_version", LEGACY.prompt),
    ("outcome", "schema_version", LEGACY.schema),
    ("record", "prompt_version", LEGACY.prompt),
    ("record", "extraction_schema_version", LEGACY.schema),
    ("record", "validator_version", LEGACY.validator),
])
def test_persistence_rejects_old_outcome_or_record_contract_before_new_writes(status, target, field, value):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        notice, metadata, attempt, record = notice_fixture(legacy=False)
        outcome = ExtractionOutcome(status=status, message="SYN write boundary", api_calls=0,
            data=ExtractionPayload.model_validate(attempt.source_payload["result"]) if status == "ACCEPTED" else None)
        if target == "outcome":
            outcome = outcome.model_copy(update={field: value})
        else:
            record = record.model_copy(update={field: value})
        session.add(notice)
        session.commit()
        with pytest.raises(PpsEnrichmentError, match="EXTRACTION_WRITE_CONTRACT_MISMATCH"):
            _persist_extraction_version(session, notice_id=notice.id,
                attachment=metadata.source_payload["attachment_manifest"][0],
                manifest_sha256=attempt.source_payload["manifest_sha256"],
                current_manifest_sha256=attempt.source_payload["current_manifest_sha256"],
                document_sha256=attempt.file_sha256, outcome=outcome, error_code=None,
                quantitative_validation_record=record)
        assert session.scalar(select(func.count()).select_from(NoticeVersion)) == 2
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("prior_schema", [LEGACY.schema, None, CURRENT.schema])
def test_review_persistence_dedup_requires_exact_current_schema(prior_schema):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = build_session_factory(engine)()
    try:
        notice, metadata, old, _ = notice_fixture(legacy=False)
        old.source_payload.update(status="REVIEW", error_code="UNSUPPORTED_ATTACHMENT_TYPE",
            result=None, quantitative_validation_record=None)
        if prior_schema is None:
            old.source_payload.pop("schema_version")
        else:
            old.source_payload["schema_version"] = prior_schema
        old.extraction_status = "REVIEW"
        old.document_complete = False
        old_payload = deepcopy(old.source_payload)
        session.add(notice)
        session.commit()
        outcome = ExtractionOutcome(status="REVIEW", message="SYN deterministic review",
            review_code="R07", error_code="UNSUPPORTED_ATTACHMENT_TYPE", api_calls=0)
        params = dict(notice_id=notice.id, attachment=metadata.source_payload["attachment_manifest"][0],
            manifest_sha256=old.source_payload["manifest_sha256"],
            current_manifest_sha256=old.source_payload["current_manifest_sha256"],
            document_sha256=old.file_sha256, outcome=outcome, error_code=None)
        current = _persist_extraction_version(session, **params)
        assert (current.id == old.id) is (prior_schema == CURRENT.schema)
        assert classify_attempt_header(current.source_payload) == "CURRENT"
        assert _persist_extraction_version(session, **params).id == current.id
        assert old.source_payload == old_payload
        assert session.scalar(select(func.count()).select_from(NoticeVersion)) == (2 if prior_schema == CURRENT.schema else 3)
    finally:
        session.close()
        engine.dispose()
