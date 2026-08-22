from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

import pai_loop.pps_enrichment as pps_enrichment_module
from pai_loop.pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_METADATA_KIND,
    PPS_METADATA_SCHEMA,
    PPS_PROCESSING_VERSION,
    PpsEnrichmentError,
    _digest,
    build_attachment_manifest,
    build_notice_metadata,
    download_public_attachment,
    department_keyword_coverage_count,
    extract_document_text,
    enrich_notice_from_pps,
    has_current_accepted_pps_extraction,
    persist_pps_metadata_version,
    pps_attachment_coverage,
    public_analysis_reason,
    resolve_ingestion_keywords,
    safe_public_live_extraction,
    select_preferred_attachments,
)
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.integrations.openai_extraction import (
    CORRECTIVE_PROMPT_VERSION,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    EvidenceAnchor,
    ExtractedRequirement,
    ExtractionOutcome,
    ExtractionPayload,
    OpenAIAttemptTelemetry,
    OpenAIProviderUsage,
    aggregate_openai_attempts,
)
from pai_loop.quantitative_rule_extraction import (
    validate_quantitative_attachment_extraction,
)
from pai_loop.models import Notice, NoticeVersion


G2B_DOWNLOAD = (
    "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
    "?bidPbancNo=R26BK00000001&bidPbancOrd=000&fileSeq=1"
    "&fileType=1&prcmBsneSeCd=01"
)


def test_attachment_manifest_and_notice_metadata_are_strict_allowlists() -> None:
    raw = {
        "bidNtceNo": "R26BK00000001",
        "bidNtceOrd": "000",
        "ntceSpecFileNm1": "입찰공고문.pdf",
        "ntceSpecDocUrl1": G2B_DOWNLOAD,
        "ntceSpecFileNm2": "evil.pdf",
        "ntceSpecDocUrl2": "https://evil.example/download?fileSeq=2",
        "bidMethdNm": "전자입찰",
        "cntrctCnclsMthdNm": "협상에 의한 계약",
        "sucsfbidMthdNm": "협상계약",
        "ntceInsttOfclEmailAdrs": "must-not-persist" + "@" + "example.invalid",
        "ntceInsttOfclTelNo": "010" + "-" + "0000" + "-" + "0000",
        "untrusted": {"raw": "payload"},
    }

    manifest = build_attachment_manifest(raw)
    assert len(manifest) == 2
    assert manifest[0]["file_name"] == "입찰공고문.pdf"
    assert manifest[0]["media_type"] == "application/pdf"
    assert manifest[0]["attachment_id"].startswith("PPS-ATT-")
    assert manifest[1] == {
        "invalid_attachment_slot": 2,
        "status": "INVALID",
        "error_code": "UNSAFE_ATTACHMENT_URL",
        "metadata_sha256": manifest[1]["metadata_sha256"],
    }
    assert len(manifest[1]["metadata_sha256"]) == 64

    metadata = build_notice_metadata(raw)
    assert metadata == {
        "bid_method": "전자입찰",
        "contract_method": "협상에 의한 계약",
        "award_method": "협상계약",
    }
    serialised = json.dumps({"manifest": manifest, "metadata": metadata}, ensure_ascii=False)
    assert "example.invalid" not in serialised
    assert raw["ntceInsttOfclTelNo"] not in serialised
    assert "untrusted" not in serialised


def test_manifest_preserves_invalid_slot_count_without_raw_provider_values() -> None:
    raw = {
        "bidNtceNo": "R26BK00000001",
        "bidNtceOrd": "000",
        "ntceSpecFileNm1": "공고문.pdf",
        "ntceSpecDocUrl1": G2B_DOWNLOAD,
        "ntceSpecFileNm2": "제안요청서.hwpx",
        "ntceSpecDocUrl2": G2B_DOWNLOAD.replace("fileSeq=1", "fileSeq=2"),
        "ntceSpecFileNm3": "private.pdf",
        "ntceSpecDocUrl3": "https://unsafe.invalid/private?token=secret",
    }

    manifest = build_attachment_manifest(raw)

    assert len(manifest) == 3
    assert sum("attachment_id" in item for item in manifest) == 2
    invalid = manifest[2]
    assert invalid["invalid_attachment_slot"] == 3
    assert invalid["error_code"] == "UNSAFE_ATTACHMENT_URL"
    serialised = json.dumps(manifest, ensure_ascii=False)
    assert "unsafe.invalid" not in serialised
    assert "token" not in serialised
    reason = public_analysis_reason(
        [
            NoticeVersion(
                notice_id="notice",
                version_no=1,
                file_sha256="1" * 64,
                document_complete=False,
                extraction_status="METADATA",
                extraction_confidence=1.0,
                source_payload={
                    "kind": PPS_METADATA_KIND,
                    "schema_version": PPS_METADATA_SCHEMA,
                    "attachment_manifest": manifest,
                },
            )
        ]
    )
    assert reason.reason_code == "ATTACHMENT_COVERAGE_INCOMPLETE"
    assert reason.attachment_count == 3


