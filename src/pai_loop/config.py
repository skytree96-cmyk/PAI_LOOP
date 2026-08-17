from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    database_url: str = "sqlite:///./data/pai_loop.db"
    seed_synthetic: bool = False
    cors_origins: tuple[str, ...] = ("http://localhost:8000", "http://localhost:5173")
    log_level: str = "INFO"
    api_key: str | None = None
    public_read_only: bool = False
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"
    pps_api_key: str | None = None
    pps_base_url: str = "https://apis.data.go.kr/1230000"
    pps_notice_operation: str = "ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"
    pps_award_operation: str = "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch"

    @classmethod
    def from_env(cls, *, database_url: str | None = None) -> "Settings":
        return cls(
            environment=os.getenv("PAI_LOOP_ENV", "development"),
            database_url=database_url
            or os.getenv("PAI_LOOP_DATABASE_URL", "sqlite:///./data/pai_loop.db"),
            seed_synthetic=_as_bool(os.getenv("PAI_LOOP_SEED_SYNTHETIC")),
            cors_origins=_csv(os.getenv("PAI_LOOP_CORS_ORIGINS"))
            or ("http://localhost:8000", "http://localhost:5173"),
            log_level=os.getenv("PAI_LOOP_LOG_LEVEL", "INFO"),
            api_key=os.getenv("PAI_LOOP_API_KEY") or None,
            public_read_only=_as_bool(os.getenv("PAI_LOOP_PUBLIC_READ_ONLY")),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("PAI_LOOP_OPENAI_MODEL", "gpt-5.6-luna"),
            pps_api_key=os.getenv("PPS_API_KEY") or None,
            pps_base_url=os.getenv("PAI_LOOP_PPS_BASE_URL", "https://apis.data.go.kr/1230000"),
            pps_notice_operation=os.getenv(
                "PAI_LOOP_PPS_NOTICE_OPERATION",
                "ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch",
            ),
            pps_award_operation=os.getenv(
                "PAI_LOOP_PPS_AWARD_OPERATION",
                "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch",
            ),
        )

    def validate_security(self) -> None:
        if self.environment.casefold() != "production":
            return
        if not self.api_key:
            raise RuntimeError(
                "PAI_LOOP_API_KEY is required in production until Entra SSO/RBAC is configured"
            )
        if self.database_url.startswith("sqlite"):
            raise RuntimeError(
                "PAI_LOOP_DATABASE_URL must use managed PostgreSQL in production; SQLite is local-only"
            )
        if self.seed_synthetic:
            raise RuntimeError("PAI_LOOP_SEED_SYNTHETIC must be false in production")

    def ensure_local_directories(self) -> None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix) or self.database_url.endswith(":memory:"):
            return
        raw_path = self.database_url.removeprefix(prefix)
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
