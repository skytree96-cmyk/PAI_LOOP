from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from pai_loop.main import SecretSafeGZipMiddleware


def test_static_app_bundle_is_compressed_when_the_browser_accepts_gzip(client):
    response = client.get("/app.js", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "loadApplicationData" in response.text


def test_secret_bearing_paths_are_never_compressed():
    app = FastAPI()
    body = "SYN-csrf " * 400

    @app.get("/api/v1/accounts/me")
    def account() -> PlainTextResponse:
        return PlainTextResponse(body)

    @app.post("/api/v1/teams/link-code")
    def link_code() -> PlainTextResponse:
        return PlainTextResponse(body)

    @app.get("/api/v1/notices")
    def notices() -> PlainTextResponse:
        return PlainTextResponse(body)

    app.add_middleware(SecretSafeGZipMiddleware, minimum_size=1024, compresslevel=6)
    client = TestClient(app)
    gzip = {"Accept-Encoding": "gzip"}
    assert "content-encoding" not in client.get("/api/v1/accounts/me", headers=gzip).headers
    assert "content-encoding" not in client.post("/api/v1/teams/link-code", headers=gzip).headers
    assert client.get("/api/v1/notices", headers=gzip).headers["content-encoding"] == "gzip"
