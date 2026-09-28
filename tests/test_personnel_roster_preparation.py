from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape

import pytest

from tools.prepare_personnel_roster import (
    HEADERS, RosterPreparationError, main, normalize_roster_rows, prepare_roster,
)


def _rows():
    return [
        {"__row__": 1, **HEADERS},
        {"__row__": 2, "A": "SYN-PRIVATE-BIRTH", "B": "2020-01-31",
         "C": "SYN-PRIVATE-DEPARTMENT", "D": "SYN-PRIVATE-POSITION",
         "E": "SYN 교육학", "F": "SYN 경영학", "G": "SYN 교육학",
         "H": "박사(수료)", "I": "평생교육사\nSYN 자격", "J": "SYN-PRIVATE-HISTORY",
         "L": 999, "M": "연구보조원", "N": "책임연구원"},
        {"__row__": 3, "A": "SYN-SECOND-PRIVATE-BIRTH", "B": "2021-02-01",
         "E": "SYN 교육학", "F": "SYN 교육학", "H": "석사", "I": None,
         "M": "책임연구원", "N": "연구원"},
    ]


def _kwargs():
    return dict(source_sha256="a" * 64, snapshot_date=date(2026, 8, 10),
                verified_through=date(2026, 9, 28), confirm_corrected_grades=True,
                confirm_employment=True)


def _workbook(path: Path, rows=None, *, formula_column=None, duplicate_cell=False, date1904=False):
    rows = rows if rows is not None else _rows()
    encoded = []
    for row in rows:
        number = row["__row__"]
        cells = []
        for column, value in row.items():
            if column == "__row__" or value is None:
                continue
            reference = f"{column}{number}"
            if isinstance(value, (int, float)):
                cell = f'<c r="{reference}"><v>{value}</v></c>'
            else:
                cell = f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            if number == 2 and column == formula_column:
                cell = f'<c r="{reference}"><f>TODAY()</f><v>45000</v></c>'
            cells.append(cell)
            if number == 2 and column == "B" and duplicate_cell:
                cells.append(cell)
        encoded.append(f'<row r="{number}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    props = '<workbookPr date1904="1"/>' if date1904 else ""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{rel}">{props}<sheets><sheet name="인력DB" sheetId="1" r:id="r1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{"".join(encoded)}</sheetData></worksheet>')


def test_preparation_preserves_completed_degree_linkage_and_corrected_grade_only():
    result = normalize_roster_rows(_rows(), **_kwargs())
    first, second = result["members"]
    assert first["degrees"] == [
        {"level": "BACHELOR", "major": "SYN 교육학"},
        {"level": "MASTER", "major": "SYN 경영학"},
    ]
    assert first["research_grade"] == "LEAD_RESEARCHER"
    assert second["research_grade"] == "RESEARCHER"
    assert first["joined_on"] == "2020-01-31"
    assert not second["credentials_recorded"] and second["credentials"] == []
    assert len({member["member_key"] for member in result["members"]}) == 2
    serialized = json.dumps(result)
    for private in ("SYN-PRIVATE", "SYN-SECOND-PRIVATE", "tenure_months", "degree_level"):
        assert private not in serialized


@pytest.mark.parametrize("mutation,reason", [
    ("grade", "CORRECTED_GRADE_NOT_SUPPORTED"), ("degree", "DEGREE_NOT_SUPPORTED"),
    ("join", "JOIN_DATE_INVALID"), ("future", "JOIN_AFTER_SNAPSHOT"),
    ("duplicate", "DUPLICATE_SOURCE_ROW"), ("row", "ROW_REFERENCE_DUPLICATE_OR_INVALID"),
    ("header", "HEADER_MISMATCH"), ("empty", "ROSTER_EMPTY"),
    ("contradiction", "DEGREE_MAJOR_CONTRADICTION"),
])
def test_bad_inputs_fail_closed_without_revealing_values(mutation, reason):
    rows = _rows()
    if mutation == "grade": rows[1]["N"] = "SYN-PRIVATE-INVALID"
    elif mutation == "degree": rows[1]["H"] = "SYN-PRIVATE-INVALID"
    elif mutation == "join": rows[1]["B"] = "SYN-PRIVATE-INVALID"
    elif mutation == "future": rows[1]["B"] = "2027-01-01"
    elif mutation == "duplicate": rows.append({**deepcopy(rows[1]), "__row__": 4})
    elif mutation == "row": rows[2]["__row__"] = 2
    elif mutation == "header": rows[0]["N"] = "SYN-PRIVATE-INVALID"
    elif mutation == "empty": rows = rows[:1]
    elif mutation == "contradiction": rows[1]["H"] = "고졸"
    with pytest.raises(RosterPreparationError, match=reason) as error:
        normalize_roster_rows(rows, **_kwargs())
    assert "SYN-PRIVATE" not in str(error.value)


