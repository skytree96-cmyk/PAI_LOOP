"""Bounded, non-executing extraction for public procurement documents.

This module deliberately keeps container parsing separate from PPS download and
LLM orchestration.  It never writes archive members to disk, follows links,
loads macros, or evaluates spreadsheet formulas.  Callers may provide audited
leaf extractors for formats such as PDF and HWPX while retaining explicit
coverage information for every member of a generic ZIP attachment.
"""

from __future__ import annotations

import io
import re
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePath, PurePosixPath
from typing import Callable, Mapping
from xml.etree import ElementTree


LeafExtractor = Callable[[str, bytes], str]

# Both packages declare the OSI-approved BSD license in their distribution
# metadata. Keep this explicit so an AGPL HWP parser cannot enter the
# proprietary deployment transitively without a deliberate license review.
BINARY_READER_DEPENDENCIES = (
    "olefile>=0.47,<1",
    "xlrd>=2.0.1,<3",
)


class DocumentExtractionError(RuntimeError):
    """A deterministic, public-safe extraction failure code."""


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_input_bytes: int = 8 * 1024 * 1024
    max_total_input_bytes: int = 24 * 1024 * 1024
    max_entries_per_archive: int = 512
    max_total_entries: int = 1_024
    max_member_uncompressed_bytes: int = 16 * 1024 * 1024
    max_total_uncompressed_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 2_000
    max_archive_depth: int = 2
    # Extraction and LLM prompt sizing are separate contracts. Real public
    # quantitative workbooks in the regression corpus exceed one million
    # characters; the extractor must preserve them so a caller can chunk or
    # deterministically select table regions without claiming false coverage.
    max_document_chars: int = 2_000_000

    def __post_init__(self) -> None:
        values = (
            self.max_input_bytes,
            self.max_total_input_bytes,
            self.max_entries_per_archive,
            self.max_total_entries,
            self.max_member_uncompressed_bytes,
            self.max_total_uncompressed_bytes,
            self.max_compression_ratio,
            self.max_archive_depth,
            self.max_document_chars,
        )
        if any(value <= 0 for value in values):
            raise ValueError("all extraction limits must be positive")
        if self.max_total_input_bytes < self.max_input_bytes:
            raise ValueError("total input limit must cover one input")
        if self.max_total_entries < self.max_entries_per_archive:
            raise ValueError("total entry limit must cover one archive")
        if self.max_total_uncompressed_bytes < self.max_member_uncompressed_bytes:
            raise ValueError("total uncompressed limit must cover one member")


