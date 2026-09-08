from __future__ import annotations

import io
import zipfile

import pytest

from pai_loop.pps_enrichment import PpsEnrichmentError, _extract_hwpx_text, extract_document_text


def package(body: str, **members: bytes) -> bytes:
    section = (
        "<sec xmlns:hp='urn:synthetic:paragraph'>" + body + "</sec>"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", section)
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_nested_table_text_occurs_once_in_document_order_and_repeated_cells_survive():
    body = (
        "<hp:p><hp:run><hp:t>합성 표 앞 문구</hp:t></hp:run><hp:tbl><hp:tr>"
        "<hp:tc><hp:p><hp:run><hp:t>동일 셀</hp:t></hp:run></hp:p></hp:tc>"
        "<hp:tc><hp:p><hp:run><hp:t>동일 셀</hp:t></hp:run></hp:p></hp:tc>"
        "</hp:tr></hp:tbl><hp:run><hp:t>합성 표 뒤 문구</hp:t></hp:run></hp:p>"
    )
    assert extract_document_text("SYN.hwpx", package(body)) == "합성 표 앞 문구\n동일 셀\n동일 셀\n합성 표 뒤 문구"


@pytest.mark.parametrize("body,expected", [
    ("<p><t>앞</t><tbl><p><t>안 앞</t><tbl><p><t>깊은 셀</t></p></tbl>"
     "<t>안 뒤</t></p></tbl><t>뒤</t></p>", "앞\n안 앞\n깊은 셀\n안 뒤\n뒤"),
    ("<p>직접 앞<tbl><p><t>셀</t></p></tbl>직접 뒤</p>", "셀"),
    ("<p>직접 앞<tbl><p><t>셀</t></p></tbl><run><t>표 뒤</t></run>끝</p>",
     "셀\n표 뒤"),
    ("<p>하나<b> 둘</b> 셋<empty/> 넷</p>", "하나 둘 셋 넷"),
    ("<p>하나<b> </b>둘</p>", "하나 둘"),
    ("<p><run><t>입찰참가</t></run>\n  <run><t>자격</t></run></p>", "입찰참가자격"),
    ("<p><t>A<inline>B</inline>C<t>D</t>E</t></p>", "ABCDE"),
    ("<p><t>ᄀ</t><t>ᅡ</t><t>\t  나\n다 </t></p>", "가 나 다"),
    ("<p/><p> \t </p><p><t/></p><p><t>본문</t></p><p/>", "본문"),
    ("<p><t>앞</t><tbl><p/><p> </p><p><t> </t></p></tbl><t>뒤</t></p>", "앞뒤"),
    ("<p>앞<tbl><p> </p></tbl> 뒤</p>", "앞 뒤"),
    ("외부<p><t>본문</t></p>외부", "본문"),
    ("<p><t>같은 문단</t></p><p><t>같은 문단</t></p>", "같은 문단\n같은 문단"),
])
def test_paragraph_boundaries_run_text_and_legacy_text_tails(body, expected):
    assert _extract_hwpx_text(package(body)) == expected


@pytest.mark.parametrize("metadata", [
    "<shapeComment>SYN_SHAPE_METADATA</shapeComment>",
    "<parameters><stringParam>SYN_STRING_METADATA</stringParam></parameters>",
    "<parameters><integerParam>17</integerParam></parameters>",
])
@pytest.mark.parametrize("nested", [False, True])
def test_metadata_outside_t_is_excluded_for_own_and_descendant_t(metadata, nested):
    visible = "<run><t>합성 본문</t></run>"
    if nested:
        visible = "<tbl><p>" + visible + "</p></tbl>"
    body = "<p>" + metadata + visible + metadata + "</p>"
    assert _extract_hwpx_text(package(body)) == "합성 본문"


def test_deep_paragraph_tree_is_iterative_and_each_owner_is_emitted_once():
    depth = 1500
    body = "<p><t>앞</t>" * depth + "<p><t>가운데</t></p>" + "<t>뒤</t></p>" * depth
    lines = _extract_hwpx_text(package(body)).splitlines()
    assert lines == ["앞"] * depth + ["가운데"] + ["뒤"] * depth


def test_many_equal_cells_are_not_string_deduplicated():
    body = "<p><tbl>" + "<p><t>반복</t></p>" * 300 + "</tbl></p>"
    assert _extract_hwpx_text(package(body)).splitlines() == ["반복"] * 300


def test_character_limit_counts_retained_text_once_but_still_rejects_overflow(monkeypatch):
    monkeypatch.setattr("pai_loop.pps_enrichment.MAX_EXTRACTED_DOCUMENT_CHARS", 5)
    assert _extract_hwpx_text(package("<p><tbl><p><t>abcd</t></p></tbl></p>")) == "abcd"
    with pytest.raises(PpsEnrichmentError, match="DOCUMENT_TEXT_TOO_LARGE"):
        _extract_hwpx_text(package("<p><t>abcd</t></p><p><t>x</t></p>"))


@pytest.mark.parametrize("setting,value,error", [
    ("MAX_HWPX_ENTRIES", 1, "HWPX_ENTRY_LIMIT"),
    ("MAX_HWPX_UNCOMPRESSED_BYTES", 1, "HWPX_UNCOMPRESSED_LIMIT"),
])
def test_archive_limits_still_precede_paragraph_processing(monkeypatch, setting, value, error):
    monkeypatch.setattr("pai_loop.pps_enrichment." + setting, value)
    with pytest.raises(PpsEnrichmentError, match=error):
        _extract_hwpx_text(package("<p><t>SYN</t></p>"))


@pytest.mark.parametrize("members,error", [
    ({"../unsafe.xml": b"SYN"}, "HWPX_INVALID_ENTRY_PATH"),
    ({"dir/../unsafe.xml": b"SYN"}, "HWPX_INVALID_ENTRY_PATH"),
    ({"/absolute.xml": b"SYN"}, "HWPX_INVALID_ENTRY_PATH"),
    ({"..\\unsafe.xml": b"SYN"}, "HWPX_INVALID_ENTRY_PATH"),
    ({"Scripts/default.js": b"SYN"}, "HWPX_ACTIVE_CONTENT_NOT_EXTRACTED"),
    ({"BinData/embedded.pdf": b"%PDF-SYN"}, "HWPX_EMBEDDED_ATTACHMENT_NOT_EXTRACTED"),
    ({"Contents/section0.xml.rels": b"<Relationships><Relationship TargetMode='External'/></Relationships>"},
     "HWPX_EXTERNAL_RELATIONSHIP"),
])
def test_package_security_rejections_are_unchanged(members, error):
    with pytest.raises(PpsEnrichmentError, match=error):
        _extract_hwpx_text(package("<p><t>SYN</t></p>", **members))


def test_invalid_xml_still_fails_closed():
    with pytest.raises(PpsEnrichmentError, match="HWPX_XML_INVALID"):
        _extract_hwpx_text(package("<p><t>SYN</p>"))
