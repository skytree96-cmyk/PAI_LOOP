from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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
    current_retryable_review_version_ids,
    download_public_attachment,
    department_keyword_coverage_count,
    extract_document_text,
    enrich_notice_from_pps,
    has_current_accepted_pps_extraction,
    persist_pps_metadata_version,
    pps_attachment_coverage,
    pps_recorded_attachment_attempt_count,
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
    QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION,
    ValidatedQuantitativeAttachmentRecord,
    validate_quantitative_attachment_extraction,
    validated_quantitative_record_fingerprint,
)
from pai_loop.models import Evaluation, Notice, NoticeVersion
from pai_loop.notice_freshness import latest_current_evaluation


G2B_DOWNLOAD = (
    "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
    "?bidPbancNo=R26BK00000001&bidPbancOrd=000&fileSeq=1"
    "&fileType=1&prcmBsneSeCd=01"
)


def _metadata_test_notice(*, notice_key: str, bid_notice_no: str) -> Notice:
    return Notice(
        notice_key=notice_key,
        bid_notice_no=bid_notice_no,
        revision_no="00",
        title="교육 컨설팅 용역",
        agency="공공기관",
        published_at=datetime(2026, 8, 16, 1, tzinfo=timezone.utc),
        deadline=datetime(2026, 8, 30, 8, tzinfo=timezone.utc),
        status="OPEN",
        category="용역",
        estimated_amount=100_000_000,
        source_url="https://example.go.kr/notices/safe",
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


def test_metadata_provenance_aggregation_does_not_advance_material_version() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    raw = {
        "bidNtceNo": "R26BK-PROVENANCE",
        "bidNtceOrd": "00",
        "ntceKindNm": "등록공고",
        "bidMethdNm": "전자입찰",
    }
    first_changed = datetime(2026, 8, 16, 2, tzinfo=timezone.utc)
    second_changed = first_changed + timedelta(hours=1)
    with factory() as session:
        notice = _metadata_test_notice(
            notice_key="PPS-PROVENANCE",
            bid_notice_no="R26BK-PROVENANCE",
        )
        session.add(notice)
        first = persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw,
            search_keywords=["교육"],
            dry_run=False,
            provider_changed_at=first_changed,
            canonical_material_changed=False,
        )
        session.commit()
        first_row = session.get(NoticeVersion, first.version_id)
        assert first_row is not None
        original = (first_row.id, first_row.version_no, first_row.file_sha256)
        second = persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw,
            search_keywords=["컨설팅", "교육"],
            dry_run=False,
            provider_changed_at=second_changed,
            canonical_material_changed=False,
        )
        session.commit()

    with factory() as session:
        rows = list(session.scalars(select(NoticeVersion)).all())
        assert len(rows) == 1
        assert second.created is False
        assert second.reused is True
        assert (rows[0].id, rows[0].version_no, rows[0].file_sha256) == original
        assert rows[0].source_payload["provenance"] == {
            "provider": "PPS_PUBLIC_API",
            "search_keywords": ["교육", "컨설팅"],
            "provider_changed_at": second_changed.isoformat(),
        }
    engine.dispose()


def test_legacy_metadata_semantic_reuse_marks_debt_without_staling_evaluation() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    raw = {
        "bidNtceNo": "R26BK-LEGACY",
        "bidNtceOrd": "00",
        "ntceKindNm": "등록공고",
    }
    with factory() as session:
        notice = _metadata_test_notice(
            notice_key="PPS-LEGACY",
            bid_notice_no="R26BK-LEGACY",
        )
        session.add(notice)
        session.flush()
        legacy_payload = {
            "kind": PPS_METADATA_KIND,
            "source_kind": "PPS",
            "schema_version": PPS_METADATA_SCHEMA,
            "notice_identity": {
                "bid_notice_no": notice.bid_notice_no,
                "revision_no": notice.revision_no,
            },
            "provenance": {
                "provider": "PPS_PUBLIC_API",
                "search_keywords": ["교육"],
            },
            "notice_metadata": build_notice_metadata(raw),
            "attachment_manifest": build_attachment_manifest(raw),
        }
        legacy = NoticeVersion(
            notice_id=notice.id,
            version_no=1,
            file_sha256=_digest(legacy_payload),
            document_complete=False,
            extraction_status="METADATA",
            extraction_confidence=1,
            source_payload=legacy_payload,
        )
        session.add(legacy)
        session.flush()
        session.add(
            Evaluation(
                notice_id=notice.id,
                notice_version_id=legacy.id,
                deadline_snapshot_at=notice.deadline,
                eligibility="PASS",
                reason_code="PASS",
                readiness_score=100,
                readiness_status="GREEN",
                evidence_coverage=100,
                risk_score=None,
                risk_band="UNKNOWN",
                ruleset_version="legacy-test",
                atomic_results=[],
                explanation={},
            )
        )
        session.commit()
        original = (legacy.id, legacy.version_no, legacy.file_sha256)
        reused = persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw,
            search_keywords=["컨설팅"],
            dry_run=False,
            provider_changed_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
            canonical_material_changed=False,
        )
        session.commit()

    with factory() as session:
        notice = session.scalar(
            select(Notice).where(Notice.notice_key == "PPS-LEGACY")
        )
        assert notice is not None
        rows = sorted(notice.versions, key=lambda item: item.version_no)
        assert len(rows) == 1
        assert reused.reused is True
        assert (rows[0].id, rows[0].version_no, rows[0].file_sha256) == original
        assert rows[0].source_payload["material_basis_status"] == (
            "UNVERIFIED_LEGACY_COMPAT"
        )
        assert rows[0].source_payload["provenance"]["search_keywords"] == [
            "교육",
            "컨설팅",
        ]
        assert latest_current_evaluation(notice) is not None
        notice.title = "교육 컨설팅 용역 정정"
        changed = persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw,
            search_keywords=["교육"],
            dry_run=False,
            canonical_material_changed=True,
        )
        session.commit()
        assert changed.created is True

    with factory() as session:
        notice = session.scalar(
            select(Notice).where(Notice.notice_key == "PPS-LEGACY")
        )
        assert notice is not None
        assert len(notice.versions) == 2
        assert latest_current_evaluation(notice) is None
    engine.dispose()


