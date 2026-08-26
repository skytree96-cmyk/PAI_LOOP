from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pai_loop.config import Settings, _bounded_int, _csv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_environment_parsers_fail_closed_and_clamp_bounds() -> None:
    assert _csv(None) == ()
    assert _csv(" first, , second ") == ("first", "second")
    assert _bounded_int("invalid", default=12, minimum=1, maximum=30) == 12
    assert _bounded_int("0", default=12, minimum=1, maximum=30) == 1
    assert _bounded_int("99", default=12, minimum=1, maximum=30) == 30


def test_manual_operator_token_requires_exactly_four_ascii_digits() -> None:
    assert Settings(public_manual_analysis_token=None).public_manual_analysis_token_valid is False
    assert Settings(public_manual_analysis_token="123").public_manual_analysis_token_valid is False
    assert Settings(public_manual_analysis_token="12345").public_manual_analysis_token_valid is False
    assert Settings(public_manual_analysis_token="12a4").public_manual_analysis_token_valid is False
    assert Settings(public_manual_analysis_token="１２３４").public_manual_analysis_token_valid is False
    assert Settings(public_manual_analysis_token="1234 ").public_manual_analysis_token_valid is False
    assert Settings(public_manual_analysis_token="1234").public_manual_analysis_token_valid is True


def test_n8n_claude_selection_reuses_server_boundary_without_openai_key() -> None:
    settings = Settings(
        api_key="server-boundary-key",
        openai_api_key=None,
        llm_provider="n8n_claude",
        llm_gateway_base_url="https://n8n.example/webhook/pai-loop-claude",
        claude_model="claude-sonnet-5",
    )

    assert settings.extraction_configured is True
    assert settings.extraction_api_key == "server-boundary-key"
    assert settings.extraction_model == "claude-sonnet-5"
    settings.validate_security()


def test_n8n_claude_model_must_match_gateway_contract() -> None:
    settings = Settings(
        api_key="server-boundary-key",
        llm_provider="n8n_claude",
        llm_gateway_base_url="https://n8n.example/webhook/pai-loop-claude",
        claude_model="typo-model",
    )

    with pytest.raises(RuntimeError, match="gateway contract"):
        settings.validate_security()


def test_manual_analysis_default_hourly_quota_is_disabled() -> None:
    assert Settings().public_manual_analysis_hourly_limit == 0


def test_render_manual_analysis_secret_and_cost_cap_are_fail_closed() -> None:
    manifest = yaml.safe_load((PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8"))
    env_vars = {
        item["key"]: item
        for item in manifest["services"][0]["envVars"]
    }

    assert env_vars["PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_TOKEN"] == {
        "key": "PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_TOKEN",
        "sync": False,
    }
    assert env_vars["PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_HOURLY_LIMIT"]["value"] == "5"
    assert env_vars["PAI_LOOP_LLM_PROVIDER"]["value"] == "n8n_claude"
    assert env_vars["PAI_LOOP_CLAUDE_MODEL"]["value"] == "claude-sonnet-5"
    assert "OPENAI_API_KEY" not in env_vars
    assert "PAI_LOOP_OPENAI_MODEL" not in env_vars


def test_production_allows_only_the_pinned_n8n_claude_boundary() -> None:
    base = {
        "environment": "production",
        "database_url": "postgresql+psycopg://database.example/pai",
        "api_key": "configured-server-key",
        "llm_provider": "n8n_claude",
        "llm_gateway_base_url": "https://n8n.kma.or.kr/webhook/pai-loop-claude",
        "claude_model": "claude-sonnet-5",
    }

    Settings(**base).validate_security()
    with pytest.raises(RuntimeError, match="direct OpenAI is disabled"):
        Settings(**{**base, "llm_provider": "openai"}).validate_security()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        Settings(**base, openai_api_key="must-not-exist").validate_security()
    with pytest.raises(RuntimeError, match="production Claude gateway"):
        Settings(
            **{**base, "llm_gateway_base_url": "https://other.example/webhook/pai-loop-claude"}
        ).validate_security()


def test_production_security_rejects_synthetic_and_unguarded_manual_analysis() -> None:
    base = {
        "environment": "production",
        "database_url": "postgresql+psycopg://database.example/pai",
        "api_key": "configured-server-key",
        "llm_provider": "n8n_claude",
        "llm_gateway_base_url": "https://n8n.kma.or.kr/webhook/pai-loop-claude",
        "claude_model": "claude-sonnet-5",
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
