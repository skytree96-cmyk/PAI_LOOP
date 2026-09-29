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
            return [(row.target_notice_id, row.title, row.winner_name)
                    for row in session.scalars(select(AwardHistoryItem).order_by(AwardHistoryItem.title))]

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
