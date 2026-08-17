from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pai_loop.pps_enrichment import (
    PPS_ATTACHMENT_SOURCE,
    PPS_METADATA_KIND,
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
    public_analysis_reason,
    resolve_ingestion_keywords,
    safe_public_live_extraction,
    select_preferred_attachments,
)
from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.integrations.openai_extraction import (
    PROMPT_VERSION,
    EvidenceAnchor,
    ExtractedRequirement,
    ExtractionOutcome,
    ExtractionPayload,
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
    assert len(manifest) == 1
    assert manifest[0]["file_name"] == "입찰공고문.pdf"
    assert manifest[0]["media_type"] == "application/pdf"
    assert manifest[0]["attachment_id"].startswith("PPS-ATT-")

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


def test_attachment_selection_prefers_paired_pdf_and_fails_closed_for_hwp_only() -> None:
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
    assert selected == []
    assert warnings == ["HWP_ONLY_UNSUPPORTED_R07"]


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

    with pytest.raises(PpsEnrichmentError, match="HWP_BINARY_UNSUPPORTED"):
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
            "attachment_manifest": manifest,
        },
    )
    if error_code is None and status == "REVIEW":
        return [metadata]
    attachment = manifest[0]
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
            "prompt_version": PROMPT_VERSION,
            "status": status,
            "error_code": error_code,
        },
    )
    return [metadata, attempt]


@pytest.mark.parametrize(
    ("extension", "error_code", "status", "reason_code"),
    [
        (".pdf", None, "REVIEW", "NOT_SELECTED"),
        (".hwp", None, "REVIEW", "HWP_ONLY_UNSUPPORTED"),
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
    assert profiled[:2] == ["교육", "컨설팅"]
    assert len(profiled) == 26
    assert len(set(profiled)) == 26
    assert truncated is False
    assert department_keyword_coverage_count(
        profiled,
        profile_department_ids=[],
    ) == 24


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
        "prompt_version": "pai-loop-extraction-0.2.1",
        "schema_version": "pai-loop-requirements-0.1.0",
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
        "prompt_version": "pai-loop-extraction-0.2.1",
        "schema_version": "pai-loop-requirements-0.1.0",
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


def test_identical_attachment_and_prompt_reuses_accepted_extraction_without_openai() -> None:
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
    assert first.openai_calls == 1
    assert second.status == "REUSED"
    assert second.openai_calls == 0
    assert first.version_id == second.version_id
    assert _CountingExtractionClient.calls == 1
    with factory() as session:
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
