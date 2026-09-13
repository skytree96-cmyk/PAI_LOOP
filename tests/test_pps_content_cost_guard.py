"""Synthetic, paid-disabled regressions for the pre-provider body-text gate."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from pai_loop.database import Base, build_engine, build_session_factory
from pai_loop.integrations.openai_extraction import ExtractionOutcome
from pai_loop.models import Notice, NoticeVersion
from pai_loop.pps_enrichment import (
    PpsEnrichmentError,
    _extract_pdf_text,
    enrich_notice_from_pps,
    extract_document_text,
    extract_pps_document_content,
    persist_pps_metadata_version,
)


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return output.getvalue()


def _fake_pdf_reader(source: io.BytesIO, **_kwargs: object) -> SimpleNamespace:
    body = source.read().decode("utf-8")
    return SimpleNamespace(
        is_encrypted=False,
        trailer={"/Root": {}},
        attachment_list=[],
        pages=[SimpleNamespace(extract_text=lambda: body)],
    )


@pytest.mark.parametrize("body", ["", "SYN fragment text!!", "SYN submit evidence."])
def test_pdf_body_minimum_excludes_page_labels(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    monkeypatch.setattr("pypdf.PdfReader", _fake_pdf_reader)
    result = extract_pps_document_content("SYN-short.pdf", body.encode() or b" ")

    assert result.analysis_content_characters == len(body)
    if len(body) < 20:
        with pytest.raises(PpsEnrichmentError, match="DOCUMENT_TEXT_EMPTY"):
            extract_document_text("SYN-short.pdf", body.encode() or b" ")
    else:
        assert extract_document_text("SYN-short.pdf", body.encode()) == "[PAGE 1]\n" + body
    assert _extract_pdf_text(body.encode()) == ("[PAGE 1]\n" + body if body else "")


@pytest.mark.parametrize(
    ("body", "expected_calls"),
    [("", 0), ("SYN fragment text!!", 0), ("SYN submit evidence.", 1)],
)
def test_nested_zip_gates_provider_on_body_and_preserves_review_audit(
    monkeypatch: pytest.MonkeyPatch, body: str, expected_calls: int
) -> None:
    monkeypatch.setattr("pypdf.PdfReader", _fake_pdf_reader)
    content = _zip({
        "SYN-inner.zip": _zip({"SYN-page.pdf": body.encode() or b" "}),
        "SYN-empty.pdf": b" ",
        "SYN-unsupported.bin": b"SYN",
    })
    extracted = extract_pps_document_content("SYN-bundle.zip", content)
    assert extracted.analysis_content_characters == len(body)
    assert extracted.members_discovered == 3
    assert extracted.members_processed == int(bool(body))
    assert extracted.complete is False
    assert len(extracted.member_issues) == 3 - int(bool(body))
    if body:
        assert len(extracted.text) > 20

    class FakeClient:
        calls = 0
        constructed = 0

        def __init__(self, **_kwargs: object) -> None:
            type(self).constructed += 1

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def extract(self, *, document_text: str, **_kwargs: object) -> ExtractionOutcome:
            type(self).calls += 1
            assert body in document_text
            assert "[DOCUMENT SYN-page.pdf]" in document_text
            return ExtractionOutcome(
                status="REVIEW", message="SYN review", api_calls=1,
                error_code="SYN_REVIEW",
            )

    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as session:
            notice = Notice(
                notice_key="SYN-CONTENT-COST-GUARD", bid_notice_no="SYN-CONTENT",
                revision_no="000", title="SYN notice", agency="SYN agency",
                deadline=datetime(2027, 1, 1, tzinfo=timezone.utc), status="OPEN",
            )
            session.add(notice)
            session.flush()
            persist_pps_metadata_version(
                session, notice,
                raw_item={
                    "bidNtceNo": "SYN-CONTENT", "bidNtceOrd": "000",
                    "ntceSpecFileNm1": "SYN-bundle.zip",
                    "ntceSpecDocUrl1": (
                        "https://www.g2b.go.kr/pn/pnp/pnpe/UntyAtchFile/downloadFile.do"
                        "?bidPbancNo=SYN-CONTENT&bidPbancOrd=000&fileSeq=1"
                    ),
                },
                search_keywords=["SYN"], dry_run=False,
            )
            session.commit()
            notice_id = notice.id
        transport = httpx.MockTransport(lambda _request: httpx.Response(
            200, content=content, headers={"Content-Type": "application/zip"},
        ))
        with factory() as session:
            result = enrich_notice_from_pps(
                session, notice_id=notice_id,
                openai_api_key="SYN-disabled-provider", openai_model="SYN-model",
                openai_client_factory=FakeClient, transport=transport,
            )
        assert result.status == "REVIEW"
        assert result.openai_calls == FakeClient.calls == FakeClient.constructed == expected_calls
        with factory() as session:
            version = session.get(NoticeVersion, result.version_id)
            assert version is not None
            assert version.document_complete is False
            audit = version.source_payload["document_processing"]
            assert audit["source_content_characters"] == len(body)
            assert audit["source_characters"] == len(extracted.text)
            assert audit["members_discovered"] == 3
            assert audit["members_processed"] == int(bool(body))
            assert len(audit["member_issues"]) == 3 - int(bool(body))
            assert audit["analysis_input_complete"] is bool(expected_calls)
            assert {issue["reason"] for issue in audit["member_issues"]} == {
                "DOCUMENT_TEXT_EMPTY", "UNSUPPORTED_ARCHIVE_MEMBER_TYPE",
            }
    finally:
        engine.dispose()