@dataclass(frozen=True, slots=True)
class MemberIssue:
    member_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class DocumentExtractionResult:
    text: str
    warnings: tuple[str, ...]
    members_discovered: int
    members_processed: int
    complete: bool
    member_issues: tuple[MemberIssue, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class _Budget:
    limits: ExtractionLimits
    input_bytes: int = 0
    entries: int = 0
    uncompressed_bytes: int = 0

    def add_input(self, size: int) -> None:
        if size <= 0:
            raise DocumentExtractionError("DOCUMENT_EMPTY")
        if size > self.limits.max_input_bytes:
            raise DocumentExtractionError("DOCUMENT_INPUT_TOO_LARGE")
        self.input_bytes += size
        if self.input_bytes > self.limits.max_total_input_bytes:
            raise DocumentExtractionError("DOCUMENT_TOTAL_INPUT_LIMIT")

    def add_archive(self, entries: list[zipfile.ZipInfo]) -> None:
        if len(entries) > self.limits.max_entries_per_archive:
            raise DocumentExtractionError("ARCHIVE_ENTRY_LIMIT")
        self.entries += len(entries)
        if self.entries > self.limits.max_total_entries:
            raise DocumentExtractionError("ARCHIVE_TOTAL_ENTRY_LIMIT")
        archive_size = 0
        for item in entries:
            if item.is_dir():
                continue
            if item.file_size < 0 or item.file_size > self.limits.max_member_uncompressed_bytes:
                raise DocumentExtractionError("ARCHIVE_MEMBER_SIZE_LIMIT")
            if item.file_size > 1_024:
                if item.compress_size <= 0:
                    raise DocumentExtractionError("ARCHIVE_COMPRESSION_RATIO_LIMIT")
                if item.file_size / item.compress_size > self.limits.max_compression_ratio:
                    raise DocumentExtractionError("ARCHIVE_COMPRESSION_RATIO_LIMIT")
            archive_size += item.file_size
        self.uncompressed_bytes += archive_size
        if self.uncompressed_bytes > self.limits.max_total_uncompressed_bytes:
            raise DocumentExtractionError("ARCHIVE_UNCOMPRESSED_LIMIT")

    def add_uncompressed(self, size: int) -> None:
        if size < 0 or size > self.limits.max_member_uncompressed_bytes:
            raise DocumentExtractionError("DOCUMENT_UNCOMPRESSED_SIZE_LIMIT")
        self.uncompressed_bytes += size
        if self.uncompressed_bytes > self.limits.max_total_uncompressed_bytes:
            raise DocumentExtractionError("DOCUMENT_TOTAL_UNCOMPRESSED_LIMIT")


@dataclass(frozen=True, slots=True)
class _ParsedText:
    text: str
    warnings: tuple[str, ...] = ()
    complete: bool = True
    member_issues: tuple[MemberIssue, ...] = field(default_factory=tuple)


_BUILTIN_EXTENSIONS = {
    ".docx",
    ".hwp",
    ".html",
    ".htm",
    ".pptx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".zip",
}
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
_XML_DTD = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_WORD_PART = re.compile(
    r"^word/(?:document|header\d+|footer\d+|footnotes|endnotes|comments)\.xml$",
    re.IGNORECASE,
)
_SLIDE_PART = re.compile(
    r"^ppt/(?:slides/slide\d+|notesSlides/notesSlide\d+)\.xml$",
    re.IGNORECASE,
)
_WORKSHEET_PART = re.compile(r"^xl/worksheets/sheet\d+\.xml$", re.IGNORECASE)
_CELL_REFERENCE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")
_HWP_SIGNATURE = b"HWP Document File"
_HWP_FILE_HEADER_BYTES = 256
_HWP_PARA_TEXT_TAG = 67
_HWP_MAX_STREAMS = 512
_HWP_MAX_SECTIONS = 64
_HWP_MAX_RECORDS_PER_SECTION = 200_000
_HWP_CONTROL_CHAR_UNITS = {0x00, 0x0A, 0x0D, 0x18, 0x1E, 0x1F}
_HWP_CONTROL_EXTENDED_UNITS = {
    0x01,
    0x02,
    0x03,
    0x0B,
    0x0C,
    0x0E,
    0x0F,
    0x10,
    0x11,
    0x12,
    0x15,
    0x16,
    0x17,
}
_HWP_CONTROL_INLINE_UNITS = {0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x13, 0x14}
_OLE_CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def extract_document_content(
    file_name: str,
    content: bytes,
    *,
    leaf_extractors: Mapping[str, LeafExtractor] | None = None,
    limits: ExtractionLimits | None = None,
) -> DocumentExtractionResult:
    """Extract a document without hiding unsupported ZIP members.

    ``leaf_extractors`` is keyed by lowercase extension (for example ``.pdf``
    and ``.hwpx``).  A generic ZIP recursively processes every built-in or
    registered document.  Unsupported members remain in ``member_issues`` and
    make ``complete`` false while successfully extracted sibling text remains
    available for evidence generation.
    """

    safe_name = _safe_root_name(file_name)
    normalised_extractors = {
        _normalise_extension(extension): extractor
        for extension, extractor in (leaf_extractors or {}).items()
    }
    selected_limits = limits or ExtractionLimits()
    budget = _Budget(selected_limits)
    result = _extract(
        safe_name,
        bytes(content),
        leaf_extractors=normalised_extractors,
        budget=budget,
        archive_depth=0,
    )
    return _bound_result(result, selected_limits.max_document_chars)


def _extract(
    file_name: str,
    content: bytes,
    *,
    leaf_extractors: Mapping[str, LeafExtractor],
    budget: _Budget,
    archive_depth: int,
) -> DocumentExtractionResult:
    budget.add_input(len(content))
    extension = PurePath(file_name).suffix.casefold()
    if extension == ".hwp":
        return _result_from_parsed(_extract_hwp5(content, budget), file_name)
    if extension == ".xls":
        return _result_from_parsed(_extract_xls(content), file_name)
    if extension in leaf_extractors:
        try:
            text = leaf_extractors[extension](file_name, content)
            if not isinstance(text, str) or not text.strip():
                raise DocumentExtractionError("DOCUMENT_TEXT_EMPTY")
        except Exception as exc:  # the leaf boundary must not leak raw errors
            return _issue_result(file_name, _safe_exception_code(exc, "LEAF_EXTRACTION_FAILED"))
        return _single_result(text)
    if extension == ".docx":
        return _result_from_parsed(_extract_docx(content, budget), file_name)
    if extension in {".xlsx", ".xlsm"}:
        return _result_from_parsed(_extract_xlsx(content, budget), file_name)
    if extension == ".pptx":
        return _result_from_parsed(_extract_pptx(content, budget), file_name)
    if extension in {".html", ".htm"}:
        return _result_from_parsed(_extract_html(content), file_name)
    if extension == ".zip":
        if archive_depth >= budget.limits.max_archive_depth:
            return _issue_result(file_name, "ARCHIVE_DEPTH_LIMIT")
        return _extract_generic_zip(
            content,
            leaf_extractors=leaf_extractors,
            budget=budget,
            archive_depth=archive_depth + 1,
        )
    return _issue_result(file_name, "UNSUPPORTED_DOCUMENT_TYPE")


def _extract_generic_zip(
    content: bytes,
    *,
    leaf_extractors: Mapping[str, LeafExtractor],
    budget: _Budget,
    archive_depth: int,
) -> DocumentExtractionResult:
    with _open_archive(content, budget) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if not members:
            return DocumentExtractionResult(
                text="",
                warnings=("ARCHIVE_EMPTY",),
                members_discovered=0,
                members_processed=0,
                complete=False,
            )
        texts: list[str] = []
        warnings: list[str] = []
        issues: list[MemberIssue] = []
        discovered = 0
        processed = 0
        for member in sorted(members, key=lambda item: item.filename.casefold()):
            member_name = _safe_member_name(member.filename)
            extension = PurePosixPath(member_name).suffix.casefold()
            if extension not in _BUILTIN_EXTENSIONS and extension not in leaf_extractors:
                reason = "UNSUPPORTED_ARCHIVE_MEMBER_TYPE"
                discovered += 1
                issues.append(MemberIssue(member_name, reason))
                warnings.append(reason)
                continue
            try:
                child_content = _read_member(archive, member, budget.limits)
                child = _extract(
                    member_name,
                    child_content,
                    leaf_extractors=leaf_extractors,
                    budget=budget,
                    archive_depth=archive_depth,
                )
            except DocumentExtractionError as exc:
                child = _issue_result(
                    member_name,
                    _safe_exception_code(exc, "MEMBER_EXTRACTION_FAILED"),
                )
            discovered += child.members_discovered
            processed += child.members_processed
            warnings.extend(child.warnings)
            issues.extend(child.member_issues)
            if child.text.strip():
                texts.append(f"[DOCUMENT {member_name}]\n{child.text.strip()}")
        if discovered == 0:
            warnings.append("ARCHIVE_NO_DOCUMENT_MEMBERS")
        complete = discovered > 0 and processed == discovered and not issues
        return DocumentExtractionResult(
            text="\n\n".join(texts),
            warnings=_unique(warnings),
            members_discovered=discovered,
            members_processed=processed,
            complete=complete,
            member_issues=tuple(issues),
        )


def _extract_hwp5(content: bytes, budget: _Budget) -> _ParsedText:
    """Extract every HWP5 BodyText paragraph without executing embedded data.

    ``olefile`` is BSD-licensed and is used only as a bounded compound-file
    reader.  The HWP record and control-character parser below is implemented
    independently from the public HWP5 binary specification.  AGPL ``pyhwp``
    is intentionally not a runtime dependency of this proprietary service.
    """

    try:
        import olefile  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DocumentExtractionError("HWP_EXTRACTOR_UNAVAILABLE") from exc
    ole = None
    try:
        kwargs: dict[str, object] = {}
        defect_level = getattr(olefile, "DEFECT_INCORRECT", None)
        if defect_level is not None:
            kwargs["raise_defects"] = defect_level
        ole_reader = getattr(olefile, "OleFileIO", None)
        if not callable(ole_reader):
            raise DocumentExtractionError("HWP_EXTRACTOR_UNAVAILABLE")
        ole = ole_reader(io.BytesIO(content), **kwargs)
        stream_paths = ole.listdir(streams=True, storages=False)
        if len(stream_paths) > _HWP_MAX_STREAMS:
            raise DocumentExtractionError("HWP_STREAM_LIMIT")
        streams: dict[str, list[str]] = {}
        for raw_path in stream_paths:
            if list(raw_path) in [
                ["\x05HwpSummaryInformation"],
                ["\x05DocumentSummaryInformation"],
            ]:
                # Standard OLE property-set stream names begin with U+0005.
                # They contain no BodyText evidence and are never opened.
                continue
            if (
                not isinstance(raw_path, (list, tuple))
                or not raw_path
                or any(
                    not isinstance(part, str)
                    or not part
                    or len(part) > 255
                    or any(ord(character) < 32 for character in part)
                    for part in raw_path
                )
            ):
                raise DocumentExtractionError("HWP_INVALID_STREAM_PATH")
            path = [str(part) for part in raw_path]
            key = "/".join(path).casefold()
            if key in streams:
                raise DocumentExtractionError("HWP_DUPLICATE_STREAM")
            streams[key] = path
        header_path = streams.get("fileheader")
        if header_path is None:
            raise DocumentExtractionError("HWP_FILE_HEADER_MISSING")
        header = _read_ole_stream(
            ole,
            header_path,
            maximum=_HWP_FILE_HEADER_BYTES,
            exact=_HWP_FILE_HEADER_BYTES,
        )
        if header[:32].rstrip(b"\x00") != _HWP_SIGNATURE:
            raise DocumentExtractionError("HWP_FILE_HEADER_INVALID")
        if header[35] != 5:
            raise DocumentExtractionError("HWP_VERSION_UNSUPPORTED")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & ((1 << 1) | (1 << 4) | (1 << 8) | (1 << 10)):
            raise DocumentExtractionError("HWP_ENCRYPTED")
        if flags & (1 << 2):
            raise DocumentExtractionError("HWP_DISTRIBUTABLE")
        compressed = bool(flags & 1)
        warnings: list[str] = []
        issues: list[MemberIssue] = []
        if flags & (1 << 3):
            _add_member_issue(
                warnings,
                issues,
                "/".join(header_path),
                "HWP_ACTIVE_CONTENT_NOT_EXTRACTED",
            )
        for key, path in streams.items():
            if key.startswith("scripts/"):
                _add_member_issue(
                    warnings,
                    issues,
                    "/".join(path),
                    "HWP_ACTIVE_CONTENT_NOT_EXTRACTED",
                )
                continue
            if not key.startswith("bindata/"):
                continue
            embedded = _read_ole_stream(
                ole,
                path,
                maximum=budget.limits.max_member_uncompressed_bytes,
            )
            budget.add_uncompressed(len(embedded))
            kind = _hwp_bindata_kind(embedded)
            if kind == "IMAGE":
                continue
            reason = (
                "HWP_EMBEDDED_DOCUMENT_NOT_EXTRACTED"
                if kind == "DOCUMENT"
                else "HWP_BINDATA_TYPE_UNVERIFIED"
            )
            _add_member_issue(
                warnings,
                issues,
                "/".join(path),
                reason,
            )
        sections: list[tuple[int, list[str]]] = []
        for key, path in streams.items():
            matched = re.fullmatch(r"bodytext/section([0-9]+)", key)
            if matched:
                sections.append((int(matched.group(1)), path))
        sections.sort(key=lambda item: item[0])
        if not sections:
            raise DocumentExtractionError("HWP_BODYTEXT_MISSING")
        if len(sections) > _HWP_MAX_SECTIONS:
            raise DocumentExtractionError("HWP_SECTION_LIMIT")
        if [index for index, _path in sections] != list(range(len(sections))):
            raise DocumentExtractionError("HWP_SECTION_SEQUENCE_INVALID")
        section_texts: list[str] = []
        semantic_texts: list[str] = []
        for index, path in sections:
            raw = _read_ole_stream(
                ole,
                path,
                maximum=budget.limits.max_member_uncompressed_bytes,
            )
            section = (
                _decompress_hwp_section(raw, budget.limits.max_member_uncompressed_bytes)
                if compressed
                else raw
            )
            budget.add_uncompressed(len(section))
            paragraphs = _extract_hwp_section_paragraphs(section)
            if paragraphs is None:
                raise DocumentExtractionError("HWP_PARA_TEXT_MISSING")
            visible = [item for item in paragraphs if item.strip()]
            if visible:
                section_texts.append(f"[HWP SECTION {index}]")
                section_texts.extend(visible)
                semantic_texts.extend(visible)
        text = "\n".join(section_texts).strip()
        _validate_semantic_text(
            "\n".join(semantic_texts),
            error_code="HWP_TEXT_NOT_SEMANTIC",
        )
        return _ParsedText(
            text,
            _unique(warnings),
            not issues,
            tuple(issues),
        )
    except DocumentExtractionError:
        raise
    except Exception as exc:
        # olefile defects and malformed compound streams remain one stable,
        # non-sensitive review code.
        raise DocumentExtractionError("HWP_CONTAINER_INVALID") from exc
    finally:
        if ole is not None:
            try:
                ole.close()
            except Exception:
                pass


def _read_ole_stream(
    ole: object,
    path: list[str],
    *,
    maximum: int,
    exact: int | None = None,
) -> bytes:
    try:
        stream = ole.openstream(path)  # type: ignore[attr-defined]
        try:
            content = stream.read(maximum + 1)
        finally:
            stream.close()
    except Exception as exc:
        raise DocumentExtractionError("HWP_STREAM_READ_FAILED") from exc
    if not isinstance(content, bytes) or len(content) > maximum:
        raise DocumentExtractionError("HWP_STREAM_SIZE_LIMIT")
    if exact is not None and len(content) != exact:
        raise DocumentExtractionError("HWP_STREAM_SIZE_INVALID")
    return content


def _hwp_bindata_kind(content: bytes) -> str:
    """Classify BinData conservatively without executing or decoding it."""

    if (
        content.startswith((b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM"))
        or content.startswith(b"\x89PNG\r\n\x1a\n")
        or content.startswith(b"\xd7\xcd\xc6\x9a")
        or (
            len(content) >= 44
            and content[:4] == b"\x01\x00\x00\x00"
            and content[40:44] == b" EMF"
        )
    ):
        return "IMAGE"
    lowered = content[:512].lstrip().lower()
    if (
        content.startswith(_OLE_CFB_SIGNATURE)
        or content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
        or content.startswith(b"%PDF-")
        or lowered.startswith(b"{\\rtf")
        or b"<pkg:package" in lowered
        or b"<?mso-application" in lowered
    ):
        return "DOCUMENT"
    return "UNKNOWN"


def _decompress_hwp_section(content: bytes, maximum: int) -> bytes:
    try:
        decompressor = zlib.decompressobj(-15)
        result = decompressor.decompress(content, maximum + 1)
        if len(result) > maximum or decompressor.unconsumed_tail:
            raise DocumentExtractionError("HWP_SECTION_SIZE_LIMIT")
        remaining = maximum + 1 - len(result)
        result += decompressor.flush(remaining)
        if len(result) > maximum:
            raise DocumentExtractionError("HWP_SECTION_SIZE_LIMIT")
        if not decompressor.eof:
            raise DocumentExtractionError("HWP_SECTION_DEFLATE_INVALID")
        if decompressor.unused_data:
            # Some HWP5 producers append the standard DEFLATE CRC32/ISIZE
            # trailer without a gzip header. Validate it rather than silently
            # ignoring arbitrary trailing bytes.
            if len(decompressor.unused_data) != 8:
                raise DocumentExtractionError("HWP_SECTION_DEFLATE_INVALID")
            expected_crc, expected_size = struct.unpack("<II", decompressor.unused_data)
            if (
                expected_crc != (zlib.crc32(result) & 0xFFFFFFFF)
                or expected_size != (len(result) & 0xFFFFFFFF)
            ):
                raise DocumentExtractionError("HWP_SECTION_DEFLATE_INVALID")
        return result
    except DocumentExtractionError:
        raise
    except zlib.error as exc:
        raise DocumentExtractionError("HWP_SECTION_DEFLATE_INVALID") from exc


def _extract_hwp_section_paragraphs(content: bytes) -> list[str] | None:
    offset = 0
    records = 0
    paragraphs: list[str] = []
    found_para_text = False
    while offset < len(content):
        records += 1
        if records > _HWP_MAX_RECORDS_PER_SECTION:
            raise DocumentExtractionError("HWP_RECORD_LIMIT")
        if len(content) - offset < 4:
            raise DocumentExtractionError("HWP_RECORD_TRUNCATED")
        header = struct.unpack_from("<I", content, offset)[0]
        offset += 4
        tag_id = header & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if len(content) - offset < 4:
                raise DocumentExtractionError("HWP_RECORD_TRUNCATED")
            size = struct.unpack_from("<I", content, offset)[0]
            offset += 4
        if size > len(content) - offset:
            raise DocumentExtractionError("HWP_RECORD_TRUNCATED")
        payload = content[offset : offset + size]
        offset += size
        if tag_id == _HWP_PARA_TEXT_TAG:
            found_para_text = True
            paragraphs.append(_extract_hwp_para_text(payload))
    if offset != len(content):
        raise DocumentExtractionError("HWP_RECORD_TRAILING_DATA")
    return paragraphs if found_para_text else None


def _extract_hwp_para_text(payload: bytes) -> str:
    if len(payload) % 2:
        raise DocumentExtractionError("HWP_PARA_TEXT_ODD_LENGTH")
    fragments: list[str] = []
    plain = bytearray()

    def flush_plain() -> None:
        if not plain:
            return
        try:
            decoded = bytes(plain).decode("utf-16le", errors="strict")
        except UnicodeDecodeError as exc:
            raise DocumentExtractionError("HWP_PARA_TEXT_UTF16_INVALID") from exc
        fragments.append(decoded)
        plain.clear()

    offset = 0
    while offset < len(payload):
        code = struct.unpack_from("<H", payload, offset)[0]
        if code > 0x1F:
            plain.extend(payload[offset : offset + 2])
            offset += 2
            continue
        flush_plain()
        if code in _HWP_CONTROL_CHAR_UNITS:
            if code in {0x0A, 0x0D}:
                fragments.append("\n")
            elif code == 0x18:
                fragments.append("-")
            elif code in {0x1E, 0x1F}:
                fragments.append(" ")
            offset += 2
            continue
        if code in _HWP_CONTROL_EXTENDED_UNITS or code in _HWP_CONTROL_INLINE_UNITS:
            if len(payload) - offset < 16:
                raise DocumentExtractionError("HWP_CONTROL_TRUNCATED")
            if code == 0x09:
                fragments.append("\t")
            offset += 16
            continue
        # Codes 0x19-0x1d are not defined by the HWP5 control table. Treating
        # them as a one-unit character could expose binary control payload.
        raise DocumentExtractionError("HWP_CONTROL_UNKNOWN")
    flush_plain()
    text = unicodedata.normalize("NFC", "".join(fragments))
    text = "".join(
        character
        for character in text
        if character in {"\n", "\t"} or not unicodedata.category(character).startswith("C")
    )
    lines = [_normalise_line(item) for item in text.splitlines()]
    return "\n".join(item for item in lines if item)


def _validate_semantic_text(text: str, *, error_code: str) -> None:
    visible = [character for character in text if not character.isspace()]
    if len(visible) < 20:
        raise DocumentExtractionError(error_code)
    semantic = sum(character.isalnum() for character in visible)
    if semantic < 10 or semantic / len(visible) < 0.2:
        raise DocumentExtractionError(error_code)


def _extract_xls(content: bytes) -> _ParsedText:
    try:
        import xlrd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DocumentExtractionError("XLS_EXTRACTOR_UNAVAILABLE") from exc
    workbook = None
    try:
        workbook_reader = getattr(xlrd, "open_workbook", None)
        if not callable(workbook_reader):
            raise DocumentExtractionError("XLS_EXTRACTOR_UNAVAILABLE")
        workbook = workbook_reader(
            file_contents=content,
            on_demand=True,
            ragged_rows=True,
            formatting_info=False,
        )
        if workbook.nsheets <= 0:
            raise DocumentExtractionError("XLS_WORKBOOK_EMPTY")
        if workbook.nsheets > 64:
            raise DocumentExtractionError("XLS_SHEET_LIMIT")
        lines: list[str] = []
        total_cells = 0
        scanned_cells = 0
        semantic_values: list[str] = []
        for sheet in workbook.sheets():
            if sheet.nrows > 200_000 or sheet.ncols > 16_384:
                raise DocumentExtractionError("XLS_DIMENSION_LIMIT")
            lines.append(f"[SHEET {_normalise_line(str(sheet.name))}]")
            for row_index in range(sheet.nrows):
                row_values: list[str] = []
                row = sheet.row(row_index)
                scanned_cells += len(row)
                if scanned_cells > 1_000_000:
                    raise DocumentExtractionError("XLS_CELL_LIMIT")
                for column_index, cell in enumerate(row):
                    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                        continue
                    total_cells += 1
                    if total_cells > 1_000_000:
                        raise DocumentExtractionError("XLS_CELL_LIMIT")
                    value = _xls_cell_value(cell, workbook, xlrd)
                    if value:
                        semantic_values.append(value)
                        row_values.append(
                            f"{_spreadsheet_column(column_index)}{row_index + 1}={value}"
                        )
                if row_values:
                    lines.append("\t".join(row_values))
        if total_cells == 0:
            raise DocumentExtractionError("XLS_WORKBOOK_EMPTY")
        text = "\n".join(lines)
        _validate_semantic_text(
            "\n".join(semantic_values),
            error_code="XLS_TEXT_NOT_SEMANTIC",
        )
        # xlrd exposes cached formula results, but not the formula expressions.
        # Keep the useful labels/values while prohibiting an automatic claim of
        # complete quantitative evidence.
        return _ParsedText(
            text,
            warnings=("XLS_FORMULA_EXPRESSIONS_UNAVAILABLE",),
            complete=False,
        )
    except DocumentExtractionError:
        raise
    except Exception as exc:
        message = str(exc).casefold()
        code = "XLS_ENCRYPTED" if "encrypt" in message or "password" in message else "XLS_PARSE_FAILED"
        raise DocumentExtractionError(code) from exc
    finally:
        if workbook is not None:
            try:
                workbook.release_resources()
            except Exception:
                pass


def _xls_cell_value(cell: object, workbook: object, xlrd: object) -> str:
    cell_type = cell.ctype  # type: ignore[attr-defined]
    value = cell.value  # type: ignore[attr-defined]
    if cell_type == xlrd.XL_CELL_TEXT:  # type: ignore[attr-defined]
        return _normalise_line(str(value))
    if cell_type == xlrd.XL_CELL_NUMBER:  # type: ignore[attr-defined]
        return format(float(value), ".15g")
    if cell_type == xlrd.XL_CELL_DATE:  # type: ignore[attr-defined]
        try:
            return xlrd.xldate_as_datetime(value, workbook.datemode).isoformat()  # type: ignore[attr-defined]
        except Exception:
            return format(float(value), ".15g")
    if cell_type == xlrd.XL_CELL_BOOLEAN:  # type: ignore[attr-defined]
        return "TRUE" if bool(value) else "FALSE"
    if cell_type == xlrd.XL_CELL_ERROR:  # type: ignore[attr-defined]
        return str(xlrd.error_text_from_code.get(value, "[CELL_ERROR]"))  # type: ignore[attr-defined]
    return _normalise_line(str(value))


def _spreadsheet_column(index: int) -> str:
    if index < 0 or index >= 16_384:
        raise DocumentExtractionError("XLS_DIMENSION_LIMIT")
    result = ""
    current = index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _extract_docx(content: bytes, budget: _Budget) -> _ParsedText:
    with _open_archive(content, budget) as archive:
        names = _archive_names(archive)
        if "[content_types].xml" not in names or "word/document.xml" not in names:
            raise DocumentExtractionError("DOCX_REQUIRED_PART_MISSING")
        warnings, issues = _office_package_coverage(
            archive,
            names,
            limits=budget.limits,
            package_prefix="word/",
            code_prefix="DOCX",
        )
        parts = [
            item
            for item in archive.infolist()
            if not item.is_dir() and _WORD_PART.fullmatch(item.filename)
        ]
        parts.sort(key=lambda item: _natural_key(item.filename))
        lines: list[str] = []
        for item in parts:
            root = _parse_xml(
                _read_member(archive, item, budget.limits),
                "DOCX_XML_INVALID",
            )
            if any(_local_name(node.tag).casefold() == "altchunk" for node in root.iter()):
                _add_member_issue(
                    warnings,
                    issues,
                    item.filename,
                    "DOCX_ALTCHUNK_NOT_EXTRACTED",
                )
            part_lines = _paragraph_text(root)
            if part_lines:
                lines.append(f"[DOCX {item.filename}]")
                lines.extend(part_lines)
        if not lines and not issues:
            raise DocumentExtractionError("DOCUMENT_TEXT_EMPTY")
        return _ParsedText(
            "\n".join(lines),
            _unique(warnings),
            not issues,
            tuple(issues),
        )


def _extract_pptx(content: bytes, budget: _Budget) -> _ParsedText:
    with _open_archive(content, budget) as archive:
        names = _archive_names(archive)
        if "[content_types].xml" not in names or "ppt/presentation.xml" not in names:
            raise DocumentExtractionError("PPTX_REQUIRED_PART_MISSING")
        warnings, issues = _office_package_coverage(
            archive,
            names,
            limits=budget.limits,
            package_prefix="ppt/",
            code_prefix="PPTX",
        )
        parts = [
            item
            for item in archive.infolist()
            if not item.is_dir() and _SLIDE_PART.fullmatch(item.filename)
        ]
        parts.sort(key=lambda item: _natural_key(item.filename))
        lines: list[str] = []
        for item in parts:
            root = _parse_xml(
                _read_member(archive, item, budget.limits),
                "PPTX_XML_INVALID",
            )
            part_lines = _paragraph_text(root)
            if part_lines:
                lines.append(f"[PPTX {item.filename}]")
                lines.extend(part_lines)
        if not lines and not issues:
            raise DocumentExtractionError("DOCUMENT_TEXT_EMPTY")
        return _ParsedText(
            "\n".join(lines),
            _unique(warnings),
            not issues,
            tuple(issues),
        )


def _office_package_coverage(
    archive: zipfile.ZipFile,
    names: Mapping[str, str],
    *,
    limits: ExtractionLimits,
    package_prefix: str,
    code_prefix: str,
    macro_issue_code: str | None = None,
) -> tuple[list[str], list[MemberIssue]]:
    """Report package content the bounded text parser intentionally skips."""

    warnings: list[str] = []
    issues: list[MemberIssue] = []
    embedding_reason = f"{code_prefix}_EMBEDDED_DOCUMENT_NOT_EXTRACTED"
    macro_reason = macro_issue_code or f"{code_prefix}_MACRO_NOT_EXECUTED"
    altchunk_reason = f"{code_prefix}_ALTCHUNK_NOT_EXTRACTED"
    external_reason = f"{code_prefix}_EXTERNAL_RELATIONSHIP_NOT_FETCHED"
    relationship_error = f"{code_prefix}_RELATIONSHIPS_INVALID"

    for lowered, actual in sorted(names.items()):
        if lowered.startswith(f"{package_prefix}embeddings/"):
            _add_member_issue(warnings, issues, actual, embedding_reason)
        if lowered.startswith(package_prefix) and lowered.endswith("vbaproject.bin"):
            _add_member_issue(warnings, issues, actual, macro_reason)
        if lowered.startswith(package_prefix) and (
            "/afchunk" in lowered or "/altchunk" in lowered
        ):
            _add_member_issue(warnings, issues, actual, altchunk_reason)

    content_types_name = names["[content_types].xml"]
    content_types = _read_member(
        archive,
        archive.getinfo(content_types_name),
        limits,
    ).lower()
    if b"macroenabled" in content_types or b"vbaproject" in content_types:
        if not any(issue.reason == macro_reason for issue in issues):
            _add_member_issue(
                warnings,
                issues,
                content_types_name,
                macro_reason,
            )

    relationship_parts = sorted(
        (
            actual
            for lowered, actual in names.items()
            if lowered.endswith(".rels")
        ),
        key=_natural_key,
    )
    for actual in relationship_parts:
        try:
            root = _parse_xml(
                _read_member(archive, archive.getinfo(actual), limits),
                relationship_error,
            )
        except DocumentExtractionError:
            _add_member_issue(warnings, issues, actual, relationship_error)
            continue
        for relationship in root.iter():
            if _local_name(relationship.tag).casefold() != "relationship":
                continue
            attributes = {
                _local_name(key).casefold(): str(value)
                for key, value in relationship.attrib.items()
            }
            relationship_type = attributes.get("type", "").casefold()
            if relationship_type.endswith(("/package", "/oleobject")):
                _add_member_issue(warnings, issues, actual, embedding_reason)
            if relationship_type.endswith("/vbaproject"):
                _add_member_issue(warnings, issues, actual, macro_reason)
            if relationship_type.endswith("/afchunk"):
                _add_member_issue(warnings, issues, actual, altchunk_reason)
            if attributes.get("targetmode", "").casefold() == "external":
                _add_member_issue(warnings, issues, actual, external_reason)
    return warnings, issues


def _add_member_issue(
    warnings: list[str],
    issues: list[MemberIssue],
    member_path: str,
    reason: str,
) -> None:
    warnings.append(reason)
    issue = MemberIssue(member_path, reason)
    if issue not in issues:
        issues.append(issue)


def _extract_xlsx(content: bytes, budget: _Budget) -> _ParsedText:
    with _open_archive(content, budget) as archive:
        names = _archive_names(archive)
        if "[content_types].xml" not in names or "xl/workbook.xml" not in names:
            raise DocumentExtractionError("XLSX_REQUIRED_PART_MISSING")
        warnings, issues = _office_package_coverage(
            archive,
            names,
            limits=budget.limits,
            package_prefix="xl/",
            code_prefix="XLSX",
            macro_issue_code="XLSM_MACRO_NOT_EXECUTED",
        )
        shared_strings = _xlsx_shared_strings(archive, names, budget.limits)
        sheet_names = _xlsx_sheet_names(archive, names, budget.limits)
        sheet_parts = [
            item
            for item in archive.infolist()
            if not item.is_dir() and _WORKSHEET_PART.fullmatch(item.filename)
        ]
        sheet_parts.sort(key=lambda item: _natural_key(item.filename))
        lines: list[str] = []
        for index, item in enumerate(sheet_parts, start=1):
            root = _parse_xml(
                _read_member(archive, item, budget.limits),
                "XLSX_XML_INVALID",
            )
            label = sheet_names.get(item.filename, f"Sheet {index}")
            lines.append(f"[SHEET {label}]")
            lines.extend(_xlsx_rows(root, shared_strings))
        if not lines or not any(not line.startswith("[SHEET ") for line in lines):
            raise DocumentExtractionError("DOCUMENT_TEXT_EMPTY")
        for lowered, actual in sorted(names.items()):
            if lowered.startswith("xl/externallinks/"):
                _add_member_issue(
                    warnings,
                    issues,
                    actual,
                    "XLSX_EXTERNAL_LINK_NOT_FETCHED",
                )
        return _ParsedText(
            "\n".join(lines),
            _unique(warnings),
            not issues,
            tuple(issues),
        )


def _xlsx_shared_strings(
    archive: zipfile.ZipFile,
    names: Mapping[str, str],
    limits: ExtractionLimits,
) -> list[str]:
    actual = names.get("xl/sharedstrings.xml")
    if actual is None:
        return []
    root = _parse_xml(
        _read_member(archive, archive.getinfo(actual), limits),
        "XLSX_XML_INVALID",
    )
    values: list[str] = []
    for item in root:
        if _local_name(item.tag) != "si":
            continue
        values.append(_normalise_line("".join(_all_named_text(item, "t"))))
    return values


def _xlsx_sheet_names(
    archive: zipfile.ZipFile,
    names: Mapping[str, str],
    limits: ExtractionLimits,
) -> dict[str, str]:
    workbook_actual = names["xl/workbook.xml"]
    root = _parse_xml(
        _read_member(archive, archive.getinfo(workbook_actual), limits),
        "XLSX_XML_INVALID",
    )
    ordered_names = [
        _normalise_line(str(item.attrib.get("name") or ""))
        for item in root.iter()
        if _local_name(item.tag) == "sheet"
    ]
    sheet_paths = sorted(
        (actual for lowered, actual in names.items() if _WORKSHEET_PART.fullmatch(lowered)),
        key=_natural_key,
    )
    return {
        path: ordered_names[index]
        for index, path in enumerate(sheet_paths)
        if index < len(ordered_names) and ordered_names[index]
    }


def _xlsx_rows(root: ElementTree.Element, shared_strings: list[str]) -> list[str]:
    rows: list[str] = []
    for row in root.iter():
        if _local_name(row.tag) != "row":
            continue
        cells: list[str] = []
        for cell in row:
            if _local_name(cell.tag) != "c":
                continue
            reference = str(cell.attrib.get("r") or "?").upper()
            if reference != "?" and not _CELL_REFERENCE.fullmatch(reference):
                reference = "?"
            cell_type = str(cell.attrib.get("t") or "")
            formula = _first_child_text(cell, "f")
            cached = _first_child_text(cell, "v")
            if cell_type == "inlineStr":
                cached = "".join(_all_named_text(cell, "t"))
            elif cell_type == "s" and cached is not None:
                try:
                    cached = shared_strings[int(cached)]
                except (ValueError, IndexError):
                    cached = "[INVALID_SHARED_STRING]"
            elif cell_type == "b" and cached is not None:
                cached = "TRUE" if cached == "1" else "FALSE" if cached == "0" else cached
            values: list[str] = []
            if formula is not None:
                values.append("=" + _normalise_line(formula))
            if cached is not None and _normalise_line(cached):
                values.append(_normalise_line(cached))
            if values:
                cells.append(f"{reference}=" + " | ".join(values))
        if cells:
            rows.append("\t".join(cells))
    return rows


class _VisibleHTMLParser(HTMLParser):
    _hidden = {"script", "style", "noscript", "template", "svg", "canvas"}
    _blocks = {
        "address", "article", "aside", "blockquote", "br", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main",
        "nav", "p", "section", "table", "td", "th", "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_stack: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered in self._hidden:
            self.hidden_stack.append(lowered)
        elif not self.hidden_stack and lowered in self._blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if self.hidden_stack:
            if lowered == self.hidden_stack[-1]:
                self.hidden_stack.pop()
            return
        if lowered in self._blocks:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_stack:
            self.parts.append(data)


def _extract_html(content: bytes) -> _ParsedText:
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = content.decode("cp949")
        except UnicodeDecodeError as exc:
            raise DocumentExtractionError("HTML_ENCODING_UNSUPPORTED") from exc
    parser = _VisibleHTMLParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:
        raise DocumentExtractionError("HTML_PARSE_FAILED") from exc
    lines = [_normalise_line(item) for item in "".join(parser.parts).splitlines()]
    text = "\n".join(item for item in lines if item)
    if not text:
        raise DocumentExtractionError("DOCUMENT_TEXT_EMPTY")
    return _ParsedText(text)


def _open_archive(content: bytes, budget: _Budget) -> zipfile.ZipFile:
    archive: zipfile.ZipFile | None = None
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        entries = archive.infolist()
        _validate_archive_entries(entries)
        budget.add_archive(entries)
        return archive
    except DocumentExtractionError:
        if archive is not None:
            archive.close()
        raise
    except (ValueError, zipfile.BadZipFile, OSError) as exc:
        if archive is not None:
            archive.close()
        raise DocumentExtractionError("ARCHIVE_INVALID") from exc


def _validate_archive_entries(entries: list[zipfile.ZipInfo]) -> None:
    seen: set[str] = set()
    for item in entries:
        name = _safe_member_name(item.filename)
        folded = name.casefold()
        if folded in seen:
            raise DocumentExtractionError("ARCHIVE_DUPLICATE_MEMBER")
        seen.add(folded)
        if item.flag_bits & 0x1:
            raise DocumentExtractionError("ARCHIVE_ENCRYPTED_MEMBER")
        unix_type = (item.external_attr >> 16) & 0o170000
        if unix_type == 0o120000:
            raise DocumentExtractionError("ARCHIVE_LINK_MEMBER")


def _read_member(
    archive: zipfile.ZipFile,
    item: zipfile.ZipInfo,
    limits: ExtractionLimits,
) -> bytes:
    try:
        with archive.open(item, "r") as stream:
            content = stream.read(limits.max_member_uncompressed_bytes + 1)
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zlib.error,
    ) as exc:
        raise DocumentExtractionError("ARCHIVE_MEMBER_READ_FAILED") from exc
    if len(content) > limits.max_member_uncompressed_bytes:
        raise DocumentExtractionError("ARCHIVE_MEMBER_SIZE_LIMIT")
    if len(content) != item.file_size:
        raise DocumentExtractionError("ARCHIVE_MEMBER_SIZE_MISMATCH")
    return content


def _archive_names(archive: zipfile.ZipFile) -> dict[str, str]:
    return {
        item.filename.casefold(): item.filename
        for item in archive.infolist()
        if not item.is_dir()
    }


def _parse_xml(content: bytes, error_code: str) -> ElementTree.Element:
    if _XML_DTD.search(content):
        raise DocumentExtractionError("XML_DTD_FORBIDDEN")
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise DocumentExtractionError(error_code) from exc


def _paragraph_text(root: ElementTree.Element) -> list[str]:
    lines: list[str] = []
    for paragraph in root.iter():
        if _local_name(paragraph.tag) != "p":
            continue
        fragments: list[str] = []
        for node in paragraph.iter():
            local = _local_name(node.tag)
            if local == "t" and node.text:
                fragments.append(node.text)
            elif local == "tab":
                fragments.append("\t")
            elif local in {"br", "cr"}:
                fragments.append("\n")
        line = _normalise_line("".join(fragments))
        if line:
            lines.append(line)
    return lines


def _all_named_text(root: ElementTree.Element, local_name: str) -> list[str]:
    return [
        item.text or ""
        for item in root.iter()
        if _local_name(item.tag) == local_name
    ]


def _first_child_text(root: ElementTree.Element, local_name: str) -> str | None:
    for item in root:
        if _local_name(item.tag) == local_name:
            return item.text or ""
    return None


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _normalise_line(value: str) -> str:
    text = unicodedata.normalize("NFC", value)
    return re.sub(r"[ \f\v]+", " ", text).strip()


def _safe_root_name(value: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    if (
        not name
        or len(name) > 255
        or PurePath(name).name != name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise DocumentExtractionError("UNSAFE_DOCUMENT_FILENAME")
    return name


def _safe_member_name(value: str) -> str:
    name = unicodedata.normalize("NFC", str(value or ""))
    candidate = name[:-1] if name.endswith("/") else name
    raw_parts = candidate.split("/")
    path = PurePosixPath(candidate)
    if (
        not candidate
        or len(name) > 512
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or any(":" in part for part in raw_parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise DocumentExtractionError("ARCHIVE_UNSAFE_MEMBER_PATH")
    return path.as_posix() + ("/" if name.endswith("/") else "")


def _normalise_extension(value: str) -> str:
    extension = str(value or "").strip().casefold()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
        raise ValueError("leaf extractor extensions must be simple lowercase suffixes")
    return extension


def _safe_exception_code(exc: Exception, default: str) -> str:
    code = str(exc).strip()
    return code if _SAFE_ERROR_CODE.fullmatch(code) else default


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def _single_result(text: str) -> DocumentExtractionResult:
    return DocumentExtractionResult(
        text=text.strip(),
        warnings=(),
        members_discovered=1,
        members_processed=1,
        complete=True,
    )


def _issue_result(path: str, reason: str) -> DocumentExtractionResult:
    return DocumentExtractionResult(
        text="",
        warnings=(reason,),
        members_discovered=1,
        members_processed=0,
        complete=False,
        member_issues=(MemberIssue(path, reason),),
    )


def _result_from_parsed(parsed: _ParsedText, path: str) -> DocumentExtractionResult:
    issues = [
        MemberIssue(f"{path}!/{issue.member_path}", issue.reason)
        for issue in parsed.member_issues
    ]
    processed = 1
    if not parsed.complete:
        covered_reasons = {issue.reason for issue in issues}
        issues.extend(
            MemberIssue(path, warning)
            for warning in parsed.warnings
            if warning not in covered_reasons
        )
    return DocumentExtractionResult(
        text=parsed.text.strip(),
        warnings=parsed.warnings,
        members_discovered=1,
        members_processed=processed,
        complete=parsed.complete,
        member_issues=tuple(issues),
    )


def _bound_result(
    result: DocumentExtractionResult,
    max_document_chars: int,
) -> DocumentExtractionResult:
    # Never truncate silently: losing the tail could hide a quantitative table
    # while still claiming full attachment coverage.
    if len(result.text) > max_document_chars:
        raise DocumentExtractionError("DOCUMENT_TEXT_TOO_LARGE")
    return result
