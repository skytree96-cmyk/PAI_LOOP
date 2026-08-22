from __future__ import annotations

import tomllib
from pathlib import Path

from pai_loop import __version__


ROOT = Path(__file__).parents[1]


def test_release_version_is_aligned_across_package_and_documentation() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert __version__ == "0.9.3"
    assert project["project"]["version"] == __version__
    assert "현재 구현 범위: v0.9.3" in readme
    assert "SOURCE_VALIDATED / AUTO_ACTIVE" in readme
    assert "fact_binding_sha256" in readme
