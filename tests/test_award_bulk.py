from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from pai_loop import award_automation, award_bulk
from pai_loop.award_bulk import BULK_OPERATION, BULK_SOURCE, advance_bulk_sweep
from pai_loop.integrations.pps import PpsApiError
from pai_loop.models import AwardHistoryItem, IngestionJob, Notice
from test_award_automation import add_scope_version

NOW = datetime.now(timezone.utc)


def _award(no, title, *, code="SYN-DEMAND", name="SYN 수요기관", winner="SYN 낙찰사"):
    return {"bidNtceNo": no, "bidNtceOrd": "000", "bidClsfcNo": "0", "rbidNo": "000", "bidNtceNm": title,
            "dminsttCd": code, "dminsttNm": name, "bidwinnrNm": winner, "prtcptCnum": "3",
            "sucsfbidAmt": "98000000", "sucsfbidRate": "88.1", "rlOpengDt": "2025-09-02 11:00:00",
            "fnlSucsfDate": "2025-09-09", "bidwinnrBizno": "PRIVATE-NUMBER", "bidwinnrCeoNm": "PRIVATE-PERSON"}


class _FakePps:
    """First window, first page holds the only rows; every other window is empty."""

    def __init__(self, rows, *, fail_after=None, error=None):
        self.rows, self.fail_after, self.error = rows, fail_after, error
        self.request_count = 0
        self.params: list[dict] = []

    def _request(self, operation, params, *, timeout_seconds=None):
        assert operation == BULK_OPERATION
        self.request_count += 1
        self.params.append(params)
        if self.fail_after is not None and self.request_count > self.fail_after:
            raise self.error
        first = len(self.params) == 1
        items = self.rows if first else []
        return {"response": {"header": {"resultCode": "00"},
                             "body": {"totalCount": len(items), "items": items}}}

    def close(self):
        pass


@pytest.fixture
def bulk(client, monkeypatch):
    settings = replace(client.app.state.settings, pps_api_key="SYN-never-network", environment="production")
    original = award_automation._source_kind
    monkeypatch.setattr(award_automation, "_source_kind",
                        lambda notice: "PPS" if notice.notice_key.startswith("SYN-BULK-") else original(notice))

    def add(key, title, *, code="SYN-DEMAND", name="SYN 수요기관"):
        with client.app.state.session_factory() as session:
            notice = Notice(notice_key=f"SYN-BULK-{key}", bid_notice_no=f"SYN-B{key}", title=title,
                            category="용역", status="OPEN", agency="SYN 공고기관",
                            deadline=NOW + timedelta(days=30), published_at=NOW - timedelta(days=1))
            session.add(notice)
            session.flush()
            add_scope_version(session, notice, demand_agency_name=name, demand_agency_code=code)
            session.commit()
            return notice.id

    def run(fake, now=NOW):
        monkeypatch.setattr(award_bulk, "_client_factory", lambda _settings: fake)
        with client.app.state.session_factory() as session:
            return advance_bulk_sweep(session, settings, now)

    def history():
        with client.app.state.session_factory() as session:
            # Same-agency history only; other-agency candidates are asserted separately.
            return [(row.target_notice_id, row.title, row.winner_name)
                    for row in session.scalars(select(AwardHistoryItem).where(AwardHistoryItem.source == "PPS")
                                               .order_by(AwardHistoryItem.title))]

    def jobs():
        with client.app.state.session_factory() as session:
            return list(session.scalars(select(IngestionJob).where(IngestionJob.source == BULK_SOURCE)
                                        .order_by(IngestionJob.created_at)))

    return add, run, history, jobs, settings


