from __future__ import annotations

import io
import struct
import sys
import types
import zipfile
import zlib

import pytest

from pai_loop.document_extraction import (
    BINARY_READER_DEPENDENCIES,
    DocumentExtractionError,
    ExtractionLimits,
    extract_document_content,
)


def _archive(
    entries: dict[str, bytes | str],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, value in entries.items():
            archive.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)
    return buffer.getvalue()


def _content_types() -> str:
    return '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


def _minimal_docx(text: str = "정량평가 수행실적 배점표") -> bytes:
    return _archive(
        {
            "[Content_Types].xml": _content_types(),
            "word/document.xml": (
                '<w:document xmlns:w="urn:w"><w:body><w:p>'
                f"<w:r><w:t>{text}</w:t></w:r>"
                "</w:p></w:body></w:document>"
            ),
        }
    )


def _minimal_xlsx(*, macro: bool = False, external_link: bool = False) -> bytes:
    entries: dict[str, bytes | str] = {
        "[Content_Types].xml": _content_types(),
        "xl/workbook.xml": (
            '<workbook xmlns="urn:x" xmlns:r="urn:r"><sheets>'
            '<sheet name="정량평가" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/sharedStrings.xml": (
            '<sst xmlns="urn:x"><si><t>수행실적</t></si>'
            "<si><r><t>배</t></r><r><t>점</t></r></si></sst>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet xmlns="urn:x"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="inlineStr"><is><t>최근 3년</t></is></c></row>'
            '<row r="2"><c r="A2"><f>SUM(C2:D2)</f><v>30</v></c>'
            '<c r="B2" t="s"><v>1</v></c></row>'
            "</sheetData></worksheet>"
        ),
    }
    if macro:
        entries["xl/vbaProject.bin"] = b"never execute"
    if external_link:
        entries["xl/externalLinks/externalLink1.xml"] = "<externalLink/>"
    return _archive(entries)


def _hwp_header(*, flags: int = 1) -> bytes:
    header = bytearray(256)
    header[: len(b"HWP Document File")] = b"HWP Document File"
    header[35] = 5
    struct.pack_into("<I", header, 36, flags)
    return bytes(header)


def test_binary_reader_dependency_contract_is_bsd_only() -> None:
    assert BINARY_READER_DEPENDENCIES == (
        "olefile>=0.47,<1",
        "xlrd>=2.0.1,<3",
    )
    assert all("pyhwp" not in item for item in BINARY_READER_DEPENDENCIES)


def _hwp_record(tag: int, payload: bytes, *, level: int = 0) -> bytes:
    assert len(payload) < 0xFFF
    header = tag | (level << 10) | (len(payload) << 20)
    return struct.pack("<I", header) + payload


