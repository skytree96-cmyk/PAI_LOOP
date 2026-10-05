from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest
from sqlalchemy import event

from conftest import login_department_reader
from pai_loop import api as api_module
from pai_loop.models import NoticeVersion
from pai_loop.version_payload_headers import HeaderFirstPayload, parse_payload_prefix
from test_dashboard_lean_projection import _FixedDateTime, _seed_history


@pytest.mark.parametrize(("text", "expected"), [
    ('{"kind": "A", "n": 1}', ({"kind": "A", "n": 1}, True)),
    ('{}', ({}, True)),
    ('  {"kind": "A", "big": {"x": [1, 2', ({"kind": "A"}, False)),
    ('{"kind": "A", "n": 12', ({"kind": "A"}, False)),
    ('{"kind": "A", "s": "abc', ({"kind": "A"}, False)),
    ('{"kind": "A", "nested": {"kind": "B"}, "status": "OK", "', ({"kind": "A", "nested": {"kind": "B"}, "status": "OK"}, False)),
    ('{"kind"', ({}, False)),
    ('null', None),
    ('[1, 2]', None),
    ('', None),
    (None, None),
])
def test_prefix_parser_returns_only_complete_members(text, expected):
    assert parse_payload_prefix(text) == expected


def test_header_first_payload_loads_before_anything_but_a_known_header():
    full = {"kind": "K", "status": None, "result": {"blob": "x" * 10}}
    loads = []

    def load():
        loads.append(1)
        lazy._fill(copy.deepcopy(full))

    lazy = HeaderFirstPayload({"kind": "K", "status": None}, load)
    assert isinstance(lazy, dict) and bool(lazy)
    assert lazy.get("kind") == "K" and lazy["kind"] == "K" and lazy.get("status") is None
    assert not loads
    assert lazy.get("result") == {"blob": "x" * 10}
    assert len(loads) == 1
    for probe in (
        lambda p: json.loads(json.dumps(p)), dict, lambda p: {**p}, copy.deepcopy, copy.copy,
        lambda p: dict(p.items()), lambda p: {key: p[key] for key in p},
    ):
        fresh = HeaderFirstPayload({"kind": "K", "status": None}, lambda: None)
        fresh._load = lambda fresh=fresh: fresh._fill(copy.deepcopy(full))
        assert probe(fresh) == full
    other = HeaderFirstPayload({"kind": "K"}, lambda: None)
    other._load = lambda: other._fill(copy.deepcopy(full))
    assert other == full
    member = HeaderFirstPayload({"kind": "K"}, lambda: None)
    member._load = lambda: member._fill(copy.deepcopy(full))
    assert "result" in member and member._loaded
    missing = HeaderFirstPayload({"kind": "K"}, lambda: None)
    missing._load = lambda: missing._fill({"kind": "K"})
    assert missing.get("absent", "fallback") == "fallback" and len(missing) == 1
    missing["kind"] = "changed"
    assert missing == {"kind": "changed"}
    mutable = HeaderFirstPayload({"kind": "K"}, lambda: None)
    mutable._load = lambda: mutable._fill(copy.deepcopy(full))
    assert mutable.setdefault("extra", 1) == 1 and mutable["result"] == {"blob": "x" * 10}


def _add_retries(client, ids, *, stale_newest_every=4):
    """Give each PPS notice superseded attempts; some newest attempts are stale."""
    with client.app.state.session_factory() as session:
        for position, notice_id in enumerate(ids):
            versions = session.query(NoticeVersion).filter_by(notice_id=notice_id).order_by(NoticeVersion.version_no).all()
            attempt = next((v for v in versions if (v.source_payload or {}).get("kind") == "OPENAI_REQUIREMENT_EXTRACTION"), None)
            if attempt is None:
                continue
            next_no = versions[-1].version_no
            for retry in range(3):
                next_no += 1
                payload = copy.deepcopy(attempt.source_payload)
                payload["result"] = {"summary": f"SYN retry {retry}", "blob": "r" * 6000}
                if retry == 2 and position % stale_newest_every == 0:
                    payload["prompt_version"] = "SYN-stale-prompt"
                session.add(NoticeVersion(
                    notice_id=notice_id, version_no=next_no, file_sha256=f"{position:032x}{retry:032x}",
                    source_payload=payload, extraction_status="ACCEPTED", document_complete=True,
                ))
        session.commit()


@pytest.mark.parametrize("prefix_chars", [4096, 700, 300])
@pytest.mark.parametrize("public_view", [False, True])
def test_board_and_dashboard_match_full_payload_reads_with_superseded_attempts(client, monkeypatch, public_view, prefix_chars):
    from pai_loop import version_payload_headers

    monkeypatch.setattr(version_payload_headers, "PREFIX_CHARS", prefix_chars)
    monkeypatch.setattr(api_module, "datetime", _FixedDateTime)
    client.app.state.settings = replace(client.app.state.settings, public_read_only=public_view)
    ids = _seed_history(client, depth=2, blob_size=6000)
    _add_retries(client, ids)
    if public_view:
        login_department_reader(client)
    params = {"limit": 200, "department_id": "organization"}
    with monkeypatch.context() as patch:
        patch.setattr(api_module, "attach_header_first_payloads", _read_everything)
        full_board = client.get("/api/v1/notices", params=params)
        full_dashboard = client.get("/api/v1/dashboard")
    assert full_board.status_code == full_dashboard.status_code == 200
    board = client.get("/api/v1/notices", params=params)
    dashboard = client.get("/api/v1/dashboard")
    assert board.json() == full_board.json()
    assert dashboard.json() == full_dashboard.json()
    stats = {
        part.split("desc=")[1].strip('"') for part in board.headers["server-timing"].split(", ") if part.startswith("payload")
    }
    late = int(next(iter(stats)).split("late=")[1])
    # 700 characters hold the binding header but not the validation inputs, so
    # the fallback must read those attempts in full. 300 characters cut the
    # binding itself, so every attempt is read up front instead.
    assert (late > 0) == (prefix_chars == 700)


def _read_everything(session, notices):
    from sqlalchemy.orm.attributes import set_committed_value

    for notice in notices:
        for version in notice.versions:
            payload = session.query(NoticeVersion.source_payload).filter(NoticeVersion.id == version.id).scalar()
            set_committed_value(version, "source_payload", payload)


def test_superseded_attempts_stay_header_only_unless_a_fallback_needs_them(client, monkeypatch):
    monkeypatch.setattr(api_module, "datetime", _FixedDateTime)
    ids = _seed_history(client, depth=1, blob_size=6000)
    _add_retries(client, ids, stale_newest_every=10**9)
    with client.app.state.session_factory() as session:
        queries = []
        event.listen(session.get_bind(), "before_cursor_execute", lambda *args: queries.append(args[2]))
        notices = api_module._load_board_notice_summary_batch(session, ids[25:45])
        for notice in notices:
            api_module._summary(notice)
        lazies = [v.source_payload for n in notices for v in n.versions if isinstance(v.source_payload, HeaderFirstPayload)]
        assert len(lazies) >= 40
        assert not any(payload._loaded for payload in lazies)
        assert not session.dirty and not session.new