@pytest.mark.parametrize("field", ["confirm_corrected_grades", "confirm_employment"])
def test_review_attestation_is_explicit(field):
    options = _kwargs()
    options[field] = False
    with pytest.raises(RosterPreparationError, match="ATTESTATIONS_REQUIRED"):
        normalize_roster_rows(_rows(), **options)


def test_missing_major_is_unknown_not_reused_from_another_degree():
    rows = _rows()
    rows[1]["F"] = None
    result = normalize_roster_rows(rows, **_kwargs())
    assert result["members"][0]["degrees"][1]["major"] is None


def test_explicitly_uncompleted_attendance_does_not_become_a_degree():
    rows = _rows()[:2]
    rows[1].update(H="고졸", E="SYN 전공 (중퇴)", F=None, G=None)
    result = normalize_roster_rows(rows, **_kwargs())
    assert result["members"][0]["degrees"] == []


@pytest.mark.parametrize("annotation", ["중퇴", "수료", "재학", "휴학"])
@pytest.mark.parametrize("degree,column", [("학사", "E"), ("석사", "F"), ("박사", "G")])
def test_degree_award_conflicting_with_uncompleted_major_is_rejected(annotation, degree, column):
    rows = _rows()[:2]
    rows[1]["H"] = degree
    rows[1][column] = f"SYN 교육학({annotation})"
    with pytest.raises(RosterPreparationError, match="DEGREE_MAJOR_CONTRADICTION"):
        normalize_roster_rows(rows, **_kwargs())


def test_future_projection_is_bounded_opt_in_not_extended_verification():
    result = normalize_roster_rows(_rows(), **_kwargs(),
        project_through=date(2026, 10, 6), confirm_no_change_projection=True)
    assert result["verified_through"] == "2026-09-28"
    assert result["projection_through"] == "2026-10-06"
    assert result["projection_assumption"] == "CURRENT_ROSTER_UNCHANGED"


@pytest.mark.parametrize("projection,confirmation", [
    (date(2026, 10, 6), False), (None, True), (date(2026, 9, 28), True),
])
def test_ambiguous_future_attestation_is_rejected(projection, confirmation):
    with pytest.raises(RosterPreparationError, match="PROJECTION"):
        normalize_roster_rows(_rows(), **_kwargs(),
            project_through=projection, confirm_no_change_projection=confirmation)


def test_real_reader_uses_join_date_not_volatile_tenure_cache(tmp_path):
    path = tmp_path / "SYN-roster.xlsx"
    rows = _rows()
    rows[1]["B"] = (date(2020, 1, 31) - date(1899, 12, 30)).days
    _workbook(path, rows, formula_column="L")
    options = _kwargs()
    options.pop("source_sha256")
    result = prepare_roster(path, **options)
    assert result["members"][0]["joined_on"] == "2020-01-31"
    assert len(result["source_sha256"]) == 64


@pytest.mark.parametrize("options,reason", [
    ({"formula_column": "N"}, "FORMULA_OR_ERROR_INPUT"),
    ({"formula_column": "B"}, "FORMULA_OR_ERROR_INPUT"),
    ({"duplicate_cell": True}, "CELL_REFERENCE_DUPLICATE_OR_INVALID"),
    ({"date1904": True}, "WORKBOOK_READ_FAILED"),
])
def test_reader_rejects_ambiguous_workbook_cells(tmp_path, options, reason):
    path = tmp_path / "SYN-roster.xlsx"
    _workbook(path, **options)
    kwargs = _kwargs()
    kwargs.pop("source_sha256")
    with pytest.raises(RosterPreparationError, match=reason):
        prepare_roster(path, **kwargs)


def test_cli_is_local_only_and_default_output_is_aggregate(tmp_path, capsys):
    path = tmp_path / "SYN-roster.xlsx"
    _workbook(path)
    args = [str(path), "--snapshot-date", "2026-08-10", "--verified-through", "2026-09-28",
            "--confirm-corrected-grades", "--confirm-employment"]
    assert main(args) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["member_count"] == 2
    assert "SYN-PRIVATE" not in printed and "members" not in printed
    target = tmp_path / "SYN-private.json"
    assert main([*args, "--output", str(target)]) == 0
    original = target.read_bytes()
    assert main([*args, "--output", str(target)]) == 2
    assert target.read_bytes() == original
    assert main([*args, "--output", str(Path(__file__).parent / "SYN-private.json")]) == 2
    assert not (Path(__file__).parent / "SYN-private.json").exists()
