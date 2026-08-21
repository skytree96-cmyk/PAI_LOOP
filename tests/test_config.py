from __future__ import annotations

from pathlib import Path

import pytest

from pai_loop.config import Settings, _bounded_int, _csv


def test_environment_parsers_fail_closed_and_clamp_bounds() -> None:
    assert _csv(None) == ()
    assert _csv(" first, , second ") == ("first", "second")
    assert _bounded_int("invalid", default=12, minimum=1, maximum=30) == 12
    assert _bounded_int("0", default=12, minimum=1, maximum=30) == 1
    assert _bounded_int("99", default=12, minimum=1, maximum=30) == 30


def test_production_security_rejects_synthetic_and_unguarded_manual_analysis() -> None:
    base = {
        "environment": "production",
        "database_url": "postgresql+psycopg://database.example/pai",
        "api_key": "configured-server-key",
    }
    with pytest.raises(RuntimeError, match="SEED_SYNTHETIC"):
        Settings(**base, seed_synthetic=True).validate_security()
    with pytest.raises(RuntimeError, match="PUBLIC_READ_ONLY"):
        Settings(
            **base,
            public_manual_analysis_enabled=True,
            public_read_only=False,
        ).validate_security()

    Settings(
        **base,
        public_manual_analysis_enabled=True,
        public_read_only=True,
    ).validate_security()


def test_local_sqlite_directory_creation_is_bounded(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "pai-loop.db"

    Settings(database_url=f"sqlite:///{database.as_posix()}").ensure_local_directories()
    Settings(database_url="sqlite:///:memory:").ensure_local_directories()
    Settings(database_url="postgresql+psycopg://database.example/pai").ensure_local_directories()

    assert database.parent.is_dir()
