from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.document_extraction import (
    DocumentExtractionError, ExtractionLimits, extract_document_content,
)
from pai_loop.models import Notice, NoticeVersion
import pai_loop.pps_enrichment as pps
from test_pps_enrichment import G2B_DOWNLOAD, _CountingExtractionClient, _RetryableReviewClient


TEXT = "교육 컨설팅 수행실적을 제출해야 합니다.\n마감일까지 제출합니다."


def package(*, mimetype: bytes | None = b"application/hwp+zip", extras=()) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        def write(name, value):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
        if mimetype is not None:
            write("mimetype", mimetype)
        write("Contents/section0.xml", "<s>" + "".join(
            f"<p>{line}</p>" for line in TEXT.splitlines()
        ) + "</s>")
        for name, value in extras:
            write(name, value)
    return result.getvalue()


def test_exact_hwpx_package_with_hwp_name_retains_same_source_and_text() -> None:
    content = package()
    canonical = pps.extract_pps_document_content("SYN.hwpx", content)
    mislabeled = pps.extract_pps_document_content("SYN.hwp", content)
    assert mislabeled == canonical
    assert mislabeled.complete and mislabeled.text == TEXT
    observed = []

    def leaf(name: str, value: bytes) -> str:
        observed.append((name, value))
        return TEXT

    extract_document_content("SYN.hwp", content, leaf_extractors={".hwpx": leaf})
    assert observed == [("SYN.hwp", content)]


@pytest.mark.parametrize("mimetype", [None, b"application/zip", b"Application/hwp+zip", b"application/hwp+zip\n"])
def test_generic_zip_or_inexact_media_type_is_not_promoted_to_hwpx(mimetype) -> None:
    with pytest.raises(pps.PpsEnrichmentError, match="HWP_CONTAINER_INVALID"):
        pps.extract_pps_document_content("SYN.hwp", package(mimetype=mimetype))


@pytest.mark.parametrize("extras,limits,code", [
    ((("../escape.xml", "ignored"),), None, "ARCHIVE_UNSAFE_MEMBER_PATH"),
    ((("mimetype", "application/hwp+zip"),), None, "ARCHIVE_DUPLICATE_MEMBER"),
    ((("large.bin", "x" * 2000),), ExtractionLimits(max_member_uncompressed_bytes=512), "ARCHIVE_MEMBER_SIZE_LIMIT"),
    ((("large.bin", "x" * 2000),), ExtractionLimits(max_compression_ratio=2), "ARCHIVE_COMPRESSION_RATIO_LIMIT"),
])
def test_mislabeled_package_keeps_archive_validation(extras, limits, code) -> None:
    def leaf(_name, _content):
        raise AssertionError("unsafe archive reached HWPX leaf")
    with pytest.raises(DocumentExtractionError, match=code):
        extract_document_content("SYN.hwp", package(extras=extras),
                                 leaf_extractors={".hwpx": leaf}, limits=limits)


def test_exact_mimetype_does_not_hide_missing_sections_or_embedded_documents() -> None:
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("mimetype", b"application/hwp+zip")
    missing = pps.extract_pps_document_content("SYN.hwp", empty.getvalue())
    assert not missing.complete and missing.warnings == ("HWPX_SECTION_MISSING",)
    embedded = pps.extract_pps_document_content(
        "SYN.hwp", package(extras=(("BinData/hidden.pdf", b"%PDF-SYN"),))
    )
    assert not embedded.complete
    assert embedded.warnings == ("HWPX_EMBEDDED_ATTACHMENT_NOT_EXTRACTED",)


def make_case(file_name="SYN.hwp"):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        notice = Notice(notice_key="PPS-SYN-FORMAT", bid_notice_no="R26BK00000001",
                        revision_no="000", title="합성 첨부 용역", agency="합성 기관",
                        deadline=datetime(2027, 1, 1, tzinfo=timezone.utc), status="OPEN")
        session.add(notice)
        session.flush()
        pps.persist_pps_metadata_version(session, notice, raw_item={
            "bidNtceNo": "R26BK00000001", "bidNtceOrd": "000",
            "ntceSpecFileNm1": file_name, "ntceSpecDocUrl1": G2B_DOWNLOAD,
        }, search_keywords=["합성"], dry_run=False)
        session.commit()
        notice_id = notice.id
    return engine, factory, notice_id


