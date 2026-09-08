from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pai_loop.config import Settings, _safe_teams_url
from pai_loop.main import create_app


ROOT = Path(__file__).parents[1]
DESTINATION = "https://teams.microsoft.com/l/channel/SYN-CHANNEL/SYN?groupId=SYN-GROUP&tenantId=SYN-TENANT"


def runtime_config(html: str) -> dict:
    matches = re.findall(r'<script id="paiLoopRuntimeConfig" type="application/json">(.*?)</script>', html, re.S)
    assert len(matches) == 1
    return json.loads(matches[0])


def test_runtime_destination_and_icon_work_on_every_frontend_entry(monkeypatch):
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    monkeypatch.setenv("PAI_BOT_TEAMS_URL", DESTINATION)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        for path in ("/", "/index.html", "/notices", "/reviews", "/urgent", "/fail", "/cancelled",
                     "/result-missing", "/decisions", "/results", "/awards", "/prespec", "/performance"):
            response = client.get(path)
            assert response.status_code == 200
            assert runtime_config(response.text) == {"paiBotTeamsUrl": DESTINATION}
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert 'src="/teams-icon.png"' in response.text
        image = client.get("/teams-icon.png")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content == (ROOT / "src/pai_loop/static/teams-icon.png").read_bytes()


def test_unconfigured_destination_is_empty_and_not_a_generic_homepage(monkeypatch):
    monkeypatch.delenv("PAI_BOT_TEAMS_URL", raising=False)
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        assert runtime_config(client.get("/").text) == {"paiBotTeamsUrl": ""}


@pytest.mark.parametrize("destination", [
    "", "javascript:alert('SYN')", "//teams.microsoft.com/l/channel/SYN",
    "http://teams.microsoft.com/l/channel/SYN", "https://teams.microsoft.com.evil.test/SYN",
    "https://evil.test/teams.microsoft.com/SYN", "https://" + "@".join(("SYN", "teams.microsoft.com/l/channel/SYN")),
    "https://" + "@".join(("SYN:SYN", "teams.microsoft.com/l/channel/SYN")), "https://teams.microsoft.com:444/SYN",
    "https://@teams.microsoft.com/l/channel/SYN",
    "https://teams.microsoft.com:bad/SYN", "https://teams.microsoft.com./SYN",
    "https://teams.microsoft.com/\nSYN", "https://teams.microsoft.com/\\SYN",
    "https://teams.microsoft.com/" + "S" * 8192,
])
def test_invalid_runtime_destination_fails_closed_without_logging_value(monkeypatch, destination):
    monkeypatch.setenv("PAI_BOT_TEAMS_URL", destination)
    settings = Settings.from_env()
    assert settings.safe_pai_bot_teams_url == ""
    assert "pai_bot_teams_url" not in repr(settings)


def test_script_delimiters_in_an_allowed_url_cannot_escape_runtime_json(monkeypatch):
    destination = DESTINATION + "&context=</script><script>SYN-CANARY</script>&quoted=\"SYN\""
    monkeypatch.setenv("PAI_BOT_TEAMS_URL", destination)
    assert _safe_teams_url(destination) == destination
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        response = client.get("/index.html")
        assert runtime_config(response.text) == {"paiBotTeamsUrl": destination}
        assert "<script>SYN-CANARY" not in response.text
        assert "\\u003c/script\\u003e" in response.text


def test_teams_button_uses_configured_destination_and_blocks_unsafe_runtime_values():
    source = (ROOT / "src/pai_loop/static/app.js").read_text(encoding="utf-8")
    functions = source[source.index("  function configurePaiBotTeamsAccess()"):source.index("  function detectTeamsContext()")]
    script = r"""
const assert = require('node:assert/strict');
let PAI_BOT_TEAMS_URL = '';
const button = { dataset: {}, setAttribute(name, value) { this[name] = value; } };
const els = { paiBotTeamsButton: button, paiBotTeamsAccessNote: {} };
const opened = [];
const window = { open(...args) { opened.push(args); } };
__FUNCTIONS__
for (const candidate of ['', 'javascript:alert(1)', '//teams.microsoft.com/SYN',
  'https://teams.microsoft.com.evil.test/SYN', 'https://' + ['SYN', 'teams.microsoft.com/SYN'].join('@'),
  'https://@teams.microsoft.com/SYN',
  'https://teams.microsoft.com:444/SYN', 'https://teams.microsoft.com/\nSYN']) {
  PAI_BOT_TEAMS_URL = candidate;
  configurePaiBotTeamsAccess(); openPaiBotTeams();
  assert.equal(button.disabled, true); assert.equal(button['aria-disabled'], 'true');
  assert.equal(button.dataset.state, 'pending');
}
assert.equal(opened.length, 0);
PAI_BOT_TEAMS_URL = __DESTINATION__;
configurePaiBotTeamsAccess(); openPaiBotTeams();
assert.equal(button.disabled, false); assert.equal(button['aria-disabled'], 'false');
assert.equal(button.dataset.state, 'ready');
assert.deepEqual(opened, [[PAI_BOT_TEAMS_URL, '_blank', 'noopener,noreferrer']]);
""".replace("__FUNCTIONS__", functions).replace("__DESTINATION__", json.dumps(DESTINATION))
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
