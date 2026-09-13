from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from conftest import internal_server_client

from pai_loop.award_intelligence import (
    ANNUAL_AWARD_TABLE_VERSION,
    build_annual_award_table,
    normalise_project_title,
)
from pai_loop.integrations.awards import (
    DEFAULT_OPENING_RESULT_OPERATION,
    OpeningResultsIncomplete,
    PpsAwardClient,
    normalise_opening_result,
)
from pai_loop.integrations.pps import PpsApiError
from pai_loop.main import create_app
from pai_loop.models import AwardHistoryItem, Notice
from pai_loop.schemas import AnnualAwardTableOut, AnnualAwardTableRowOut


AS_OF = datetime(2026, 9, 8, tzinfo=timezone.utc)
TARGET_TITLE = "2026년도 SYN 리더십 교육과정 위탁운영"
TARGET_AGENCY = "SYN 발주기관"


def _award(
    *,
    title: str,
    year: int | None,
    winner: str,
    agency: str = TARGET_AGENCY,
    opening_results: list[dict[str, object]] | None = None,
    opening_results_status: str | None = None,
    award_amount: float | None = 100_000_000.0,
    similarity_score: float = 92.0,
) -> dict[str, object]:
    return {
        "bid_notice_no": f"SYN-{year or 'NA'}-{winner}",
        "revision_no": "000",
        "title": title,
        "agency": agency,
        "winner_name": winner,
        "award_amount": award_amount,
        "awarded_at": datetime(year, 5, 1, tzinfo=timezone.utc) if year else None,
        "similarity_score": similarity_score,
        "opening_results": opening_results,
        "opening_results_status": opening_results_status,
    }


def _company(
    name: str,
    *,
    bid: float | None = None,
    technical: float | None = None,
    price: float | None = None,
    total: float | None = None,
    rank: int | None = None,
) -> dict[str, object]:
    return {
        "company_name": name,
        "bid_amount": bid,
        "technical_evaluation": technical,
        "price_evaluation": price,
        "total_evaluation": total,
        "opening_rank": rank,
    }


# --- Title normalisation ----------------------------------------------------


@pytest.mark.parametrize("title", [
    "2026년도 SYN 리더십 교육과정 위탁운영",
    "2025년 SYN 리더십 교육과정 위탁운영",
    "2024 SYN 리더십 교육과정 위탁운영",
    "'24년 SYN 리더십 교육과정 위탁운영",
    "2025년도 SYN 리더십 교육과정 위탁운영(2차)",
    "SYN 리더십 교육과정 위탁운영 제1차",
])
def test_editions_of_one_project_reduce_to_the_same_key(title: str) -> None:
    """Removing the year is what lets 2024/2025/2026 editions match."""

    assert normalise_project_title(title) == normalise_project_title(TARGET_TITLE)


def test_a_different_project_does_not_collapse_into_the_target_key() -> None:
    assert normalise_project_title("2026년도 SYN 안전보건 교육 위탁운영") != normalise_project_title(
        TARGET_TITLE
    )


# --- Table construction -----------------------------------------------------


def test_year_columns_follow_the_evaluation_clock() -> None:
    table = build_annual_award_table([], target_title=TARGET_TITLE, as_of=AS_OF)

    assert table["years"] == [2026, 2025, 2024]
    assert table["table_version"] == ANNUAL_AWARD_TABLE_VERSION
    assert table["match_basis"] == "NONE"
    assert table["rows"] == []