def test_one_sweep_matches_every_notice_by_agency_and_terms_and_finishes(bulk) -> None:
    add, run, history, jobs, _settings = bulk
    party = add("P", "2026년 제1·2차 정당원 해외정책연수")
    school = add("S", "2027학년도 SYN고등학교 2학년 현장체험학습", code="SYN-SCHOOL", name="SYN고등학교")
    rows = [
        _award("A1", "2025년 제1·2차 정당원 해외정책연수"),                 # same agency, all terms
        _award("A2", "2025년 정당원 해외정책연수", code="SYN-OTHER"),          # other agency code
        _award("A3", "2025년 정당원 국내연수"),                              # a term is missing
        _award("A4", "2026학년도 SYN고등학교 2학년 현장체험학습", code="SYN-SCHOOL", name="SYN고등학교"),
        _award("A5", "2025년 정당원 해외정책연수", winner=""),                 # no winner: never stored
    ]
    fake = _FakePps(rows)
    result = run(fake)
    assert result["status"] == "COMPLETED" and result["next"] is None
    assert result["created"] == 2 and result["matched"] == 2
    assert result["other_agency_created"] == 1  # A2: same terms, other agency, kept as its own kind
    assert history() == [(party, "2025년 제1·2차 정당원 해외정책연수", "SYN 낙찰사"),
                         (school, "2026학년도 SYN고등학교 2학년 현장체험학습", "SYN 낙찰사")]
    windows = award_bulk._windows(NOW.astimezone(award_bulk._KST).date())
    assert fake.request_count == len(windows)
    assert fake.params[0]["inqryDiv"] == "3" and fake.params[0]["numOfRows"] == 999
    assert fake.params[-1]["inqryBgnDt"] == "202401010000"
    [job] = jobs()
    assert job.status == "COMPLETED" and job.api_calls == len(windows) and job.created_count == 2
    assert "PRIVATE" not in str(history())
    # A finished sweep does not restart the same KST day.
    assert run(_FakePps(rows))["status"] == "IDLE"


def test_sweep_resumes_from_its_cursor_and_respects_the_daily_cap(bulk, monkeypatch) -> None:
    add, run, history, jobs, _settings = bulk
    add("P", "정당원 해외정책연수")
    monkeypatch.setattr(award_bulk, "DAILY_CALL_CAP", 3)
    first = run(_FakePps([_award("A1", "정당원 해외정책연수")]))
    assert first["calls"] == 3 and first["next"] == {"window": 3, "page": 1, "rows": 999}
    assert run(_FakePps([]))["status"] == "DAILY_LIMIT"
    # Next KST day continues the same sweep from window 3 instead of restarting.
    tomorrow = NOW + timedelta(days=1)
    fake = _FakePps([_award("A1", "정당원 해외정책연수")])
    second = run(fake, tomorrow)
    assert jobs()[-1].request_json["start"] == {"window": 3, "page": 1, "rows": 999}
    assert second["created"] == 0 and len(history()) == 1  # the same award is never duplicated


def test_provider_quota_stops_the_day_and_keeps_committed_pages(bulk) -> None:
    add, run, history, jobs, _settings = bulk
    add("P", "정당원 해외정책연수")
    quota = PpsApiError("quota", error_type="SERVICE_ERROR", provider_code="22")
    result = run(_FakePps([_award("A1", "정당원 해외정책연수")], fail_after=2, error=quota))
    assert result["status"] == "PARTIAL" and len(history()) == 1
    assert jobs()[-1].request_json["next"] == {"window": 2, "page": 1, "rows": 999}
    assert run(_FakePps([]))["status"] == "DAILY_LIMIT"


def test_sweep_is_disabled_outside_production(bulk, client) -> None:
    _add, run, _history, _jobs, settings = bulk
    with client.app.state.session_factory() as session:
        dev = replace(settings, environment="development")
        assert advance_bulk_sweep(session, dev, NOW) == {"status": "DISABLED"}