def test_material_reversion_appends_new_current_version_instead_of_reusing_history() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    raw_a = {
        "bidNtceNo": "R26BK-A-B-A",
        "bidNtceOrd": "00",
        "ntceKindNm": "등록공고",
        "bidMethdNm": "전자입찰",
    }
    raw_b = {**raw_a, "bidMethdNm": "직찰"}
    with factory() as session:
        notice = _metadata_test_notice(
            notice_key="PPS-A-B-A",
            bid_notice_no="R26BK-A-B-A",
        )
        session.add(notice)
        first = persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw_a,
            search_keywords=["교육"],
            dry_run=False,
            canonical_material_changed=False,
        )
        second = persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw_b,
            search_keywords=["교육"],
            dry_run=False,
            canonical_material_changed=False,
        )
        session.flush()
        second_row = session.get(NoticeVersion, second.version_id)
        assert second_row is not None
        session.add(
            Evaluation(
                notice_id=notice.id,
                notice_version_id=second_row.id,
                deadline_snapshot_at=notice.deadline,
                eligibility="PASS",
                reason_code="PASS",
                readiness_score=100,
                readiness_status="GREEN",
                evidence_coverage=100,
                risk_score=None,
                risk_band="UNKNOWN",
                ruleset_version="material-b-test",
                atomic_results=[],
                explanation={},
            )
        )
        third = persist_pps_metadata_version(
            session,
            notice,
            raw_item=raw_a,
            search_keywords=["교육"],
            dry_run=False,
            canonical_material_changed=False,
        )
        session.commit()

    with factory() as session:
        notice = session.scalar(
            select(Notice).where(Notice.notice_key == "PPS-A-B-A")
        )
        assert notice is not None
        rows = sorted(notice.versions, key=lambda item: item.version_no)
        assert first.created is second.created is third.created is True
        assert len(rows) == 3
        assert rows[0].file_sha256 == rows[2].file_sha256
        assert rows[0].id != rows[2].id
        assert rows[2].source_payload["notice_metadata"]["bid_method"] == (
            "전자입찰"
        )
        assert latest_current_evaluation(notice) is None
    engine.dispose()


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
        # Shared container/budget/safety failures carry no format prefix. They
        # must still resolve to the extraction marker for the attachment's own
        # format, never to OPENAI_REVIEW, whose operator text states the
        # document was read and only the LLM stage failed.
        (".pdf", "DOCUMENT_EMPTY", "REVIEW", "PDF_EXTRACT_FAILED"),
        (".pdf", "DOCUMENT_INPUT_TOO_LARGE", "REVIEW", "PDF_EXTRACT_FAILED"),
        (".hwpx", "ARCHIVE_INVALID", "REVIEW", "HWPX_EXTRACT_FAILED"),
        (".hwpx", "ARCHIVE_ENCRYPTED_MEMBER", "REVIEW", "HWPX_EXTRACT_FAILED"),
        (".hwpx", "UNSAFE_DOCUMENT_FILENAME", "REVIEW", "HWPX_EXTRACT_FAILED"),
        # An .hwpx carrying OLE bytes is deliberately routed to the HWP5 reader.
        (".hwpx", "HWP_FILE_HEADER_INVALID", "REVIEW", "HWPX_EXTRACT_FAILED"),
        (".xlsx", "ARCHIVE_NO_DOCUMENT_MEMBERS", "REVIEW", "DOCUMENT_EXTRACT_FAILED"),
        (".xls", "XLS_CODEPAGE_UNVERIFIED", "REVIEW", "DOCUMENT_EXTRACT_FAILED"),
        (".zip", "MEMBER_EXTRACTION_FAILED", "REVIEW", "DOCUMENT_EXTRACT_FAILED"),
        (".hwp", "XML_DTD_FORBIDDEN", "REVIEW", "DOCUMENT_EXTRACT_FAILED"),
        # The more specific earlier labels keep priority.
        (".hwp", "UNSUPPORTED_ATTACHMENT_TYPE", "REVIEW", "UNSUPPORTED_ATTACHMENT"),
        (".hwp", "HWP_ONLY_UNSUPPORTED_R07", "REVIEW", "HWP_ONLY_UNSUPPORTED"),
        # An LLM-stage failure must not be relabelled as an extraction failure.
        (".hwpx", "SCHEMA_VALIDATION_ERROR", "REVIEW", "OPENAI_REVIEW"),
        (".xls", "PROVIDER_TIMEOUT", "REVIEW", "OPENAI_REVIEW"),
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


class _QuoteReviewClient:
    calls = 0

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_QuoteReviewClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract(self, **_kwargs: object) -> ExtractionOutcome:
        type(self).calls += 1
        return ExtractionOutcome(
            status="REVIEW",
            review_code="R07",
            error_code="UNVERIFIED_QUOTE",
            message="quote correction did not verify",
            api_calls=2,
            corrective_retry_used=True,
            correction_prompt_version=CORRECTIVE_PROMPT_VERSION,
        )


class _SemanticGapExtractionClient(_CountingExtractionClient):
    calls = 0

    def extract(self, **kwargs: object) -> ExtractionOutcome:
        outcome = super().extract(**kwargs)
        assert outcome.data is not None
        return outcome.model_copy(
            update={
                "data": outcome.data.model_copy(
                    update={
                        "missing_or_unreadable": [
                            "별도 제안요청서의 세부 평가표는 이 첨부에 포함되지 않음"
                        ]
                    }
                )
            }
        )


_QUANTITATIVE_RETRY_SOURCE = """정량평가표
수행실적 10점
5억원 이상 충족 10점 미충족 0점
정량평가 총점 10점
"""


