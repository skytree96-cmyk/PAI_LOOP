#!/usr/bin/env python3
"""Normalize a private performance workbook and import it without uploading the XLSX.

The source workbook stays on the operator workstation.  Only the fields needed
by the private performance register, an opaque row anchor, and the workbook
SHA-256 are sent to the authenticated API.
"""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import math
import os
import re
import ssl
import sys
import zipfile
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from xml.etree import ElementTree


SHEET_NAME = "프로젝트DB"
SCHEMA_VERSION = "kma-private-performance-v1"
IMPORT_ENDPOINT = "/api/v1/performance-records/private-import"
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_RECORDS = 20_000
MAX_BATCH_SIZE = 100
VERIFICATION_ATTESTATION = (
    "OPERATOR_CONFIRMED_CERTIFICATE_BACKED_COMPLETED_VAT_INCLUDED_"
    "RECOGNIZED_AMOUNT_NET_OF_SHARE"
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS = {"m": _MAIN_NS, "r": _REL_NS, "p": _PKG_REL_NS}
_CELL_REF_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_TOKEN_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:[./-]|년)\s*(?P<month>\d{1,2})"
    r"\s*(?:[./-]|월)\s*(?P<day>\d{1,2})\s*일?(?!\d)"
)
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_SHORT_DATE_RE = re.compile(
    r"(?<!\d)(?P<year>\d{2})\s*[./-]\s*(?P<month>\d{1,2})"
    r"\s*[./-]\s*(?P<day>\d{1,2})(?!\d)"
)

_EXPECTED_HEADERS = {
    "A": "식별번호",
    "B": "공동계약여부",
    "C": "용역명",
    "D": "용역개요",
    "E": "계약번호",
    "F": "계약일자",
    "G": "계약기간",
    "H": "계약금액",
    "I": "이행비율",
    "J": "실적",
    "K": "발주처",
    "L": "주소",
    "M": "담당부서",
    "N": "담당자",
    "O": "전화번호",
    "P": "키워드",
    "Q": "수행부서",
}


class ImportErrorDetail(RuntimeError):
    """Operator-safe importer failure without source row content."""


@dataclass(frozen=True)
class NormalizedRecord:
    source_row: int
    row_key: str
    joint_procurement: bool
    fields: dict[str, Any]
    issues: tuple[str, ...]

    def api_payload(self) -> dict[str, Any]:
        return {
            "source_row": self.source_row,
            "row_key": self.row_key,
            "fields": self.fields,
        }


@dataclass(frozen=True)
class ImportBundle:
    source_sha256: str
    sheet_name: str
    schema_version: str
    source_data_rows: int
    placeholder_rows: int
    records: tuple[NormalizedRecord, ...]

    @property
    def validated_rows(self) -> int:
        return sum(item.fields["record_status"] == "VALIDATED" for item in self.records)

    @property
    def draft_rows(self) -> int:
        return len(self.records) - self.validated_rows

    @property
    def joint_rows(self) -> int:
        return sum(item.joint_procurement for item in self.records)


def _normalise_text(value: object, *, limit: int | None = None) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).replace("\u00a0", " ").split())
    if limit is not None:
        text = text[:limit]
    return text


def _column_from_reference(reference: str) -> str:
    match = _CELL_REF_RE.fullmatch(reference)
    if not match:
        raise ImportErrorDetail("워크북에 유효하지 않은 셀 참조가 있습니다.")
    return match.group(1)


def _validate_archive(archive: zipfile.ZipFile) -> zipfile.ZipFile:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_FILES:
        archive.close()
        raise ImportErrorDetail("XLSX 내부 파일 수가 안전 한도를 초과했습니다.")
    total = 0
    for member in members:
        if member.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
            archive.close()
            raise ImportErrorDetail("XLSX 내부 파일 크기가 안전 한도를 초과했습니다.")
        total += member.file_size
    if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        archive.close()
        raise ImportErrorDetail("XLSX 압축 해제 크기가 안전 한도를 초과했습니다.")
    return archive


def _safe_zip(path: Path) -> zipfile.ZipFile:
    if path.suffix.casefold() != ".xlsx":
        raise ImportErrorDetail(".xlsx 원본만 지원합니다.")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImportErrorDetail("원본 XLSX를 열 수 없습니다.") from exc
    return _validate_archive(archive)