def retry_ids(factory, notice_id):
    with factory() as session:
        return pps.current_retryable_review_version_ids(list(session.scalars(
            select(NoticeVersion).where(NoticeVersion.notice_id == notice_id)
        )))


def run(factory, notice_id, content, *, frozen=frozenset(), requests=None, client=_CountingExtractionClient):
    def request(_request):
        if requests is not None:
            requests.append(1)
        if isinstance(content, Exception):
            raise content
        return httpx.Response(200, headers={"Content-Type": "application/octet-stream"}, content=content)
    with factory() as session:
        return pps.enrich_notice_from_pps(session, notice_id=notice_id,
            openai_api_key="SYN-KEY", openai_model="SYN-MODEL",
            transport=httpx.MockTransport(request), openai_client_factory=client,
            retry_reviewed_version_ids=frozen)


def seed_legacy_failure(factory, notice_id):
    with factory() as session:
        metadata = session.scalar(select(NoticeVersion).where(NoticeVersion.notice_id == notice_id))
        manifest = metadata.source_payload["attachment_manifest"]
        attachment = manifest[0]
        manifest_sha = pps._digest(attachment)
        digest = pps._digest({"manifest": manifest_sha, "error": "HWP_CONTAINER_INVALID"})
        version = pps._persist_extraction_version(session, notice_id=notice_id,
            attachment=attachment, manifest_sha256=manifest_sha,
            current_manifest_sha256=pps._digest(manifest), document_sha256=digest,
            outcome=None, error_code="HWP_CONTAINER_INVALID", processing_audit={
                "processing_version": pps.PPS_PROCESSING_VERSION,
                "source_read_complete": False, "analysis_input_complete": False,
                "source_characters": 0, "analysis_input_characters": 0,
                "members_discovered": 0, "members_processed": 0,
                "warnings": ["HWP_CONTAINER_INVALID"], "member_issues": [],
            })
        session.commit()
        return version.id, digest


def test_frozen_old_failure_retries_once_and_success_preserves_old_audit() -> None:
    engine, factory, notice_id = make_case()
    old_id, old_digest = seed_legacy_failure(factory, notice_id)
    frozen = retry_ids(factory, notice_id)
    assert frozen == frozenset({old_id})
    requests = []
    _CountingExtractionClient.calls = 0
    cached = run(factory, notice_id, package(), requests=requests)
    assert cached.version_id == old_id and requests == []
    recovered = run(factory, notice_id, package(), frozen=frozen, requests=requests)
    continued = run(factory, notice_id, package(), frozen=frozen, requests=requests)
    assert recovered.status == "COMPLETED" and continued.status == "REUSED"
    assert continued.version_id == recovered.version_id != old_id
    assert len(requests) == _CountingExtractionClient.calls == 1
    with factory() as session:
        prior = session.get(NoticeVersion, old_id)
        current = session.get(NoticeVersion, recovered.version_id)
        assert prior.file_sha256 == prior.source_payload["document_sha256"] == old_digest
        assert "download_complete" not in prior.source_payload["document_processing"]
        assert current.file_sha256 == hashlib.sha256(package()).hexdigest()
        assert current.source_payload["source_label"] == "SYN.hwp"
        assert current.source_payload["processing_version"] == pps.PPS_PROCESSING_VERSION
        assert pps.has_current_accepted_pps_extraction(session, notice_id)
    engine.dispose()


def test_failed_retry_gets_actual_digest_then_frozen_continuation_reuses_it() -> None:
    engine, factory, notice_id = make_case()
    old_id, old_digest = seed_legacy_failure(factory, notice_id)
    frozen = retry_ids(factory, notice_id)
    requests = []
    _CountingExtractionClient.calls = 0
    content = b"SYN-invalid-container-one"
    current = run(factory, notice_id, content, frozen=frozen, requests=requests)
    repeated = run(factory, notice_id, content, frozen=frozen, requests=requests)
    assert current.version_id != old_id and current.version_id == repeated.version_id
    assert current.openai_calls == repeated.openai_calls == _CountingExtractionClient.calls == 0
    assert len(requests) == 1 and current.downloaded_bytes == len(content)
    with factory() as session:
        row = session.get(NoticeVersion, current.version_id)
        digest = hashlib.sha256(content).hexdigest()
        assert row.file_sha256 == row.source_payload["document_sha256"] == digest != old_digest
        audit = row.source_payload["document_processing"]
        assert audit["download_complete"] is True
        assert audit["downloaded_bytes"] == len(content)
        assert audit["document_digest_basis"] == "DOWNLOADED_BYTES"
        assert row.document_complete is False and row.extraction_status == "REVIEW"
    changed = b"SYN-invalid-container-two"
    fresh = run(factory, notice_id, changed, frozen=retry_ids(factory, notice_id))
    with factory() as session:
        row = session.get(NoticeVersion, fresh.version_id)
        assert row.file_sha256 == hashlib.sha256(changed).hexdigest() != digest
    engine.dispose()