def test_attachment_selection_prefers_pdf_but_keeps_hwp_as_supported() -> None:
    hwp = {
        "attachment_id": "PPS-ATT-111111111111111111111111",
        "file_name": "제안요청서.hwp",
        "media_type": "application/x-hwp",
        "url": G2B_DOWNLOAD.replace("fileSeq=1", "fileSeq=2"),
        "slot": 2,
    }
    pdf = {
        "attachment_id": "PPS-ATT-222222222222222222222222",
        "file_name": "제안요청서.pdf",
        "media_type": "application/pdf",
        "url": G2B_DOWNLOAD,
        "slot": 1,
    }

    selected, warnings = select_preferred_attachments([hwp, pdf], limit=1)
    assert [item["attachment_id"] for item in selected] == ["PPS-ATT-222222222222222222222222"]
    assert warnings == []

    selected, warnings = select_preferred_attachments([hwp], limit=1)
    assert [item["attachment_id"] for item in selected] == [hwp["attachment_id"]]
    assert warnings == []


def test_hwpx_is_extracted_in_memory_with_archive_limits() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<hs:section xmlns:hs='urn:hancom:section'><hs:p>입찰참가자격</hs:p>"
            "<hs:p>교육 컨설팅 수행실적을 제출해야 합니다.</hs:p></hs:section>",
        )

    text = extract_document_text("제안요청서.hwpx", buffer.getvalue())
    assert "입찰참가자격" in text
    assert "교육 컨설팅 수행실적" in text

    with pytest.raises(
        PpsEnrichmentError,
        match=r"HWP_(?:EXTRACTOR_UNAVAILABLE|CONTAINER_INVALID)",
    ):
        extract_document_text("제안요청서.hwp", b"binary-hwp")


def test_hwpx_preserves_paragraphs_and_does_not_insert_spaces_between_runs() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<hs:sec xmlns:hs='urn:hancom:section' xmlns:hp='urn:hancom:paragraph'>"
            "<hp:p><hp:run><hp:t>입찰참가</hp:t></hp:run>"
            "<hp:run><hp:t>자격등록을 완료해야 합니다.</hp:t></hp:run></hp:p>"
            "<hp:p><hp:run><hp:t>제안설명회 참석이 필수입니다.</hp:t></hp:run></hp:p>"
            "</hs:sec>",
        )

    text = extract_document_text("과업지시서.hwpx", buffer.getvalue())

    assert text == "입찰참가자격등록을 완료해야 합니다.\n제안설명회 참석이 필수입니다."


def test_pdf_extraction_replaces_lone_surrogates_before_json_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Malformed embedded PDF character maps can make pypdf return lone UTF-16
    # surrogates. They are valid Python string contents but cannot be emitted
    # in the UTF-8 JSON body used by the model client.
    monkeypatch.setattr(
        "pai_loop.pps_enrichment._extract_pdf_text",
        lambda _content: "입찰 참가 자격과 제출 요건을 확인합니다.\udb80추가 조건입니다.",
    )

    text = extract_document_text("공고문.pdf", b"synthetic-pdf")

    assert "\udb80" not in text
    assert "\ufffd" in text
    # Regression for the exact live failure boundary: this must not raise
    # UnicodeEncodeError before the Responses API request can be made.
    assert json.dumps({"input": text}, ensure_ascii=False).encode("utf-8")


def test_pdf_embedded_attachment_is_not_silently_marked_complete() -> None:
    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_attachment("hidden-rules.txt", b"quantitative rules")
    writer.write(buffer)

    with pytest.raises(
        PpsEnrichmentError,
        match="PDF_EMBEDDED_ATTACHMENT_NOT_EXTRACTED",
    ):
        extract_document_text("공고문.pdf", buffer.getvalue())


@pytest.mark.parametrize(
    ("member_name", "member_content", "error_code"),
    [
        (
            "BinData/embedded.xlsx",
            b"PK\x03\x04synthetic-workbook",
            "HWPX_EMBEDDED_ATTACHMENT_NOT_EXTRACTED",
        ),
        (
            "Scripts/default.js",
            b"alert(1)",
            "HWPX_ACTIVE_CONTENT_NOT_EXTRACTED",
        ),
    ],
)
def test_hwpx_hidden_package_content_is_not_silently_marked_complete(
    member_name: str,
    member_content: bytes,
    error_code: str,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<hs:section xmlns:hs='urn:hancom:section'><hs:p>공개 본문</hs:p></hs:section>",
        )
        archive.writestr(member_name, member_content)

    with pytest.raises(PpsEnrichmentError, match=error_code):
        extract_document_text("제안요청서.hwpx", buffer.getvalue())


