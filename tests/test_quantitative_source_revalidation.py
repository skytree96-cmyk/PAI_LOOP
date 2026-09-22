"""Source-revalidation proofs use synthetic native bytes, without network/DB."""
from copy import deepcopy
import hashlib
import io
import json
import zipfile
from xml.sax.saxutils import escape

import pytest
from pydantic import ValidationError

from pai_loop.extraction_contracts import (
    CURRENT_EXTRACTION_CONTRACT as CURRENT, PREVIOUS_CASE_CONTRACT as PREVIOUS,
    PREVIOUS_EXTRACTION_CONTRACT, PREVIOUS_PROCESSING_CONTRACT,
    classify_attempt_header,
)
from pai_loop.integrations.openai_extraction import ExtractionOutcome, ExtractionPayload
from pai_loop.pps_enrichment import build_attachment_manifest, extract_pps_document_content
from pai_loop.quantitative_rule_extraction import (
    ValidatedQuantitativeAttachmentRecord, validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint,
)
from pai_loop.quantitative_scoring import quantitative_request_from_candidate_profile
from pai_loop.quantitative_source_revalidation import (
    QuantitativeSourceRevalidation, revalidate_quantitative_source, revalidation_json_sha256,
)
from test_extraction_contract_compatibility import source_payload


def sha(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def native_docx(source, *, embedded=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in source.splitlines())
            + "</w:body></w:document>")
        if embedded:
            archive.writestr("word/embeddings/SYN-unsupported.bin", b"SYN")
    return output.getvalue()


def fixture(*, gap=False, embedded=False, extension="docx", sibling=False,
            contract=PREVIOUS, credit=False):
    metadata = dict(bidNtceNo="SYN-REVALIDATION", bidNtceOrd="000",
        ntceSpecFileNm1=f"SYN 제안요청서.{extension}",
        ntceSpecDocUrl1="https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-REVALIDATION&fileSeq=1")
    if sibling:
        metadata.update(ntceSpecFileNm2="SYN 형제 첨부.pdf",
            ntceSpecDocUrl2="https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do?bidPbancNo=SYN-REVALIDATION&fileSeq=2")
    manifest = build_attachment_manifest(metadata)
    attachment = manifest[0]
    aid = attachment["attachment_id"]
    payload, text = source_payload(aid, tail="LT", gap=gap)
    if credit:
        from test_dense_case_source_binding import credit_fixture, ATT
        credit_raw, text = credit_fixture()
        credit_raw["quantitative_tables"][0]["criteria"][0]["unit"] = None
        payload = ExtractionPayload.model_validate(json.loads(json.dumps(credit_raw).replace(ATT, aid)))
    raw = payload.model_dump(mode="json")
    for table in raw["quantitative_tables"]:
        for candidate in table["criteria"]:
            for row in candidate["cases"]:
                row.pop("comparison_upper_value", None)
    native = native_docx(text, embedded=embedded) if extension == "docx" else b"SYN invalid HWP"
    canonical = extract_pps_document_content(attachment["file_name"], native).text if extension == "docx" else text
    native_sha, manifest_sha = sha(native), revalidation_json_sha256(manifest)
    record = validate_quantitative_attachment_extraction(payload, source_text=canonical,
        attachment_id=aid, document_sha256=native_sha, manifest_sha256=manifest_sha)
    record = record.model_copy(update=dict(prompt_version=contract.prompt,
        extraction_schema_version=contract.schema, validator_version=contract.validator))
    record = record.model_copy(update=dict(validation_fingerprint_sha256=validated_quantitative_record_fingerprint(record)))
    attempt = dict(kind="OPENAI_REQUIREMENT_EXTRACTION", source_kind="PPS_PUBLIC_ATTACHMENT",
        attachment_id=aid, source_label=attachment["file_name"], status="ACCEPTED",
        prompt_version=contract.prompt, schema_version=contract.schema, processing_version=contract.processing,
        document_sha256=native_sha, manifest_sha256=revalidation_json_sha256(attachment),
        document_processing=dict(source_text_sha256=sha(canonical)),
        current_manifest_sha256=manifest_sha, result=raw, quantitative_validation_record=record.model_dump(mode="json"))
    return dict(source_version_id="SYN-SOURCE-VERSION", source_attempt=attempt,
        expected_attempt_sha256=revalidation_json_sha256(attempt), native_bytes=native,
        expected_native_sha256=native_sha, canonical_text=canonical, expected_canonical_sha256=sha(canonical),
        full_manifest=manifest, expected_manifest_sha256=manifest_sha)