def test_same_project_and_agency_outranks_similar_candidates() -> None:
    """Exact matches suppress candidates only within the same year."""

    rows = build_annual_award_table(
        [
            _award(title="2024년 SYN 유사 리더십 세미나", year=2024, winner="SYN-기관C"),
            _award(title="2025년 SYN 리더십 교육과정 위탁운영", year=2025, winner="SYN-기관A"),
            _award(title="2025년 SYN 유사 세미나", year=2025, winner="SYN-기관B"),
            _award(title="2026년 SYN 유사 세미나", year=2026, winner="SYN-기관D"),
        ],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert rows["match_basis"] == "MIXED_BY_YEAR"
    assert [(item["year"], item["match_kind"], item["company_name"]) for item in rows["rows"]] == [
        (2026, "SIMILAR_CANDIDATE", "SYN-기관D"),
        (2025, "SAME_PROJECT", "SYN-기관A"),
        (2024, "SIMILAR_CANDIDATE", "SYN-기관C"),
    ]


def test_similar_candidates_appear_only_when_no_same_project_row_exists() -> None:
    table = build_annual_award_table(
        [_award(title="2025년 SYN 유사 리더십 세미나", year=2025, winner="SYN-기관C")],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert table["match_basis"] == "SIMILAR_CANDIDATES_ONLY"
    assert [item["match_kind"] for item in table["rows"]] == ["SIMILAR_CANDIDATE"]
    assert any("동일 발주라는 증거가 아닙니다" in note for note in table["notes"])


def test_a_matching_title_at_another_agency_is_only_a_candidate() -> None:
    table = build_annual_award_table(
        [_award(title="2025년 SYN 리더십 교육과정 위탁운영", year=2025, winner="SYN-기관A", agency="SYN 다른기관")],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert table["match_basis"] == "SIMILAR_CANDIDATES_ONLY"


def test_every_opening_company_becomes_a_row_with_its_own_scores() -> None:
    table = build_annual_award_table(
        [
            _award(
                title="2025년 SYN 리더십 교육과정 위탁운영",
                year=2025,
                winner="SYN-기관A",
                opening_results=[
                    _company("SYN-기관B", bid=95_000_000, technical=80.0, price=9.1, total=89.1, rank=2),
                    _company("SYN-기관A", bid=100_000_000, technical=90.0, price=9.5, total=99.5, rank=1),
                ],
                opening_results_status="COLLECTED",
            )
        ],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert table["row_count"] == 2
    assert table["scored_row_count"] == 2
    # The winner comes first, then remaining companies by total evaluation.
    assert [item["company_name"] for item in table["rows"]] == ["SYN-기관A", "SYN-기관B"]
    assert [item["participation_kind"] for item in table["rows"]] == ["WINNER", "PARTICIPANT"]
    assert table["rows"][0]["technical_evaluation"] == 90.0
    assert table["rows"][0]["price_evaluation"] == 9.5
    assert table["rows"][0]["total_evaluation"] == 99.5
    assert table["rows"][1]["bid_amount"] == 95_000_000


def test_response_local_groups_preserve_independent_same_notice_lots_without_private_identity() -> None:
    records = [
        _award(title=TARGET_TITLE, year=2025, winner=winner,
               opening_results=[_company(winner), _company(participant)])
        for winner, participant in (("SYN-A", "SYN-B"), ("SYN-C", "SYN-D"))
    ]
    for index, record in enumerate(records):
        # Same public notice, revision, date, title and agency. The stored
        # results can still refer to different lots or rebids.
        record["bid_notice_no"] = "SYN-SHARED-NOTICE"
        record["id"] = f"SYN-private-row-{index}"
        record["external_identity"] = f"SYN-SHARED-NOTICE|000|{index}|000"
    table = build_annual_award_table(iter(records), target_title=TARGET_TITLE,
                                    target_agency=TARGET_AGENCY, as_of=AS_OF)
    serialized = AnnualAwardTableOut.model_validate(table).model_dump(mode="json")
    groups = {}
    for row in serialized["rows"]:
        groups.setdefault(row["result_group_key"], set()).add(row["company_name"])
        assert "id" not in row and "external_identity" not in row
    assert serialized["table_version"] == "annual-award-table-1.1.0"
    assert groups == {"award-1": {"SYN-A", "SYN-B"}, "award-2": {"SYN-C", "SYN-D"}}
    # Dict fixtures need no IDs at all; backward-compatible schema accepts a
    # previous response without pretending that its rows have a proven group.
    legacy = {key: value for key, value in serialized["rows"][0].items() if key != "result_group_key"}
    assert AnnualAwardTableRowOut.model_validate(legacy).result_group_key is None


@pytest.mark.parametrize("opening_results", [None, []])
def test_unavailable_result_counts_do_not_merge_same_notice_lots(opening_results) -> None:
    records = [_award(title=TARGET_TITLE, year=2025, winner="SYN-A", opening_results=opening_results)
               for _ in range(2)]
    table = build_annual_award_table(records, target_title=TARGET_TITLE, target_agency=TARGET_AGENCY, as_of=AS_OF)
    assert {row["result_group_key"] for row in table["rows"]} == {"award-1", "award-2"}
    if opening_results is None:
        assert table["opening_results_not_collected"] == 2
    else:
        assert any(note.startswith("2건은 개찰 결과 조회에서") for note in table["notes"])


def test_opening_rank_alone_never_names_a_winner() -> None:
    """The award endpoint names the winner; an opening rank does not."""

    table = build_annual_award_table(
        [
            _award(
                title="2025년 SYN 리더십 교육과정 위탁운영",
                year=2025,
                winner="",
                opening_results=[_company("SYN-기관B", rank=1), _company("SYN-기관A", rank=2)],
                opening_results_status="COLLECTED",
            )
        ],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert {item["participation_kind"] for item in table["rows"]} == {"UNKNOWN"}


@pytest.mark.parametrize("column", [
    "technical_evaluation",
    "price_evaluation",
    "total_evaluation",
    "bid_amount",
])
def test_absent_values_stay_none_and_are_never_zeroed_or_derived(column: str) -> None:
    table = build_annual_award_table(
        [
            _award(
                title="2025년 SYN 리더십 교육과정 위탁운영",
                year=2025,
                winner="SYN-기관A",
                award_amount=100_000_000.0,
                opening_results=[_company("SYN-기관A")],
                opening_results_status="COLLECTED",
            )
        ],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert table["rows"][0][column] is None
    assert table["scored_row_count"] == 0
    assert any("0점" in note for note in table["notes"])


def test_an_uncollected_award_shows_the_winner_without_inventing_scores() -> None:
    table = build_annual_award_table(
        [_award(title="2025년 SYN 리더십 교육과정 위탁운영", year=2025, winner="SYN-기관A")],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )
    row = table["rows"][0]

    assert table["opening_results_not_collected"] == 1
    assert row["source_status"] == "NOT_COLLECTED"
    assert row["participation_kind"] == "WINNER"
    assert row["bid_amount"] is None
    assert row["technical_evaluation"] is None
    assert row["price_evaluation"] is None
    assert row["total_evaluation"] is None
    assert any("아직 조회하지 않아" in note for note in table["notes"])


def test_records_outside_the_three_year_window_are_dropped() -> None:
    table = build_annual_award_table(
        [
            _award(title="2023년 SYN 리더십 교육과정 위탁운영", year=2023, winner="SYN-기관A"),
            _award(title="2024년 SYN 리더십 교육과정 위탁운영", year=2024, winner="SYN-기관A"),
        ],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert [item["year"] for item in table["rows"]] == [2024]


def test_an_undated_record_is_reported_but_not_assigned_to_a_year() -> None:
    table = build_annual_award_table(
        [_award(title="SYN 리더십 교육과정 위탁운영", year=None, winner="SYN-기관A")],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert table["rows"] == []
    assert any("연도를 확정하지 못했습니다" in note for note in table["notes"])


def test_the_notes_separate_bid_technical_score_from_the_quantitative_component() -> None:
    table = build_annual_award_table([], target_title=TARGET_TITLE, as_of=AS_OF)

    assert any("정량평가 항목과 다릅니다" in note for note in table["notes"])


def test_annual_year_boundary_uses_korea_time_and_requires_agency() -> None:
    record = _award(title=TARGET_TITLE, year=2024, winner="SYN-A")
    record["awarded_at"] = datetime(2023, 12, 31, 15, tzinfo=timezone.utc)
    table = build_annual_award_table([record], target_title=TARGET_TITLE, as_of=AS_OF)
    assert table["rows"][0]["year"] == 2024
    assert table["rows"][0]["event_date"] == "2024-01-01"
    assert table["match_basis"] == "SIMILAR_CANDIDATES_ONLY"
    january = build_annual_award_table([], as_of=datetime(2026, 12, 31, 15, tzinfo=timezone.utc))
    assert january["years"] == [2027, 2026, 2025]


def test_historical_link_is_identity_bound_and_never_the_target_link() -> None:
    record = _award(title=TARGET_TITLE, year=2025, winner="SYN-A")
    common = dict(target_title=TARGET_TITLE, target_agency=TARGET_AGENCY, as_of=AS_OF)
    wrong = build_annual_award_table([record], notice_source_url="https://example.test/SYN-current", **common)
    assert wrong["rows"][0]["source_notice_url"] is None
    linked = build_annual_award_table([record], historical_notice_urls={
        (record["bid_notice_no"], "000"): "https://example.test/SYN-historical",
    }, **common)
    assert linked["rows"][0]["source_notice_url"] == "https://example.test/SYN-historical"


# --- Bounded opening-result adapter ----------------------------------------


def _opening_payload(rows: list[dict[str, object]], total: int | None = None) -> dict[str, object]:
    items = [{
        "bidNtceNo": "SYN-2025-1", "bidNtceOrd": "000",
        "bidClsfcNo": "0", "rbidNo": "000", **row,
    } for row in rows]
    return {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"totalCount": total if total is not None else len(rows), "items": items},
        }
    }


def test_opening_result_normalisation_reads_confirmed_score_fields() -> None:
    company = normalise_opening_result({
        "prcbdrNm": "SYN-기관A",
        "bidprcAmt": "100,000,000",
        "techEvlVal": "90.0",
        "bidPrceEvlVal": "9.5",
        "totalEvlAmtVal": "99.5",
        "opengRank": "1",
    })

    assert company == {
        "company_name": "SYN-기관A",
        "bid_amount": 100_000_000.0,
        "technical_evaluation": 90.0,
        "price_evaluation": 9.5,
        "total_evaluation": 99.5,
        "opening_rank": 1,
    }


def test_opening_result_normalisation_leaves_empty_provider_fields_missing() -> None:
    """techEvlNaturVal and the bid rate came back empty in the observed call."""

    company = normalise_opening_result({
        "prcbdrNm": "SYN-기관A",
        "techEvlVal": "",
        "bidPrceEvlVal": None,
        "techEvlNaturVal": "",
    })

    assert company["technical_evaluation"] is None
    assert company["price_evaluation"] is None
    assert company["total_evaluation"] is None
    assert company["bid_amount"] is None


def test_opening_fields_do_not_accept_award_amount_remark_or_guessed_aliases() -> None:
    company = normalise_opening_result({
        "bidwinnrNm": "SYN-WINNER", "cmpnyNm": "SYN-COMPANY",
        "sucsfbidAmt": "123", "bidPrceAmt": "456", "rmrk": "1",
        "techEvlNaturVal": "90", "techEvlVal": "NaN",
        "bidPrceEvlVal": "Infinity", "totalEvlAmtVal": "-1",
    })
    assert company["company_name"] == ""
    assert all(value is None for key, value in company.items() if key != "company_name")


@pytest.mark.parametrize("rows,total", [
    ([{"prcbdrNm": ""}], 1),
    ([{"prcbdrNm": "SYN-A", "bidNtceNo": "SYN-WRONG"}], 1),
    ([{"prcbdrNm": "SYN-A", "bidNtceOrd": "001"}], 1),
    ([{"prcbdrNm": "SYN-A"}, {"prcbdrNm": "SYN-A"}], 2),
    ([{"prcbdrNm": "SYN-A"}], 2),
    ([], 1),
    ([], -1),
])
def test_incomplete_company_sets_raise_instead_of_returning_partial_rows(rows, total) -> None:
    with PpsAwardClient(service_key="SYN-key", base_url="https://example.test", max_retries=0,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_opening_payload(rows, total)))) as client:
        with pytest.raises(OpeningResultsIncomplete):
            client.fetch_opening_results(bid_notice_no="SYN-2025-1")
        assert client.request_count == 1


def test_empty_opening_requires_a_valid_explicit_zero_envelope() -> None:
    payloads = [_opening_payload([]), {"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 0}}}]
    with PpsAwardClient(service_key="SYN-key", base_url="https://example.test", max_retries=0,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payloads.pop(0)))) as client:
        assert client.fetch_opening_results(bid_notice_no="SYN-2025-1") == []
        with pytest.raises(OpeningResultsIncomplete):
            client.fetch_opening_results(bid_notice_no="SYN-2025-1")


def test_opening_deadline_stops_before_any_request() -> None:
    with PpsAwardClient(service_key="SYN-key", base_url="https://example.test",
                        transport=httpx.MockTransport(lambda request: pytest.fail("request after deadline"))) as client:
        with pytest.raises(OpeningResultsIncomplete):
            client.fetch_opening_results(bid_notice_no="SYN-2025-1", deadline_monotonic=0)
        assert client.request_count == 0
        assert client.hit_time_limit


@pytest.mark.parametrize("second_total", [2, 3])
def test_opening_pagination_checks_total_and_resets_per_notice_flags(second_total: int) -> None:
    payloads = [_opening_payload([{"prcbdrNm": "SYN-A"}], 2), _opening_payload([{"prcbdrNm": "SYN-B"}], second_total)]
    with PpsAwardClient(service_key="SYN-key", base_url="https://example.test", max_retries=0,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payloads.pop(0)))) as client:
        client.hit_page_limit = True
        client.hit_time_limit = True
        if second_total == 2:
            result = client.fetch_opening_results(bid_notice_no="SYN-2025-1", rows=1, max_pages=2)
            assert [company["company_name"] for company in result] == ["SYN-A", "SYN-B"]
        else:
            with pytest.raises(OpeningResultsIncomplete):
                client.fetch_opening_results(bid_notice_no="SYN-2025-1", rows=1, max_pages=2)
        assert client.request_count == 2
        assert not client.hit_page_limit and not client.hit_time_limit


def test_opening_result_client_reads_one_notice_within_its_page_cap() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_opening_payload(
                [
                    {"prcbdrNm": "SYN-기관A", "techEvlVal": "90", "bidPrceEvlVal": "9.5", "totalEvlAmtVal": "99.5"},
                    {"prcbdrNm": "SYN-기관B", "techEvlVal": "80", "bidPrceEvlVal": "9.1", "totalEvlAmtVal": "89.1"},
                ],
                total=999,
            ),
        )

    with PpsAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(OpeningResultsIncomplete, match="페이지 제한"):
            client.fetch_opening_results(
                bid_notice_no="SYN-2025-1", revision_no="000", rows=2, max_pages=1,
            )

    assert len(requests) == 1
    assert requests[0].url.path.endswith(DEFAULT_OPENING_RESULT_OPERATION.rsplit("/", 1)[-1])
    assert requests[0].url.params["bidNtceNo"] == "SYN-2025-1"
    assert requests[0].url.params["numOfRows"] == "2"
    assert "inqryDiv" not in requests[0].url.params
    assert client.hit_page_limit is True


def test_opening_result_client_stops_on_a_single_page_of_results() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=_opening_payload([{"prcbdrNm": "SYN-기관A"}]))

    with PpsAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        companies = client.fetch_opening_results(bid_notice_no="SYN-2025-1", max_pages=3)

    assert calls["count"] == 1
    assert len(companies) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bid_notice_no": "  "}, "bid_notice_no is required"),
        ({"bid_notice_no": "SYN-1", "rows": 0}, "rows must be between 1 and 999"),
        ({"bid_notice_no": "SYN-1", "max_pages": 0}, "max_pages must be between 1 and 3"),
        ({"bid_notice_no": "SYN-1", "max_pages": 4}, "max_pages must be between 1 and 3"),
    ],
)
def test_opening_result_client_rejects_unbounded_arguments(
    kwargs: dict[str, object], message: str,
) -> None:
    with PpsAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_opening_payload([]))),
    ) as client:
        with pytest.raises(ValueError, match=message):
            client.fetch_opening_results(**kwargs)