def _raw_deflate(value: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(value) + compressor.flush()


def _fake_olefile_module(
    sections: list[bytes],
    *,
    flags: int = 1,
) -> types.SimpleNamespace:
    streams = {"FileHeader": _hwp_header(flags=flags)}
    streams.update(
        {f"BodyText/Section{index}": value for index, value in enumerate(sections)}
    )

    class FakeOle:
        def __init__(self, _source: object, **_kwargs: object) -> None:
            self.closed = False

        def listdir(self, **_kwargs: object) -> list[list[str]]:
            return [name.split("/") for name in streams]

        def openstream(self, path: list[str]) -> io.BytesIO:
            return io.BytesIO(streams["/".join(path)])

        def close(self) -> None:
            self.closed = True

    return types.SimpleNamespace(OleFileIO=FakeOle, DEFECT_INCORRECT=40)


def _with_fake_module(name: str, module: object):
    class Installed:
        def __enter__(self) -> None:
            self.previous = sys.modules.get(name)
            sys.modules[name] = module  # type: ignore[assignment]

        def __exit__(self, *_args: object) -> None:
            if self.previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = self.previous

    return Installed()


def test_docx_extracts_body_table_header_and_footer_text() -> None:
    content = _archive(
        {
            "[Content_Types].xml": _content_types(),
            "word/document.xml": (
                '<w:document xmlns:w="urn:w"><w:body><w:p><w:r>'
                "<w:t>제안요청서</w:t></w:r></w:p><w:tbl><w:tr><w:tc>"
                "<w:p><w:r><w:t>정량평가 20점</w:t></w:r></w:p>"
                "</w:tc></w:tr></w:tbl></w:body></w:document>"
            ),
            "word/header1.xml": (
                '<w:hdr xmlns:w="urn:w"><w:p><w:r><w:t>입찰 참가자격</w:t>'
                "</w:r></w:p></w:hdr>"
            ),
            "word/footer1.xml": (
                '<w:ftr xmlns:w="urn:w"><w:p><w:r><w:t>문의 마감일</w:t>'
                "</w:r></w:p></w:ftr>"
            ),
        }
    )

    result = extract_document_content("제안요청서.docx", content)

    assert result.complete is True
    assert result.members_discovered == result.members_processed == 1
    assert "제안요청서" in result.text
    assert "정량평가 20점" in result.text
    assert "입찰 참가자격" in result.text
    assert "문의 마감일" in result.text


def test_xlsx_preserves_all_cells_formula_and_cached_value() -> None:
    result = extract_document_content("산출내역서.xlsx", _minimal_xlsx())

    assert result.complete is True
    assert "[SHEET 정량평가]" in result.text
    assert "A1=수행실적" in result.text
    assert "B1=최근 3년" in result.text
    assert "=SUM(C2:D2)" in result.text
    assert "30" in result.text
    assert "B2=배점" in result.text


def test_xlsm_extracts_cells_but_macro_is_never_executed_and_is_incomplete() -> None:
    result = extract_document_content("평가표.xlsm", _minimal_xlsx(macro=True))

    assert "수행실적" in result.text
    assert result.complete is False
    assert result.warnings == ("XLSM_MACRO_NOT_EXECUTED",)
    assert result.member_issues[0].reason == "XLSM_MACRO_NOT_EXECUTED"


def test_xlsx_external_link_is_not_fetched_and_is_incomplete() -> None:
    result = extract_document_content(
        "평가표.xlsx",
        _minimal_xlsx(external_link=True),
    )

    assert "수행실적" in result.text
    assert result.complete is False
    assert result.warnings == ("XLSX_EXTERNAL_LINK_NOT_FETCHED",)


def test_pptx_extracts_every_slide_and_notes_part() -> None:
    content = _archive(
        {
            "[Content_Types].xml": _content_types(),
            "ppt/presentation.xml": '<p:presentation xmlns:p="urn:p"/>',
            "ppt/slides/slide1.xml": (
                '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r>'
                "<a:t>사업 수행계획</a:t></a:r></a:p></p:sld>"
            ),
            "ppt/slides/slide2.xml": (
                '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r>'
                "<a:t>정량 배점</a:t></a:r></a:p></p:sld>"
            ),
            "ppt/notesSlides/notesSlide1.xml": (
                '<p:notes xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r>'
                "<a:t>발표자 참고사항</a:t></a:r></a:p></p:notes>"
            ),
        }
    )

    result = extract_document_content("안내문.pptx", content)

    assert result.complete is True
    assert "사업 수행계획" in result.text
    assert "정량 배점" in result.text
    assert "발표자 참고사항" in result.text


def test_html_extracts_visible_korean_text_without_fetching_or_script_content() -> None:
    html = """
    <html><head><style>.x{display:none}</style><script>steal()</script></head>
    <body><h1>입찰공고</h1><p>참가자격 및 정량평가 기준</p>
    <a href="https://invalid.example/private">세부 기준</a></body></html>
    """.encode()

    result = extract_document_content("공고.html", html)

    assert result.complete is True
    assert "입찰공고" in result.text
    assert "참가자격 및 정량평가 기준" in result.text
    assert "세부 기준" in result.text
    assert "display:none" not in result.text
    assert "steal" not in result.text
    assert "invalid.example" not in result.text


def test_generic_zip_keeps_hwpx_text_and_reports_malformed_hwp_members() -> None:
    content = _archive(
        {
            "제안요청서.hwpx": b"one",
            "과업지시서.hwpx": b"two",
            "공고문1.hwp": b"old-one",
            "공고문2.hwp": b"old-two",
        }
    )

    broken_olefile = types.SimpleNamespace(
        OleFileIO=lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("broken")),
        DEFECT_INCORRECT=40,
    )
    with _with_fake_module("olefile", broken_olefile):
        result = extract_document_content(
            "모든서류.zip",
            content,
            leaf_extractors={
                ".hwpx": lambda name, data: f"{name}에서 추출한 정량평가 근거 {len(data)}"
            },
        )

    assert result.members_discovered == 4
    assert result.members_processed == 2
    assert result.complete is False
    assert result.text.count("정량평가 근거") == 2
    assert result.warnings == ("HWP_CONTAINER_INVALID",)
    assert len(result.member_issues) == 2
    assert {issue.reason for issue in result.member_issues} == {"HWP_CONTAINER_INVALID"}


