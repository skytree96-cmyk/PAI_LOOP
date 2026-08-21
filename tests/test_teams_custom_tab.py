from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from pai_loop.main import create_app


ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "src" / "pai_loop" / "static" / "app.js"
INDEX_HTML = APP_JS.with_name("index.html")
CONFIG_HTML = APP_JS.with_name("teams-config.html")
STYLES_CSS = APP_JS.with_name("styles.css")
TEAMS_DIR = ROOT / "teams-app"


def _png_header(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    width, height, _bit_depth, color_type, *_rest = struct.unpack(">IIBBBBB", data[16:29])
    return width, height, color_type


def test_teams_manifest_declares_configurable_channel_tab_and_render_domain() -> None:
    manifest = json.loads((TEAMS_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["$schema"].endswith("/v1.28/MicrosoftTeams.schema.json")
    assert manifest["manifestVersion"] == "1.28"
    assert manifest["icons"] == {"outline": "outline.png", "color": "color.png"}
    assert "pai-loop-demo.onrender.com" in manifest["validDomains"]
    assert len(manifest["configurableTabs"]) == 1
    tab = manifest["configurableTabs"][0]
    assert tab["configurationUrl"] == "https://pai-loop-demo.onrender.com/teams-config.html"
    assert set(tab["scopes"]) == {"team", "groupChat"}
    assert set(tab["context"]) == {"channelTab", "privateChatTab"}
    assert tab["canUpdateConfiguration"] is False


def test_teams_package_has_only_manifest_and_exact_icons() -> None:
    package = TEAMS_DIR / "PAI-LOOP-Teams-App.zip"
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {"manifest.json", "color.png", "outline.png"}
        assert json.loads(archive.read("manifest.json")) == json.loads(
            (TEAMS_DIR / "manifest.json").read_text(encoding="utf-8")
        )
    assert _png_header(TEAMS_DIR / "color.png")[:2] == (192, 192)
    assert _png_header(TEAMS_DIR / "outline.png") == (32, 32, 6)


def test_teams_config_initializes_sdk_and_keeps_content_in_iframe() -> None:
    config = CONFIG_HTML.read_text(encoding="utf-8")
    index = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    assert "teams-js/2.19.0/js/MicrosoftTeams.min.js" in config
    assert "await teams.app.initialize()" in config
    assert "teams.pages.config.registerOnSaveHandler" in config
    assert "teams.pages.config.setValidityState(true)" in config
    assert 'contentUrl: `${root}/?host=teams`' in config
    assert 'websiteUrl: `${root}/`' in config
    assert "teams-js/2.19.0/js/MicrosoftTeams.min.js" in index
    assert "await teamsApp.initialize()" in source
    assert "history.pushState" in source
    assert "window.location.href =" not in source


def test_teams_iframe_headers_allow_only_declared_microsoft_hosts(monkeypatch) -> None:
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    app = create_app(database_url="sqlite:///:memory:", seed_synthetic=False)
    with TestClient(app) as client:
        for path in ("/", "/teams-config.html", "/api/v1/runtime-profile"):
            response = client.get(path)
            assert response.status_code == 200
            csp = response.headers["Content-Security-Policy"]
            assert "frame-ancestors 'self'" in csp
            assert "https://teams.microsoft.com" in csp
            assert "https://*.teams.microsoft.com" in csp
            assert "https://*.cloud.microsoft" in csp
            assert "x-frame-options" not in response.headers


def test_table_and_card_detail_arrows_are_functional() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")
    assert (
        'class="row-arrow" type="button" data-open-notice '
        'aria-label="${escapeAttribute(notice.title)} 상세 패널 열기"'
    ) in source
    assert '<span class="row-arrow" aria-hidden="true">' not in source
    assert '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>' in source
    assert 'class="recommendation-arrow"' in source
    assert 'data-open-notice aria-label="${escapeAttribute(notice.title)} 상세 패널 열기"' in source
    assert 'els.noticeTableBody.addEventListener("click", handleNoticeActivation)' in source
    assert 'event.target.closest("[data-open-notice]")' in source
    assert ".row-arrow:focus-visible" in styles
    assert ".recommendation-arrow" in styles
    assert "styles.css?v=20260820-ui2" in html
    assert "app.js?v=20260820-ui2" in html


def test_manual_analysis_actions_are_functional() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="manualAnalyzeButton"' in html
    assert "manualAnalysisRequests: new Map()" in source
    assert 'state.manualAnalysisRequests.get(noticeKey) === "running"' in source
    assert "/analysis/request`" in source
    assert '{ method: "POST" }' in source
    assert "/analysis/requests/${encodeURIComponent(requestId)}`" in source
    assert "for (let poll = 0; poll < 240; poll += 1)" in source
    assert "data-manual-analysis" in source
    assert "공고 분석 요청 실패" in source
