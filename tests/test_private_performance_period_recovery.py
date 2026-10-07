"""워크북이 정확한 수행기간을 주지 않을 때의 복구 범위.

운영 워크북의 계약기간 칸은 표기가 고르지 않다. 줄바꿈이 숫자를 끊고, 연도에 숫자가
하나 더 붙고, 엑셀이 날짜를 시리얼 숫자로 주고, 기간을 일수로만 적은 칸이 있다.
표기 흔들림은 되돌리고 계약일을 기준으로 기간을 추론하되, 무엇을 채웠는지 남긴다.
추론할 수 없는 칸은 여전히 채우지 않는다.
"""

from __future__ import annotations

from datetime import date

import pytest

from tools import import_private_performance_records as importer

CONTRACT = date(2023, 7, 28)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20220324-2022121\n9", "20220324-20221219"),   # 숫자 한가운데 끊긴 줄바꿈
        ("2024.04.17~\n2024.12.15", "2024.04.17~2024.12.15"),
        ("2022. 4.\n2022. 12.", "2022. 4.~2022. 12."),  # 두 날짜를 끊은 줄바꿈
        ("20222.05.23~2022.11.30", "2022.05.23~2022.11.30"),  # 연도에 숫자가 하나 더
        ("2025.03.21 ~ 202512.15", "2025.03.21 ~ 2025.12.15"),  # 연·월 구분점 누락
    ],
)
def test_repair_restores_notation_without_inventing_dates(raw, expected):
    assert importer.repair_period_text(raw) == expected


def test_repair_keeps_an_exact_period_parseable():
    start, end = importer.parse_period(importer.repair_period_text("20220324-2022121\n9"))
    assert (start, end) == (date(2022, 3, 24), date(2022, 12, 19))


@pytest.mark.parametrize(
    ("raw", "start", "end"),
    [
        ("2021.12~2022.8", date(2021, 12, 1), date(2022, 8, 31)),
        ("23.12~24.01", date(2023, 12, 1), date(2024, 1, 31)),
        ("22.12.~23.08.", date(2022, 12, 1), date(2023, 8, 31)),
        ("2024.02~2024.02", date(2024, 2, 1), date(2024, 2, 29)),  # 윤년 말일
    ],
)
def test_month_only_range_spans_whole_months(raw, start, end):
    assert importer.infer_period(raw, CONTRACT) == (start, end, "MONTH_RANGE")


def test_month_range_needs_no_contract_date():
    assert importer.infer_period("2021.12~2022.8", None)[2] == "MONTH_RANGE"


@pytest.mark.parametrize("raw", ["261일", "계약체결후 266 일", "  90 일 "])
def test_duration_counts_from_the_contract_date(raw):
    start, end, basis = importer.infer_period(raw, CONTRACT)
    assert basis == "CONTRACT_PLUS_DAYS"
    assert start == CONTRACT and end > CONTRACT


@pytest.mark.parametrize("raw", [45701, "20231231", "2023.12.20."])
def test_a_single_date_is_read_as_the_completion_date(raw):
    start, end, basis = importer.infer_period(raw, CONTRACT)
    assert basis == "CONTRACT_TO_SINGLE_DATE"
    assert start == CONTRACT and end >= CONTRACT


@pytest.mark.parametrize(
    "raw",
    [
        None, "", "-",                      # 값이 없다
        "1년",                              # 단위가 기간인지 무엇인지 불명확
        "계약일로부터 채용 완료시까지",       # 날짜가 아니다
        "177",                              # 단위 없는 숫자
        "2023.11.02~2021.01.15",            # 종료가 시작보다 빠르다
    ],
)
def test_an_unresolvable_cell_is_never_filled(raw):
    assert importer.infer_period(raw, CONTRACT) == (None, None, None)


def test_a_single_date_before_the_contract_date_is_refused():
    # 계약보다 이른 날짜를 종료일로 삼으면 실적 기간이 뒤집힌다.
    assert importer.infer_period("2020.01.01", CONTRACT) == (None, None, None)


def test_duration_without_a_contract_date_is_refused():
    assert importer.infer_period("261일", None) == (None, None, None)


@pytest.mark.parametrize("raw", ["0일", "4000일"])
def test_an_implausible_duration_is_refused(raw):
    assert importer.infer_period(raw, CONTRACT) == (None, None, None)


def _row(period: object) -> dict[str, object]:
    return {
        "A": "1", "B": "단독", "C": "SYN 용역", "D": "SYN 개요", "E": "SYN-1",
        "F": "2023.07.28", "G": period, "H": 100_000_000, "I": 100,
        "J": 100_000_000, "K": "한국테스트진흥원", "P": "교육", "Q": "SYN팀",
    }


def _bundle(period: object, *, archive_unresolved: bool = False):
    header = {**importer._EXPECTED_HEADERS, "__row__": 1}
    body = {**_row(period), "__row__": 2}
    return importer.normalize_rows(
        [header, body],
        source_sha256="a" * 64,
        archive_unresolved=archive_unresolved,
    )


def test_an_inferred_period_is_validated_but_recorded_as_inferred():
    record = _bundle("261일").records[0]
    assert record.fields["record_status"] == "VALIDATED"
    assert "INFERRED_PERIOD_CONTRACT_PLUS_DAYS" in record.issues
    assert record.fields["start_date"] == "2023-07-28"


def test_an_exact_period_carries_no_inference_marker():
    record = _bundle("2023.07.28~2023.12.31").records[0]
    assert record.fields["record_status"] == "VALIDATED"
    assert not [item for item in record.issues if item.startswith("INFERRED_PERIOD")]


def test_an_unresolved_row_stays_draft_by_default():
    # 기본값은 fail-closed 다. 미해소 행 하나가 집계를 멈추는 것이 설계다.
    record = _bundle("-").records[0]
    assert record.fields["record_status"] == "DRAFT"
    assert "INVALID_CONTRACT_PERIOD" in record.issues
    assert "ARCHIVED_UNRESOLVED_BY_OPERATOR" not in record.issues


def test_the_operator_can_archive_an_unresolved_row_explicitly():
    record = _bundle("-", archive_unresolved=True).records[0]
    assert record.fields["record_status"] == "ARCHIVED"
    assert "ARCHIVED_UNRESOLVED_BY_OPERATOR" in record.issues
    # 버린 사실이 사유와 함께 남아야 나중에 되짚을 수 있다.
    assert "INVALID_CONTRACT_PERIOD" in record.issues


def test_archiving_never_touches_a_resolved_row():
    record = _bundle("2023.07.28~2023.12.31", archive_unresolved=True).records[0]
    assert record.fields["record_status"] == "VALIDATED"
    assert "ARCHIVED_UNRESOLVED_BY_OPERATOR" not in record.issues