# --- API integration --------------------------------------------------------


@pytest.fixture()
def award_client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return AS_OF.astimezone(tz) if tz else AS_OF.replace(tzinfo=None)

    monkeypatch.setattr("pai_loop.api.datetime", _Clock)
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'awards.db').as_posix()}",
        seed_synthetic=False,
    )
    with internal_server_client(app) as client:
        yield client


def _stored_notice_with_awards(client: TestClient) -> str:
    notice_key = "SYN-AWARD-TABLE-001"
    created = client.post(
        "/api/v1/notices",
        json={
            "notice_key": notice_key,
            "bid_notice_no": "SYN-AWARD-TABLE-001-NO",
            "title": TARGET_TITLE,
            "agency": TARGET_AGENCY,
            "deadline": "2026-10-01T09:00:00Z",
            "published_at": AS_OF.isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    with client.app.state.session_factory() as session:
        notice = session.query(Notice).filter(Notice.notice_key == notice_key).one()
        session.add_all([
            AwardHistoryItem(
                target_notice_id=notice.id,
                external_identity="SYN-2025|000|0|000",
                bid_notice_no="SYN-2025",
                title="2025년 SYN 리더십 교육과정 위탁운영",
                agency=TARGET_AGENCY,
                winner_name="SYN-기관A",
                award_amount=100_000_000.0,
                awarded_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
                similarity_score=95.0,
                opening_results=[
                    _company("SYN-기관A", bid=100_000_000, technical=90.0, price=9.5, total=99.5, rank=1),
                    _company("SYN-기관B", bid=95_000_000, technical=80.0, total=89.1, rank=2),
                ],
                opening_results_status="COLLECTED",
            ),
            AwardHistoryItem(
                target_notice_id=notice.id,
                external_identity="SYN-2024|000|0|000",
                bid_notice_no="SYN-2024",
                title="2024년 SYN 리더십 교육과정 위탁운영",
                agency=TARGET_AGENCY,
                winner_name="SYN-기관A",
                award_amount=90_000_000.0,
                awarded_at=datetime(2024, 5, 1, tzinfo=timezone.utc),
                similarity_score=93.0,
            ),
        ])
        session.commit()
    return notice_key


def test_award_intelligence_serves_the_annual_table_without_remote_calls(
    award_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public read is stored-only: any client construction would fail here."""

    notice_key = _stored_notice_with_awards(award_client)

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the public award table must not construct a PPS client")

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _forbidden)
    response = award_client.get(f"/api/v1/notices/{notice_key}/award-intelligence")

    assert response.status_code == 200, response.text
    table = response.json()["annual_award_table"]
    assert table["years"] == [2026, 2025, 2024]
    assert table["match_basis"] == "SAME_PROJECT_AND_AGENCY"
    assert table["opening_results_not_collected"] == 1
    assert [(row["year"], row["company_name"], row["participation_kind"]) for row in table["rows"]] == [
        (2025, "SYN-기관A", "WINNER"),
        (2025, "SYN-기관B", "PARTICIPANT"),
        (2024, "SYN-기관A", "WINNER"),
    ]
    # The 2024 row was never opened, so its evaluation columns stay missing.
    assert table["rows"][2]["technical_evaluation"] is None
    assert table["rows"][2]["source_status"] == "NOT_COLLECTED"
    # A partially reported company keeps the one score the provider gave.
    assert table["rows"][1]["price_evaluation"] is None
    assert table["rows"][1]["technical_evaluation"] == 80.0


def test_api_serializes_separate_same_notice_result_groups(award_client: TestClient) -> None:
    notice_key = _stored_notice_with_awards(award_client)
    with award_client.app.state.session_factory() as session:
        target = session.query(Notice).filter(Notice.notice_key == notice_key).one()
        session.add(AwardHistoryItem(target_notice_id=target.id,
            external_identity="SYN-2025|000|1|001", bid_notice_no="SYN-2025", revision_no="000",
            title="2025년 SYN 리더십 교육과정 위탁운영", agency=TARGET_AGENCY,
            winner_name="SYN-기관C", awarded_at=datetime(2025, 5, 1, tzinfo=timezone.utc), similarity_score=93,
            opening_results=[_company("SYN-기관C"), _company("SYN-기관D")], opening_results_status="COLLECTED"))
        session.commit()
    response = award_client.get(f"/api/v1/notices/{notice_key}/award-intelligence")
    assert response.status_code == 200, response.text
    table = response.json()["annual_award_table"]
    groups = {}
    for row in table["rows"]:
        assert row["result_group_key"].startswith("award-")
        if row["year"] == 2025:
            groups.setdefault(row["result_group_key"], set()).add(row["company_name"])
    assert len(groups) == 2
    assert {frozenset(names) for names in groups.values()} == {
        frozenset({"SYN-기관A", "SYN-기관B"}), frozenset({"SYN-기관C", "SYN-기관D"})}


def test_stored_award_history_exposes_opening_results_as_nullable(
    award_client: TestClient,
) -> None:
    notice_key = _stored_notice_with_awards(award_client)

    items = award_client.get(f"/api/v1/notices/{notice_key}/award-history").json()
    by_notice = {item["bid_notice_no"]: item for item in items}

    assert by_notice["SYN-2024"]["opening_results"] is None
    assert by_notice["SYN-2024"]["opening_results_status"] is None
    assert by_notice["SYN-2025"]["opening_results_status"] == "COLLECTED"
    assert len(by_notice["SYN-2025"]["opening_results"]) == 2
    assert by_notice["SYN-2025"]["opening_results"][1]["price_evaluation"] is None


def test_api_annual_table_uses_current_year_and_only_historical_notice_urls(award_client: TestClient) -> None:
    key = _stored_notice_with_awards(award_client)
    with award_client.app.state.session_factory() as session:
        target = session.query(Notice).filter(Notice.notice_key == key).one()
        target.published_at = datetime(2022, 1, 1, tzinfo=timezone.utc)
        target.source_url = "https://example.test/SYN-current"
        session.add(Notice(notice_key="SYN-PAST", bid_notice_no="SYN-2025", revision_no="00",
                           title="SYN old notice", deadline=AS_OF, source_url="https://example.test/SYN-2025"))
        session.commit()
    table = award_client.get(f"/api/v1/notices/{key}/award-intelligence").json()["annual_award_table"]
    assert table["years"] == [2026, 2025, 2024]
    assert {row["source_notice_url"] for row in table["rows"]} == {"https://example.test/SYN-2025", None}
    assert len(table["rows"]) == 3
    with award_client.app.state.session_factory() as session:
        session.add(Notice(notice_key="SYN-PAST-AMBIGUOUS", bid_notice_no="SYN-2025", revision_no="000",
                           title="SYN other source", deadline=AS_OF, source_url="https://example.test/SYN-other"))
        session.commit()
    table = award_client.get(f"/api/v1/notices/{key}/award-intelligence").json()["annual_award_table"]
    assert all(row["source_notice_url"] is None for row in table["rows"])


def test_refresh_leaves_opening_results_uncollected_unless_explicitly_requested(
    award_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default refresh keeps its existing call bound: no extra requests."""

    notice_key = _stored_notice_with_awards(award_client)
    award_client.app.state.settings = replace(
        award_client.app.state.settings, pps_api_key="server-side-key"
    )
    opening_calls: list[str] = []

    class _StubClient:
        request_count = 1
        hit_page_limit = False
        hit_time_limit = False
        fallback_window_count = 0
        window_errors: list[str] = []

        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_StubClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def iter_awards(self, **kwargs: object):
            yield {
                "identity": "SYN-2025|000|0|000",
                "bid_notice_no": "SYN-2025",
                "revision_no": "000",
                "classification_no": "0",
                "rebid_no": "000",
                "title": "2025년 SYN 리더십 교육과정 위탁운영",
                "agency": TARGET_AGENCY,
                "winner_name": "SYN-기관A",
                "award_amount": 100_000_000.0,
                "award_rate": None,
                "participant_count": 2,
                "opened_at": None,
                "awarded_at": datetime(2025, 5, 1, tzinfo=timezone.utc),
            }

        def fetch_opening_results(self, *, bid_notice_no: str, **kwargs: object):
            opening_calls.append(bid_notice_no)
            return [_company("SYN-기관A", technical=90.0, price=9.5, total=99.5)]

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _StubClient)

    default_run = award_client.post(
        f"/api/v1/notices/{notice_key}/award-history/refresh",
        json={"keyword": "리더십", "years": 1},
    )
    assert default_run.status_code == 200, default_run.text
    assert opening_calls == []

    opted_in = award_client.post(
        f"/api/v1/notices/{notice_key}/award-history/refresh",
        json={
            "keyword": "리더십",
            "years": 1,
            "include_opening_results": True,
            "max_opening_result_notices": 5,
        },
    )
    assert opted_in.status_code == 200, opted_in.text
    assert opening_calls == ["SYN-2025"]
    assert any("개찰 결과를 1건 조회했습니다" in warning for warning in opted_in.json()["warnings"])


@pytest.mark.parametrize("failure", ["ERROR", "PARTIAL", "EMPTY"])
def test_a_failed_opening_read_never_overwrites_a_stored_competitor_set(
    award_client: TestClient, monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    notice_key = _stored_notice_with_awards(award_client)
    award_client.app.state.settings = replace(
        award_client.app.state.settings, pps_api_key="server-side-key"
    )

    class _FailingOpening:
        request_count = 1
        hit_page_limit = False
        hit_time_limit = False
        fallback_window_count = 0
        window_errors: list[str] = []

        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_FailingOpening":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def iter_awards(self, **kwargs: object):
            yield {
                "identity": "SYN-2025|000|0|000",
                "bid_notice_no": "SYN-2025",
                "revision_no": "000",
                "classification_no": "0",
                "rebid_no": "000",
                "title": "2025년 SYN 리더십 교육과정 위탁운영",
                "agency": "",
                "winner_name": "SYN-기관A",
                "award_amount": None,
                "award_rate": None,
                "participant_count": 2,
                "opened_at": None,
                "awarded_at": None,
            }

        def fetch_opening_results(self, **kwargs: object):
            if failure == "EMPTY":
                return []
            if failure == "PARTIAL":
                raise OpeningResultsIncomplete("SYN incomplete page")
            raise PpsApiError("조달청 개찰결과 조회 실패")

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _FailingOpening)
    response = award_client.post(
        f"/api/v1/notices/{notice_key}/award-history/refresh",
        json={"keyword": "리더십", "years": 1, "include_opening_results": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PARTIAL"
    assert any("기존 저장본을 유지" in warning for warning in response.json()["warnings"])
    stored = award_client.get(f"/api/v1/notices/{notice_key}/award-history").json()
    kept = next(item for item in stored if item["bid_notice_no"] == "SYN-2025")
    assert kept["opening_results_status"] == ("ERROR" if failure == "ERROR" else "PARTIAL")
    assert len(kept["opening_results"]) == 2
    assert kept["award_amount"] == 100_000_000.0
    assert kept["agency"] == TARGET_AGENCY
    assert kept["awarded_at"].startswith("2025-05-01")


@pytest.mark.parametrize("failure_flag", ["hit_page_limit", "hit_time_limit", "hit_incomplete_response", "window_errors"])
def test_award_collection_failure_flags_never_report_full_completion(award_client: TestClient, monkeypatch, failure_flag: str) -> None:
    key = _stored_notice_with_awards(award_client)
    award_client.app.state.settings = replace(award_client.app.state.settings, pps_api_key="SYN-key")

    class _Partial:
        request_count = 1
        hit_page_limit = False
        hit_time_limit = False
        hit_incomplete_response = False
        fallback_window_count = 0
        window_errors = []

        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            setattr(self, failure_flag, ["SYN-window"] if failure_flag == "window_errors" else True)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_awards(self, **kwargs):
            assert str(kwargs["start"]) == "2024-01-01"
            assert str(kwargs["end"]) == "2026-09-08"
            return iter(())

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _Partial)
    response = award_client.post(f"/api/v1/notices/{key}/award-history/refresh", json={"keyword": "SYN 교육"})
    assert response.json()["status"] == "PARTIAL"
    assert len(award_client.get(f"/api/v1/notices/{key}/award-history").json()) == 2


def test_opening_notice_and_page_caps_are_counted_without_hidden_retries(award_client: TestClient, monkeypatch) -> None:
    key = _stored_notice_with_awards(award_client)
    award_client.app.state.settings = replace(award_client.app.state.settings, pps_api_key="SYN-key")
    calls = []

    class _Bounded:
        request_count = 0
        hit_page_limit = False
        hit_time_limit = False
        fallback_window_count = 0
        window_errors = []

        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_awards(self, **kwargs):
            self.request_count = 1
            for index in range(3):
                yield {"identity": f"SYN-{index}", "bid_notice_no": f"SYN-{index}",
                       "title": TARGET_TITLE, "winner_name": "SYN-A", "agency": TARGET_AGENCY,
                       "awarded_at": AS_OF}

        def fetch_opening_results(self, **kwargs):
            calls.append(kwargs)
            self.request_count += 1
            return [_company("SYN-A")]

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _Bounded)
    response = award_client.post(f"/api/v1/notices/{key}/award-history/refresh", json={
        "keyword": "SYN 교육", "include_opening_results": True,
        "max_opening_result_notices": 2, "opening_result_max_pages": 1,
    })
    assert response.json()["status"] == "PARTIAL"
    assert response.json()["api_calls"] == 3
    assert len(calls) == 2
    assert all(call["max_pages"] == 1 for call in calls)
    for field, value in [("max_opening_result_notices", 31), ("opening_result_max_pages", 4), ("years", 4)]:
        rejected = award_client.post(f"/api/v1/notices/{key}/award-history/refresh", json={field: value})
        assert rejected.status_code == 422


def test_dry_run_refresh_never_calls_the_opening_endpoint(
    award_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_key = _stored_notice_with_awards(award_client)
    award_client.app.state.settings = replace(
        award_client.app.state.settings, pps_api_key="server-side-key"
    )

    class _NoOpening:
        request_count = 1
        hit_page_limit = False
        hit_time_limit = False
        fallback_window_count = 0
        window_errors: list[str] = []

        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_NoOpening":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def iter_awards(self, **kwargs: object):
            return iter(())

        def fetch_opening_results(self, **kwargs: object):
            raise AssertionError("dry_run must not call the opening endpoint")

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _NoOpening)
    response = award_client.post(
        f"/api/v1/notices/{notice_key}/award-history/refresh",
        json={"keyword": "리더십", "years": 1, "dry_run": True, "include_opening_results": True},
    )

    assert response.status_code == 200, response.text
    assert any("dry_run이므로 개찰 결과 조회를 실행하지 않았습니다" in w for w in response.json()["warnings"])


@pytest.mark.parametrize("mode", [
    "bid_amount", "technical_evaluation", "price_evaluation", "total_evaluation", "opening_rank",
    "zero", "correction", "different_company", "revision", "rebid",
])
def test_opening_numeric_refresh_preserves_whole_snapshot_only_on_missing_fields(
    award_client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    key = _stored_notice_with_awards(award_client)
    award_client.app.state.settings = replace(award_client.app.state.settings, pps_api_key="SYN-key")
    with award_client.app.state.session_factory() as session:
        stored = session.query(AwardHistoryItem).filter(AwardHistoryItem.bid_notice_no == "SYN-2025").one()
        stored.opening_results_read_at = datetime(2026, 9, 7, tzinfo=timezone.utc)
        session.commit()
    before = next(row for row in award_client.get(f"/api/v1/notices/{key}/award-history").json()
                  if row["bid_notice_no"] == "SYN-2025")
    incoming = [dict(company) for company in reversed(before["opening_results"])]
    company_a = next(company for company in incoming if company["company_name"] == "SYN-기관A")
    missing = mode in {"bid_amount", "technical_evaluation", "price_evaluation", "total_evaluation", "opening_rank"}
    if missing:
        company_a[mode] = None
        # A second company's corrected value must not be spliced into the old
        # snapshot when a different company's numeric field disappeared.
        incoming[0]["technical_evaluation"] = 81.0
    elif mode == "zero":
        for field in ("bid_amount", "technical_evaluation", "price_evaluation", "total_evaluation"):
            company_a[field] = 0.0
    elif mode == "correction":
        company_a.update(bid_amount=123_456_789.0, technical_evaluation=91.0, price_evaluation=8.5, total_evaluation=99.5)
    else:
        incoming = [_company("SYN-기관C" if mode == "different_company" else "SYN-기관A")]
    revision = "001" if mode == "revision" else "000"
    rebid = "001" if mode == "rebid" else "000"
    identity = f"SYN-2025|{revision}|0|{rebid}"

    class _Refresh:
        request_count = 1
        hit_page_limit = False
        hit_time_limit = False
        fallback_window_count = 0
        window_errors = []

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_awards(self, **kwargs):
            yield {"identity": identity, "bid_notice_no": "SYN-2025", "revision_no": revision,
                   "classification_no": "0", "rebid_no": rebid, "title": TARGET_TITLE,
                   "agency": TARGET_AGENCY, "winner_name": "SYN-기관A",
                   "awarded_at": datetime(2025, 5, 1, tzinfo=timezone.utc)}

        def fetch_opening_results(self, **kwargs):
            assert kwargs["revision_no"] == revision and kwargs["rebid_no"] == rebid
            return incoming

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _Refresh)
    response = award_client.post(f"/api/v1/notices/{key}/award-history/refresh", json={
        "keyword": "SYN 교육", "include_opening_results": True,
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ("PARTIAL" if missing else "COMPLETED")
    after = next(row for row in award_client.get(f"/api/v1/notices/{key}/award-history").json()
                 if row["id"] == before["id"])
    if missing or mode in {"revision", "rebid"}:
        assert after["opening_results"] == before["opening_results"]
        assert after["opening_results_read_at"] == before["opening_results_read_at"]
        assert after["opening_results_status"] == ("PARTIAL" if missing else "COLLECTED")
        if missing:
            assert any("스냅샷 전체를 유지" in warning for warning in response.json()["warnings"])
        else:
            with award_client.app.state.session_factory() as session:
                other = session.query(AwardHistoryItem).filter(AwardHistoryItem.external_identity == identity).one()
                assert other.id != before["id"]
                assert other.opening_results == incoming
    else:
        assert after["opening_results"] == incoming
        assert after["opening_results_status"] == "COLLECTED"
        assert after["opening_results_read_at"] != before["opening_results_read_at"]