def _quantitative_confidence_payload(
    attachment_id: str,
    *,
    confidence: float,
) -> ExtractionPayload:
    return ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [
                {
                    "table_id": "QUANT-RETRY-TABLE",
                    "label": "정량평가표",
                    "criteria": [
                        {
                            "criterion_id": "QUANT-RETRY-PERFORMANCE",
                            "label": "수행실적",
                            "criterion_literal": "수행실적 10점",
                            "max_points": 10,
                            "scoring_method": "THRESHOLD",
                            "metric": "PERFORMANCE_AMOUNT",
                            "unit": "억원",
                            "brackets": [],
                            "threshold": {
                                "literal": "5억원 이상 충족 10점 미충족 0점",
                                "operator": "GTE",
                                "threshold_value": 5,
                                "points_if_met": 10,
                                "points_if_not_met": 0,
                                "evidence": {
                                    "attachment_id": attachment_id,
                                    "page": 1,
                                    "section": "정량평가표",
                                    "quote": "5억원 이상 충족 10점 미충족 0점",
                                    "confidence": 0.99,
                                },
                            },
                            "formula_literal": None,
                            "cases": [],
                            "recognition_conditions": [],
                            "required_evidence": ["company.performance.amount"],
                            "evidence": {
                                "attachment_id": attachment_id,
                                "page": 1,
                                "section": "정량평가표",
                                "quote": "수행실적 10점",
                                "confidence": confidence,
                            },
                            "ambiguity_reason": None,
                        }
                    ],
                    "total_points": 10,
                    "total_evidence": {
                        "attachment_id": attachment_id,
                        "page": 1,
                        "section": "정량평가표",
                        "quote": "정량평가 총점 10점",
                        "confidence": 0.99,
                    },
                    "minimum_score": None,
                    "minimum_evidence": None,
                    "ambiguity_reason": None,
                }
            ],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "정량평가표 추출",
        }
    )


class _QuantitativeConfidenceRetryClient:
    calls = 0

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "_QuantitativeConfidenceRetryClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract(
        self,
        *,
        document_text: str,
        allowed_attachment_ids: set[str],
    ) -> ExtractionOutcome:
        type(self).calls += 1
        assert _QUANTITATIVE_RETRY_SOURCE.strip() in document_text
        attachment_id = next(iter(allowed_attachment_ids))
        return ExtractionOutcome(
            status="ACCEPTED",
            message="validated",
            api_calls=1,
            data=_quantitative_confidence_payload(
                attachment_id,
                confidence=0.80 if type(self).calls == 1 else 0.99,
            ),
        )


class _PersistentQuantitativeReviewClient(_QuantitativeConfidenceRetryClient):
    calls = 0

    def extract(
        self,
        *,
        document_text: str,
        allowed_attachment_ids: set[str],
    ) -> ExtractionOutcome:
        type(self).calls += 1
        assert _QUANTITATIVE_RETRY_SOURCE.strip() in document_text
        attachment_id = next(iter(allowed_attachment_ids))
        return ExtractionOutcome(
            status="ACCEPTED",
            message="validated with quantitative review",
            api_calls=1,
            data=_quantitative_confidence_payload(
                attachment_id,
                confidence=0.80,
            ),
        )


def _single_hwpx_reuse_case(
    *,
    notice_key: str,
    source_text: str = (
        "교육 컨설팅 수행실적을 제출해야 합니다.\n마감일까지 제출합니다."
    ),
) -> tuple[Engine, sessionmaker[Session], str, httpx.MockTransport]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<s>"
            + "".join(f"<p>{line}</p>" for line in source_text.splitlines())
            + "</s>",
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
            notice_key=notice_key,
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
            raw_item={
                "bidNtceNo": "R26BK00000001",
                "bidNtceOrd": "000",
                "ntceSpecFileNm1": "제안요청서.hwpx",
                "ntceSpecDocUrl1": G2B_DOWNLOAD,
            },
            search_keywords=["교육", "컨설팅"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id
    return engine, factory, notice_id, transport


def _append_extraction_history_version(
    session: Session,
    template: NoticeVersion,
    *,
    status: str,
    error_code: str | None = None,
) -> NoticeVersion:
    payload = json.loads(json.dumps(template.source_payload, ensure_ascii=False))
    payload["status"] = status
    if status == "REVIEW":
        payload.update(
            {
                "review_code": "R07",
                "error_code": error_code,
                "message": "pre-request review history",
                "result": None,
                "quantitative_validation_record": None,
            }
        )
    existing = list(
        session.scalars(
            select(NoticeVersion).where(NoticeVersion.notice_id == template.notice_id)
        ).all()
    )
    version = NoticeVersion(
        notice_id=template.notice_id,
        version_no=max(item.version_no for item in existing) + 1,
        file_sha256=template.file_sha256,
        document_complete=template.document_complete if status == "ACCEPTED" else False,
        extraction_status=status,
        extraction_confidence=template.extraction_confidence if status == "ACCEPTED" else 0.0,
        source_payload=payload,
    )
    session.add(version)
    session.flush()
    return version


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


def test_cross_processing_version_reuses_identical_source_and_analysis_input() -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-CROSS-PROCESSING-REUSE",
    )
    _CountingExtractionClient.calls = 0
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
        legacy = session.get(NoticeVersion, first.version_id)
        assert legacy is not None
        legacy_payload = json.loads(json.dumps(legacy.source_payload, ensure_ascii=False))
        legacy_payload["processing_version"] = "pps-document-processing-0.3.0"
        legacy.source_payload = legacy_payload
        source_hash = legacy_payload["document_processing"]["source_text_sha256"]
        input_hash = legacy_payload["document_processing"]["analysis_input_sha256"]
        session.commit()

    with factory() as session:
        second = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_CountingExtractionClient,
        )
        current = session.get(NoticeVersion, second.version_id)
        assert current is not None
        assert current.id != first.version_id
        assert current.document_complete is True
        assert current.source_payload["processing_version"] == PPS_PROCESSING_VERSION
        assert current.source_payload["document_processing"]["processing_version"] == (
            PPS_PROCESSING_VERSION
        )
        assert current.source_payload["document_processing"]["source_text_sha256"] == (
            source_hash
        )
        assert current.source_payload["document_processing"]["analysis_input_sha256"] == (
            input_hash
        )

    assert first.status == "COMPLETED"
    assert first.openai_calls == 2
    assert second.status == "REUSED"
    assert second.openai_calls == 0
    assert "DUPLICATE_CONTENT_REUSED" in second.warnings
    assert _CountingExtractionClient.calls == 1
    engine.dispose()