def test_generic_zip_recursively_extracts_builtin_documents() -> None:
    nested = _archive({"평가표.xlsx": _minimal_xlsx()})
    content = _archive({"요청서.docx": _minimal_docx(), "내역.zip": nested})

    result = extract_document_content("묶음.zip", content)

    assert result.complete is True
    assert result.members_discovered == result.members_processed == 2
    assert "정량평가 수행실적 배점표" in result.text
    assert "수행실적" in result.text


def test_hwp5_extracts_every_para_text_and_skips_inline_control_payload() -> None:
    hidden_payload = "악성페이로드X".encode("utf-16le")
    assert len(hidden_payload) == 14
    first = (
        "입찰 참가자격과 수행실적 기준 ".encode("utf-16le")
        + struct.pack("<H", 0x0B)
        + hidden_payload
        + " 정량평가 배점표".encode("utf-16le")
        + struct.pack("<H", 0x0D)
    )
    second = "표 셀의 최근 3년 유사사업 실적 20점".encode("utf-16le")
    section = _hwp_record(67, first) + _hwp_record(67, second, level=1)
    module = _fake_olefile_module([_raw_deflate(section)])

    with _with_fake_module("olefile", module):
        result = extract_document_content("공고문.hwp", b"synthetic compound file")

    assert result.complete is True
    assert result.members_discovered == result.members_processed == 1
    assert "입찰 참가자격" in result.text
    assert "정량평가 배점표" in result.text
    assert "최근 3년 유사사업 실적 20점" in result.text
    assert "악성페이로드" not in result.text


def test_hwp5_rejects_encrypted_distributable_and_unknown_control() -> None:
    valid = _hwp_record(67, "충분히 긴 입찰 공고 본문과 정량평가 근거입니다".encode("utf-16le"))
    for flags, code in [(1 | (1 << 1), "HWP_ENCRYPTED"), (1 | (1 << 2), "HWP_DISTRIBUTABLE")]:
        with _with_fake_module("olefile", _fake_olefile_module([_raw_deflate(valid)], flags=flags)):
            with pytest.raises(DocumentExtractionError, match=code):
                extract_document_content("차단.hwp", b"synthetic compound file")

    unknown = _hwp_record(
        67,
        "정량평가 근거 앞".encode("utf-16le")
        + struct.pack("<H", 0x19)
        + "정량평가 근거 뒤".encode("utf-16le"),
    )
    with _with_fake_module("olefile", _fake_olefile_module([_raw_deflate(unknown)])):
        with pytest.raises(DocumentExtractionError, match="HWP_CONTROL_UNKNOWN"):
            extract_document_content("오염.hwp", b"synthetic compound file")