def _analysis_versions(
    extension: str,
    *,
    error_code: str | None = None,
    status: str = "REVIEW",
) -> list[NoticeVersion]:
    manifest = build_attachment_manifest(
        {
            "bidNtceNo": "R26BK00000009",
            "bidNtceOrd": "000",
            "ntceSpecFileNm1": f"제안요청서{extension}",
            "ntceSpecDocUrl1": G2B_DOWNLOAD,
        }
    )
    metadata = NoticeVersion(
        notice_id="notice",
        version_no=1,
        file_sha256="1" * 64,
        document_complete=False,
        extraction_status="METADATA",
        extraction_confidence=1.0,
        source_payload={
            "kind": PPS_METADATA_KIND,
            "schema_version": PPS_METADATA_SCHEMA,
            "attachment_manifest": manifest,
        },
    )
    if error_code is None and status == "REVIEW":
        return [metadata]
    attachment = manifest[0]
    current_manifest_sha256 = _digest(manifest)
    quantitative_record = validate_quantitative_attachment_extraction(
        ExtractionPayload(
            document_type="RFP",
            summary="공개 첨부 원문",
            requirements=[],
            missing_or_unreadable=[],
            quantitative_tables=[],
            quantitative_table_not_applicable=None,
        ),
        source_text="공개 첨부 원문",
        attachment_id=attachment["attachment_id"],
        document_sha256="2" * 64,
        manifest_sha256=current_manifest_sha256,
    )
    attempt = NoticeVersion(
        notice_id="notice",
        version_no=2,
        file_sha256="2" * 64,
        document_complete=status == "ACCEPTED",
        extraction_status=status,
        extraction_confidence=1.0 if status == "ACCEPTED" else 0.0,
        source_payload={
            "kind": "OPENAI_REQUIREMENT_EXTRACTION",
            "source_kind": PPS_ATTACHMENT_SOURCE,
            "attachment_id": attachment["attachment_id"],
            "manifest_sha256": _digest(attachment),
            "current_manifest_sha256": current_manifest_sha256,
            "prompt_version": PROMPT_VERSION,
            "processing_version": PPS_PROCESSING_VERSION,
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "error_code": error_code,
            "quantitative_validation_record": quantitative_record.model_dump(mode="json"),
        },
    )
    return [metadata, attempt]


@pytest.mark.parametrize(
    ("extension", "error_code", "status", "reason_code"),
    [
        (".pdf", None, "REVIEW", "NOT_SELECTED"),
        (".hwp", None, "REVIEW", "NOT_SELECTED"),
        (".hwpx", "HWPX_XML_INVALID", "REVIEW", "HWPX_EXTRACT_FAILED"),
        (".pdf", "PDF_TEXT_EXTRACTION_FAILED", "REVIEW", "PDF_EXTRACT_FAILED"),
        (".hwpx", "UNVERIFIED_QUOTE", "REVIEW", "QUOTE_UNVERIFIED"),
        (".pdf", "SCHEMA_VALIDATION_ERROR", "REVIEW", "OPENAI_REVIEW"),
        (".pdf", None, "ACCEPTED", "ANALYZED"),
    ],
)
def test_public_analysis_reason_is_current_manifest_bound_and_public_safe(
    extension: str,
    error_code: str | None,
    status: str,
    reason_code: str,
) -> None:
    reason = public_analysis_reason(
        _analysis_versions(extension, error_code=error_code, status=status)
    )

    assert reason.reason_code == reason_code
    assert reason.state == (
        "ANALYZED"
        if reason_code == "ANALYZED"
        else "PENDING"
        if reason_code == "NOT_SELECTED"
        else "REVIEW"
    )
    assert "http" not in reason.reason.casefold()
    assert "PPS-ATT" not in reason.reason


def test_newest_stale_metadata_never_falls_back_to_prior_current_manifest() -> None:
    versions = _analysis_versions(".pdf", status="ACCEPTED")
    current_manifest = list(versions[0].source_payload["attachment_manifest"])
    versions.append(
        NoticeVersion(
            notice_id="notice",
            version_no=3,
            file_sha256="3" * 64,
            document_complete=False,
            extraction_status="METADATA",
            extraction_confidence=1.0,
            source_payload={
                "kind": PPS_METADATA_KIND,
                "schema_version": "pai-loop-pps-notice-metadata-0.1.0",
                "attachment_manifest": current_manifest,
            },
        )
    )

    reason = public_analysis_reason(versions)
    coverage = pps_attachment_coverage(versions)
    assert reason.state == "REVIEW"
    assert reason.reason_code == "ATTACHMENT_COVERAGE_INCOMPLETE"
    assert coverage.discovered == 1
    assert coverage.accepted == 0
    assert coverage.complete is False

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        session.add(
            Notice(
                id="notice",
                notice_key="PPS-STALE-METADATA",
                bid_notice_no="R26BK-STALE-METADATA",
                revision_no="000",
                title="stale manifest regression",
                agency="공공기관",
                deadline=datetime(2027, 1, 1, tzinfo=timezone.utc),
                status="OPEN",
            )
        )
        session.add_all(versions)
        session.commit()
    with factory() as session:
        assert has_current_accepted_pps_extraction(session, "notice") is False
        session.rollback()
        enrichment = enrich_notice_from_pps(
            session,
            notice_id="notice",
            openai_api_key=None,
            openai_model="unused",
        )
    assert enrichment.status == "REVIEW"
    assert "PPS_ATTACHMENT_MANIFEST_SCHEMA_STALE" in enrichment.warnings
    engine.dispose()


