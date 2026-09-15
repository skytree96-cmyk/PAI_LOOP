"""SYN native -> QRE -> shared company resolvers -> local score integration."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import zipfile

import pytest
from pydantic import ValidationError

from pai_loop.integrations.openai_extraction import ExtractionPayload
from pai_loop.pps_enrichment import extract_pps_document_content
from pai_loop.quantitative_review_preview import (
    ReviewedNativeAttachment, preview_reviewed_quantitative_inputs,
)
from pai_loop.quantitative_rule_extraction import (
    merge_validated_quantitative_records, validate_quantitative_attachment_extraction,
)
from pai_loop.quantitative_scoring import quantitative_request_from_candidate_profile
from pai_loop.quantitative_source_revalidation import revalidation_json_sha256 as digest
from test_dense_case_source_binding import credit_fixture, ATT
from test_quantitative_auto_activation import _company_fact
from test_quantitative_replay_cli import replay
from test_quantitative_source_revalidation import fixture, native_docx, sha

AS_OF = datetime(2026, 9, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_effects():
    with replay.no_external_effects() as calls:
        yield
        assert not calls


def inputs(*, sibling=False):
    base = fixture(sibling=sibling)
    aid = base["source_attempt"]["attachment_id"]
    raw, text = credit_fixture()
    raw = json.loads(json.dumps(raw).replace(ATT, aid))
    native = native_docx(text)
    documents = {item["attachment_id"]: sha(native) for item in base["full_manifest"]}
    return dict(full_manifest=base["full_manifest"],
        expected_manifest_sha256=base["expected_manifest_sha256"],
        expected_documents=documents,
        reviewed_attachments=[ReviewedNativeAttachment(aid, native, raw, digest(raw))], as_of=AS_OF)


def bound_fact(data):
    item = data["reviewed_attachments"][0]
    source = extract_pps_document_content(data["full_manifest"][0]["file_name"], item.native_bytes).text
    record = validate_quantitative_attachment_extraction(
        ExtractionPayload.model_validate(item.reviewed_payload), source_text=source,
        attachment_id=item.attachment_id, document_sha256=sha(item.native_bytes),
        manifest_sha256=data["expected_manifest_sha256"],
    )
    profile = merge_validated_quantitative_records([record],
        expected_documents=data["expected_documents"], manifest_sha256=data["expected_manifest_sha256"])
    criterion = quantitative_request_from_candidate_profile(profile).criteria[0]
    return _company_fact(fact_key="company.credit_rating", value={"value": "A0", "unit": "등급",
        "fact_binding_sha256": criterion.fact_binding_sha256})


def test_native_rule_and_exact_company_evidence_reach_same_engine_without_approval():
    data = inputs()
    before = deepcopy(data)
    fact = bound_fact(data)
    missing = preview_reviewed_quantitative_inputs(**data)
    connected = preview_reviewed_quantitative_inputs(**data, company_facts=(f for f in [fact]))
    assert missing.estimate.estimated_points is None
    assert connected.estimate.overall_status == "CONFIRMED"
    assert connected.estimate.estimated_points == 9
    assert data == before
    report = connected.private_report()
    assert report["purpose"] == "LOCAL_REVIEWED_QUANTITATIVE_PREVIEW"
    assert report["persistence_eligible"] is report["production_eligible"] is False
    assert report["source_coverage_verified"] is False
    assert not {"profile", "record", "compiled_request"} & report.keys()


@pytest.mark.parametrize("field", ["expected_manifest_sha256", "native", "payload"])
def test_changed_frozen_inputs_stop_before_parser(field, monkeypatch):
    data = inputs()
    if field == "expected_manifest_sha256":
        data[field] = "f" * 64
    elif field == "native":
        data["reviewed_attachments"][0] = replace(data["reviewed_attachments"][0], native_bytes=b"SYN changed")
    else:
        data["reviewed_attachments"][0].reviewed_payload["summary"] = "SYN changed"
    monkeypatch.setattr("pai_loop.pps_enrichment.extract_pps_document_content",
                        lambda *_: pytest.fail("changed input reached parser"))
    with pytest.raises(ValueError, match="SHA256_MISMATCH"):
        preview_reviewed_quantitative_inputs(**data)


@pytest.mark.parametrize("change", ["missing_binding", "extra_binding", "duplicate_manifest", "duplicate_input", "outside_input"])
def test_exact_manifest_and_unique_inputs_required(change):
    data = inputs()
    item = data["reviewed_attachments"][0]
    if change == "missing_binding":
        data["expected_documents"] = {}
    elif change == "extra_binding":
        data["expected_documents"]["SYN-EXTRA"] = "a" * 64
    elif change == "duplicate_manifest":
        data["full_manifest"].append(deepcopy(data["full_manifest"][0]))
        data["expected_manifest_sha256"] = digest(data["full_manifest"])
    elif change == "duplicate_input":
        data["reviewed_attachments"].append(item)
    else:
        data["reviewed_attachments"] = [replace(item, attachment_id="SYN-OUTSIDE")]
    with pytest.raises(ValueError):
        preview_reviewed_quantitative_inputs(**data)


def test_missing_sibling_blocks_whole_preview_without_consuming_company():
    data = inputs(sibling=True)

    def forbidden():
        pytest.fail("incomplete source consumed company inputs")
        yield

    result = preview_reviewed_quantitative_inputs(**data, company_facts=forbidden())
    assert result.estimate.activation_status == "REVIEW_REQUIRED"
    assert result.estimate.estimated_points is None
    assert "VALIDATED_RECORD_MISSING" in result.profile_issue_codes
    assert "REVIEWED_NATIVE_INPUT_MISSING" in [c.code for c in result.attachment_checks]


@pytest.mark.parametrize("kind", ["bad_hwp", "empty", "embedded"])
def test_failed_or_incomplete_native_never_reaches_company_score(kind):
    base = fixture(extension="hwp" if kind == "bad_hwp" else "docx", embedded=kind == "embedded")
    item = base["source_attempt"]
    native = native_docx("") if kind == "empty" else base["native_bytes"]
    result = preview_reviewed_quantitative_inputs(
        full_manifest=base["full_manifest"], expected_manifest_sha256=base["expected_manifest_sha256"],
        expected_documents={item["attachment_id"]: sha(native)}, as_of=AS_OF,
        reviewed_attachments=[ReviewedNativeAttachment(item["attachment_id"], native, item["result"], digest(item["result"]))],
    )
    assert result.estimate.estimated_points is None
    assert result.estimate.activation_status == "REVIEW_REQUIRED"
    assert result.attachment_checks[0].code in {"NATIVE_PARSER_INCOMPLETE", "NATIVE_PARSER_REJECTED"}


@pytest.mark.parametrize("defect", ["missing_quote", "wrong_award", "source_gap"])
def test_source_validation_defects_are_retained(defect):
    data = inputs()
    item = data["reviewed_attachments"][0]
    raw = deepcopy(item.reviewed_payload)
    row = raw["quantitative_tables"][0]["criteria"][0]["cases"][0]
    if defect == "missing_quote":
        row["evidence"]["quote"] = "SYN source quote absent"
    elif defect == "wrong_award":
        row["award_value"] = 1
    else:
        raw["missing_or_unreadable"] = ["SYN 정량평가 배점표 누락"]
    data["reviewed_attachments"][0] = replace(item, reviewed_payload=raw, reviewed_payload_sha256=digest(raw))
    result = preview_reviewed_quantitative_inputs(**data)
    assert result.profile_issue_codes
    assert result.estimate.estimated_points is None
    assert result.estimate.activation_status == "REVIEW_REQUIRED"


@pytest.mark.parametrize("injected", ["facts", "performance_scope", "engine_eligible", "fact_binding_sha256"])
def test_caller_execution_or_scope_fields_are_not_source_rules(injected):
    data = inputs()
    item = data["reviewed_attachments"][0]
    raw = {**item.reviewed_payload, injected: {}}
    data["reviewed_attachments"][0] = replace(item, reviewed_payload=raw, reviewed_payload_sha256=digest(raw))
    with pytest.raises(ValidationError):
        preview_reviewed_quantitative_inputs(**data)


@pytest.mark.parametrize("defect", ["missing_id", "wrong_binding", "changed_value", "expired"])
def test_source_success_does_not_repair_company_evidence(defect):
    data = inputs()
    fact = bound_fact(data)
    if defect == "missing_id":
        fact.evidence_id = fact.evidence.id = None
    elif defect == "wrong_binding":
        fact.value["fact_binding_sha256"] = "f" * 64
    elif defect == "changed_value":
        fact.value["value"] = "AAA"
    else:
        fact.evidence.valid_until = AS_OF.replace(year=2025)
    before = deepcopy(fact.value), fact.evidence_id, deepcopy(fact.evidence.metadata_json)
    result = preview_reviewed_quantitative_inputs(**data, company_facts=[fact])
    assert result.estimate.estimated_points is None
    assert (fact.value, fact.evidence_id, fact.evidence.metadata_json) == before


@pytest.mark.parametrize("archive", [False, True])
def test_mixed_text_and_textless_pdf_is_not_complete_review(archive):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 200 Td (SYN text page with scoring references) Tj ET")
    page[NameObject("/Contents")] = content
    writer.add_blank_page(width=300, height=300)
    stream = io.BytesIO()
    writer.write(stream)
    native = stream.getvalue()
    assert extract_pps_document_content("SYN.pdf", native).complete is True
    if archive:
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as package:
            package.writestr("SYN mixed.pdf", native)
        native = zipped.getvalue()
    base = fixture(extension="zip" if archive else "pdf")
    # Use explicit source raw only; parser quality must block before row checks.
    aid = base["source_attempt"]["attachment_id"]
    raw = base["source_attempt"]["result"]
    result = preview_reviewed_quantitative_inputs(
        full_manifest=base["full_manifest"], expected_manifest_sha256=base["expected_manifest_sha256"],
        expected_documents={aid: sha(native)}, as_of=AS_OF,
        reviewed_attachments=[ReviewedNativeAttachment(aid, native, raw, digest(raw))],
    )
    assert result.attachment_checks[0].code == (
        "NATIVE_MEMBER_TEXT_COVERAGE_UNVERIFIED" if archive else "PDF_PAGE_TEXT_UNVERIFIED"
    )
    assert result.estimate.estimated_points is None


@pytest.mark.parametrize("as_of", [None, datetime(2026, 9, 10)])
def test_deadline_cannot_be_defaulted_to_today(as_of):
    data = inputs()
    data["as_of"] = as_of
    with pytest.raises(ValueError, match="EXPLICIT_DEADLINE_REQUIRED"):
        preview_reviewed_quantitative_inputs(**data)
