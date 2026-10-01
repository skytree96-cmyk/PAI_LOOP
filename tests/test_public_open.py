import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from pai_loop.main import create_app


@pytest.fixture
def public_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAI_LOOP_ENV", "development")
    app = create_app(database_url=f"sqlite:///{tmp_path / 'SYN-open.db'}", seed_synthetic=False)
    app.state.settings = replace(app.state.settings, department_accounts_enabled=True)
    # Static serving and anonymous denial must work without opening a DB session.
    with TestClient(app, base_url="https://testserver") as client:
        yield client


@pytest.mark.parametrize("path", ["/open", "/open/", "/open/index.html"])
def test_open_is_public_with_same_origin_buttons_and_scoped_assets(public_client, path):
    response = public_client.get(path)
    assert response.status_code == 200
    assert 'PAI 시작하기' in response.text
    assert 'entryLoginForm' not in response.text
    assert response.headers["cache-control"] == "no-cache"
    assert "connect-src 'none'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'self' https://dancing-smakager-57e08c.netlify.app;" in response.headers["content-security-policy"]
    assert "x-frame-options" not in response.headers
    links = re.findall(r'data-app-path="([^"]+)" href="([^"]+)"', response.text)
    assert len(links) == 13
    assert all(route == href and href.startswith("/") for route, href in links)
    assert "run.app" not in response.text and "onrender.com" not in response.text
    assets = re.findall(r'(?:src|poster|href)="(/open/[^"?]+)', response.text)
    assert len(assets) == 15
    for asset in assets:
        assert public_client.get(asset).status_code == 200
    assert re.findall(r'data-ps-step="(\d+)"', response.text) == [str(i) for i in range(8)]
    assert "MS Teams<br>맞춤 알림 연동" in response.text
    assert "데일리 브리핑" in response.text
    assert "로그인과 권한 관리는 현재 준비 중" not in response.text
    assert public_client.head(path).status_code == 200
    assert not public_client.head(path).content


def test_open_media_supports_range_requests(public_client):
    response = public_client.get("/open/assets/pai-product-tour.webm", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert len(response.content) == 100
    assert response.headers["content-type"].startswith("video/webm")
    assert response.headers["content-range"].startswith("bytes 0-99/")


@pytest.mark.parametrize("path", ["/open/README.md", "/open/build.mjs", "/open/_headers", "/open/api/v1/notices", "/open/assets/unknown.webp", "/open/assets/pai-screen-9.webp", "/open/%2e%2e/main.py", "/api/v1/dashboard"])
def test_open_allowlist_does_not_expose_other_files_or_api(public_client, path):
    assert public_client.get(path).status_code in (401, 404)


def test_open_does_not_bypass_existing_app_login_or_allow_writes(public_client):
    assert 'id="entryLoginForm"' in public_client.get("/").text
    assert public_client.post("/open").status_code == 401
    assert public_client.post("/open/app.js").status_code == 401


def test_authenticated_unknown_open_asset_is_still_not_served(client):
    assert client.get("/open/README.md").status_code == 404
