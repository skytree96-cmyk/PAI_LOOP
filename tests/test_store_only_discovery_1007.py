"""Store-only discovery after the daily PPS ingestion (2026-10-07): extra notices are saved MANUAL_ONLY, never analysed."""
from datetime import datetime

import pytest
from sqlalchemy import select

from pai_loop.analysis_selection import MANUAL_ONLY_POLICY
from pai_loop.main import create_app
from pai_loop.models import IngestionJob, Notice, NoticeAnalysisPolicy
from test_api import internal_server_client

DAILY = {"X-PAI-Request-Source": "n8n-daily-v4-window8"}
BODY = {"from_date": "2026-10-01", "to_date": "2026-10-08", "keywords": ["교육"], "max_pages": 1, "dry_run": False}


def _row(number, revision="00", title="합성 용역"):
    deadline = datetime.fromisoformat("2026-10-20T17:00:00+09:00")
    return {
        "identity": f"{number}|{revision}|{deadline.isoformat()}", "bid_notice_no": number, "revision_no": revision,
        "title": title, "agency": "공공기관", "published_at": datetime.fromisoformat("2026-10-06T09:00:00+09:00"),
        "deadline": deadline, "estimated_amount": 100_000_000, "notice_kind": "등록공고",
        "source_url": "https://example.go.kr/notice", "raw": {},
    }


class _Client:
    calls: list[dict] = []
    rows_by_query: dict = {}
    fail_extras = False

    def __init__(self, **_kwargs):
        self.request_count = 0
        self.hit_page_limit = False
        self.hit_time_limit = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def iter_notices(self, **kwargs):
        params = dict(kwargs.get("extra_params") or {})
        type(self).calls.append(params)
        self.request_count += 1
        query = params.get("bidNtceNm") or ("업종코드:" + params["indstrytyCd"] if "indstrytyCd" in params else None)
        if query != "교육" and type(self).fail_extras:
            raise RuntimeError("synthetic extra failure")
        yield from type(self).rows_by_query.get(query, [])


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("PPS_API_KEY", "server-side-key")
    monkeypatch.setattr("pai_loop.api.PpsClient", _Client)
    _Client.calls = []
    _Client.fail_extras = False
    _Client.rows_by_query = {
        "교육": [_row("R26BK90000001")],
        "채용": [_row("R26BK90000001"), _row("R26BK90000002", title="신입직원 채용대행")],
        "업종코드:9901": [_row("R26BK90000003", title="성과공유회 행사 대행")],
        "워크숍": [_row("R26BK90000004", revision="01", title="역량강화 워크숍(정정)")],
    }
    return create_app(database_url="sqlite:///:memory:", seed_synthetic=False)


def _post(client, headers=DAILY):
    response = client.post("/api/v1/ingestion/pps/notices", json=BODY, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_daily_run_stores_extra_notices_manual_only_without_changing_its_response(app):
    with internal_server_client(app) as client:
        session = app.state.session_factory()
        session.add(Notice(notice_key="PPS-R26BK90000004-00-existing", bid_notice_no="R26BK90000004", revision_no="00",
                           title="역량강화 워크숍", agency="공공기관",
                           deadline=datetime.fromisoformat("2026-10-20T17:00:00+09:00"), status="OPEN"))
        session.commit()
        body = _post(client)
        keys = {key for key in body["notice_keys"]}
        assert all("R26BK90000001" in key for key in keys)                 # the orchestrator sees only daily terms
        assert all("R26BK90000001" in key for key in body["created_notice_keys"])
        assert any("분석 없이 저장한 관련 공고 2건" in warning for warning in body["warnings"])
        session.expire_all()
        stored = {n.bid_notice_no: n for n in session.scalars(select(Notice))}
        assert {"R26BK90000002", "R26BK90000003"} <= set(stored)
        policies = {p.bid_notice_no: p for p in session.scalars(select(NoticeAnalysisPolicy))}
        assert policies["R26BK90000002"].analysis_policy == MANUAL_ONLY_POLICY
        assert policies["R26BK90000003"].policy_source == "STORE_ONLY_DISCOVERY"
        assert "R26BK90000001" not in policies                              # found by daily terms: stays automatic
        assert "R26BK90000004" not in policies                              # amendment of a stored notice: untouched
        assert {"indstrytyCd": "9901"} in _Client.calls
        assert session.scalar(select(IngestionJob).where(IngestionJob.source == "PPS_STORE_ONLY")).request_json[
            "analysis_requested"] is False
        session.close()


def test_other_callers_and_the_kill_switch_never_run_the_extra_pass(app, monkeypatch):
    with internal_server_client(app) as client:
        _post(client, headers={})
        assert _Client.calls == [{"bidNtceNm": "교육"}]
    monkeypatch.setenv("PAI_LOOP_STORE_ONLY_DISCOVERY", "false")
    off = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    _Client.calls = []
    with internal_server_client(off) as client:
        _post(client)
        assert _Client.calls == [{"bidNtceNm": "교육"}]


def test_an_extra_pass_failure_never_fails_the_daily_run(app):
    _Client.fail_extras = True
    with internal_server_client(app) as client:
        body = _post(client)
        assert body["status"] in {"COMPLETED", "PARTIAL"}
        assert any("건너뛰었습니다" in warning for warning in body["warnings"])
        assert all("R26BK90000001" in key for key in body["notice_keys"])


def test_board_marks_manual_only_notices_and_the_ui_explains_them(app):
    from pathlib import Path
    with internal_server_client(app) as client:
        _post(client)
        rows = client.get("/api/v1/notices", params={"limit": 50}).json()
        flags = {row["bid_notice_no"]: row["analysis_manual_only"] for row in rows}
        assert flags["R26BK90000002"] is True and flags["R26BK90000001"] is False
    script = Path(__file__).resolve().parents[1].joinpath("src", "pai_loop", "static", "app.js").read_text(encoding="utf-8")
    assert "source.analysis_manual_only === true" in script
    assert "MANUAL_ONLY:" in script