def test_legacy_quantitative_validator_rebuilds_record_without_provider_call() -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-QUANT-VALIDATOR-REBUILD",
    )
    _CountingExtractionClient.calls = 0
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
        legacy = session.get(NoticeVersion, first.version_id)
        assert legacy is not None
        legacy_payload = json.loads(json.dumps(legacy.source_payload, ensure_ascii=False))
        record = ValidatedQuantitativeAttachmentRecord.model_validate(
            legacy_payload["quantitative_validation_record"]
        )
        legacy_record = record.model_copy(
            update={"validator_version": "pai-loop-quantitative-attachment-validator-0.3.0"}
        )
        legacy_record = legacy_record.model_copy(
            update={
                "validation_fingerprint_sha256": validated_quantitative_record_fingerprint(
                    legacy_record
                )
            }
        )
        legacy_payload["quantitative_validation_record"] = legacy_record.model_dump(
            mode="json"
        )
        legacy.source_payload = legacy_payload
        session.commit()

    with factory() as session:
        second = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_CountingExtractionClient,
        )
        current = session.get(NoticeVersion, second.version_id)
        original = session.get(NoticeVersion, first.version_id)
        assert current is not None
        assert original is not None
        assert current.id != original.id
        current_record = ValidatedQuantitativeAttachmentRecord.model_validate(
            current.source_payload["quantitative_validation_record"]
        )
        original_record = ValidatedQuantitativeAttachmentRecord.model_validate(
            original.source_payload["quantitative_validation_record"]
        )
        assert current_record.validator_version == QUANTITATIVE_ATTACHMENT_VALIDATOR_VERSION
        assert original_record.validator_version.endswith("-0.3.0")
        assert current_record.validation_fingerprint_sha256 == (
            validated_quantitative_record_fingerprint(current_record)
        )

    assert first.status == "COMPLETED"
    assert first.openai_calls == 2
    assert second.status == "REUSED"
    assert second.openai_calls == 0
    assert "DUPLICATE_CONTENT_REUSED" in second.warnings
    assert _CountingExtractionClient.calls == 1
    engine.dispose()


def test_matching_extraction_version_targets_quantitative_fingerprint_revision(
) -> None:
    attachment_id = "PPS-ATT-TARGETED-REVISION"
    document_sha256 = "a" * 64
    current_manifest_sha256 = "b" * 64
    attachment_manifest_sha256 = "c" * 64
    threshold_literal = "5억원 이상 충족 10점 미충족 0점"
    source = "\n".join(
        [
            "정량평가표",
            "수행실적 10점",
            threshold_literal,
            "정량평가 총점 10점",
        ]
    )

    def evidence(quote: str) -> dict[str, object]:
        return {
            "attachment_id": attachment_id,
            "page": 1,
            "section": "정량평가표",
            "quote": quote,
            "confidence": 0.99,
        }

    payload = ExtractionPayload.model_validate(
        {
            "document_type": "RFP",
            "requirements": [],
            "quantitative_tables": [
                {
                    "table_id": "TABLE-1",
                    "label": "정량평가표",
                    "criteria": [
                        {
                            "criterion_id": "PERFORMANCE-THRESHOLD-1",
                            "label": "수행실적",
                            "criterion_literal": "수행실적 10점",
                            "max_points": 10,
                            "scoring_method": "THRESHOLD",
                            "metric": "PERFORMANCE_AMOUNT",
                            "unit": "억원",
                            "brackets": [],
                            "threshold": {
                                "literal": threshold_literal,
                                "operator": "GTE",
                                "threshold_value": 5,
                                "points_if_met": 10,
                                "points_if_not_met": 0,
                                "evidence": evidence(threshold_literal),
                            },
                            "formula_literal": None,
                            "required_evidence": ["company.performance.amount"],
                            "evidence": evidence("수행실적 10점"),
                            "ambiguity_reason": None,
                        }
                    ],
                    "total_points": 10,
                    "total_evidence": evidence("정량평가 총점 10점"),
                    "minimum_score": None,
                    "minimum_evidence": None,
                    "ambiguity_reason": None,
                }
            ],
            "quantitative_table_not_applicable": None,
            "missing_or_unreadable": [],
            "summary": "검증 가능한 정량평가표",
        }
    )
    available = validate_quantitative_attachment_extraction(
        payload,
        source_text=source,
        attachment_id=attachment_id,
        document_sha256=document_sha256,
        manifest_sha256=current_manifest_sha256,
    )
    assert available.status == "AVAILABLE"
    confidence_values = pps_enrichment_module._extraction_evidence_confidences(payload)
    assert confidence_values
    assert set(confidence_values) == {0.99}

    available_legacy_digest = _digest(
        available.model_dump(
            mode="json",
            exclude={"validation_fingerprint_sha256"},
        )
    )
    unaffected_legacy = available.model_copy(
        update={"validation_fingerprint_sha256": available_legacy_digest}
    )
    assert (
        validated_quantitative_record_fingerprint(unaffected_legacy)
        == available_legacy_digest
    )

    affected_data = available.model_dump(mode="json")
    affected_data["status"] = "REVIEW"
    affected_data["tables"][0]["status"] = "REVIEW"
    affected_data["issues"] = [
        {
            "code": "SOURCEWIDE_AMBIGUITY_SIGNATURE_UNSUPPORTED",
            "disposition": "REVIEW",
            "message": "구조 서명의 선별 재검증이 필요합니다.",
            "attachment_id": attachment_id,
            "table_id": "TABLE-1",
            "criterion_id": None,
            "required_sibling_document_types": [],
            "required_sibling_label_markers": [],
            "source_gap_statement": None,
            "source_gap_document_type": None,
        }
    ]
    affected_data["validation_fingerprint_sha256"] = "0" * 64
    affected = ValidatedQuantitativeAttachmentRecord.model_validate(affected_data)
    affected_legacy_digest = _digest(
        affected.model_dump(
            mode="json",
            exclude={"validation_fingerprint_sha256"},
        )
    )
    affected_legacy = affected.model_copy(
        update={"validation_fingerprint_sha256": affected_legacy_digest}
    )
    affected_revised = affected.model_copy(
        update={
            "validation_fingerprint_sha256": (
                validated_quantitative_record_fingerprint(affected)
            )
        }
    )
    assert affected_revised.validation_fingerprint_sha256 != affected_legacy_digest

    def version_for(
        record: ValidatedQuantitativeAttachmentRecord,
        *,
        version_no: int,
    ) -> NoticeVersion:
        return NoticeVersion(
            notice_id="notice",
            version_no=version_no,
            file_sha256=document_sha256,
            document_complete=True,
            extraction_status="ACCEPTED",
            extraction_confidence=1.0,
            source_payload={
                "kind": "OPENAI_REQUIREMENT_EXTRACTION",
                "source_kind": PPS_ATTACHMENT_SOURCE,
                "attachment_id": attachment_id,
                "manifest_sha256": attachment_manifest_sha256,
                "current_manifest_sha256": current_manifest_sha256,
                "document_sha256": document_sha256,
                "prompt_version": PROMPT_VERSION,
                "processing_version": PPS_PROCESSING_VERSION,
                "schema_version": SCHEMA_VERSION,
                "status": "ACCEPTED",
                "error_code": None,
                "quantitative_validation_record": record.model_dump(mode="json"),
            },
        )

    legacy_affected_version = version_for(affected_legacy, version_no=1)
    unaffected_version = version_for(unaffected_legacy, version_no=2)
    revised_affected_version = version_for(affected_revised, version_no=3)

    def matching(versions: list[NoticeVersion]) -> NoticeVersion | None:
        return pps_enrichment_module._matching_extraction_version(
            versions,
            attachment_id=attachment_id,
            manifest_sha256=attachment_manifest_sha256,
            current_manifest_sha256=current_manifest_sha256,
            document_sha256=document_sha256,
        )

    assert matching([legacy_affected_version]) is None
    assert matching([unaffected_version]) is unaffected_version
    assert matching([revised_affected_version]) is revised_affected_version


