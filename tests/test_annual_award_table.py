from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from pai_loop.award_intelligence import (
    ANNUAL_AWARD_TABLE_VERSION,
    build_annual_award_table,
    normalise_project_title,
)
from pai_loop.integrations.awards import (
    DEFAULT_OPENING_RESULT_OPERATION,
    PpsAwardClient,
    normalise_opening_result,
)
from pai_loop.integrations.pps import PpsApiError
from pai_loop.main import create_app
from pai_loop.models import AwardHistoryItem, Notice


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
    """A confirmed same-project row must not sit beside similarity guesses."""

    rows = build_annual_award_table(
        [
            _award(title="2024년 SYN 유사 리더십 세미나", year=2024, winner="SYN-기관C"),
            _award(title="2025년 SYN 리더십 교육과정 위탁운영", year=2025, winner="SYN-기관A"),
        ],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert rows["match_basis"] == "SAME_PROJECT_AND_AGENCY"
    assert [item["match_kind"] for item in rows["rows"]] == ["SAME_PROJECT"]
    assert [item["company_name"] for item in rows["rows"]] == ["SYN-기관A"]


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
    assert row["bid_amount"] == 100_000_000.0
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


def test_an_undated_record_is_kept_and_reported_rather_than_dated() -> None:
    table = build_annual_award_table(
        [_award(title="SYN 리더십 교육과정 위탁운영", year=None, winner="SYN-기관A")],
        target_title=TARGET_TITLE,
        target_agency=TARGET_AGENCY,
        as_of=AS_OF,
    )

    assert table["rows"][0]["year"] is None
    assert any("연도를 확정하지 못했습니다" in note for note in table["notes"])


def test_the_notes_separate_bid_technical_score_from_the_quantitative_component() -> None:
    table = build_annual_award_table([], target_title=TARGET_TITLE, as_of=AS_OF)

    assert any("정량평가 항목과 다릅니다" in note for note in table["notes"])


# --- Bounded opening-result adapter ----------------------------------------


def _opening_payload(rows: list[dict[str, object]], total: int | None = None) -> dict[str, object]:
    return {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"totalCount": total if total is not None else len(rows), "items": rows},
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
                    {"prcbdrNm": "", "techEvlVal": "70"},
                ],
                total=999,
            ),
        )

    with PpsAwardClient(
        service_key="server-key",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        companies = client.fetch_opening_results(
            bid_notice_no="SYN-2025-1",
            revision_no="000",
            rows=2,
            max_pages=1,
        )

    assert len(requests) == 1
    assert requests[0].url.path.endswith(DEFAULT_OPENING_RESULT_OPERATION.rsplit("/", 1)[-1])
    assert requests[0].url.params["bidNtceNo"] == "SYN-2025-1"
    assert requests[0].url.params["numOfRows"] == "2"
    # The nameless provider row is dropped rather than stored as a blank bidder.
    assert [item["company_name"] for item in companies] == ["SYN-기관A", "SYN-기관B"]
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
        ({"bid_notice_no": "SYN-1", "max_pages": 0}, "max_pages must be positive"),
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
def award_client(tmp_path) -> TestClient:
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'awards.db').as_posix()}",
        seed_synthetic=False,
    )
    with TestClient(app) as client:
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


def test_a_failed_opening_read_never_overwrites_a_stored_competitor_set(
    award_client: TestClient, monkeypatch: pytest.MonkeyPatch,
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
                "agency": TARGET_AGENCY,
                "winner_name": "SYN-기관A",
                "award_amount": 100_000_000.0,
                "award_rate": None,
                "participant_count": 2,
                "opened_at": None,
                "awarded_at": datetime(2025, 5, 1, tzinfo=timezone.utc),
            }

        def fetch_opening_results(self, **kwargs: object):
            raise PpsApiError("조달청 개찰결과 조회 실패")

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", _FailingOpening)
    response = award_client.post(
        f"/api/v1/notices/{notice_key}/award-history/refresh",
        json={"keyword": "리더십", "years": 1, "include_opening_results": True},
    )

    assert response.status_code == 200, response.text
    assert any("개찰 결과 조회가 실패" in warning for warning in response.json()["warnings"])
    stored = award_client.get(f"/api/v1/notices/{notice_key}/award-history").json()
    kept = next(item for item in stored if item["bid_notice_no"] == "SYN-2025")
    assert kept["opening_results_status"] == "COLLECTED"
    assert len(kept["opening_results"]) == 2


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