def test_non_object_manifest_entry_counts_as_invalid_current_coverage() -> None:
    versions = _analysis_versions(".pdf", status="ACCEPTED")
    manifest = versions[0].source_payload["attachment_manifest"]
    manifest.append("UNKNOWN_RAW_SLOT")

    reason = public_analysis_reason(versions)
    coverage = pps_attachment_coverage(versions)
    assert reason.state == "REVIEW"
    assert reason.reason_code == "ATTACHMENT_COVERAGE_INCOMPLETE"
    assert reason.attachment_count == 2
    assert coverage.discovered == 2
    assert coverage.valid == 1
    assert coverage.complete is False
    assert "UNKNOWN_RAW_SLOT" not in reason.reason


def test_download_rejects_unsafe_redirect_and_limits_bytes() -> None:
    def unsafe_redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/file.pdf"})

    with pytest.raises(PpsEnrichmentError, match="UNSAFE_ATTACHMENT_URL"):
        download_public_attachment(
            {"url": G2B_DOWNLOAD, "file_name": "공고문.pdf"},
            transport=httpx.MockTransport(unsafe_redirect),
        )

    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf", "Content-Length": "100"},
            content=b"x" * 100,
        )

    with pytest.raises(PpsEnrichmentError, match="ATTACHMENT_TOO_LARGE"):
        download_public_attachment(
            {"url": G2B_DOWNLOAD, "file_name": "공고문.pdf"},
            max_bytes=20,
            transport=httpx.MockTransport(oversized),
        )


def test_profile_keyword_resolution_is_diverse_bounded_and_legacy_compatible() -> None:
    legacy, truncated = resolve_ingestion_keywords(
        keyword="교육",
        keywords=["컨설팅", "교육"],
        use_profile_keywords=False,
        profile_department_ids=[],
    )
    assert legacy == ["교육", "컨설팅"]
    assert truncated is False

    profiled, truncated = resolve_ingestion_keywords(
        keyword=None,
        keywords=[],
        use_profile_keywords=True,
        profile_department_ids=[],
    )
    assert profiled[:5] == ["교육", "컨설팅", "연수", "포럼", "위탁 운영"]
    assert len(profiled) == 29
    assert len(set(profiled)) == 29
    assert truncated is False
    assert department_keyword_coverage_count(
        profiled,
        profile_department_ids=[],
    ) == 24

    explicit_profiled, truncated = resolve_ingestion_keywords(
        keyword="경제안보외교",
        keywords=[],
        use_profile_keywords=True,
        profile_department_ids=[],
        limit=6,
    )
    assert explicit_profiled == ["교육", "컨설팅", "연수", "포럼", "위탁 운영", "경제안보외교"]
    assert truncated is True


def test_live_public_extraction_exposes_only_validated_procurement_evidence() -> None:
    payload = {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "source_kind": "PPS_PUBLIC_ATTACHMENT",
        "attachment_id": "PPS-ATT-0123456789abcdef01234567",
        "source_label": "입찰공고문.pdf",
        "document_sha256": "a" * 64,
        "status": "ACCEPTED",
        "response_id": "must-not-be-public",
        "model": "must-not-be-public",
        "prompt_version": PROMPT_VERSION,
        "processing_version": PPS_PROCESSING_VERSION,
        "schema_version": SCHEMA_VERSION,
        "document_processing": {
            "source_read_complete": True,
            "analysis_input_complete": True,
        },
        "result": {
            "document_type": "NOTICE",
            "requirements": [
                {
                    "requirement_id": "REQ-1",
                    "category": "ENTITY",
                    "logic": "SINGLE",
                    "normalized_condition": "입찰참가자격 등록 필요",
                    "mandatory": True,
                    "deadline_basis": "입찰 마감일",
                    "evidence": [
                        {
                            "attachment_id": "PPS-ATT-0123456789abcdef01234567",
                            "page": 1,
                            "section": "입찰참가자격",
                            "quote": "입찰참가자격 등록 필요",
                            "confidence": 0.98,
                        }
                    ],
                    "ambiguity_reason": None,
                }
            ],
            "missing_or_unreadable": [],
            "quantitative_tables": [],
            "quantitative_table_not_applicable": None,
            "summary": "입찰참가자격 조건 1건",
        },
    }

    public = safe_public_live_extraction(payload)
    assert public is not None
    assert public["document_name"] == "입찰공고문.pdf"
    assert public["requirements"][0]["evidence"][0]["quote"] == "입찰참가자격 등록 필요"
    assert "response_id" not in public
    assert "model" not in public

    assert safe_public_live_extraction({**payload, "source_kind": "UNTRUSTED"}) is None


