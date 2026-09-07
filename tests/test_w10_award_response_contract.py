import json
import subprocess
from dataclasses import replace

import pytest


@pytest.mark.parametrize("diagnostics", ["empty", "recovered", "partial"])
def test_real_award_refresh_response_passes_w10_without_promoting_partial(client, monkeypatch, diagnostics):
    key = "PPS-SYN-W10-CONTRACT"
    assert client.post("/api/v1/notices", json={
        "notice_key": key, "bid_notice_no": key, "title": "SYN 교육", "agency": "SYN 기관",
        "deadline": "2027-01-01T00:00:00Z",
    }).status_code == 201
    client.app.state.settings = replace(client.app.state.settings, pps_api_key="SYN-provider", api_key="SYN-server")

    class Provider:
        request_count = 2
        hit_page_limit = hit_incomplete_response = hit_time_limit = False
        fallback_window_count = 1
        window_errors = ["SYN-unresolved-window"] if diagnostics == "partial" else []
        window_error_counts = [] if diagnostics == "empty" else [{
            "phase": "FALLBACK" if diagnostics == "partial" else "PRIMARY",
            "error_type": "HTTP_ERROR", "http_status": 503, "provider_code": None, "count": 1,
        }]

        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def iter_awards(self, **_kwargs): return iter(())

    monkeypatch.setattr("pai_loop.api.PpsAwardClient", Provider)
    response = client.post(f"/api/v1/notices/{key}/award-history/refresh",
                           headers={"X-PAI-LOOP-API-KEY": "SYN-server"}, json={"keyword": "SYN", "years": 1})
    assert response.status_code == 200, response.text
    expected = "PARTIAL" if diagnostics == "partial" else "COMPLETED"
    assert response.json()["status"] == expected
    script = r'''
const fs = require('node:fs');
const workflow = JSON.parse(fs.readFileSync('workflows/pai-loop-10-daily-opportunity-briefing.json', 'utf8'));
const code = workflow.nodes.find(n=>n.name==='Validate Award Refresh Batch').parameters.jsCode;
const response = JSON.parse(fs.readFileSync(0, 'utf8'));
const result = new Function('$input','$node',code)(
  {all:()=>[{json:response}]},
  {'Build Bounded Award Refresh Plan':{json:{runtime:{},awardRefresh:{limit:1,dryRun:false}}}}
);
process.stdout.write(JSON.stringify(result[0].json.awardRefresh));
'''
    checked = subprocess.run(["node", "-e", script], input=response.text, text=True,
                             capture_output=True, encoding="utf-8", check=False)
    assert checked.returncode == 0, checked.stderr
    result = json.loads(checked.stdout)
    assert result["status"] == expected
    assert result["completed"] == (0 if diagnostics == "partial" else 1)
    assert result["privacyBoundary"] == {"candidateRowsEmitted": False, "piiFieldsEmitted": False}
    assert "window_error_counts" not in result["summaries"][0]
