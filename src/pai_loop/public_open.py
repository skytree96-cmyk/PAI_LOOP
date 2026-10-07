"""Public, data-free introduction served alongside the authenticated app."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

OPEN_FILES = frozenset({
    "index.html", "styles.css", "app.js", "favicon.svg", "story.css", "story.js",
    "assets/pai-product-poster.webp", "assets/pai-product-tour.webm",
    *(f"assets/pai-screen-{i}.webp" for i in range(1, 9)),
})
OPEN_PATHS = frozenset({"/open", "/open/", *(f"/open/{name}" for name in OPEN_FILES)})
# Contest judging sites embed this data-free page in an iframe. Allow only
# their exact origins; the authenticated app keeps its own Teams-only list.
OPEN_FRAME_ANCESTORS = (
    "'self'",
    "https://dancing-smakager-57e08c.netlify.app",
    "https://aiedu.kma.or.kr",
)
OPEN_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://cdn.jsdelivr.net; "
    "font-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; media-src 'self'; "
    f"connect-src 'none'; frame-ancestors {' '.join(OPEN_FRAME_ANCESTORS)}; base-uri 'self'; "
    "form-action 'none'; object-src 'none'"
)
router = APIRouter(include_in_schema=False)


def _open_file(name: str) -> FileResponse:
    if name not in OPEN_FILES:
        raise HTTPException(404, "Not found")
    # Wheels carry only allowlisted public files. Source checkouts use the
    # authoritative web directory without generating duplicate assets.
    root = Path(__file__).parent / "open"
    if not root.is_dir():
        root = Path(__file__).resolve().parents[2] / "web" / "pai-open"
    return FileResponse(root / name, headers={
        "Cache-Control": "no-cache" if name == "index.html" else "public, max-age=300",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    })


@router.api_route("/open", methods=["GET", "HEAD"])
@router.api_route("/open/", methods=["GET", "HEAD"])
def open_index() -> FileResponse:
    return _open_file("index.html")


@router.api_route("/open/{name:path}", methods=["GET", "HEAD"])
def open_asset(name: str) -> FileResponse:
    return _open_file(name)