@pytest.mark.parametrize(
    ("changed_scope", "changed_field", "changed_value"),
    [
        ("document_processing", "source_text_sha256", "0" * 64),
        ("document_processing", "analysis_input_sha256", "0" * 64),
        ("payload", "document_sha256", "0" * 64),
        ("payload", "prompt_version", "legacy-prompt"),
        ("payload", "schema_version", "legacy-schema"),
    ],
)
def test_cross_processing_version_changed_hash_or_contract_forces_openai(
    changed_scope: str,
    changed_field: str,
    changed_value: str,
) -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key=f"PPS-CROSS-PROCESSING-{changed_field}",
    )
    _CountingExtractionClient.calls = 0
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
        legacy = session.get(NoticeVersion, first.version_id)
        assert legacy is not None
        legacy_payload = json.loads(json.dumps(legacy.source_payload, ensure_ascii=False))
        legacy_payload["processing_version"] = "pps-document-processing-0.3.0"
        if changed_scope == "document_processing":
            legacy_payload["document_processing"][changed_field] = changed_value
        else:
            legacy_payload[changed_field] = changed_value
        legacy.source_payload = legacy_payload
        session.commit()

    with factory() as session:
        second = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_CountingExtractionClient,
        )
        current = session.get(NoticeVersion, second.version_id)
        assert current is not None
        assert current.id != first.version_id
        assert current.source_payload["processing_version"] == PPS_PROCESSING_VERSION

    assert first.openai_calls == 2
    assert second.status == "COMPLETED"
    assert second.openai_calls == 2
    assert "DUPLICATE_CONTENT_REUSED" not in second.warnings
    assert _CountingExtractionClient.calls == 2
    engine.dispose()


def test_accepted_semantic_gap_does_not_downgrade_technical_document_coverage() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr(
            "Contents/section0.xml",
            "<s><p>교육 컨설팅 수행실적을 제출해야 합니다.</p>"
            "<p>별도 제안요청서를 참조합니다.</p></s>",
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
            notice_key="PPS-SEMANTIC-GAP-001",
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
            raw_item={
                "bidNtceNo": "R26BK00000001",
                "bidNtceOrd": "000",
                "ntceSpecFileNm1": "입찰공고.hwpx",
                "ntceSpecDocUrl1": G2B_DOWNLOAD,
            },
            search_keywords=["교육", "컨설팅"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id

    _SemanticGapExtractionClient.calls = 0
    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_SemanticGapExtractionClient,
        )
    with factory() as session:
        second = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_SemanticGapExtractionClient,
        )
        version = session.get(NoticeVersion, first.version_id)
        assert version is not None
        assert version.document_complete is True
        assert version.source_payload["result"]["missing_or_unreadable"]
        coverage = pps_attachment_coverage(list(version.notice.versions))
        reason = public_analysis_reason(list(version.notice.versions))
        assert coverage.complete is True
        assert coverage.accepted == 1
        assert reason.reason_code == "ANALYZED"
        assert has_current_accepted_pps_extraction(session, notice_id) is True

    assert first.status == "COMPLETED"
    assert first.attachment_results[0].status == "COMPLETED"
    assert first.attachment_results[0].reason_code == "ANALYZED"
    assert pps_enrichment_module.SEMANTIC_SOURCE_GAPS_WARNING in first.warnings
    assert second.status == "REUSED"
    assert pps_enrichment_module.SEMANTIC_SOURCE_GAPS_WARNING in second.warnings
    assert first.version_id == second.version_id
    assert _SemanticGapExtractionClient.calls == 1
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


@pytest.mark.parametrize("request_limit", [2, 10])
def test_five_attachments_persist_and_resume_with_configured_limit(monkeypatch, request_limit) -> None:
    from pai_loop import pps_enrichment

    assert pps_enrichment.MAX_NEW_ATTACHMENTS_PER_REQUEST == 10
    monkeypatch.setattr(pps_enrichment, "MAX_NEW_ATTACHMENTS_PER_REQUEST", request_limit)
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

    if request_limit == 2:
        assert [item.status for item in results] == ["SKIPPED", "SKIPPED", "COMPLETED", "REUSED"]
        assert [item.attachments_attempted for item in results] == [2, 4, 5, 5]
        assert [item.openai_calls for item in results] == [4, 4, 2, 0]
        assert "ATTACHMENT_CONTINUATION_REQUIRED" in results[0].warnings
        assert "ATTACHMENT_CONTINUATION_REQUIRED" in results[1].warnings
    else:
        assert [item.status for item in results] == ["COMPLETED", "REUSED", "REUSED", "REUSED"]
        assert [item.attachments_attempted for item in results] == [5, 5, 5, 5]
        assert [item.openai_calls for item in results] == [10, 0, 0, 0]
        assert "ATTACHMENT_CONTINUATION_REQUIRED" not in results[0].warnings
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


