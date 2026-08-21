from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from pai_loop.pps_enrichment import (
    PpsEnrichmentError,
    build_notice_metadata,
    department_keyword_coverage_count,
    download_public_attachment,
    extract_document_text,
    resolve_ingestion_keywords,
    safe_public_live_extraction,
    select_preferred_attachments,
)


G2B_DOWNLOAD = (
    "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
    "?bidPbancNo=R26BK00000001&fileSeq=1"
)


def _attachment() -> dict[str, object]:
    return {
        "attachment_id": "PPS-ATT-0123456789abcdef01234567",
        "file_name": "제안요청서.pdf",
        "media_type": "application/pdf",
        "url": G2B_DOWNLOAD,
        "slot": 1,
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(503), "ATTACHMENT_HTTP_503"),
        (
            httpx.Response(
                200,
                headers={"Content-Type": "application/pdf", "Content-Length": "not-a-number"},
                content=b"payload",
            ),
            "INVALID_CONTENT_LENGTH",
        ),
        (
            httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"payload"),
            "UNEXPECTED_ATTACHMENT_CONTENT_TYPE",
        ),
        (httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b""), "ATTACHMENT_EMPTY"),
    ],
)
def test_public_attachment_download_fails_closed_for_provider_anomalies(
    response: httpx.Response,
    expected: str,
) -> None:
    transport = httpx.MockTransport(lambda _request: response)

    with pytest.raises(PpsEnrichmentError, match=expected):
        download_public_attachment(_attachment(), transport=transport)


def test_public_attachment_download_bounds_redirects_and_network_errors() -> None:
    redirect = httpx.MockTransport(
        lambda _request: httpx.Response(302, headers={"Location": G2B_DOWNLOAD})
    )
    with pytest.raises(PpsEnrichmentError, match="ATTACHMENT_REDIRECT_LIMIT"):
        download_public_attachment(_attachment(), transport=redirect, max_redirects=0)

    def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic disconnect", request=request)

    with pytest.raises(PpsEnrichmentError, match="ATTACHMENT_NETWORK_ERROR"):
        download_public_attachment(
            _attachment(),
            transport=httpx.MockTransport(disconnect),
        )


def test_document_extraction_rejects_malformed_or_unusable_archives() -> None:
    with pytest.raises(PpsEnrichmentError, match="HWPX_INVALID_ARCHIVE"):
        extract_document_text("제안요청서.hwpx", b"not-a-zip")

    no_sections = io.BytesIO()
    with zipfile.ZipFile(no_sections, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")
    with pytest.raises(PpsEnrichmentError, match="HWPX_SECTION_MISSING"):
        extract_document_text("제안요청서.hwpx", no_sections.getvalue())

    invalid_xml = io.BytesIO()
    with zipfile.ZipFile(invalid_xml, "w") as archive:
        archive.writestr("Contents/section0.xml", "<section>")
    with pytest.raises(PpsEnrichmentError, match="HWPX_XML_INVALID"):
        extract_document_text("제안요청서.hwpx", invalid_xml.getvalue())

    short_text = io.BytesIO()
    with zipfile.ZipFile(short_text, "w") as archive:
        archive.writestr("Contents/section0.xml", "<section>짧음</section>")
    with pytest.raises(PpsEnrichmentError, match="DOCUMENT_TEXT_EMPTY"):
        extract_document_text("제안요청서.hwpx", short_text.getvalue())

    with pytest.raises(PpsEnrichmentError, match="UNSUPPORTED_DOCUMENT_TYPE"):
        extract_document_text("제안요청서.txt", b"synthetic")
    with pytest.raises(PpsEnrichmentError, match="UNSAFE_ATTACHMENT_FILENAME"):
        extract_document_text("../제안요청서.pdf", b"synthetic")


def test_keyword_and_metadata_boundaries_remain_bounded_and_public() -> None:
    metadata = build_notice_metadata(
        {
            "bidPrceEvlRt": "20.5",
            "techAbltEvlRt": "79,5",
            "sucsfbidLwltRate": "not-numeric",
            "bidMethdNm": " 전자   입찰 ",
        }
    )
    assert metadata == {
        "price_evaluation_rate": 20.5,
        "technical_evaluation_rate": 795.0,
        "bid_method": "전자 입찰",
    }

    direct_bid_metadata = build_notice_metadata(
        {
            "bidClseDt": "",
            "opengDt": "2026-08-24 18:00:00",
            "bidMethdNm": "직찰",
            "ofclNm": "저장 금지 담당자",
            "ofclTelNo": "TEST-PRIVATE-TEL-0000",
        }
    )
    assert direct_bid_metadata == {
        "bid_method": "직찰",
        "deadline_basis": "OPENING_FALLBACK",
        "opening_at": "2026-08-24T18:00:00+09:00",
    }

    with pytest.raises(ValueError, match="60자"):
        resolve_ingestion_keywords(
            keyword="가" * 61,
            keywords=[],
            use_profile_keywords=False,
            profile_department_ids=[],
        )
    with pytest.raises(ValueError, match="최대 2개"):
        resolve_ingestion_keywords(
            keyword="교육",
            keywords=["컨설팅", "역량개발"],
            use_profile_keywords=False,
            profile_department_ids=[],
            limit=2,
        )

    profiled, truncated = resolve_ingestion_keywords(
        keyword=None,
        keywords=[],
        use_profile_keywords=True,
        profile_department_ids=["organization"],
        limit=3,
    )
    assert profiled == ["교육", "컨설팅", "연수"]
    assert len(profiled) == 3
    assert truncated is True
    assert department_keyword_coverage_count(
        ["K-12"],
        profile_department_ids=["future-ai-education", "finance-support"],
    ) == 1


def test_attachment_selection_and_public_projection_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="between 1 and 10"):
        select_preferred_attachments([], limit=0)
    with pytest.raises(ValueError, match="between 1 and 10"):
        select_preferred_attachments([], limit=11)

    selected, warnings = select_preferred_attachments(
        [{**_attachment(), "attachment_id": "invalid"}],
        limit=1,
    )
    assert selected == []
    assert warnings == ["ATTACHMENT_MANIFEST_EMPTY", "INVALID_ATTACHMENT_MANIFEST"]

    generic = {**_attachment(), "file_name": "자료.pdf"}
    selected, warnings = select_preferred_attachments([generic], limit=1)
    assert selected == [generic]
    assert warnings == []

    assert safe_public_live_extraction("not-a-mapping") is None
    assert safe_public_live_extraction(
        {
            "kind": "OPENAI_REQUIREMENT_EXTRACTION",
            "source_kind": "PPS_PUBLIC_ATTACHMENT",
            "status": "ACCEPTED",
            "prompt_version": "pai-loop-extraction-0.2.1",
            "schema_version": "pai-loop-requirements-0.1.0",
            "attachment_id": _attachment()["attachment_id"],
            "source_label": "제안요청서.pdf",
            "document_sha256": "a" * 64,
            "result": {"malformed": True},
        }
    ) is None