def test_planner_runs_the_sweep_only_when_w14_will_not_call_run(client, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(award_bulk, "advance_bulk_sweep", lambda *args: calls.append(args) or {})
    body = client.post("/api/v1/operations/award-refresh/plan", json={"refresh_after_days": 30}).json()
    assert body["eligible"] == 0 and len(calls) == 1
    assert set(body) >= {"status", "enrolled", "requeued"} and "bulk" not in str(body)
    assert award_automation._run_follows({"eligible": 1, "running": 0, "api_calls_24h": 900,
                                          "budget_reserved_24h": 0}) is True
    assert award_automation._run_follows({"eligible": 1, "running": 0, "api_calls_24h": 951,
                                          "budget_reserved_24h": 0}) is False


def test_planner_survives_a_sweep_crash(client, monkeypatch) -> None:
    def boom(*_args):
        raise RuntimeError("SYN sweep failure")
    monkeypatch.setattr(award_bulk, "advance_bulk_sweep", boom)
    response = client.post("/api/v1/operations/award-refresh/plan", json={})
    assert response.status_code == 200 and response.json()["status"] == "PLANNED"


class _FakeOpenings:
    def __init__(self, *, fail=()):
        self.fail, self.request_count, self.read = set(fail), 0, []

    def fetch_opening_results(self, *, bid_notice_no, **_kwargs):
        self.request_count += 1
        self.read.append(bid_notice_no)
        if bid_notice_no in self.fail:
            raise PpsApiError("SYN opening failure", error_type="SERVICE_ERROR", provider_code="99")
        return [{"company_name": "SYN 낙찰사", "bid_amount": 97000000.0, "technical_evaluation": 85.0,
                 "price_evaluation": 9.5, "total_evaluation": 94.5, "opening_rank": 1}]

    def close(self):
        pass


def _stored(client, notice_id, no, title, year, *, code="SYN-DEMAND"):
    with client.app.state.session_factory() as session:
        session.add(AwardHistoryItem(
            target_notice_id=notice_id, external_identity=f"{no}|000|0|000", bid_notice_no=no,
            revision_no="000", title=title, agency="SYN 수요기관", demand_agency_code=code,
            winner_name="SYN 낙찰사", similarity_score=50.0, source="PPS",
            awarded_at=datetime(year, 6, 1, tzinfo=timezone.utc)))
        session.commit()


def test_openings_are_read_once_per_displayed_award_by_notice_number(bulk, client, monkeypatch) -> None:
    add, _run, _history, _jobs, settings = bulk
    party = add("P", "2026년 정당원 해외정책연수")
    twin = add("T", "정당원 해외정책연수 추가 공고")  # shares one historical award with party
    _stored(client, party, "Y2025", "2025년 정당원 해외정책연수", 2025)       # same project: shown
    _stored(client, party, "C2025", "정당원 해외정책연수 사전교육", 2025)      # similar, year has same project: hidden
    _stored(client, party, "C2024", "정당원 해외정책연수 사전교육 2024", 2024)  # similar, only row of 2024: shown
    _stored(client, twin, "Y2025", "2025년 정당원 해외정책연수", 2025)        # same award on another notice
    fake = _FakeOpenings()
    monkeypatch.setattr(award_bulk, "_opening_client_factory", lambda _settings: fake)
    with client.app.state.session_factory() as session:
        result = award_bulk.advance_opening_backfill(session, settings, NOW)
    assert sorted(fake.read) == ["C2024", "Y2025"] and result["read"] == 2
    with client.app.state.session_factory() as session:
        rows = {(row.target_notice_id, row.bid_notice_no): row for row in session.scalars(select(AwardHistoryItem))}
    assert rows[(party, "Y2025")].opening_results[0]["total_evaluation"] == 94.5
    assert rows[(twin, "Y2025")].opening_results_status == "COLLECTED"  # written to both notices
    assert rows[(party, "C2025")].opening_results is None                 # never displayed, never read
    with client.app.state.session_factory() as session:
        assert award_bulk.advance_opening_backfill(session, settings, NOW)["status"] == "IDLE"


def test_failed_opening_read_stays_unread_and_is_not_retried(bulk, client, monkeypatch) -> None:
    add, _run, _history, _jobs, settings = bulk
    party = add("P", "정당원 해외정책연수")
    _stored(client, party, "Y2025", "2025년 정당원 해외정책연수", 2025)
    fake = _FakeOpenings(fail={"Y2025"})
    monkeypatch.setattr(award_bulk, "_opening_client_factory", lambda _settings: fake)
    with client.app.state.session_factory() as session:
        award_bulk.advance_opening_backfill(session, settings, NOW)
        row = session.scalar(select(AwardHistoryItem))
        assert row.opening_results is None and row.opening_results_status == "ERROR"
        assert award_bulk.advance_opening_backfill(session, settings, NOW)["status"] == "IDLE"
    assert fake.request_count == 1


def test_no_request_starts_unless_its_full_timeout_fits_in_the_step(bulk, client, monkeypatch) -> None:
    add, _run, _history, _jobs, settings = bulk
    add("P", "정당원 해외정책연수")
    clock = [0.0]

    class _SlowPps(_FakePps):
        def _request(self, operation, params, *, timeout_seconds=None):
            assert timeout_seconds is None  # the client's own full timeout applies
            assert clock[0] + award_bulk.REQUEST_TIMEOUT_SECONDS <= award_bulk.STEP_WALL_SECONDS
            clock[0] += 6.0  # a healthy 999-row page
            return super()._request(operation, params)

    fake = _SlowPps([])
    monkeypatch.setattr(award_bulk, "_client_factory", lambda _settings: fake)
    with client.app.state.session_factory() as session:
        result = advance_bulk_sweep(session, settings, NOW, monotonic=lambda: clock[0])
    windows = len(award_bulk._windows(NOW.astimezone(award_bulk._KST).date()))
    fits = int((award_bulk.STEP_WALL_SECONDS - award_bulk.REQUEST_TIMEOUT_SECONDS) // 6) + 1
    assert result["status"] == "COMPLETED" and fake.request_count == min(windows, fits)


def test_slow_page_timeouts_do_not_stop_the_day_like_real_failures(bulk) -> None:
    add, run, _history, jobs, _settings = bulk
    add("P", "정당원 해외정책연수")
    timeout = PpsApiError("slow", error_type="NETWORK_ERROR")
    for _ in range(award_bulk.DAILY_FAILURE_CAP):
        assert run(_FakePps([], fail_after=0, error=timeout))["status"] == "FAILED"
    # Five slow pages: the sweep keeps going from the same cursor.
    assert run(_FakePps([]))["status"] == "COMPLETED"
    assert jobs()[-1].request_json["start"] == {"window": 0, "page": 1, "rows": 999}


def test_real_failures_still_stop_the_day(bulk) -> None:
    add, run, _history, _jobs, _settings = bulk
    add("P", "정당원 해외정책연수")
    broken = PpsApiError("bad", error_type="INVALID_JSON")
    for _ in range(award_bulk.DAILY_FAILURE_CAP):
        assert run(_FakePps([], fail_after=0, error=broken))["status"] == "FAILED"
    assert run(_FakePps([]))["status"] == "DAILY_LIMIT"


def test_sweep_keeps_other_agency_projects_as_their_own_kind(bulk, client) -> None:
    from pai_loop.award_scope import OTHER_AGENCY_SOURCE
    add, run, _history, _jobs, _settings = bulk
    target = add("I", "정보보호 및 개인정보보호 관리체계 인증(ISMS-P) 컨설팅 용역")
    rows = [
        _award("O1", "KERIS 정보보호 및 개인정보보호 관리체계(ISMS-P) 인증 사전 컨설팅",
               code="SYN-KERIS", name="SYN 교육학술정보원"),
        _award("O2", "정보보호 교육 운영", code="SYN-OTHER", name="SYN 다른기관"),  # one core term only
    ]
    result = run(_FakePps(rows))
    assert result["other_agency_created"] == 1 and result["created"] == 0
    with client.app.state.session_factory() as session:
        stored = list(session.scalars(select(AwardHistoryItem)))
    assert [(row.target_notice_id, row.bid_notice_no, row.source) for row in stored] == [
        (target, "O1", OTHER_AGENCY_SOURCE)]
    assert stored[0].similarity_score >= 30
