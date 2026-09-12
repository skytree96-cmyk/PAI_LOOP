"""배점표가 있을 법한 첨부를 먼저 읽는지 검사한다.

한 요청의 보강 예산은 첨부 한두 개에서 끝난다. 그래서 읽는 순서가 곧 그 요청이
배점표를 얻는지를 정한다. 저장된 첨부에서 정량표가 나온 비율은 제안요청서 95%,
입찰공고·공고문 26%, 과업지시서 14%였다.
"""

from __future__ import annotations

import pytest

from pai_loop.pps_enrichment import _scoring_table_reading_order


def order(*names: str) -> list[str]:
    items = [
        {"slot": index, "file_name": name} for index, name in enumerate(names, 1)
    ]
    return [
        item["file_name"]
        for item in sorted(items, key=_scoring_table_reading_order)
    ]


def test_the_request_for_proposals_is_read_before_the_notice():
    """배점표는 제안요청서에 실린다. 공고문부터 읽으면 예산이 먼저 끝난다."""

    assert order("붙임1 입찰공고문.pdf", "붙임2 제안요청서.hwpx") == [
        "붙임2 제안요청서.hwpx",
        "붙임1 입찰공고문.pdf",
    ]


def test_the_statement_of_work_waits_its_turn():
    """과업지시서는 과업 수행 방법을 적은 문서라 배점표가 실리는 일이 드물다."""

    assert order("붙임1 과업지시서.hwp", "붙임2 제안요청서.hwpx")[0] == (
        "붙임2 제안요청서.hwpx"
    )
    assert order("붙임1 과업지시서.hwp", "붙임2 입찰공고문.pdf")[0] == (
        "붙임2 입찰공고문.pdf"
    )


def test_an_unnamed_attachment_never_outranks_a_named_one():
    assert order("산출내역서.xlsx", "붙임2 제안요청서.hwpx")[0] == (
        "붙임2 제안요청서.hwpx"
    )


def test_the_manifest_order_survives_inside_one_rank():
    """같은 등급 안에서는 발주처가 매긴 순번을 그대로 지킨다."""

    assert order(
        "붙임3 제안요청서(2).hwpx",
        "붙임1 제안요청서(1).hwpx",
    ) == ["붙임3 제안요청서(2).hwpx", "붙임1 제안요청서(1).hwpx"]
    items = [
        {"slot": 5, "file_name": "제안요청서 B.hwpx"},
        {"slot": 2, "file_name": "제안요청서 A.hwpx"},
    ]
    assert [
        item["file_name"] for item in sorted(items, key=_scoring_table_reading_order)
    ] == ["제안요청서 A.hwpx", "제안요청서 B.hwpx"]


@pytest.mark.parametrize(
    ("name", "rank"),
    [
        ("2026년 용역 제안요청서.hwpx", 0),
        ("입찰공고문.pdf", 1),
        ("긴급 재공고.pdf", 1),
        ("제안서 작성 서식.hwp", 2),
        ("과업지시서.hwp", 3),
        ("산출내역서.xlsx", 3),
    ],
)
def test_each_kind_lands_in_its_own_rank(name, rank):
    """등급은 이름으로 정해진다. 과업지시서는 이름 없는 첨부와 같은 자리다."""

    ranks = sorted(
        {
            _scoring_table_reading_order({"slot": 1, "file_name": value})[0]
            for value in (
                "제안요청서.hwpx",
                "입찰공고문.pdf",
                "제안서 서식.hwp",
                "기타.xlsx",
            )
        }
    )
    actual = _scoring_table_reading_order({"slot": 1, "file_name": name})[0]
    assert ranks.index(actual) == rank