def test_live_public_extraction_redacts_contact_identifiers() -> None:
    email = "review" + "@" + "example.invalid"
    phone = "010" + "-" + "1234" + "-" + "5678"
    payload = {
        "kind": "OPENAI_REQUIREMENT_EXTRACTION",
        "source_kind": "PPS_PUBLIC_ATTACHMENT",
        "attachment_id": "PPS-ATT-0123456789abcdef01234567",
        "source_label": "제안요청서.pdf",
        "document_sha256": "b" * 64,
        "status": "ACCEPTED",
        "prompt_version": PROMPT_VERSION,
        "processing_version": PPS_PROCESSING_VERSION,
        "schema_version": SCHEMA_VERSION,
        "document_processing": {
            "source_read_complete": True,
            "analysis_input_complete": True,
        },
        "result": {
            "document_type": "RFP",
            "requirements": [
                {
                    "requirement_id": "REQ-PII",
                    "category": "SUBMISSION",
                    "logic": "SINGLE",
                    "normalized_condition": "담당 합성가 주무관, 문의는 " + email,
                    "mandatory": True,
                    "deadline_basis": None,
                    "evidence": [
                        {
                            "attachment_id": "PPS-ATT-0123456789abcdef01234567",
                            "page": 1,
                            "section": "문의 합성나",
                            "quote": "연락처 " + phone,
                            "confidence": 0.9,
                        }
                    ],
                    "ambiguity_reason": None,
                }
            ],
            "missing_or_unreadable": [],
            "quantitative_tables": [],
            "quantitative_table_not_applicable": None,
            "summary": "담당자 합성다 " + email,
        },
    }
    public = safe_public_live_extraction(payload)
    assert public is not None
    serialised = json.dumps(public, ensure_ascii=False)
    assert email not in serialised
    assert phone not in serialised
    assert all(name not in serialised for name in ("합성가", "합성나", "합성다"))
    assert "[비공개]" in serialised


class _CountingExtractionClient:
    calls = 0

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_CountingExtractionClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract(self, *, document_text: str, allowed_attachment_ids: set[str]) -> ExtractionOutcome:
        type(self).calls += 1
        attachment_id = next(iter(allowed_attachment_ids))
        quote = "교육 컨설팅 수행실적을 제출해야 합니다."
        assert quote in document_text
        return ExtractionOutcome(
            status="ACCEPTED",
            message="validated",
            api_calls=2,
            openai_telemetry=aggregate_openai_attempts(
                [
                    OpenAIAttemptTelemetry(
                        attempt=1,
                        request_latency_ms=125,
                        response_received=True,
                        model="gpt-5.6-luna",
                        service_tier="default",
                        usage=OpenAIProviderUsage(
                            input_tokens=1_000,
                            cached_input_tokens=100,
                            cache_write_tokens=50,
                            output_tokens=200,
                            reasoning_output_tokens=50,
                            total_tokens=1_200,
                        ),
                    ),
                    OpenAIAttemptTelemetry(
                        attempt=2,
                        request_latency_ms=250,
                        response_received=True,
                        model="gpt-5.6-luna",
                        service_tier="default",
                        usage=OpenAIProviderUsage(
                            input_tokens=900,
                            cached_input_tokens=0,
                            cache_write_tokens=0,
                            output_tokens=150,
                            reasoning_output_tokens=25,
                            total_tokens=1_050,
                        ),
                    ),
                ]
            ),
            corrective_retry_used=True,
            correction_prompt_version=CORRECTIVE_PROMPT_VERSION,
            data=ExtractionPayload(
                document_type="RFP",
                requirements=[
                    ExtractedRequirement(
                        requirement_id="REQ-1",
                        category="PERFORMANCE",
                        logic="SINGLE",
                        normalized_condition="교육 컨설팅 수행실적 제출",
                        mandatory=True,
                        deadline_basis="입찰 마감일",
                        evidence=[
                            EvidenceAnchor(
                                attachment_id=attachment_id,
                                page=1,
                                section="수행실적",
                                quote=quote,
                                confidence=0.95,
                            )
                        ],
                        ambiguity_reason=None,
                    )
                ],
                missing_or_unreadable=[],
                summary="수행실적 조건 1건",
            ),
        )