def test_failed_download_retains_explicit_non_source_marker_and_zero_calls() -> None:
    engine, factory, notice_id = make_case()
    _CountingExtractionClient.calls = 0
    result = run(factory, notice_id, httpx.ConnectError("SYN-no-network"))
    assert result.status == "REVIEW" and result.openai_calls == 0
    assert result.downloaded_bytes == _CountingExtractionClient.calls == 0
    with factory() as session:
        row = session.get(NoticeVersion, result.version_id)
        audit = row.source_payload["document_processing"]
        assert audit["download_complete"] is False and audit["downloaded_bytes"] == 0
        assert audit["document_digest_basis"] == "FAILED_DOWNLOAD_MARKER"
        assert row.file_sha256 == pps._digest({
            "manifest": row.source_payload["manifest_sha256"], "error": "ATTACHMENT_NETWORK_ERROR",
        })
    engine.dispose()


@pytest.mark.parametrize("extension,expected_code", [
    ("pdf", "PDF_EXTRACT_FAILED"), ("hwpx", "HWPX_EXTRACT_FAILED"),
])
def test_empty_text_is_file_failure_without_provider_call(extension, expected_code) -> None:
    content = io.BytesIO()
    if extension == "pdf":
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(content)
    else:
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr("Contents/section0.xml", "<s><p/></s>")
    engine, factory, notice_id = make_case(f"SYN.{extension}")
    _CountingExtractionClient.calls = 0
    result = run(factory, notice_id, content.getvalue())
    assert result.status == "REVIEW"
    assert result.openai_calls == _CountingExtractionClient.calls == 0
    with factory() as session:
        versions = list(session.scalars(select(NoticeVersion).where(NoticeVersion.notice_id == notice_id)))
        failed = session.get(NoticeVersion, result.version_id)
        assert failed.source_payload["error_code"] == "DOCUMENT_TEXT_EMPTY"
        assert failed.file_sha256 == hashlib.sha256(content.getvalue()).hexdigest()
        assert pps.public_attachment_analysis_statuses(versions)[0]["reason_code"] == expected_code
        assert pps.public_analysis_reason(versions).reason_code == expected_code
    engine.dispose()


@pytest.mark.parametrize("extension", ["hwp", "xlsx", "xlsm", "xls", "docx", "pptx", "html", "htm", "zip"])
def test_other_supported_empty_text_labels_remain_document_failures(extension) -> None:
    assert pps._public_attachment_failure_reason_code(
        {"file_name": f"SYN.{extension}"}, "DOCUMENT_TEXT_EMPTY"
    ) == "DOCUMENT_EXTRACT_FAILED"


def test_genuine_model_review_retains_model_failure_label() -> None:
    engine, factory, notice_id = make_case()
    _RetryableReviewClient.calls = 0
    result = run(factory, notice_id, package(), client=_RetryableReviewClient)
    assert result.status == "REVIEW"
    assert result.openai_calls == _RetryableReviewClient.calls == 1
    with factory() as session:
        versions = list(session.scalars(select(NoticeVersion).where(NoticeVersion.notice_id == notice_id)))
        failed = session.get(NoticeVersion, result.version_id)
        assert failed.source_payload["error_code"] == "INCOMPLETE_RESPONSE"
        assert pps.public_attachment_analysis_statuses(versions)[0]["reason_code"] == "MODEL_RESPONSE_INCOMPLETE"
        assert pps.public_analysis_reason(versions).reason_code == "OPENAI_REVIEW"
    engine.dispose()