def test_explicit_review_retry_is_scoped_to_preexisting_transient_version() -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-EXPLICIT-REVIEW-RETRY",
    )
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
        recent = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
        )
    with factory() as session:
        notice = session.get(Notice, notice_id)
        assert notice is not None
        retry_version_ids = current_retryable_review_version_ids(list(notice.versions))
    assert retry_version_ids == frozenset({first.version_id, recent.version_id})

    with factory() as session:
        retried = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
            retry_reviewed_version_ids=retry_version_ids,
        )
    with factory() as session:
        continued = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
            retry_reviewed_version_ids=retry_version_ids,
        )

    assert first.status == recent.status == retried.status == continued.status == "REVIEW"
    assert len({first.version_id, recent.version_id, retried.version_id}) == 3
    assert continued.version_id == retried.version_id
    assert first.openai_calls == recent.openai_calls == retried.openai_calls == 1
    assert continued.openai_calls == 0
    assert _RetryableReviewClient.calls == 3
    engine.dispose()


def test_explicit_review_retry_never_bypasses_accepted_version() -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-EXPLICIT-ACCEPTED-REUSE",
    )
    _CountingExtractionClient.calls = 0
    with factory() as session:
        accepted = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_CountingExtractionClient,
        )
    with factory() as session:
        reused = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_CountingExtractionClient,
            retry_reviewed_version_ids=frozenset({accepted.version_id}),
        )

    assert accepted.status == "COMPLETED"
    assert reused.status == "REUSED"
    assert reused.version_id == accepted.version_id
    assert reused.openai_calls == 0
    assert _CountingExtractionClient.calls == 1
    engine.dispose()


def test_explicit_review_retry_reextracts_only_accepted_quantitative_review_once() -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-EXPLICIT-QUANTITATIVE-REVIEW-RETRY",
        source_text=_QUANTITATIVE_RETRY_SOURCE,
    )
    _QuantitativeConfidenceRetryClient.calls = 0
    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_QuantitativeConfidenceRetryClient,
        )
    with factory() as session:
        first_version = session.get(NoticeVersion, first.version_id)
        notice = session.get(Notice, notice_id)
        assert first_version is not None
        assert notice is not None
        first_record = ValidatedQuantitativeAttachmentRecord.model_validate(
            first_version.source_payload["quantitative_validation_record"]
        )
        retry_version_ids = current_retryable_review_version_ids(
            list(notice.versions)
        )
        assert first_record.status == "INCOMPLETE"
        assert first_record.review_candidates
        assert retry_version_ids == frozenset({first.version_id})
        assert has_current_accepted_pps_extraction(session, notice_id) is True

    with factory() as session:
        retried = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_QuantitativeConfidenceRetryClient,
            retry_reviewed_version_ids=retry_version_ids,
        )
    with factory() as session:
        continued = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_QuantitativeConfidenceRetryClient,
            retry_reviewed_version_ids=retry_version_ids,
        )
        retried_version = session.get(NoticeVersion, retried.version_id)
        assert retried_version is not None
        retried_record = ValidatedQuantitativeAttachmentRecord.model_validate(
            retried_version.source_payload["quantitative_validation_record"]
        )

    assert first.status == retried.status == "COMPLETED"
    assert continued.status == "REUSED"
    assert retried.version_id != first.version_id
    assert continued.version_id == retried.version_id
    assert first.openai_calls == retried.openai_calls == 1
    assert continued.openai_calls == 0
    assert retried_record.status == "AVAILABLE"
    assert retried_record.review_candidates == ()
    assert _QuantitativeConfidenceRetryClient.calls == 2
    assert "DUPLICATE_CONTENT_REUSED" not in retried.warnings
    engine.dispose()


def test_accepted_quantitative_retry_does_not_dedupe_to_older_accepted_history() -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-QUANTITATIVE-RETRY-OLDER-ACCEPTED",
        source_text=_QUANTITATIVE_RETRY_SOURCE,
    )
    _PersistentQuantitativeReviewClient.calls = 0
    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_PersistentQuantitativeReviewClient,
        )
    with factory() as session:
        first_version = session.get(NoticeVersion, first.version_id)
        assert first_version is not None
        latest_before_request = _append_extraction_history_version(
            session,
            first_version,
            status="ACCEPTED",
        )
        session.commit()
        latest_before_request_id = latest_before_request.id
    with factory() as session:
        notice = session.get(Notice, notice_id)
        assert notice is not None
        retry_version_ids = current_retryable_review_version_ids(list(notice.versions))
    assert retry_version_ids == frozenset({latest_before_request_id})

    with factory() as session:
        retried = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_PersistentQuantitativeReviewClient,
            retry_reviewed_version_ids=retry_version_ids,
        )
    with factory() as session:
        continued = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_PersistentQuantitativeReviewClient,
            retry_reviewed_version_ids=retry_version_ids,
        )

    assert retried.version_id not in {first.version_id, latest_before_request_id}
    assert continued.version_id == retried.version_id
    assert retried.openai_calls == 1
    assert continued.openai_calls == 0
    assert _PersistentQuantitativeReviewClient.calls == 2
    engine.dispose()


@pytest.mark.parametrize(
    "older_error_code",
    ["INCOMPLETE_RESPONSE", "UNSUPPORTED_ATTACHMENT_TYPE"],
)
def test_accepted_quantitative_retry_ignores_all_pre_request_review_history(
    older_error_code: str,
) -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key=f"PPS-QUANTITATIVE-RETRY-OLDER-{older_error_code}",
        source_text=_QUANTITATIVE_RETRY_SOURCE,
    )
    _PersistentQuantitativeReviewClient.calls = 0
    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_PersistentQuantitativeReviewClient,
        )
    with factory() as session:
        first_version = session.get(NoticeVersion, first.version_id)
        assert first_version is not None
        older_review = _append_extraction_history_version(
            session,
            first_version,
            status="REVIEW",
            error_code=older_error_code,
        )
        latest_before_request = _append_extraction_history_version(
            session,
            first_version,
            status="ACCEPTED",
        )
        session.commit()
        pre_request_ids = {
            first.version_id,
            older_review.id,
            latest_before_request.id,
        }
        latest_before_request_id = latest_before_request.id
    with factory() as session:
        notice = session.get(Notice, notice_id)
        assert notice is not None
        retry_version_ids = current_retryable_review_version_ids(list(notice.versions))
    assert retry_version_ids == frozenset({latest_before_request_id})

    _RetryableReviewClient.calls = 0
    with factory() as session:
        retried = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
            retry_reviewed_version_ids=retry_version_ids,
        )
    with factory() as session:
        continued = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_RetryableReviewClient,
            retry_reviewed_version_ids=retry_version_ids,
        )

    assert retried.status == continued.status == "REVIEW"
    assert retried.version_id not in pre_request_ids
    assert continued.version_id == retried.version_id
    assert retried.openai_calls == 1
    assert continued.openai_calls == 0
    assert _RetryableReviewClient.calls == 1
    engine.dispose()