def _safe_zip_bytes(source_bytes: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(BytesIO(source_bytes))
    except zipfile.BadZipFile as exc:
        raise ImportErrorDetail("원본 XLSX를 열 수 없습니다.") from exc
    return _validate_archive(archive)


def _xml(archive: zipfile.ZipFile, member: str) -> ElementTree.Element:
    try:
        data = archive.read(member)
    except KeyError as exc:
        raise ImportErrorDetail("XLSX 필수 구성요소가 없습니다.") from exc
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ImportErrorDetail("XLSX XML을 해석할 수 없습니다.") from exc


def _worksheet_member(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = _xml(archive, "xl/workbook.xml")
    relationship_id = None
    for sheet in workbook.findall("m:sheets/m:sheet", _NS):
        if sheet.attrib.get("name") == sheet_name:
            relationship_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
            break
    if not relationship_id:
        raise ImportErrorDetail(f"'{sheet_name}' 시트를 찾을 수 없습니다.")
    relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
    target = None
    for item in relationships.findall("p:Relationship", _NS):
        if item.attrib.get("Id") == relationship_id:
            target = item.attrib.get("Target")
            break
    if not target or target.startswith(("/", "\\")) or ".." in Path(target).parts:
        raise ImportErrorDetail("XLSX 시트 연결 정보가 안전하지 않습니다.")
    member = target.replace("\\", "/")
    if not member.startswith("xl/"):
        member = "xl/" + member.lstrip("/")
    return member


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(archive, "xl/sharedStrings.xml")
    return [
        "".join(node.text or "" for node in item.findall(".//m:t", _NS))
        for item in root.findall("m:si", _NS)
    ]


def _cell_value(cell: ElementTree.Element, shared_strings: Sequence[str]) -> object:
    cell_type = cell.attrib.get("t", "n")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", _NS))
    value_node = cell.find("m:v", _NS)
    if value_node is None or value_node.text is None:
        return None
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError) as exc:
            raise ImportErrorDetail("XLSX 공유 문자열 인덱스가 유효하지 않습니다.") from exc
    if cell_type in {"str", "e"}:
        return raw
    if cell_type == "b":
        return raw == "1"
    try:
        decimal = Decimal(raw)
    except InvalidOperation:
        return raw
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return decimal


def _read_sheet_rows_from_archive(
    archive: zipfile.ZipFile,
    *,
    sheet_name: str,
) -> list[dict[str, object]]:
    workbook = _xml(archive, "xl/workbook.xml")
    workbook_properties = workbook.find("m:workbookPr", _NS)
    if workbook_properties is not None and str(
        workbook_properties.attrib.get("date1904", "")
    ).casefold() in {"1", "true"}:
        raise ImportErrorDetail("1904 날짜 체계 XLSX는 지원하지 않습니다.")
    shared_strings = _shared_strings(archive)
    root = _xml(archive, _worksheet_member(archive, sheet_name))
    rows: list[dict[str, object]] = []
    for row_node in root.findall("m:sheetData/m:row", _NS):
        row_number = int(row_node.attrib.get("r", "0") or "0")
        if row_number <= 0:
            raise ImportErrorDetail("XLSX 행 번호가 유효하지 않습니다.")
        values: dict[str, object] = {"__row__": row_number}
        for cell in row_node.findall("m:c", _NS):
            reference = cell.attrib.get("r", "")
            values[_column_from_reference(reference)] = _cell_value(cell, shared_strings)
        rows.append(values)
        if len(rows) > MAX_RECORDS + 1:
            raise ImportErrorDetail("워크북 행 수가 안전 한도를 초과했습니다.")
    return rows


def read_sheet_rows(path: Path, *, sheet_name: str = SHEET_NAME) -> list[dict[str, object]]:
    with _safe_zip(path) as archive:
        return _read_sheet_rows_from_archive(archive, sheet_name=sheet_name)


def _validate_headers(header: dict[str, object]) -> None:
    mismatched = [
        column
        for column, expected in _EXPECTED_HEADERS.items()
        if _normalise_text(header.get(column)) != expected
    ]
    if mismatched:
        raise ImportErrorDetail(
            "프로젝트DB 스키마가 예상 형식과 다릅니다(열: " + ", ".join(mismatched) + ")."
        )


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    cleaned = re.sub(r"[^0-9.+-]", "", str(value))
    if not cleaned or cleaned in {"+", "-", "."}:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _amount(value: object) -> int | None:
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(
            r"(?:₩\s*)?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?\s*(?:원)?",
            text,
        ):
            # H/J are contract-register KRW cells. Never reinterpret abbreviated
            # Korean units (for example 억원) as a literal number of won.
            return None
        value = re.sub(r"[₩,원\s]", "", text)
    parsed = _decimal(value)
    if parsed is None or parsed < 0:
        return None
    return int(parsed.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def normalise_share(value: object) -> float | None:
    parsed = _decimal(value)
    if parsed is None or parsed < 0:
        return None
    explicit_percent = isinstance(value, str) and "%" in value
    if not explicit_percent and Decimal("0") < parsed <= Decimal("1"):
        parsed *= 100
    if parsed > 100:
        return None
    return float(parsed)


def _excel_date(value: object) -> date | None:
    parsed = _decimal(value)
    if parsed is None or parsed < 1 or parsed > 100_000:
        return None
    # Excel's 1900 date system includes a fictional 1900-02-29.  The standard
    # 1899-12-30 epoch compensates for it for all modern dates.
    return date(1899, 12, 30) + timedelta(days=int(parsed))


def _date_from_match(match: re.Match[str], *, period_end: bool = False) -> date | None:
    year = int(match.group("year"))
    month = int(match.group("month"))
    day_text = match.group("day")
    if not 1 <= month <= 12:
        return None
    day = int(day_text) if day_text else (monthrange(year, month)[1] if period_end else 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _short_date(match: re.Match[str]) -> date | None:
    try:
        return date(
            2000 + int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def _same_year_abbreviated_end(
    text: str,
    start_match: re.Match[str],
    start: date,
) -> date | None:
    tail = text[start_match.end() :]
    match = re.search(
        r"(?:~|〜|∼|-)\s*/?\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?!\s*[./-]\s*\d)",
        tail,
    )
    if not match:
        return None
    try:
        candidate = date(start.year, int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None
    # An omitted year is restored only when the same-year interpretation is
    # unambiguous.  Crossing a year boundary would require guessing.
    return candidate if candidate >= start else None


def parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return _excel_date(value)
    text = _normalise_text(value)
    if not text:
        return None
    compact = _COMPACT_DATE_RE.search(text)
    if compact:
        try:
            return date(*(int(part) for part in compact.groups()))
        except ValueError:
            return None
    match = _DATE_TOKEN_RE.search(text)
    return _date_from_match(match) if match else None


def _explicit_period_dates(text: str) -> tuple[date | None, date | None] | None:
    """Parse two explicit boundaries without joining adjacent date tokens.

    Abbreviated end years are restored only within the same calendar year.
    A single date, duration, month-only range or malformed year is not proof
    of an exact interval and remains subject to the existing strict parser.
    """
    full = r"(?:20\d{2}|\d{2})\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}\.?|20\d{6}"
    end = rf"(?:{full}|\d{{1,2}}\s*[./-]\s*\d{{1,2}}\.?)"
    match = re.fullmatch(
        rf"(?P<start>{full})(?:\s*[~〜∼～–—-]\s*|\s+)(?P<end>{end})",
        text,
    )
    if match is None:
        return None

    def boundary(token: str, *, year: int | None = None) -> date | None:
        compact = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", token)
        parts = compact.groups() if compact else tuple(
            part for part in re.split(r"[./-]", token.rstrip(".")) if part.strip()
        )
        try:
            numbers = tuple(int(part.strip()) for part in parts)
            if len(numbers) == 2 and year is not None:
                return date(year, *numbers)
            if len(numbers) == 3:
                y, month, day = numbers
                return date(y + 2000 if y < 100 else y, month, day)
        except ValueError:
            return None
        return None

    start = boundary(match.group("start"))
    end_date = boundary(match.group("end"), year=start.year if start else None)
    if start is None or end_date is None or end_date < start:
        return None, None
    return start, end_date


def parse_period(value: object) -> tuple[date | None, date | None]:
    text = _normalise_text(value)
    if not text:
        return None, None
    explicit = _explicit_period_dates(text)
    if explicit is not None:
        return explicit
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    matches = list(_DATE_TOKEN_RE.finditer(text))
    if len(matches) >= 2:
        return _date_from_match(matches[0]), _date_from_match(matches[-1], period_end=True)
    if len(matches) == 1:
        start = _date_from_match(matches[0])
        if start:
            end = _same_year_abbreviated_end(text, matches[0], start)
            if end:
                return start, end
    short_matches = list(_SHORT_DATE_RE.finditer(text))
    if len(short_matches) >= 2:
        start = _short_date(short_matches[0])
        end = _short_date(short_matches[-1])
        if start and end and end >= start:
            return start, end
    compact = list(_COMPACT_DATE_RE.finditer(text))
    if len(compact) >= 2:
        dates: list[date] = []
        for match in (compact[0], compact[-1]):
            try:
                dates.append(date(*(int(part) for part in match.groups())))
            except ValueError:
                return None, None
        return dates[0], dates[1]
    return None, None


def _keywords(value: object) -> list[str]:
    text = str(value or "")
    output: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;|/\n\r]+", text):
        keyword = _normalise_text(part, limit=80)
        folded = keyword.casefold()
        if not keyword or folded in seen:
            continue
        output.append(keyword)
        seen.add(folded)
        if len(output) == 30:
            break
    return output


def _opaque_evidence_uri(source_sha256: str, row_number: int | None = None) -> str:
    base = f"private-evidence://company-performance/{source_sha256}/workbook"
    if row_number is None:
        return base
    return f"{base}#{SHEET_NAME}!A{row_number}:Q{row_number}"


def _row_identity(row: dict[str, object]) -> str:
    # Internal identifiers and contract numbers never leave the workstation in
    # plaintext.  They contribute only to an irreversible canonical identity.
    # Workbook digest and physical row are deliberately excluded so a revised
    # workbook updates the same operational record instead of duplicating it.
    return "\x1f".join(
        (
            _normalise_text(row.get("A")),
            _normalise_text(row.get("E")),
            _normalise_text(row.get("C")),
            _normalise_text(row.get("K")),
            _normalise_text(row.get("F")),
            _normalise_text(row.get("G")),
        )
    )


def normalize_rows(
    rows: Sequence[dict[str, object]],
    *,
    source_sha256: str,
) -> ImportBundle:
    if not _HEX64_RE.fullmatch(source_sha256):
        raise ImportErrorDetail("원본 SHA-256 형식이 유효하지 않습니다.")
    if not rows:
        raise ImportErrorDetail("프로젝트DB 시트가 비어 있습니다.")
    header = rows[0]
    if int(header.get("__row__", 0)) != 1:
        raise ImportErrorDetail("프로젝트DB 첫 행에서 헤더를 찾을 수 없습니다.")
    _validate_headers(header)
    normalized: list[NormalizedRecord] = []
    placeholder_rows = 0
    identity_occurrences: dict[str, int] = {}
    for row in rows[1:]:
        source_row = int(row.get("__row__", 0))
        project_name = _normalise_text(row.get("C"), limit=500)
        if project_name == "프로젝트 없음":
            placeholder_rows += 1
            continue
        if not project_name:
            # A fully blank formatted row is not a source record.
            if not any(_normalise_text(row.get(column)) for column in _EXPECTED_HEADERS):
                continue
            raise ImportErrorDetail(f"{source_row}행에 용역명이 없습니다.")

        agency = _normalise_text(row.get("K"), limit=255)
        division = _normalise_text(row.get("Q"), limit=255)
        overview = _normalise_text(row.get("D"), limit=4000) or None
        contract_date = parse_date(row.get("F"))
        start_date, end_date = parse_period(row.get("G"))
        gross_amount = _amount(row.get("H"))
        recognized_amount = _amount(row.get("J"))
        share_pct = normalise_share(row.get("I"))
        issues: list[str] = []
        if not agency:
            issues.append("MISSING_AGENCY")
        if not division:
            issues.append("MISSING_DIVISION")
        if contract_date is None:
            issues.append("MISSING_CONTRACT_DATE")
        if start_date is None or end_date is None or end_date < start_date:
            issues.append("INVALID_CONTRACT_PERIOD")
            start_date = None
            end_date = None
        if gross_amount is None:
            issues.append("MISSING_GROSS_AMOUNT")
        if recognized_amount is None:
            issues.append("MISSING_RECOGNIZED_AMOUNT")
        if share_pct is None:
            issues.append("INVALID_SHARE")

        record_status = "VALIDATED" if not {
            "MISSING_AGENCY",
            "MISSING_DIVISION",
            "MISSING_CONTRACT_DATE",
            "INVALID_CONTRACT_PERIOD",
            "MISSING_GROSS_AMOUNT",
            "MISSING_RECOGNIZED_AMOUNT",
            "INVALID_SHARE",
        }.intersection(issues) else "DRAFT"
        # An unknown consortium share must never silently become a full-share
        # record, even while it remains a DRAFT awaiting operator correction.
        safe_share = share_pct if share_pct is not None else 0.0
        fields: dict[str, Any] = {
            "record_status": record_status,
            "project_name": project_name,
            "agency": agency,
            "division": division,
            "overview": overview,
            "contract_date": contract_date.isoformat() if contract_date else None,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            # Legacy display/fallback field remains the gross contract amount.
            "contract_amount": gross_amount,
            "gross_contract_amount_krw": gross_amount,
            "recognized_performance_amount_krw": recognized_amount,
            "recognized_amount_is_net_of_share": recognized_amount is not None,
            "vat_basis": "INCLUDED",
            "completed": True,
            "share_pct": safe_share,
            "certificate_status": "ISSUED",
            "evidence_reference": _opaque_evidence_uri(source_sha256, source_row),
            "keywords": _keywords(row.get("P")),
        }
        identity = _row_identity(row)
        occurrence = identity_occurrences.get(identity, 0) + 1
        identity_occurrences[identity] = occurrence
        row_key = hashlib.sha256(
            f"{identity}\x1e{occurrence}".encode("utf-8")
        ).hexdigest()
        normalized.append(
            NormalizedRecord(
                source_row=source_row,
                row_key=row_key,
                joint_procurement=share_pct is not None and safe_share < 100,
                fields=fields,
                issues=tuple(issues),
            )
        )
    if len(normalized) > MAX_RECORDS:
        raise ImportErrorDetail("정규화 레코드 수가 안전 한도를 초과했습니다.")
    return ImportBundle(
        source_sha256=source_sha256,
        sheet_name=SHEET_NAME,
        schema_version=SCHEMA_VERSION,
        source_data_rows=len(rows) - 1,
        placeholder_rows=placeholder_rows,
        records=tuple(normalized),
    )


def normalize_workbook(path: Path) -> ImportBundle:
    if path.suffix.casefold() != ".xlsx":
        raise ImportErrorDetail(".xlsx 원본만 지원합니다.")
    try:
        with path.open("rb") as source:
            source_bytes = source.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise ImportErrorDetail("원본 XLSX를 읽을 수 없습니다.") from exc
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise ImportErrorDetail("원본 XLSX 크기가 안전 한도를 초과했습니다.")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    with _safe_zip_bytes(source_bytes) as archive:
        rows = _read_sheet_rows_from_archive(archive, sheet_name=SHEET_NAME)
    del source_bytes
    return normalize_rows(rows, source_sha256=source_sha256)


def _chunks(values: Sequence[NormalizedRecord], size: int) -> Iterator[Sequence[NormalizedRecord]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ImportErrorDetail("운영 API 주소는 자격증명이 없는 https URL이어야 합니다.")
    return f"https://{parsed.netloc}"


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward operator credentials to a redirected origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise HTTPError(
            req.full_url,
            code,
            "redirect rejected for private evidence upload",
            headers,
            fp,
        )


def upload_bundle(
    bundle: ImportBundle,
    *,
    base_url: str,
    api_key: str = "",
    private_evidence_token: str = "",
    batch_size: int = MAX_BATCH_SIZE,
    timeout_seconds: float = 60.0,
    attest_source_semantics: bool = False,
) -> dict[str, int]:
    if not api_key.strip() and not private_evidence_token.strip():
        raise ImportErrorDetail(
            "서버 API 키 또는 비공개 증빙 전용 토큰 환경변수가 비어 있습니다."
        )
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ImportErrorDetail(f"배치 크기는 1~{MAX_BATCH_SIZE}이어야 합니다.")
    if not attest_source_semantics:
        raise ImportErrorDetail(
            "적재 전 증명서 기반·완료·VAT 포함·지분 반영 금액 의미를 명시적으로 확인해야 합니다."
        )
    endpoint = _validated_base_url(base_url) + IMPORT_ENDPOINT
    totals = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "archived": 0,
        "activated": 0,
    }
    context = ssl.create_default_context()
    batches = list(_chunks(bundle.records, batch_size))
    opener = build_opener(_RejectRedirects(), HTTPSHandler(context=context))
    import_complete = False
    for batch_index, records in enumerate(batches):
        body = json.dumps(
            {
                "source_sha256": bundle.source_sha256,
                "sheet_name": bundle.sheet_name,
                "schema_version": bundle.schema_version,
                "source_row_count": len(bundle.records),
                "batch_index": batch_index,
                "batch_count": len(batches),
                "verification_attestation": VERIFICATION_ATTESTATION,
                "records": [item.api_payload() for item in records],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key.strip():
            headers["X-PAI-LOOP-API-KEY"] = api_key
        else:
            headers["X-PAI-Private-Evidence-Token"] = private_evidence_token
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                if response.geturl() != endpoint:
                    raise ImportErrorDetail("운영 API가 예상하지 않은 주소로 응답했습니다.")
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Never echo the response body: validation text can contain a
            # private row value if a future server changes its error contract.
            raise ImportErrorDetail(f"운영 API가 HTTP {exc.code}로 적재를 거부했습니다.") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ImportErrorDetail("운영 API 응답을 확인할 수 없습니다.") from exc
        for key in totals:
            value = result.get(key)
            if not isinstance(value, int) or value < 0:
                raise ImportErrorDetail("운영 API 적재 집계 형식이 유효하지 않습니다.")
            totals[key] += value
        touched = sum(
            int(result.get(key, -1))
            for key in ("created", "updated", "unchanged")
        )
        if touched != len(records):
            raise ImportErrorDetail("운영 API가 배치 전체의 처리 결과를 확인하지 않았습니다.")
        import_complete = result.get("import_complete") is True
    if not import_complete:
        raise ImportErrorDetail(
            "모든 배치가 전송됐지만 서버가 비공개 실적을 활성화하지 않았습니다."
        )
    return totals


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "로컬 XLSX를 비공개 실적 레코드로 정규화합니다. 원본 파일과 연락처 열은 "
            "서버로 전송되지 않습니다."
        )
    )
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--base-url", help="적재할 운영 API의 https 기본 주소")
    parser.add_argument(
        "--api-key-env",
        default="PAI_LOOP_API_KEY",
        help="서버 API 키를 읽을 환경변수 이름(기본값: PAI_LOOP_API_KEY)",
    )
    parser.add_argument(
        "--private-token-env",
        default="PAI_LOOP_PRIVATE_EVIDENCE_TOKEN",
        help=(
            "API 키가 없을 때 비공개 증빙 전용 토큰을 읽을 환경변수 이름"
            "(기본값: PAI_LOOP_PRIVATE_EVIDENCE_TOKEN)"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument(
        "--attest-source-semantics",
        action="store_true",
        help=(
            "원본 전체가 실적증명서 기반·완료·VAT 포함이고 J열이 지분 반영 인정금액임을 확인"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _summary(bundle: ImportBundle, upload: dict[str, int] | None = None) -> dict[str, int | str]:
    result: dict[str, int | str] = {
        "status": "validated" if upload is None else "uploaded",
        "source_data_rows": bundle.source_data_rows,
        "placeholder_rows": bundle.placeholder_rows,
        "normalized_rows": len(bundle.records),
        "validated_rows": bundle.validated_rows,
        "draft_rows": bundle.draft_rows,
        "joint_rows": bundle.joint_rows,
    }
    if upload is not None:
        result.update(upload)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        bundle = normalize_workbook(args.workbook)
        if args.dry_run:
            print(json.dumps(_summary(bundle), ensure_ascii=False, sort_keys=True))
            return 0
        if not args.base_url:
            raise ImportErrorDetail("적재하려면 --base-url이 필요합니다. 검증만 하려면 --dry-run을 사용하세요.")
        api_key = os.environ.get(args.api_key_env, "")
        private_evidence_token = os.environ.get(args.private_token_env, "")
        uploaded = upload_bundle(
            bundle,
            base_url=args.base_url,
            api_key=api_key,
            private_evidence_token=private_evidence_token,
            batch_size=args.batch_size,
            attest_source_semantics=args.attest_source_semantics,
        )
        print(json.dumps(_summary(bundle, uploaded), ensure_ascii=False, sort_keys=True))
        return 0
    except ImportErrorDetail as exc:
        print(f"private performance import failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
