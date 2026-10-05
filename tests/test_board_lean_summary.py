from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import inspect

from conftest import login_department_reader
from pai_loop import api as api_module
from pai_loop.notice_freshness import latest_current_analysis_run, latest_current_evaluation
from test_dashboard_lean_projection import _FixedDateTime, _seed_history


@pytest.mark.parametrize("public_view", [False, True])
@pytest.mark.parametrize("params", [
    {"limit": 200},
    {"limit": 200, "department_id": "organization"},
    {"limit": 40, "offset": 20, "department_id": "organization", "search_keywords": "SYN"},
])
def test_board_page_matches_the_full_history_graph(client, monkeypatch, public_view, params):
    monkeypatch.setattr(api_module, "datetime", _FixedDateTime)
    client.app.state.settings = replace(client.app.state.settings, public_read_only=public_view)
    _seed_history(client, depth=4, blob_size=256)
    if public_view:
        login_department_reader(client)
    with monkeypatch.context() as patch:
        patch.setattr(api_module, "_load_board_notice_summary_batch", api_module._load_notice_summary_batch)
        full = client.get("/api/v1/notices", params=params)
        assert full.status_code == 200, full.text
    lean = client.get("/api/v1/notices", params=params)
    assert lean.status_code == 200, lean.text
    assert lean.json() == full.json()
    assert any(row["recommendation"] == "HOLD" and row["recommendation_conditions"] for row in lean.json())


def test_board_loader_reads_only_the_current_history_in_full(client):
    ids = _seed_history(client, 30, depth=5, blob_size=4096)
    with client.app.state.session_factory() as session:
        notices = api_module._load_board_notice_summary_batch(session, ids)
        checked = 0
        for notice in notices:
            current = latest_current_evaluation(notice)
            run = latest_current_analysis_run(notice)
            keep = {item.id for item in (current,) if item is not None}
            if run is not None and run.evaluation_id:
                keep.add(run.evaluation_id)
            for evaluation in notice.evaluations:
                unloaded = inspect(evaluation).unloaded
                if evaluation.id in keep:
                    assert not {"atomic_results", "explanation"} & unloaded
                else:
                    assert {"atomic_results", "explanation"} <= unloaded
                    checked += 1
            for other in notice.analysis_runs:
                unloaded = inspect(other).unloaded
                assert {"input_manifest", "output_summary"} <= unloaded
                assert ("recommendations" in unloaded) == (other is not run)
        assert checked > 0
        assert not session.new and not session.dirty and not session.deleted


def test_later_board_pages_reuse_rankings_but_a_flipped_region_gate_does_not(client, monkeypatch):
    from pai_loop import department_ranking as ranking_module

    for index in range(5):
        assert client.post("/api/v1/notices", json={
            "bid_notice_no": f"SYN-CACHE-{index}", "title": f"SYN 교육 운영 용역 {index}",
            "agency": "SYN 기관", "deadline": "2027-12-01T09:00:00Z",
        }).status_code == 201
    calls = 0
    original = ranking_module.rank_notice_for_department

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(ranking_module, "rank_notice_for_department", counted)
    params = {"department_id": "organization", "limit": 2}
    first = client.get("/api/v1/notices", params=params)
    assert first.status_code == 200 and calls > 0
    calls = 0
    second = client.get("/api/v1/notices", params={**params, "offset": 2})
    assert second.status_code == 200 and calls == 0
    assert not {row["notice_key"] for row in first.json()} & {row["notice_key"] for row in second.json()}
    monkeypatch.setenv("PAI_LOOP_REGION_GATE", "off")
    assert client.get("/api/v1/notices", params=params).status_code == 200
    assert calls > 0



def test_board_and_dashboard_report_server_timing_phases(client):
    assert client.post("/api/v1/notices", json={
        "bid_notice_no": "SYN-TIMING", "title": "SYN 교육 운영 용역", "agency": "SYN 기관",
        "deadline": "2027-12-01T09:00:00Z",
    }).status_code == 201
    board = client.get("/api/v1/notices", params={"department_id": "organization"})
    assert {"rank", "load", "rows"} <= {part.split(";")[0] for part in board.headers["server-timing"].split(", ")}
    dashboard = client.get("/api/v1/dashboard")
    assert {"prepare", "load", "authority", "rows", "snapshots"} <= {
        part.split(";")[0] for part in dashboard.headers["server-timing"].split(", ")
    }