def test_explicit_review_retry_never_bypasses_deterministic_review() -> None:
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        notice = Notice(
            notice_key="PPS-EXPLICIT-DETERMINISTIC-REUSE",
            bid_notice_no="R26BK00000003",
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
                "bidNtceNo": "R26BK00000003",
                "bidNtceOrd": "000",
                "ntceSpecFileNm1": "참고자료.txt",
                "ntceSpecDocUrl1": G2B_DOWNLOAD.replace(
                    "R26BK00000001", "R26BK00000003"
                ),
            },
            search_keywords=["교육"],
            dry_run=False,
        )
        session.commit()
        notice_id = notice.id

    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
        )
    with factory() as session:
        notice = session.get(Notice, notice_id)
        assert notice is not None
        assert current_retryable_review_version_ids(list(notice.versions)) == frozenset()
        session.rollback()
        reused = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            retry_reviewed_version_ids=frozenset({first.version_id}),
        )

    assert first.status == "REVIEW"
    assert "UNSUPPORTED_ATTACHMENT_TYPE" in first.warnings
    assert reused.status == "REVIEW"
    assert reused.version_id == first.version_id
    assert reused.openai_calls == 0
    engine.dispose()


def test_stale_quote_correction_retries_immediately_then_observes_cooldown() -> None:
    engine, factory, notice_id, transport = _single_hwpx_reuse_case(
        notice_key="PPS-QUOTE-CORRECTION-VERSION",
    )
    _QuoteReviewClient.calls = 0
    with factory() as session:
        first = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_QuoteReviewClient,
        )
    with factory() as session:
        stale = session.get(NoticeVersion, first.version_id)
        assert stale is not None
        stale_payload = json.loads(json.dumps(stale.source_payload, ensure_ascii=False))
        stale_payload["correction_prompt_version"] = "pai-loop-quote-correction-legacy"
        stale.source_payload = stale_payload
        session.commit()

    with factory() as session:
        retried = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_QuoteReviewClient,
        )
    with factory() as session:
        cooldown_reuse = enrich_notice_from_pps(
            session,
            notice_id=notice_id,
            openai_api_key="test-key",
            openai_model="test-model",
            transport=transport,
            openai_client_factory=_QuoteReviewClient,
        )
        current = session.get(NoticeVersion, retried.version_id)
        assert current is not None
        assert (
            current.source_payload["correction_prompt_version"]
            == CORRECTIVE_PROMPT_VERSION
        )

    assert first.status == retried.status == cooldown_reuse.status == "REVIEW"
    assert first.openai_calls == retried.openai_calls == 2
    assert cooldown_reuse.openai_calls == 0
    assert first.version_id != retried.version_id
    assert retried.version_id == cooldown_reuse.version_id
    assert _QuoteReviewClient.calls == 2
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


@pytest.mark.parametrize(
    ("extension", "error", "status", "expected_state", "expected_code"),
    (
        (".pdf", None, "REVIEW", "PENDING", "ATTACHMENT_COVERAGE_INCOMPLETE"),
        (".hwpx", "HWPX_XML_INVALID", "REVIEW", "REVIEW", "HWPX_EXTRACT_FAILED"),
        (".pdf", "UNVERIFIED_QUOTE", "REVIEW", "REVIEW", "QUOTE_UNVERIFIED"),
        (".pdf", "SYN-PRIVATE-PROVIDER-ERROR", "REVIEW", "REVIEW", "OPENAI_REVIEW"),
        (".pdf", None, "ACCEPTED", "ANALYZED", "ANALYZED"),
    ),
)
def test_public_attachment_cards_include_failures_without_raw_errors(
    extension: str, error: str | None, status: str, expected_state: str, expected_code: str,
) -> None:
    from pai_loop.pps_enrichment import public_attachment_analysis_statuses
    versions = _analysis_versions(extension, error_code=error, status=status)
    if len(versions) > 1:
        versions[-1].source_payload["provider_response_id"] = "SYN-PRIVATE-RESPONSE"
        versions[-1].source_payload["source_text"] = "SYN-PRIVATE-SOURCE"
    rows = public_attachment_analysis_statuses(versions)
    assert len(rows) == 1
    assert rows[0]["document_name"] == f"제안요청서{extension}"
    assert rows[0]["state"] == expected_state
    assert rows[0]["reason_code"] == expected_code
    assert set(rows[0]) == {"document_name", "state", "reason_code", "reason"}
    assert "SYN-PRIVATE" not in json.dumps(rows)
    assert "http" not in json.dumps(rows)


def test_attachment_cards_use_current_manifest_and_current_attempt_only() -> None:
    from pai_loop.pps_enrichment import public_attachment_analysis_statuses
    versions = _analysis_versions(".pdf", error_code="UNVERIFIED_QUOTE")
    assert public_attachment_analysis_statuses(versions)[0]["state"] == "REVIEW"
    versions[-1].source_payload["prompt_version"] = "SYN-OLD-PROMPT"
    assert public_attachment_analysis_statuses(versions)[0]["state"] == "PENDING"
    versions[0].source_payload["schema_version"] = "SYN-OLD-SCHEMA"
    rows = public_attachment_analysis_statuses(versions)
    assert rows[-1]["document_name"] == "첨부 목록 확인 필요"
    assert rows[-1]["state"] == "REVIEW"