@pytest.fixture(autouse=True)
def no_external_operations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("SYN_NETWORK_PROVIDER_DB_FORBIDDEN")
    monkeypatch.setattr("httpx.Client.request", forbidden)
    monkeypatch.setattr("pai_loop.database.build_engine", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract", forbidden)
    monkeypatch.setattr("pai_loop.integrations.openai_extraction.OpenAIExtractionClient.extract_quantitative_probe", forbidden)


def test_native_parser_reproduces_source_without_restamping_or_manifest_completion():
    inputs = fixture(sibling=True)
    before = deepcopy(inputs)
    result = revalidate_quantitative_source(**inputs)
    assert inputs == before
    assert result.native_canonical_status == "VERIFIED"
    assert result.proof.parser_complete is True
    assert result.proof.parsed_text_sha256 == inputs["expected_canonical_sha256"]
    assert result.origin.extraction_contract == tuple(PREVIOUS)
    assert result.proof.validator_version == CURRENT.validator
    assert result.proof.processing_version == CURRENT.processing
    assert result.origin.raw_result_sha256 == revalidation_json_sha256(inputs["source_attempt"]["result"])
    assert result.profile.status == "AVAILABLE"
    assert len(result.origin.manifest_attachment_ids) == 2
    assert result.profile.document_bindings == () and result.profile.manifest_sha256 is None
    assert result.attachment_coverage_complete is result.persistence_eligible is False
    assert result.proof.previous_validation_reused is False
    request = quantitative_request_from_candidate_profile(result.profile)
    assert request.activation_status == "REVIEW_REQUIRED"
    assert "CURRENT_ATTACHMENT_COVERAGE_INCOMPLETE" in request.activation_reasons
    assert classify_attempt_header(result.model_dump(mode="json")) == "UNSUPPORTED"
    with pytest.raises(ValidationError):
        ValidatedQuantitativeAttachmentRecord.model_validate(result.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        ExtractionOutcome.model_validate(result.model_dump(mode="json"))
    assert result == revalidate_quantitative_source(**inputs)


@pytest.mark.parametrize("field", ["expected_attempt_sha256", "expected_native_sha256", "expected_canonical_sha256", "expected_manifest_sha256"])
def test_changed_frozen_digest_stops_before_native_parser(field, monkeypatch):
    inputs = fixture()
    inputs[field] = "f" * 64
    monkeypatch.setattr("pai_loop.pps_enrichment.extract_pps_document_content",
        lambda *args: pytest.fail("invalid frozen binding reached native parser"))
    with pytest.raises(ValueError, match="SHA256_MISMATCH"):
        revalidate_quantitative_source(**inputs)


@pytest.mark.parametrize("part", ["prompt", "schema", "validator", "processing"])
def test_mixed_contract_still_fails_with_consistent_input_hash(part):
    inputs = fixture()
    attempt = inputs["source_attempt"]
    field = dict(prompt="prompt_version", schema="schema_version", processing="processing_version").get(part)
    if field:
        attempt[field] = getattr(CURRENT, part)
    else:
        attempt["quantitative_validation_record"]["validator_version"] = CURRENT.validator
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(attempt)
    with pytest.raises(ValueError, match="CONTRACT_UNSUPPORTED"):
        revalidate_quantitative_source(**inputs)


@pytest.mark.parametrize("target", ["raw", "old_record"])
def test_old_contract_cannot_claim_new_case_operator(target):
    inputs = fixture()
    attempt = inputs["source_attempt"]
    row = (attempt["result"]["quantitative_tables"][0]["criteria"][0]["cases"][0]
           if target == "raw" else attempt["quantitative_validation_record"]["available_candidates"][0]["cases"][0])
    row.update(operator="BETWEEN", comparison_value=5, comparison_upper_value=6)
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(attempt)
    with pytest.raises(ValueError, match="PREVIOUS_CASE_VOCABULARY_VIOLATION"):
        revalidate_quantitative_source(**inputs)


@pytest.mark.parametrize("target", ["source_label", "document_sha256", "current_manifest_sha256", "manifest_sha256"])
def test_source_descriptor_binding_cannot_be_replaced(target):
    inputs = fixture()
    inputs["source_attempt"][target] = "SYN substituted descriptor"
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(inputs["source_attempt"])
    with pytest.raises(ValueError, match="SOURCE_ATTACHMENT_BINDING_MISMATCH"):
        revalidate_quantitative_source(**inputs)


def test_hash_consistent_but_unreproduced_canonical_is_only_a_diagnostic():
    inputs = fixture()
    inputs["canonical_text"] += "\nSYN invented scoring row"
    inputs["expected_canonical_sha256"] = sha(inputs["canonical_text"])
    inputs["source_attempt"]["document_processing"]["source_text_sha256"] = inputs["expected_canonical_sha256"]
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(inputs["source_attempt"])
    result = revalidate_quantitative_source(**inputs)
    assert result.native_canonical_status == "DIAGNOSTIC_ONLY"
    assert result.diagnostic_codes == ("NATIVE_CANONICAL_MISMATCH",)
    assert result.profile is None


def test_changed_stored_canonical_digest_fails_even_when_attempt_digest_matches():
    inputs = fixture()
    inputs["source_attempt"]["document_processing"]["source_text_sha256"] = "f" * 64
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(inputs["source_attempt"])
    with pytest.raises(ValueError, match="STORED_CANONICAL_SHA256_MISMATCH"):
        revalidate_quantitative_source(**inputs)


@pytest.mark.parametrize("options,code", [({"extension":"hwp"}, "NATIVE_PARSER_REJECTED"),
                                         ({"embedded":True}, "NATIVE_PARSER_INCOMPLETE")])
def test_native_parser_failure_or_partial_read_cannot_produce_a_profile(options, code):
    result = revalidate_quantitative_source(**fixture(**options))
    assert result.native_canonical_status == "DIAGNOSTIC_ONLY"
    assert result.profile is None and code in result.diagnostic_codes
    assert "SYN invalid HWP" not in result.model_dump_json()


def test_old_stale_fingerprint_is_history_and_raw_review_stays_visible():
    inputs = fixture(gap=True)
    inputs["source_attempt"]["quantitative_validation_record"]["validation_fingerprint_sha256"] = "0" * 64
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(inputs["source_attempt"])
    result = revalidate_quantitative_source(**inputs)
    assert result.native_canonical_status == "VERIFIED"
    assert result.origin.original_validation_fingerprint_sha256 == "0" * 64
    assert result.profile.status != "AVAILABLE" and result.profile.issues
    assert result.proof.previous_validation_reused is False


def test_quote_not_present_in_actual_native_source_remains_unavailable():
    inputs = fixture()
    candidate = inputs["source_attempt"]["result"]["quantitative_tables"][0]["criteria"][0]
    candidate["cases"][0]["evidence"]["quote"] = "SYN source omitted an intervening page 5건 이상 5점"
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(inputs["source_attempt"])
    result = revalidate_quantitative_source(**inputs)
    assert result.native_canonical_status == "VERIFIED"
    assert result.profile.status != "AVAILABLE"


def test_revalidation_wrapper_fingerprint_cannot_be_changed_on_round_trip():
    result = revalidate_quantitative_source(**fixture())
    dumped = result.model_dump(mode="json")
    dumped["origin"]["source_version_id"] = "SYN-OTHER-SOURCE"
    with pytest.raises(ValueError, match="RESULT_FINGERPRINT_MISMATCH"):
        QuantitativeSourceRevalidation.model_validate(dumped)


@pytest.mark.parametrize("contract", [CURRENT, PREVIOUS_EXTRACTION_CONTRACT, PREVIOUS_PROCESSING_CONTRACT])
def test_modern_credit_failure_revalidates_frozen_raw_without_overwriting_history(monkeypatch, contract):
    # Reproduce the old column binder's rejection of a missing unit, preserving
    # the exact failure record to compare with the new native-source proof.
    with monkeypatch.context() as old:
        old.setattr("pai_loop.quantitative_rule_extraction._bind_enterprise_credit_column",
                    lambda candidate, **kwargs: candidate)
        inputs = fixture(contract=contract, credit=True)
    before = deepcopy(inputs)
    stored = inputs["source_attempt"]["quantitative_validation_record"]
    assert stored["status"] == "INCOMPLETE"
    assert [issue["code"] for issue in stored["issues"]].count("CASE_NUMBER_MISMATCH") == 4
    assert "CASE_TABLE_NOT_DETERMINISTIC" in {issue["code"] for issue in stored["issues"]}
    result = revalidate_quantitative_source(**inputs)
    assert result.native_canonical_status == "VERIFIED"
    assert result.profile.status == "AVAILABLE"
    assert len(result.profile.available_candidates) == 1
    assert result.profile.available_candidates[0].unit is None
    assert result.origin.extraction_contract == tuple(contract)
    assert result.origin.original_record_sha256 == revalidation_json_sha256(stored)
    assert result.origin.original_validation_fingerprint_sha256 == stored["validation_fingerprint_sha256"]
    assert result.persistence_eligible is result.attachment_coverage_complete is False
    assert quantitative_request_from_candidate_profile(result.profile).activation_status == "REVIEW_REQUIRED"
    assert QuantitativeSourceRevalidation.model_validate_json(result.model_dump_json()) == result
    assert inputs == before


@pytest.mark.parametrize("contract", [CURRENT, PREVIOUS_EXTRACTION_CONTRACT, PREVIOUS_PROCESSING_CONTRACT])
@pytest.mark.parametrize("defect", ["source_digit", "cross_attachment", "mixed_contract", "incomplete_parser"])
def test_modern_revalidation_still_requires_original_source_and_exact_contract(contract, defect):
    inputs = fixture(contract=contract, credit=True, embedded=defect == "incomplete_parser")
    attempt = inputs["source_attempt"]
    if defect == "source_digit":
        attempt["result"]["quantitative_tables"][0]["criteria"][0]["cases"][0]["award_value"] = 8
    elif defect == "cross_attachment":
        attempt["result"]["quantitative_tables"][0]["criteria"][0]["cases"][0]["evidence"]["attachment_id"] = "SYN-FOREIGN"
    elif defect == "mixed_contract":
        attempt["schema_version"] = PREVIOUS.schema
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(attempt)
    if defect in {"cross_attachment", "mixed_contract"}:
        with pytest.raises(ValueError, match="RAW_ATTACHMENT_ANCHOR_MISMATCH|CONTRACT_UNSUPPORTED"):
            revalidate_quantitative_source(**inputs)
    else:
        result = revalidate_quantitative_source(**inputs)
        assert result.persistence_eligible is False
        if defect == "incomplete_parser":
            assert result.profile is None
            assert "NATIVE_PARSER_INCOMPLETE" in result.diagnostic_codes
        else:
            assert result.profile.status != "AVAILABLE"


@pytest.mark.parametrize("contract", [CURRENT, PREVIOUS_EXTRACTION_CONTRACT, PREVIOUS_PROCESSING_CONTRACT])
def test_modern_revalidation_accepts_original_bounded_count_vocabulary(contract):
    from test_quantitative_count_ranges import fixture as range_fixture

    inputs = fixture(contract=contract)
    attempt = inputs["source_attempt"]
    raw, source = range_fixture(inline=True)
    from test_dense_case_source_binding import ATT
    raw = json.loads(json.dumps(raw).replace(ATT, attempt["attachment_id"]))
    native = native_docx(source)
    canonical = extract_pps_document_content(attempt["source_label"], native).text
    inputs.update(native_bytes=native, expected_native_sha256=sha(native),
                  canonical_text=canonical, expected_canonical_sha256=sha(canonical))
    attempt.update(result=raw, document_sha256=sha(native))
    attempt["document_processing"]["source_text_sha256"] = sha(canonical)
    attempt["quantitative_validation_record"]["document_sha256"] = sha(native)
    # The old derived record is history, not reused as the new validation.
    inputs["expected_attempt_sha256"] = revalidation_json_sha256(attempt)
    result = revalidate_quantitative_source(**inputs)
    assert result.profile.status == "AVAILABLE", result.profile.issues
    assert "BETWEEN" in {case.operator for case in result.profile.available_candidates[0].cases}