class _RetryableReviewClient:
    calls = 0

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_RetryableReviewClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract(self, **_kwargs: object) -> ExtractionOutcome:
        type(self).calls += 1
        return ExtractionOutcome(
            status="REVIEW",
            review_code="R07",
            error_code="INCOMPLETE_RESPONSE",
            message="retryable review",
        )


def test_corrected_accepted_extraction_reports_two_calls_and_reuses_without_openai() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<s><p>교육 컨설팅 수행실적을 제출해야 합니다.</p><p>마감일까지 제출합니다.</p></s>",
        )
    content = buffer.getvalue()

    def attachment_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/zip"}, content=content)

    raw = {
        "bidNtceNo": "R26BK00000001",
        "bidNtceOrd": "000",
        "ntceSpecFileNm1": "제안요청서.hwpx",
        "ntceSpecDocUrl1": G2B_DOWNLOAD,
    }
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        notice = Notice(
            notice_key="PPS-REUSE-001",
            bid_notice_no="R26BK00000001",
            revision_no="000",
            title="교육 컨설팅 용역",
            agency="공공기관",
            deadline=datetime(2027, 1, 1, tzinfo=timezone.utc),
            status="OPEN",
        )
        session.add(notice)
        session.flush()
        persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw,
            search_keywords=["교육", "컨설팅"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id

    _CountingExtractionClient.calls = 0
    transport = httpx.MockTransport(attachment_response)
    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_CountingExtractionClient,
        )
    with factory() as session:
        second = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_CountingExtractionClient,
        )

    assert first.status == "COMPLETED"
    assert first.openai_calls == 2
    assert first.openai_telemetry.total_tokens == 2_250
    assert first.openai_telemetry.total_request_latency_ms == 375
    assert first.warnings == ["CORRECTIVE_EXTRACTION_RETRY_USED"]
    assert second.status == "REUSED"
    assert second.openai_calls == 0
    assert first.version_id == second.version_id
    assert _CountingExtractionClient.calls == 1
    with factory() as session:
        extraction_version = session.get(NoticeVersion, first.version_id)
        assert extraction_version is not None
        assert extraction_version.source_payload["api_calls"] == 2
        stored_telemetry = extraction_version.source_payload["document_processing"][
            "openai_telemetry"
        ]
        assert stored_telemetry["input_tokens"] == 1_900
        assert stored_telemetry["cached_input_tokens"] == 100
        assert stored_telemetry["cache_write_tokens"] == 50
        assert stored_telemetry["models"] == ["gpt-5.6-luna"]
        assert stored_telemetry["service_tiers"] == ["default"]
        assert stored_telemetry["output_tokens"] == 350
        assert stored_telemetry["reasoning_output_tokens"] == 75
        assert stored_telemetry["total_tokens"] == 2_250
        assert stored_telemetry["total_request_latency_ms"] == 375
        assert len(stored_telemetry["attempts"]) == 2
        public_projection = safe_public_live_extraction(
            extraction_version.source_payload
        )
        assert public_projection is not None
        public_serialised = json.dumps(public_projection, ensure_ascii=False)
        assert "openai_telemetry" not in public_serialised
        assert "input_tokens" not in public_serialised
        assert "request_latency_ms" not in public_serialised
        assert extraction_version.source_payload["corrective_retry_used"] is True
        assert (
            extraction_version.source_payload["correction_prompt_version"]
            == CORRECTIVE_PROMPT_VERSION
        )
        assert has_current_accepted_pps_extraction(session, notice_id) is True
        notice = session.get(Notice, notice_id)
        assert notice is not None
        persist_pps_metadata_version(
            session,
            notice,
            raw_item={
                **raw,
                "ntceSpecDocUrl1": G2B_DOWNLOAD.replace("fileSeq=1", "fileSeq=2"),
            },
            search_keywords=["교육"],
            dry_run=False,
        )
        session.commit()
    with factory() as session:
        assert has_current_accepted_pps_extraction(session, notice_id) is False
    engine.dispose()


