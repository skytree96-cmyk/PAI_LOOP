"""Build a deployable personal bot package only with explicit deployment values."""
from __future__ import annotations

import argparse
import json
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def build_package(bot_app_id: str, public_base_url: str, output: Path) -> Path:
    try:
        bot_id = str(UUID(bot_app_id))
        if UUID(bot_id).int == 0:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Use the actual registered Azure bot application UUID.") from None
    base = public_base_url.rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path or parsed.port not in {None, 443}
            or parsed.hostname in {"localhost", "example.com", "example.invalid"}):
        raise ValueError("Use the actual public HTTPS application origin.")
    app_dir = ROOT / "teams-app"
    legacy_package = app_dir / "PAI-LOOP-Teams-App.zip"
    if output.resolve() == legacy_package.resolve():
        raise ValueError("Use a separate output file for the personal bot package.")
    manifest = deepcopy(json.loads((app_dir / "manifest.json").read_text(encoding="utf-8")))
    # v1.28 forbids these legacy properties (additionalProperties: false).
    manifest.pop("packageName", None)
    manifest["version"] = "0.10.0"
    manifest["description"] = {
        "short": "관심 공고의 자격·정량점수·리스크를 개인 메시지로 확인합니다.",
        "full": "공고별 관심 등록 후 등록 시, 마감 5일 전, 마감일 오전에 본인의 Teams 개인 대화로 공고 분석을 받습니다. 앱에서 생성한 일회용 연결 코드를 개인 봇 대화에 입력하여 본인을 연결합니다."
    }
    for key in ("websiteUrl", "privacyUrl", "termsOfUseUrl"):
        manifest["developer"][key] = base + "/"
    for tab in manifest.get("configurableTabs", []):
        tab.pop("supportedPlatform", None)
        tab["configurationUrl"] = base + "/teams-config.html"
    manifest["staticTabs"] = [{"entityId": "pai-loop-personal", "name": "관심 공고",
        "contentUrl": base + "/?host=teams", "websiteUrl": base + "/", "scopes": ["personal"]}]
    manifest["bots"] = [{"botId": bot_id, "scopes": ["personal"], "isNotificationOnly": False,
        "supportsFiles": False, "commandLists": [{"scopes": ["personal"], "commands": [
            {"title": "연결", "description": "앱의 개인 알림 연결 화면에서 복사한 연결 코드를 붙여 넣으세요."}]}]}]
    manifest["validDomains"] = [parsed.hostname, "res.cdn.office.net"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        for name in ("color.png", "outline.png"):
            archive.write(app_dir / name, name)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-app-id", required=True)
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build_package(args.bot_app_id, args.public_base_url, args.output))


if __name__ == "__main__":
    main()