def test_xls_extracts_cached_cells_but_remains_incomplete_without_formulas() -> None:
    class Cell:
        def __init__(self, cell_type: int, value: object) -> None:
            self.ctype = cell_type
            self.value = value

    class Sheet:
        name = "정량평가"
        nrows = 2
        ncols = 2

        def row(self, index: int) -> list[Cell]:
            return [
                [Cell(1, "수행실적"), Cell(1, "배점")],
                [Cell(1, "최근 3년 유사사업 정량평가 기준"), Cell(2, 20.0)],
            ][index]

    class Workbook:
        nsheets = 1
        datemode = 0

        def sheets(self) -> list[Sheet]:
            return [Sheet()]

        def release_resources(self) -> None:
            pass

    module = types.SimpleNamespace(
        open_workbook=lambda **_kwargs: Workbook(),
        XL_CELL_EMPTY=0,
        XL_CELL_TEXT=1,
        XL_CELL_NUMBER=2,
        XL_CELL_DATE=3,
        XL_CELL_BOOLEAN=4,
        XL_CELL_ERROR=5,
        XL_CELL_BLANK=6,
        error_text_from_code={},
        xldate_as_datetime=lambda *_args: None,
    )

    with _with_fake_module("xlrd", module):
        result = extract_document_content("산출표.xls", b"synthetic workbook")

    assert "A1=수행실적" in result.text
    assert "B1=배점" in result.text
    assert "A2=최근 3년 유사사업 정량평가 기준" in result.text
    assert "B2=20" in result.text
    assert result.complete is False
    assert result.members_discovered == 1
    assert result.members_processed == 1
    assert result.warnings == ("XLS_FORMULA_EXPRESSIONS_UNAVAILABLE",)


def test_unknown_binary_format_is_an_explicit_incomplete_issue() -> None:
    result = extract_document_content("첨부.bin", b"unknown binary")

    assert result.complete is False
    assert result.members_processed == 0
    assert result.member_issues[0].reason == "UNSUPPORTED_DOCUMENT_TYPE"


def test_archive_rejects_traversal_and_case_colliding_members() -> None:
    traversal = _archive({"../outside.docx": _minimal_docx()})
    with pytest.raises(DocumentExtractionError, match="ARCHIVE_UNSAFE_MEMBER_PATH"):
        extract_document_content("bad.zip", traversal)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("A.docx", _minimal_docx())
        archive.writestr("a.DOCX", _minimal_docx())
    with pytest.raises(DocumentExtractionError, match="ARCHIVE_DUPLICATE_MEMBER"):
        extract_document_content("duplicate.zip", buffer.getvalue())


def test_archive_rejects_dtd_compression_bomb_and_excessive_depth() -> None:
    dtd = _archive(
        {
            "[Content_Types].xml": _content_types(),
            "word/document.xml": (
                '<!DOCTYPE x [<!ENTITY boom "boom">]>'
                '<w:document xmlns:w="urn:w"><w:p><w:t>&boom;</w:t></w:p></w:document>'
            ),
        }
    )
    with pytest.raises(DocumentExtractionError, match="XML_DTD_FORBIDDEN"):
        extract_document_content("bad.docx", dtd)

    compressed = _archive({"huge.docx": b"A" * 50_000})
    with pytest.raises(DocumentExtractionError, match="ARCHIVE_COMPRESSION_RATIO_LIMIT"):
        extract_document_content(
            "bomb.zip",
            compressed,
            limits=ExtractionLimits(max_compression_ratio=2),
        )

    deepest = _archive({"요청서.docx": _minimal_docx()})
    second = _archive({"second.zip": deepest})
    first = _archive({"first.zip": second})
    result = extract_document_content(
        "root.zip",
        first,
        limits=ExtractionLimits(max_archive_depth=1),
    )
    assert result.complete is False
    assert result.member_issues[0].reason == "ARCHIVE_DEPTH_LIMIT"


def test_generic_zip_keeps_good_sibling_when_one_supported_member_fails() -> None:
    content = _archive({"good.docx": _minimal_docx(), "bad.docx": b"not-a-zip"})

    result = extract_document_content("mixed.zip", content)

    assert "정량평가 수행실적 배점표" in result.text
    assert result.complete is False
    assert result.members_discovered == 2
    assert result.members_processed == 1
    assert result.member_issues[0].reason == "ARCHIVE_INVALID"


def test_text_limit_fails_closed_instead_of_truncating() -> None:
    with pytest.raises(DocumentExtractionError, match="DOCUMENT_TEXT_TOO_LARGE"):
        extract_document_content(
            "source.hwpx",
            b"ok",
            leaf_extractors={".hwpx": lambda _name, _data: "가" * 21},
            limits=ExtractionLimits(max_document_chars=20),
        )
