"""Historical display cannot replace current scoring or expose private basis."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from conftest import login_department_reader
from test_quantitative_public_snapshot import (
    _public_app, _notice, _version, _run, _quantitative_snapshot,
    _public_criteria_snapshot, PRIVATE_BASIS_MARKER, PRIVATE_RULESET,
)


@pytest.mark.parametrize("case", ["same", "changed", "invalid", "absent", "current", "private", "partial"])
def test_saved_history_is_separate_sanitised_and_never_current(monkeypatch, case):
    app = _public_app(monkeypatch)
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with TestClient(app) as client:
        login_department_reader(client)
        with app.state.session_factory() as session:
            notice = _notice(notice_key="SYN-SAVED-HISTORY")
            metadata = _version(notice, version_no=1, kind="PPS_NOTICE_METADATA", digest_character="1")
            manifest = [{"attachment_id": "SYN-ATT", "file_name": "SYN.txt"}]
            metadata.source_payload = {**metadata.source_payload, "attachment_manifest": manifest}
            basis = _version(notice, version_no=2, kind="MATERIALIZED_ANALYSIS", digest_character="2")
            manifest_sha = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                                                     separators=(",", ":")).encode()).hexdigest()
            basis.source_payload = {**basis.source_payload, "current_manifest_sha256":
                                    "a" * 64 if case == "changed" else manifest_sha}
            session.add(notice); session.flush()
            if case != "absent":
                old = _quantitative_snapshot(value=20, lower=20, upper=20, status="CONFIRMED",
                    band="GREEN", confirmed=20, coverage=100, public_criteria=_public_criteria_snapshot())
                run = _run(notice, basis, label="old", generated_at=now - timedelta(hours=1), score=old)
                if case == "partial":
                    run.status = "PARTIAL"
                if case == "invalid":
                    old.basis_json = {**old.basis_json, "input_sha256": "e" * 64}
                session.add(run)
            current_scored = case == "current"
            latest = _quantitative_snapshot(value=20 if current_scored else None,
                lower=20 if current_scored else 0, upper=20,
                status="CONFIRMED" if current_scored else "REVIEW",
                band="GREEN" if current_scored else "RED", confirmed=20 if current_scored else 0,
                coverage=100 if current_scored else 0,
                public_criteria=_public_criteria_snapshot() if current_scored else {"schema_version":"invalid"})
            session.add(_run(notice, basis, label="latest", generated_at=now, score=latest))
            session.commit()
        headers = {"x-pai-loop-api-key": "server-only-secret"} if case == "private" else {}
        if case == "private":
            client.cookies.clear()
        response = client.get('/api/v1/notices/SYN-SAVED-HISTORY/quantitative-estimate', headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
    for secret in (PRIVATE_BASIS_MARKER, PRIVATE_RULESET, "SYN-ATT"):
        assert secret not in json.dumps(result.get('previous_estimate'))
    previous = result['previous_estimate']
    if case in {"invalid", "absent", "current"}:
        assert previous is None
    else:
        assert previous['subtotal_points'] == previous['subtotal_max_points'] == 20
        assert len(previous['items']) == 3
        assert previous['source_status'] == ("SOURCE_CHANGED_OR_UNVERIFIED" if case == "changed" else "SAME_MANIFEST")
        assert "현재 점수·자동 분석 조건에 반영하지 않습니다" in previous['warning']
        assert result['estimated_points'] is None
        assert not any(item['estimated_points'] is not None for item in result['criteria'])
        assert result['activation_status'] != 'PARTIAL_SOURCE'  # history never promotes the current result
