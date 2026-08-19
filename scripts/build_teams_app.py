from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "teams-app"
MANIFEST_PATH = APP_DIR / "manifest.json"
COLOR_PATH = APP_DIR / "color.png"
OUTLINE_PATH = APP_DIR / "outline.png"
PACKAGE_PATH = APP_DIR / "PAI-LOOP-Teams-App.zip"
PACKAGE_FILES = (MANIFEST_PATH, COLOR_PATH, OUTLINE_PATH)
ZIP_TIMESTAMP = (2026, 8, 19, 0, 0, 0)


def _draw_loop_icon(size: int, *, outline: bool) -> Image.Image:
    scale = size / 32
    if outline:
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        color = (255, 255, 255, 255)
        width = max(2, round(2.2 * scale))
    else:
        image = Image.new("RGBA", (size, size), (14, 43, 42, 255))
        color = (131, 217, 200, 255)
        width = max(2, round(2.7 * scale))
    draw = ImageDraw.Draw(image)
    box = tuple(round(value * scale) for value in (5, 5, 27, 27))
    draw.arc(box, start=202, end=350, fill=color, width=width)
    draw.arc(box, start=22, end=170, fill=color, width=width)
    draw.line(
        [(round(24 * scale), round(5 * scale)), (round(27 * scale), round(10 * scale)), (round(21 * scale), round(9 * scale))],
        fill=color,
        width=width,
        joint="curve",
    )
    draw.line(
        [(round(8 * scale), round(27 * scale)), (round(5 * scale), round(22 * scale)), (round(11 * scale), round(23 * scale))],
        fill=color,
        width=width,
        joint="curve",
    )
    center = round(3.6 * scale)
    cx = cy = round(16 * scale)
    fill = color if outline else (239, 113, 60, 255)
    draw.ellipse((cx - center, cy - center, cx + center, cy + center), fill=fill)
    return image


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _write_assets() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    COLOR_PATH.write_bytes(_png_bytes(_draw_loop_icon(192, outline=False)))
    OUTLINE_PATH.write_bytes(_png_bytes(_draw_loop_icon(32, outline=True)))


def _validate() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    required = {"manifestVersion", "id", "version", "configurableTabs", "validDomains"}
    missing = sorted(required - manifest.keys())
    if missing:
        raise ValueError(f"Teams manifest is missing: {', '.join(missing)}")
    if "pai-loop-demo.onrender.com" not in manifest["validDomains"]:
        raise ValueError("Render hostname must be declared in validDomains")
    tab = manifest["configurableTabs"][0]
    if set(tab["scopes"]) != {"team", "groupChat"}:
        raise ValueError("configurable tab must support team and groupChat")
    with Image.open(COLOR_PATH) as color:
        if color.size != (192, 192) or color.format != "PNG":
            raise ValueError("color.png must be a 192x192 PNG")
    with Image.open(OUTLINE_PATH) as outline:
        if outline.size != (32, 32) or outline.mode != "RGBA":
            raise ValueError("outline.png must be a transparent 32x32 RGBA PNG")
        if outline.getchannel("A").getextrema()[0] != 0:
            raise ValueError("outline.png background must be transparent")


def _build_package() -> None:
    with zipfile.ZipFile(PACKAGE_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in PACKAGE_FILES:
            info = zipfile.ZipInfo(path.name, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def main() -> None:
    _write_assets()
    _validate()
    _build_package()
    print(PACKAGE_PATH)


if __name__ == "__main__":
    main()