def test_paid_usage_survives_post_openai_persistence_failure(monkeypatch) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<s><p>교육 컨설팅 수행실적을 제출해야 합니다.</p></s>",
        )
    content = buffer.getvalue()

    def attachment_response(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/zip"},
            content=content,
        )

    raw = {
        "bidNtceNo": "R26BK00000001",
        "bidNtceOrd": "000",
        "ntceSpecFileNm1": "제안요청서.hwpx",
        "ntceSpecDocUrl1": G2B_DOWNLOAD,
    }
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        notice = Notice(
            notice_key="PPS-PAID-PERSIST-FAIL",
            bid_notice_no="R26BK00000001",
            revision_no="000",
            title="호출 후 저장 실패 감사",
            agency="공공기관",
            deadline=datetime(2027, 1, 1, tzinfo=timezone.utc),
            status="OPEN",
        )
        session.add(notice)
        session.flush()
        persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw,
            search_keywords=["교육", "컨설팅"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id

    original_persist = pps_enrichment_module._persist_extraction_version
    failed_paid_persist = False

    def fail_once_after_paid_call(*args, **kwargs):
        nonlocal failed_paid_persist
        if kwargs.get("outcome") is not None and not failed_paid_persist:
            failed_paid_persist = True
            raise RuntimeError("synthetic post-provider persistence failure")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        pps_enrichment_module,
        "_persist_extraction_version",
        fail_once_after_paid_call,
    )
    _CountingExtractionClient.calls = 0
    with factory() as session:
        result = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="gpt-5.6-luna",
            transport=httpx.MockTransport(attachment_response),
            openai_client_factory=_CountingExtractionClient,
        )

    assert failed_paid_persist is True
    assert _CountingExtractionClient.calls == 1
    assert result.status == "REVIEW"
    assert "INTERNAL_ENRICHMENT_ERROR" in result.warnings
    assert result.openai_calls == result.openai_telemetry.api_calls == 2
    assert result.openai_telemetry.usage_unreported_calls == 0
    assert result.openai_telemetry.input_tokens == 1_900
    assert result.openai_telemetry.cache_write_tokens == 50
    assert result.openai_telemetry.models == ["gpt-5.6-luna"]
    assert result.openai_telemetry.service_tiers == ["default"]
    engine.dispose()


def test_five_attachments_persist_and_resume_two_plus_two_plus_one() -> None:
    def hwpx_bytes(sequence: int) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr(
                "Contents/section0.xml",
                "<s><p>교육 컨설팅 수행실적을 제출해야 합니다.</p>"
                f"<p>첨부 순번 {sequence}의 독립 근거입니다.</p></s>",
            )
        return buffer.getvalue()

    contents = {str(index): hwpx_bytes(index) for index in range(1, 6)}

    def attachment_response(request: httpx.Request) -> httpx.Response:
        sequence = dict(request.url.params)["fileSeq"]
        return httpx.Response(
            200,
            headers={"Content-Type": "application/zip"},
            content=contents[sequence],
        )

    raw: dict[str, str] = {
        "bidNtceNo": "R26BK00000055",
        "bidNtceOrd": "000",
    }
    for index in range(1, 6):
        raw[f"ntceSpecFileNm{index}"] = f"제안요청서-{index}.hwpx"
        raw[f"ntceSpecDocUrl{index}"] = G2B_DOWNLOAD.replace(
            "R26BK00000001", "R26BK00000055"
        ).replace("fileSeq=1", f"fileSeq={index}")

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        notice = Notice(
            notice_key="PPS-FIVE-ATTACHMENTS",
            bid_notice_no="R26BK00000055",
            revision_no="000",
            title="다섯 첨부 continuation 검증",
            agency="공공기관",
            deadline=datetime(2027, 1, 1, tzinfo=timezone.utc),
            status="OPEN",
        )
        session.add(notice)
        session.flush()
        persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw,
            search_keywords=["교육"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id

    _CountingExtractionClient.calls = 0
    transport = httpx.MockTransport(attachment_response)
    results = []
    for _index in range(4):
        with factory() as session:
            results.append(
                enrich_notice_from_pps(
                    session,
                    notice_id=notice_id,
                    openai_api_key="test-key",
                    openai_model="test-model",
                    transport=transport,
                    openai_client_factory=_CountingExtractionClient,
                )
            )

    assert [item.status for item in results] == [
        "SKIPPED",
        "SKIPPED",
        "COMPLETED",
        "REUSED",
    ]
    assert [item.attachments_attempted for item in results] == [2, 4, 5, 5]
    assert [item.openai_calls for item in results] == [4, 4, 2, 0]
    assert "ATTACHMENT_CONTINUATION_REQUIRED" in results[0].warnings
    assert "ATTACHMENT_CONTINUATION_REQUIRED" in results[1].warnings
    assert "ATTACHMENT_CONTINUATION_REQUIRED" not in results[2].warnings
    assert _CountingExtractionClient.calls == 5

    with factory() as session:
        assert has_current_accepted_pps_extraction(session, notice_id) is True
        attempts = [
            version
            for version in session.scalars(
                select(NoticeVersion)
                .where(NoticeVersion.notice_id == notice_id)
                .order_by(NoticeVersion.version_no)
            ).all()
            if isinstance(version.source_payload, dict)
            and version.source_payload.get("kind")
            == "OPENAI_REQUIREMENT_EXTRACTION"
        ]
        assert len(attempts) == 5
        whole_digests = {
            version.source_payload["current_manifest_sha256"]
            for version in attempts
        }
        assert len(whole_digests) == 1
        assert all(
            version.source_payload["quantitative_validation_record"]["attachment_id"]
            == version.source_payload["attachment_id"]
            for version in attempts
        )
    engine.dispose()


def test_retryable_review_creates_fresh_attempt_after_cooldown_then_reuses() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<s><p>교육 컨설팅 수행실적을 제출해야 합니다.</p><p>마감일까지 제출합니다.</p></s>",
        )
    content = buffer.getvalue()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/zip"},
            content=content,
        )
    )
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        notice = Notice(
            notice_key="PPS-REVIEW-COOLDOWN",
            bid_notice_no="R26BK00000002",
            revision_no="000",
            title="교육 컨설팅 용역",
            agency="공공기관",
            deadline=datetime(2027, 1, 1, tzinfo=timezone.utc),
            status="OPEN",
        )
        session.add(notice)
        session.flush()
        persist_pps_metadata_version(
            session,
            notice,
            raw_item={
                "bidNtceNo": "R26BK00000002",
                "bidNtceOrd": "000",
                "ntceSpecFileNm1": "제안요청서.hwpx",
                "ntceSpecDocUrl1": G2B_DOWNLOAD.replace(
                    "R26BK00000001", "R26BK00000002"
                ),
            },
            search_keywords=["교육"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id

    _RetryableReviewClient.calls = 0
    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
        )
    with factory() as session:
        first_version = session.get(NoticeVersion, first.version_id)
        assert first_version is not None
        first_version.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        session.commit()
    with factory() as session:
        second = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
        )
    with factory() as session:
        third = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
        )

    assert first.status == second.status == third.status == "REVIEW"
    assert first.version_id != second.version_id
    assert second.version_id == third.version_id
    assert first.openai_calls == second.openai_calls == 1
    assert third.openai_calls == 0
    assert _RetryableReviewClient.calls == 2
    engine.dispose()