@pytest.mark.parametrize("error, expected", (
    ("SCHEMA_VALIDATION_ERROR", "MODEL_SCHEMA_INVALID"),
    ("NETWORK_ERROR", "MODEL_NETWORK_FAILED"),
    ("HTTP_ERROR", "MODEL_HTTP_FAILED"),
    ("INCOMPLETE_RESPONSE", "MODEL_RESPONSE_INCOMPLETE"),
))
def test_public_notice_detail_exposes_only_fixed_attachment_failure_reasons(monkeypatch, error, expected) -> None:
    from fastapi.testclient import TestClient
    from conftest import login_department_reader
    from pai_loop.main import create_app
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_LOOP_PUBLIC_READ_ONLY", "true")
    monkeypatch.setenv("PAI_LOOP_API_KEY", "SYN-server-only-secret")
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        login_department_reader(client)
        with app.state.session_factory() as session:
            notice = Notice(notice_key="SYN-CARD-API", bid_notice_no="SYN-CARD-API", title="합성 첨부 검사", agency="합성 기관", status="OPEN",
                            deadline=datetime(2027, 1, 1, tzinfo=timezone.utc))
            session.add(notice)
            session.flush()
            for version in _analysis_versions(".pdf", error_code=error):
                version.notice_id = notice.id
                version.source_payload["private_error"] = "SYN-PRIVATE-ERROR"
                session.add(version)
            session.commit()
        response = client.get("/api/v1/notices/SYN-CARD-API")
        assert response.status_code == 200, response.text
        row, = response.json()["attachment_analysis_statuses"]
        assert row["state"] == "REVIEW"
        assert row["reason_code"] == expected
        assert row["document_name"] == "제안요청서.pdf"
        assert "SYN-PRIVATE" not in response.text
        assert "source_payload" not in response.text


@pytest.mark.parametrize("stale_field", ["prompt_version", "processing_version", "quantitative_validation_record"])
def test_recorded_attempt_count_preserves_processing_history_without_activating_stale_rules(stale_field):
    versions = _analysis_versions(".pdf", status="ACCEPTED")
    assert pps_recorded_attachment_attempt_count(versions) == 1
    versions[1].source_payload[stale_field] = "obsolete-contract"
    assert pps_recorded_attachment_attempt_count(versions) == 1
    assert pps_attachment_coverage(versions).accepted == 0
    assert public_analysis_reason(versions).state == "PENDING"
    # Duplicate retries are one processed file, not multiple files.
    assert pps_recorded_attachment_attempt_count([*versions, versions[1]]) == 1


@pytest.mark.parametrize("changed_field", ["manifest_sha256", "current_manifest_sha256", "attachment_id", "source_kind"])
def test_recorded_attempt_count_rejects_unbound_or_superseded_history(changed_field):
    versions = _analysis_versions(".pdf", status="ACCEPTED")
    versions[1].source_payload[changed_field] = "unbound"
    assert pps_recorded_attachment_attempt_count(versions) == 0


def test_recorded_attempt_count_separates_not_selected_from_a_file_failure():
    versions = _analysis_versions(".pdf", status="REVIEW", error_code=None)
    assert pps_recorded_attachment_attempt_count(versions) == 0
    versions = _analysis_versions(".pdf", status="REVIEW", error_code="PDF_TEXT_EXTRACTION_FAILED")
    assert pps_recorded_attachment_attempt_count(versions) == 1
    versions[0].source_payload["attachment_manifest"][0]["file_name"] = "replacement.pdf"
    assert pps_recorded_attachment_attempt_count(versions) == 0


@pytest.mark.parametrize("newest_status", ["ACCEPTED", "REVIEW", "INVALID_RECORD"])
def test_current_attempt_scan_skips_superseded_record_validation(monkeypatch, newest_status):
    from copy import deepcopy
    import pai_loop.pps_enrichment as enrichment
    versions = _analysis_versions(".pdf", status="ACCEPTED")
    template = versions[-1]
    for number in range(3, 82):
        versions.append(NoticeVersion(
            notice_id="SYN-NOTICE-HISTORY", version_no=number,
            file_sha256=template.file_sha256, document_complete=True,
            extraction_status="ACCEPTED", extraction_confidence=1,
            source_payload=deepcopy(template.source_payload),
        ))
    if newest_status == "REVIEW":
        versions[-1].source_payload.update(status="REVIEW", error_code="UNVERIFIED_QUOTE")
        versions[-1].extraction_status = "REVIEW"
    elif newest_status == "INVALID_RECORD":
        versions[-1].source_payload["quantitative_validation_record"] = {}
    checked = []
    original = enrichment._validate_quantitative_record_binding
    def track(version, **kwargs):
        checked.append(version.version_no)
        return original(version, **kwargs)
    monkeypatch.setattr(enrichment, "_validate_quantitative_record_binding", track)
    _, _, attempts = enrichment._current_manifest_attempts(list(reversed(versions)))
    selected, = attempts.values()
    assert selected.version_no == (80 if newest_status == "INVALID_RECORD" else 81)
    assert checked == ({"ACCEPTED": [81], "REVIEW": [], "INVALID_RECORD": [81, 80]}[newest_status])


def test_read_projection_reuses_validation_and_revalidates_after_source_change(monkeypatch):
    import pai_loop.pps_enrichment as enrichment
    versions = _analysis_versions(".pdf", status="ACCEPTED")
    checked = []
    original = enrichment._validate_quantitative_record_binding
    def track(version, **kwargs):
        checked.append(version.version_no)
        return original(version, **kwargs)
    monkeypatch.setattr(enrichment, "_validate_quantitative_record_binding", track)
    with enrichment.pps_attachment_audit_read_scope():
        assert public_analysis_reason(versions).state == "ANALYZED"
        with enrichment.pps_attachment_audit_read_scope():
            assert pps_attachment_coverage(versions).accepted == 1
        assert public_analysis_reason(versions).state == "ANALYZED"
    assert checked == [2]
    # A later database read must recheck every source binding and fingerprint.
    versions[-1].source_payload["quantitative_validation_record"]["document_sha256"] = "f" * 64
    with enrichment.pps_attachment_audit_read_scope():
        assert public_analysis_reason(versions).state == "PENDING"
        assert pps_attachment_coverage(versions).accepted == 0
    assert checked == [2, 2]
    assert enrichment._attachment_validation_read_cache.get() is None


def test_read_projection_scope_clears_on_exception_and_isolates_threads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import pai_loop.pps_enrichment as enrichment
    versions = _analysis_versions(".pdf", status="ACCEPTED")
    with pytest.raises(RuntimeError, match="SYN-interrupted"):
        with enrichment.pps_attachment_audit_read_scope():
            assert public_analysis_reason(versions).state == "ANALYZED"
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(enrichment._attachment_validation_read_cache.get).result() is None
            raise RuntimeError("SYN-interrupted")
    assert enrichment._attachment_validation_read_cache.get() is None
    versions[-1].file_sha256 = "f" * 64
    assert public_analysis_reason(versions).state == "PENDING"
