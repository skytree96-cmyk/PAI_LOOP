#!/usr/bin/env python3
"""Prepare a reviewed, local-only roster. No database or network operations."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import import_private_performance_records as xlsx

SHEET = "인력DB"
HEADERS = {
    "B": "입사년월일", "E": "(학사)전공", "F": "(석사)전공",
    "G": "(박사)전공", "H": "최종학력", "I": "자격증",
    "N": "학술연구용역등급(수정)",
}
DEGREE_RANK = {
    "고졸": 0, "학사": 1, "석사(수료)": 1,
    "석사": 2, "박사(수료)": 2, "박사": 3,
}
GRADES = {
    "연구보조원": "RESEARCH_ASSISTANT", "연구원": "RESEARCHER",
    "책임연구원": "LEAD_RESEARCHER",
}
MAX_MEMBERS = 5_000
REPO_ROOT = Path(__file__).resolve().parents[1]
_UNCOMPLETED_MAJOR = re.compile(r"\((?:중퇴|수료|재학|휴학)\)$")


class RosterPreparationError(ValueError):
    """Errors identify a cell/reason, never its potentially private value."""


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _joined_on(value: object, row: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        if Decimal(value).is_finite() and Decimal(value) == int(value):
            parsed = xlsx._excel_date(value)
            if parsed is not None and parsed >= date(1900, 3, 1):
                return parsed
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise RosterPreparationError(f"B{row}: JOIN_DATE_INVALID")


def _source_rows(content: bytes) -> list[dict[str, object]]:
    with xlsx._safe_zip_bytes(content) as archive:
        root = xlsx._xml(archive, xlsx._worksheet_member(archive, SHEET))
        seen_rows: set[int] = set()
        for row in root.findall("m:sheetData/m:row", xlsx._NS):
            try:
                number = int(row.attrib.get("r", "0"))
            except ValueError as exc:
                raise RosterPreparationError("ROW_REFERENCE_INVALID") from exc
            if number <= 0 or number in seen_rows:
                raise RosterPreparationError("ROW_REFERENCE_DUPLICATE_OR_INVALID")
            seen_rows.add(number)
            seen_cells: set[str] = set()
            for cell in row.findall("m:c", xlsx._NS):
                reference = cell.attrib.get("r", "")
                column = xlsx._column_from_reference(reference)
                if reference != f"{column}{number}" or column in seen_cells:
                    raise RosterPreparationError("CELL_REFERENCE_DUPLICATE_OR_INVALID")
                seen_cells.add(column)
                if column in HEADERS and (
                    cell.find("m:f", xlsx._NS) is not None
                    or cell.attrib.get("t") == "e"
                ):
                    raise RosterPreparationError(f"{column}{number}: FORMULA_OR_ERROR_INPUT")
        return xlsx._read_sheet_rows_from_archive(archive, sheet_name=SHEET)


def normalize_roster_rows(
    rows: Sequence[dict[str, object]], *, source_sha256: str,
    snapshot_date: date, verified_through: date,
    confirm_corrected_grades: bool, confirm_employment: bool,
    project_through: date | None = None, confirm_no_change_projection: bool = False,
    confirm_all_regular_employees: bool = False,
    confirm_blank_credentials_none: bool = False,
    confirm_all_qualified_staff_available: bool = False,
) -> dict[str, Any]:
    from pydantic import ValidationError
    from pai_loop.quantitative_personnel import PersonnelRosterEnvelope

    if not confirm_corrected_grades or not confirm_employment:
        raise RosterPreparationError("EXPLICIT_GRADE_AND_EMPLOYMENT_ATTESTATIONS_REQUIRED")
    if verified_through < snapshot_date:
        raise RosterPreparationError("ATTESTATION_DATE_RANGE_INVALID")
    if (project_through is not None) != confirm_no_change_projection:
        raise RosterPreparationError("EXPLICIT_PROJECTION_ATTESTATION_REQUIRED")
    if project_through is not None and project_through <= verified_through:
        raise RosterPreparationError("PROJECTION_MUST_FOLLOW_ATTESTED_PERIOD")
    if not rows or rows[0].get("__row__") != 1:
        raise RosterPreparationError("HEADER_ROW_MISSING")
    for column, expected in HEADERS.items():
        if "".join(_text(rows[0].get(column)).split()) != expected:
            raise RosterPreparationError(f"{column}1: HEADER_MISMATCH")

    members = []
    identities: set[str] = set()
    row_numbers: set[int] = set()
    for row in rows[1:]:
        if not any(_text(value) for key, value in row.items() if key != "__row__"):
            continue
        number = row.get("__row__")
        if not isinstance(number, int) or number <= 1 or number in row_numbers:
            raise RosterPreparationError("ROW_REFERENCE_DUPLICATE_OR_INVALID")
        row_numbers.add(number)
        # Use all source cells only in memory to reject repeated source rows.
        identity = hashlib.sha256(json.dumps(
            {key: _text(value) for key, value in row.items() if key != "__row__"},
            sort_keys=True, ensure_ascii=True,
        ).encode()).hexdigest()
        if identity in identities:
            raise RosterPreparationError(f"ROW_{number}: DUPLICATE_SOURCE_ROW")
        identities.add(identity)
        highest = "".join(_text(row.get("H")).split())
        if highest not in DEGREE_RANK:
            raise RosterPreparationError(f"H{number}: DEGREE_NOT_SUPPORTED")
        rank = DEGREE_RANK[highest]
        degrees = [
            {"level": level, "major": _text(row.get(column)) or None}
            for index, (column, level) in enumerate(
                (("E", "BACHELOR"), ("F", "MASTER"), ("G", "DOCTORATE")), start=1,
            ) if index <= rank
        ]
        if any(degree["major"] and _UNCOMPLETED_MAJOR.search(degree["major"]) for degree in degrees):
            raise RosterPreparationError(f"ROW_{number}: DEGREE_MAJOR_CONTRADICTION")
        if rank == 0 and any(
            _text(row.get(column)) and not _UNCOMPLETED_MAJOR.search(_text(row.get(column)))
            for column in ("E", "F", "G")
        ):
            raise RosterPreparationError(f"ROW_{number}: DEGREE_MAJOR_CONTRADICTION")
        grade = "".join(_text(row.get("N")).split())
        if grade not in GRADES:
            raise RosterPreparationError(f"N{number}: CORRECTED_GRADE_NOT_SUPPORTED")
        joined = _joined_on(row.get("B"), number)
        if joined > snapshot_date:
            raise RosterPreparationError(f"B{number}: JOIN_AFTER_SNAPSHOT")
        credential_text = _text(row.get("I"))
        credentials = list(dict.fromkeys(
            part.strip() for part in credential_text.splitlines() if part.strip()
        ))
        members.append({
            "member_key": hashlib.sha256(f"{source_sha256}:{SHEET}:{number}".encode()).hexdigest(),
            "joined_on": joined.isoformat(), "degrees": degrees,
            "credentials": credentials,
            "credentials_recorded": bool(credential_text) or confirm_blank_credentials_none,
            "research_grade": GRADES[grade],
            "regular_employee": True if confirm_all_regular_employees else None,
        })
        if len(members) > MAX_MEMBERS:
            raise RosterPreparationError("ROSTER_TOO_LARGE")
    if not members:
        raise RosterPreparationError("ROSTER_EMPTY")
    payload = {
        "schema_version": "pai-loop-personnel-roster-1.0",
        "source_sha256": source_sha256,
        "snapshot_date": snapshot_date.isoformat(),
        "verified_through": verified_through.isoformat(),
        "verification_attestation": "HUMAN_REVIEWED_ROSTER_SNAPSHOT",
        "research_grade_basis": "CORRECTED_FINAL", "members": members,
    }
    if project_through is not None:
        payload.update(projection_through=project_through.isoformat(),
                       projection_assumption="CURRENT_ROSTER_UNCHANGED")
    if confirm_all_qualified_staff_available:
        payload["assignment_assumption"] = "ALL_QUALIFIED_ROSTER_MEMBERS_AVAILABLE"
    try:
        return PersonnelRosterEnvelope.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise RosterPreparationError("NORMALIZED_ROSTER_SCHEMA_INVALID") from exc


def prepare_roster(path: Path, **kwargs: Any) -> dict[str, Any]:
    if path.suffix.lower() != ".xlsx":
        raise RosterPreparationError("XLSX_REQUIRED")
    try:
        with path.open("rb") as source:
            content = source.read(xlsx.MAX_SOURCE_BYTES + 1)
        if len(content) > xlsx.MAX_SOURCE_BYTES:
            raise RosterPreparationError("SOURCE_TOO_LARGE")
        rows = _source_rows(content)
    except (OSError, xlsx.ImportErrorDetail) as exc:
        raise RosterPreparationError("WORKBOOK_READ_FAILED") from exc
    return normalize_roster_rows(rows, source_sha256=hashlib.sha256(content).hexdigest(), **kwargs)


def roster_summary(payload: dict[str, Any]) -> dict[str, Any]:
    members = payload["members"]
    return {
        "schema_version": payload["schema_version"],
        "snapshot_date": payload["snapshot_date"], "verified_through": payload["verified_through"],
        "projection_through": payload.get("projection_through"),
        "projection_assumption": payload.get("projection_assumption"),
        "assignment_assumption": payload.get("assignment_assumption"),
        "member_count": len(members),
        "research_grade_counts": dict(Counter(row["research_grade"] for row in members)),
        "unknown_credential_count": sum(not row["credentials_recorded"] for row in members),
        "regular_employee_count": sum(row.get("regular_employee") is True for row in members),
        "unknown_employment_type_count": sum(row.get("regular_employee") is None for row in members),
        "assurance": "USER_ATTESTED_ESTIMATION_INPUT_NOT_CONFIRMED_SCORE",
        "database_writes": 0, "network_calls": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--snapshot-date", type=date.fromisoformat, required=True)
    parser.add_argument("--verified-through", type=date.fromisoformat, required=True)
    parser.add_argument("--confirm-corrected-grades", action="store_true", required=True)
    parser.add_argument("--confirm-employment", action="store_true", required=True)
    parser.add_argument("--confirm-all-regular-employees", action="store_true")
    parser.add_argument("--confirm-blank-credentials-none", action="store_true")
    parser.add_argument("--confirm-all-qualified-staff-available", action="store_true")
    parser.add_argument("--project-through", type=date.fromisoformat)
    parser.add_argument("--confirm-no-change-projection", action="store_true")
    parser.add_argument("--output", type=Path, help="Optional private JSON outside the repository; never overwritten.")
    args = parser.parse_args(argv)
    try:
        output = args.output.resolve() if args.output else None
        if output is not None and (output == REPO_ROOT or REPO_ROOT in output.parents):
            raise RosterPreparationError("PRIVATE_OUTPUT_MUST_STAY_OUTSIDE_REPOSITORY")
        payload = prepare_roster(
            args.workbook, snapshot_date=args.snapshot_date, verified_through=args.verified_through,
            confirm_corrected_grades=args.confirm_corrected_grades,
            confirm_employment=args.confirm_employment,
            confirm_all_regular_employees=args.confirm_all_regular_employees,
            confirm_blank_credentials_none=args.confirm_blank_credentials_none,
            confirm_all_qualified_staff_available=args.confirm_all_qualified_staff_available,
            project_through=args.project_through,
            confirm_no_change_projection=args.confirm_no_change_projection,
        )
        if output is not None:
            with output.open("x", encoding="utf-8") as destination:
                json.dump(payload, destination, ensure_ascii=False, indent=2)
                destination.write("\n")
        print(json.dumps(roster_summary(payload), ensure_ascii=True))
        return 0
    except (RosterPreparationError, OSError) as exc:
        print(str(exc) if isinstance(exc, RosterPreparationError) else "PRIVATE_OUTPUT_WRITE_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