def test_internal_failure_does_not_bind_concurrently_replaced_manifest() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<s><p>교육 컨설팅 수행실적을 제출해야 합니다.</p>"
            "<p>마감일까지 제출합니다.</p></s>",
        )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "application/zip"},
            content=buffer.getvalue(),
        )
    )
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    original_raw = {
        "bidNtceNo": "R26BK00000003",
        "bidNtceOrd": "000",
        "ntceSpecFileNm1": "제안요청서.hwpx",
        "ntceSpecDocUrl1": G2B_DOWNLOAD.replace(
            "R26BK00000001",
            "R26BK00000003",
        ),
    }
    corrected_raw = {
        **original_raw,
        "ntceSpecDocUrl1": original_raw["ntceSpecDocUrl1"].replace(
            "fileSeq=1",
            "fileSeq=2",
        ),
    }
    with factory() as session:
        notice = Notice(
            notice_key="PPS-MANIFEST-RACE",
            bid_notice_no="R26BK00000003",
            revision_no="000",
            title="manifest 교체 동시성 검증",
            agency="공공기관",
            deadline=datetime(2027, 1, 1, tzinfo=timezone.utc),
            status="OPEN",
        )
        session.add(notice)
        session.flush()
        persist_pps_metadata_version(
            session,
            notice,
            raw_item=original_raw,
            search_keywords=["교육"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id

    class CorrectManifestThenFailClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract(self, **_kwargs: object):
            # This commit occurs after the worker selected the original
            # attachment but before it handles the unexpected model error.
            with factory() as concurrent_session:
                concurrent_notice = concurrent_session.get(Notice, notice_id)
                assert concurrent_notice is not None
                persisted = persist_pps_metadata_version(
                    concurrent_session,
                    concurrent_notice,
                    raw_item=corrected_raw,
                    search_keywords=["교육"],
                    dry_run=False,
                )
                assert persisted.created is True
                concurrent_session.commit()
            raise RuntimeError("synthetic failure after manifest correction")

    with factory() as session:
        result = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=CorrectManifestThenFailClient,
        )

    assert result.status == "REVIEW"
    assert "INTERNAL_ENRICHMENT_ERROR" in result.warnings
    assert "PPS_MANIFEST_CHANGED_DURING_ENRICHMENT" in result.warnings
    assert result.version_id is None
    with factory() as session:
        versions = list(
            session.scalars(
                select(NoticeVersion)
                .where(NoticeVersion.notice_id == notice_id)
                .order_by(NoticeVersion.version_no)
            ).all()
        )
        assert len(versions) == 2
        assert all(
            version.source_payload.get("kind") == PPS_METADATA_KIND
            for version in versions
        )
        reason = public_analysis_reason(versions)
        assert reason.reason_code == "NOT_SELECTED"
        assert reason.attempted is False
    engine.dispose()
