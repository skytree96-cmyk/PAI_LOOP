"""Parser recoveries measured on real unscored RFPs (2026-10-04)."""
from __future__ import annotations

import io
import struct
import zipfile
import zlib

import pytest

from pai_loop import document_extraction as de
from pai_loop import pps_enrichment as pe
from pai_loop.pps_enrichment import PpsEnrichmentError, _extract_hwpx_text

SECTION = (
    '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
    "<hp:p><hp:run><hp:t>정량평가 배점표 수행실적 10점</hp:t></hp:run></hp:p></hs:sec>"
)
HEADER_SCRIPT = (
    "var Documents = XHwpDocuments;\r\n"
    "var Document = Documents.Active_XHwpDocument;\r\n"
)
SOURCE_SCRIPT = (
    "function OnDocument_New()\r\n{\r\n\t//todo : \r\n}\r\n"
    "function OnCheckBox1_Click()\r\n{\r\n\t//todo : \r\n}\r\n"
)


def _hwpx(**members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/section0.xml", SECTION)
        for name, value in members.items():
            archive.writestr(name, value)
    return buffer.getvalue()


@pytest.mark.parametrize("suffix", ["", ".js"])
def test_hancom_default_script_template_is_not_active_content(suffix: str) -> None:
    content = _hwpx(**{
        f"Scripts/headerScripts{suffix}": HEADER_SCRIPT.encode("utf-16-le"),
        f"Scripts/sourceScripts{suffix}": SOURCE_SCRIPT.encode("utf-16-le"),
    })
    assert "수행실적 10점" in _extract_hwpx_text(content)


@pytest.mark.parametrize("members", [
    {"Scripts/sourceScripts.js": "function OnDocument_New()\r\n{\r\n\tDocument.Run('x');\r\n}".encode("utf-16-le")},
    {"Scripts/default.js": HEADER_SCRIPT.encode("utf-16-le")},
    {"Scripts/sourceScripts": b"alert(1)"},
])
def test_script_with_real_code_or_unknown_name_stays_rejected(members: dict[str, bytes]) -> None:
    with pytest.raises(PpsEnrichmentError, match="HWPX_ACTIVE_CONTENT_NOT_EXTRACTED"):
        _extract_hwpx_text(_hwpx(**members))


def test_large_bmp_does_not_count_toward_uncompressed_limit(monkeypatch) -> None:
    monkeypatch.setattr("pai_loop.pps_enrichment.MAX_HWPX_UNCOMPRESSED_BYTES", 4096)
    content = _hwpx(**{"BinData/image1.BMP": b"BM" + b"\x00" * 20_000})
    assert "수행실적" in _extract_hwpx_text(content)


HWPML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE HWPML [\n\t<!ENTITY nbsp\t"&#160;">\n]>\n'
    '<HWPML Version="2.1"><HEAD SecCnt="1"/><BODY><SECTION Id="0">'
    "<P><TEXT><CHAR>제1조(목적)&nbsp;이 조건은</CHAR></TEXT></P>"
    "<P><TEXT><CHAR>경영상태 </CHAR><CHAR>평가</CHAR></TEXT></P>"
    "</SECTION></BODY></HWPML>"
)


def test_flat_hwpml_saved_as_hwpx_is_read() -> None:
    text = _extract_hwpx_text(HWPML.encode("utf-8"))
    assert text.splitlines() == ["제1조(목적) 이 조건은", "경영상태 평가"]


@pytest.mark.parametrize("doctype", [
    '<!DOCTYPE HWPML [<!ENTITY x SYSTEM "file:///etc/passwd">]>',
    '<!DOCTYPE HWPML [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>',
    '<!DOCTYPE HWPML SYSTEM "http://example.invalid/x.dtd">',
])
def test_hwpml_with_non_character_entities_fails_closed(doctype: str) -> None:
    content = HWPML.replace('<!DOCTYPE HWPML [\n\t<!ENTITY nbsp\t"&#160;">\n]>', doctype)
    with pytest.raises(PpsEnrichmentError, match="HWPX_INVALID_ARCHIVE"):
        _extract_hwpx_text(content.encode("utf-8"))


def test_lone_surrogate_in_hwp_paragraph_is_marked_not_fatal() -> None:
    payload = "정량".encode("utf-16le") + struct.pack("<H", 0xDC00) + "평가".encode("utf-16le")
    assert de._extract_hwp_para_text(payload) == "정량�평가"


def test_valid_surrogate_pair_is_kept() -> None:
    payload = "점수 😀".encode("utf-16le")
    assert de._extract_hwp_para_text(payload) == "점수 😀"


def _deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def test_oversized_metafile_bindata_is_classified_as_image() -> None:
    wmf = b"\x01\x00\x09\x00\x00\x03" + b"\x00" * 5000
    decoded, kind = de._decode_hwp_bindata(_deflate(wmf), compressed=True, maximum=1024)
    assert kind == "IMAGE"


def test_oversized_non_image_bindata_still_fails_closed() -> None:
    blob = b"PK\x03\x04" + b"\x00" * 5000
    with pytest.raises(de.DocumentExtractionError, match="HWP_SECTION_SIZE_LIMIT"):
        de._decode_hwp_bindata(_deflate(blob), compressed=True, maximum=1024)


def test_download_limit_covers_measured_public_rfps() -> None:
    # Largest unscored RFP measured on 2026-10-03 was 15.9 MB.
    assert pe.DEFAULT_MAX_DOWNLOAD_BYTES >= 16 * 1024 * 1024
