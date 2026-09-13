"""Synthetic source-gap retries; no operational prose or source documents."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

import pai_loop.pps_enrichment as pps
import pai_loop.quantitative_rule_extraction as quant
from pai_loop.extraction_contracts import CURRENT_EXTRACTION_CONTRACT, LEGACY_CASE_CONTRACT
from pai_loop.integrations.openai_extraction import ExtractionOutcome, ExtractionPayload
from pai_loop.models import Notice, NoticeVersion
from pai_loop.source_gap_policy import quantitative_table_local_absence_targets
from test_pps_enrichment import _single_hwpx_reuse_case
from test_quantitative_rule_extraction import ATTACHMENT_ID, VALID_SOURCE, payload_with_table

# Deliberately synthetic phrasing and spacing, independent of incident prose.
LOCAL_GAP = (
    "제안 요청서 원문은 첨부되지 않아 기술 능력 평가 세부 배점표"
    "(정량적 평가 기준)를 확인할 수 없습니다."
)
GENERIC_GAP = "합성 첨부의 장비 수 평가 구간을 판독할 수 없습니다."
BENIGN_GAP = "정성평가표는 본 공고문에 포함되지 않음"
SOURCE = "합성 첨부의 일반사항입니다. 이 문구는 테스트 입력만을 위한 내용입니다."


def payload(gaps=(), *, kind="NOTICE"):
    return ExtractionPayload(document_type=kind, requirements=[], quantitative_tables=[],
                             missing_or_unreadable=list(gaps), summary="Synthetic source-gap test")


def version_for(data, *, source=SOURCE):
    record = quant.validate_quantitative_attachment_extraction(
        data, source_text=source, attachment_id=ATTACHMENT_ID,
        document_sha256="a" * 64, manifest_sha256="b" * 64,
    )
    return NoticeVersion(
        id="synthetic-gap-version", notice_id="synthetic-notice", version_no=2,
        file_sha256="a" * 64, extraction_status="COMPLETE", document_complete=True,
        created_at=datetime.now(timezone.utc), source_payload={
            "kind":"OPENAI_REQUIREMENT_EXTRACTION", "source_kind":pps.PPS_ATTACHMENT_SOURCE,
            "attachment_id":ATTACHMENT_ID, "manifest_sha256":"c"*64,
            "current_manifest_sha256":"b"*64, "document_sha256":"a"*64,
            "status":"ACCEPTED", "prompt_version":CURRENT_EXTRACTION_CONTRACT.prompt,
            "schema_version":CURRENT_EXTRACTION_CONTRACT.schema,
            "processing_version":CURRENT_EXTRACTION_CONTRACT.processing,
            "result":data.model_dump(mode="json"),
            "quantitative_validation_record":record.model_dump(mode="json"),
        },
    )


def retryable(version):
    return pps._accepted_quantitative_review_is_retryable(
        version, attachment_id=ATTACHMENT_ID, current_manifest_sha256="b"*64,
    )


@pytest.mark.parametrize("gap", [LOCAL_GAP, LOCAL_GAP.replace("원문은", "원문이").replace("정량적", "정량"), LOCAL_GAP.replace(" ", "").rstrip(".")])
def test_explicit_quantitative_rfp_original_absence_targets_only_rfp(gap):
    assert quantitative_table_local_absence_targets(gap) == ((("RFP",), ("제안요청서",)),)


@pytest.mark.parametrize("gap", [
    LOCAL_GAP.replace("정량적 평가 기준", "정성 평가 기준"),
    LOCAL_GAP.replace("(정량적 평가 기준)", ""),
    LOCAL_GAP.replace("세부 배점표", "참가자격과 세부 배점표"),
    LOCAL_GAP.replace("세부 배점표", "법령과 세부 배점표"),
    LOCAL_GAP.replace("제안 요청서", "임의 안내서"),
    LOCAL_GAP.replace("첨부되지 않아", "일부만 제공되어"),
    LOCAL_GAP.replace("확인할 수 없습니다", "판독할 수 없습니다"),
    LOCAL_GAP + " 또한 면허 정보가 없음.",
    LOCAL_GAP + " 제출서류도 누락됨.",
    "합성 가격 평가 수식의 기호를 판독할 수 없습니다.",
])
def test_narrow_rfp_grammar_preserves_other_missing_subjects(gap):
    assert quantitative_table_local_absence_targets(gap) is None


def test_generic_gap_only_is_explicitly_retryable_without_candidates():
    v = version_for(payload([GENERIC_GAP]))
    record = v.source_payload["quantitative_validation_record"]
    assert record["status"] == "INCOMPLETE" and not record["review_candidates"]
    assert retryable(v) is True
    assert pps._stored_outcome_is_idempotent(v, v.source_payload) is True
    assert pps._stored_outcome_is_idempotent(v, v.source_payload, retry_reviewed_version_ids=frozenset({v.id})) is False


@pytest.mark.parametrize("gaps", [[], [BENIGN_GAP], [LOCAL_GAP]])
def test_normal_no_table_benign_and_local_markers_are_not_retried(gaps):
    v = version_for(payload(gaps))
    assert retryable(v) is False
    assert pps._stored_outcome_is_idempotent(v, v.source_payload, retry_reviewed_version_ids=frozenset({v.id})) is True


def test_valid_available_table_is_not_retried():
    v = version_for(payload_with_table(), source=VALID_SOURCE)
    assert v.source_payload["quantitative_validation_record"]["status"] == "AVAILABLE"
    assert retryable(v) is False


@pytest.mark.parametrize("replacement", [None, {}, {"missing_or_unreadable":[GENERIC_GAP]}, payload([]).model_dump(mode="json"), payload([BENIGN_GAP]).model_dump(mode="json"), payload(["  "]).model_dump(mode="json"), payload(["\u200b"]).model_dump(mode="json"), payload([BENIGN_GAP,"\u200b"]).model_dump(mode="json")])
def test_generic_record_without_valid_actual_nonbenign_gap_is_not_retried(replacement):
    v = version_for(payload([GENERIC_GAP]))
    v.source_payload = {**v.source_payload, "result":replacement}
    assert retryable(v) is False


@pytest.mark.parametrize("field", ["manifest", "document", "fingerprint", "contract"])
def test_invalid_record_proof_cannot_enter_gap_retry(field):
    v = version_for(payload([GENERIC_GAP]))
    data = copy.deepcopy(v.source_payload)
    if field == "manifest": data["quantitative_validation_record"]["manifest_sha256"] = "d"*64
    if field == "document": data["quantitative_validation_record"]["document_sha256"] = "d"*64
    if field == "fingerprint": data["quantitative_validation_record"]["validation_fingerprint_sha256"] = "d"*64
    if field == "contract": data["schema_version"] = "unsupported-schema"
    v.source_payload = data
    assert retryable(v) is False


def merge(records, gaps_by_id, *, labels=None):
    labels = labels or {}
    return quant.merge_validated_quantitative_records(
        records, expected_documents={r.attachment_id:r.document_sha256 for r in records},
        manifest_sha256="b"*64,
        attachment_profiles={r.attachment_id:{"document_type":"NOTICE" if r.attachment_id == "NOTICE-SYN" else "RFP", "source_label":labels.get(r.attachment_id,"합성 공고문.pdf" if r.attachment_id == "NOTICE-SYN" else "합성 제안요청서.pdf"), "missing_or_unreadable":gaps_by_id[r.attachment_id]} for r in records},
    )


@pytest.mark.parametrize("supplier", ["absent", "incomplete", "valid", "form", "self"])
def test_new_local_marker_requires_an_independent_available_rfp_table(supplier):
    notice = quant.validate_quantitative_attachment_extraction(payload([LOCAL_GAP]),source_text=SOURCE,attachment_id="NOTICE-SYN",document_sha256="a"*64,manifest_sha256="b"*64)
    assert {i.code for i in notice.issues} == {"ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"}
    records=[notice]; gaps={"NOTICE-SYN":[LOCAL_GAP]}; labels={}
    if supplier != "absent":
        rfp_payload=payload_with_table().model_copy(update={"missing_or_unreadable":[GENERIC_GAP] if supplier == "incomplete" else []})
        rfp=quant.validate_quantitative_attachment_extraction(rfp_payload,source_text=VALID_SOURCE,attachment_id=ATTACHMENT_ID,document_sha256="d"*64,manifest_sha256="b"*64)
        records.append(rfp);gaps[ATTACHMENT_ID]=list(rfp_payload.missing_or_unreadable)
    if supplier == "form":labels[ATTACHMENT_ID]="합성 제안요청서 작성양식.pdf"
    if supplier == "self":labels["NOTICE-SYN"]="합성 제안요청서.pdf"
    result=merge(records,gaps,labels=labels)
    assert (result.status == "AVAILABLE") is (supplier == "valid")
    if supplier != "valid": assert result.status == "INCOMPLETE"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("second_outcome", ["LOCAL_ACCEPTED", "GENERIC_ACCEPTED", "REVIEW"])
def test_prior_generic_record_enters_frozen_retry_and_new_version_stops_continuations(monkeypatch, legacy, second_outcome):
    engine,factory,notice_id,transport=_single_hwpx_reuse_case(notice_key=f"PPS-SYN-GAP-{legacy}-{second_outcome}",source_text=SOURCE)
    class Client:
        calls=0
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):return None
        def extract(self,*,document_text,allowed_attachment_ids):
            type(self).calls+=1
            assert SOURCE in document_text
            if type(self).calls > 1 and second_outcome == "REVIEW":
                return ExtractionOutcome(status="REVIEW",message="Synthetic incomplete response",error_code="SCHEMA_VALIDATION_ERROR",api_calls=1)
            gap=GENERIC_GAP if type(self).calls > 1 and second_outcome == "GENERIC_ACCEPTED" else LOCAL_GAP
            return ExtractionOutcome(status="ACCEPTED",message="Synthetic accepted gap",api_calls=1,data=payload([gap]))
    def run(ids=frozenset()):
        with factory() as session:
            return pps.enrich_notice_from_pps(session,notice_id=notice_id,openai_api_key="synthetic-key",openai_model="synthetic-model",transport=transport,openai_client_factory=Client,retry_reviewed_version_ids=ids)
    # Reproduce the predecessor classifier for this synthetic phrase only.
    shared_classifier=quant._shared_quantitative_table_local_absence_targets
    with monkeypatch.context() as prior:
        prior.setattr(quant,"_shared_quantitative_table_local_absence_targets",lambda value:None if value == LOCAL_GAP else shared_classifier(value))
        first=run()
    with factory() as session:
        old=session.get(NoticeVersion,first.version_id)
        data=copy.deepcopy(old.source_payload)
        if legacy:
            data["prompt_version"]=LEGACY_CASE_CONTRACT.prompt
            data["schema_version"]=LEGACY_CASE_CONTRACT.schema
            data["processing_version"]=LEGACY_CASE_CONTRACT.processing
            raw=data["quantitative_validation_record"]
            raw.update(prompt_version=LEGACY_CASE_CONTRACT.prompt,extraction_schema_version=LEGACY_CASE_CONTRACT.schema,validator_version=LEGACY_CASE_CONTRACT.validator)
            record=quant.ValidatedQuantitativeAttachmentRecord.model_validate(raw)
            raw["validation_fingerprint_sha256"]=quant.validated_quantitative_record_fingerprint(record)
            old.source_payload=data;session.commit()
        original=copy.deepcopy(old.source_payload)
        record=quant.ValidatedQuantitativeAttachmentRecord.model_validate(original["quantitative_validation_record"])
        aid=original["attachment_id"]
        assert pps._has_valid_quantitative_record(old,attachment_id=aid,current_manifest_sha256=original["current_manifest_sha256"]) is True
        profile=quant.merge_validated_quantitative_records([record],expected_documents={aid:old.file_sha256},manifest_sha256=original["current_manifest_sha256"],attachment_profiles={aid:{"document_type":"NOTICE","source_label":"합성 공고문.pdf","missing_or_unreadable":[LOCAL_GAP]}},source_payloads={aid:original})
        assert "SOURCE_GAP_BINDING_MISMATCH" in {i.code for i in profile.issues}
        ids=pps.current_retryable_review_version_ids(list(session.get(Notice,notice_id).versions))
        assert ids == frozenset({first.version_id})
    assert run().version_id == first.version_id
    assert Client.calls == 1
    second=run(ids)
    assert second.version_id != first.version_id and second.openai_calls == 1
    for _ in range(2):
        continued=run(ids)
        assert continued.version_id == second.version_id and continued.openai_calls == 0
    assert Client.calls == 2
    with factory() as session:
        assert session.get(NoticeVersion,first.version_id).source_payload == original
        newest=session.get(NoticeVersion,second.version_id)
        assert newest.source_payload["prompt_version"] == CURRENT_EXTRACTION_CONTRACT.prompt
        assert newest.source_payload["schema_version"] == CURRENT_EXTRACTION_CONTRACT.schema
        if second_outcome == "LOCAL_ACCEPTED":
            assert {i["code"] for i in newest.source_payload["quantitative_validation_record"]["issues"]} == {"ATTACHMENT_LOCAL_QUANTITATIVE_TABLE_ABSENT"}
            assert pps.current_retryable_review_version_ids(list(session.get(Notice,notice_id).versions)) == frozenset()
    engine.dispose()


def test_real_gap_with_format_controls_remains_retryable():
    assert retryable(version_for(payload(["\u200b" + GENERIC_GAP + "\u200b"]))) is True


def test_non_pps_pipeline_cache_refreshes_when_local_gap_policy_changes(monkeypatch):
    import pai_loop.analysis_pipeline as pipeline
    from pai_loop.database import Base, build_engine, build_session_factory
    from test_analysis_pipeline import _notice, _source_version, _requirement, _quantitative_table, _verified_boolean_fact
    engine=build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory=build_session_factory(engine)
    with factory() as session:
        notice=_notice(session,notice_key="SYN-GAP-POLICY-CACHE",title="합성 분류 캐시 검증")
        notice.risk_dimensions=None
        prior_classifier=quant._shared_quantitative_table_local_absence_targets
        with monkeypatch.context() as old:
            old.setattr(quant,"_shared_quantitative_table_local_absence_targets",lambda value:None if value == LOCAL_GAP else prior_classifier(value))
            session.add(_source_version(notice,version_no=1,attachment_id="SYN-NOTICE",digest_char="a",document_type="NOTICE",source_label="합성 공고문.pdf",requirements=[_requirement("SYN-REQ","경쟁입찰참가자격 등록을 완료한 업체여야 함",attachment_id="SYN-NOTICE")],missing=[LOCAL_GAP],quantitative_tables=[]))
        session.add(_source_version(notice,version_no=2,attachment_id="SYN-RFP",digest_char="b",document_type="RFP",source_label="합성 제안요청서.hwp",requirements=[],missing=[],quantitative_tables=[_quantitative_table("SYN-RFP")]))
        _verified_boolean_fact(session,"bidder_registration")
        session.commit()
        notice_id=notice.id
    classifier=pipeline.quantitative_table_local_absence_targets
    with monkeypatch.context() as old:
        old.setattr(pipeline,"PIPELINE_VERSION","analysis-pipeline-0.6.5")
        old.setattr(pipeline,"quantitative_table_local_absence_targets",lambda value:None if value == LOCAL_GAP else classifier(value))
        with factory() as session:
            first=pipeline.run_analysis_pipeline(session,notice_id=notice_id)
        # The old identity would reuse the stale partial result after reclassification.
        old.setattr(pipeline,"quantitative_table_local_absence_targets",classifier)
        with factory() as session:
            stale=pipeline.run_analysis_pipeline(session,notice_id=notice_id)
        assert stale.reused is True and stale.status == first.status == "PARTIAL"
    assert pipeline.PIPELINE_VERSION == "analysis-pipeline-0.6.6"
    with factory() as session:
        current=pipeline.run_analysis_pipeline(session,notice_id=notice_id)
    assert current.reused is False and current.status == "COMPLETED"
    assert current.input_sha256 != first.input_sha256
    with factory() as session:
        assert pipeline.run_analysis_pipeline(session,notice_id=notice_id).reused is True
    engine.dispose()


def test_source_bound_not_applicable_record_is_not_retried():
    statement="합성 사업은 정량평가가 해당 없음"
    data=ExtractionPayload.model_validate({
        "document_type":"RFP","requirements":[],"quantitative_tables":[],
        "quantitative_table_not_applicable":{"reason_literal":"정량평가가 해당 없음","evidence":{"attachment_id":ATTACHMENT_ID,"page":1,"section":"합성 일반사항","quote":statement,"confidence":0.99}},
        "missing_or_unreadable":[],"summary":"Synthetic no-quantitative applicability",
    })
    v=version_for(data,source=statement)
    assert v.source_payload["quantitative_validation_record"]["status"] == "NOT_APPLICABLE"
    assert retryable(v) is False
