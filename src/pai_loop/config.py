from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _safe_teams_url(value: str | None) -> str:
    if (not isinstance(value, str) or len(value) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value):
        return ""
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        if (parsed.scheme == "https" and parsed.hostname == "teams.microsoft.com"
                and parsed.username is None and parsed.password is None and parsed.port in (None, 443)):
            return candidate
    except ValueError:
        pass
    return ""


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _bounded_int(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except (AttributeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    database_url: str = "sqlite:///./data/pai_loop.db"
    seed_synthetic: bool = False
    cors_origins: tuple[str, ...] = ("http://localhost:8000", "http://localhost:5173")
    log_level: str = "INFO"
    pai_bot_teams_url: str = field(default="", repr=False)
    api_key: str | None = None
    private_evidence_token: str | None = None
    public_read_only: bool = False
    department_accounts_enabled: bool = False
    # 제안요청서(전자주문) 첨부 수집.  켜면 그 공고의 manifest 가 바뀌고, 추출
    # 재사용이 manifest 전체 해시에 묶여 있으므로 기존 첨부까지 다시 유료로
    # 읽힌다(docs/EORDER_RFP_ATTACHMENTS_20260917.md 3.3).  규모를 알고 켜야
    # 하는 항목이라 기본값은 꺼짐이다.
    eorder_rfp_attachments_enabled: bool = False
    public_manual_analysis_enabled: bool = False
    # Deprecated deployment compatibility only; never authenticates a request.
    public_manual_analysis_token: str | None = None
    # Zero disables the aggregate hourly demo quota. Per-notice cooldown,
    # advisory locking, idempotency, and bounded attachment/call budgets remain.
    public_manual_analysis_hourly_limit: int = 0
    public_manual_analysis_cooldown_hours: int = 24
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"
    llm_provider: str = "openai"
    llm_gateway_base_url: str | None = None
    claude_model: str = "claude-sonnet-5"
    pps_api_key: str | None = None
    pps_base_url: str = "https://apis.data.go.kr/1230000"
    pps_notice_operation: str = "ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch"
    pps_award_operation: str = "as/ScsbidInfoService/getScsbidListSttusServcPPSSrch"

    @property
    def safe_pai_bot_teams_url(self) -> str:
        return _safe_teams_url(self.pai_bot_teams_url)

    @property
    def public_manual_analysis_token_valid(self) -> bool:
        token = self.public_manual_analysis_token
        return bool(
            token
            and token == token.strip()
            and len(token) == 4
            and token.isascii()
            and token.isdigit()
        )

    @property
    def private_evidence_token_valid(self) -> bool:
        token = self.private_evidence_token
        return bool(
            token
            and token == token.strip()
            and 32 <= len(token) <= 512
            and token.isascii()
        )

    @property
    def extraction_api_key(self) -> str | None:
        """Return the server-only credential used at the selected LLM boundary."""

        if self.llm_provider == "n8n_claude":
            # The same server credential already used by W10/W11 authenticates
            # the private n8n webhook. The Anthropic credential never leaves n8n.
            return self.api_key
        return self.openai_api_key

    @property
    def extraction_model(self) -> str:
        return self.claude_model if self.llm_provider == "n8n_claude" else self.openai_model

    @property
    def extraction_configured(self) -> bool:
        if self.llm_provider == "n8n_claude":
            return bool(self.extraction_api_key and self.llm_gateway_base_url)
        return bool(self.openai_api_key)

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
            pai_bot_teams_url=os.getenv("PAI_BOT_TEAMS_URL", ""),
            api_key=os.getenv("PAI_LOOP_API_KEY") or None,
            private_evidence_token=(
                os.getenv("PAI_LOOP_PRIVATE_EVIDENCE_TOKEN") or None
            ),
            public_read_only=_as_bool(os.getenv("PAI_LOOP_PUBLIC_READ_ONLY")),
            department_accounts_enabled=_as_bool(os.getenv("PAI_LOOP_DEPARTMENT_ACCOUNTS_ENABLED")),
            eorder_rfp_attachments_enabled=_as_bool(
                os.getenv("PAI_LOOP_EORDER_RFP_ATTACHMENTS_ENABLED")
            ),
            public_manual_analysis_enabled=_as_bool(
                os.getenv("PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_ENABLED")
            ),
            public_manual_analysis_token=(
                os.getenv("PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_TOKEN") or None
            ),
            public_manual_analysis_hourly_limit=_bounded_int(
                os.getenv("PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_HOURLY_LIMIT"),
                default=0,
                minimum=0,
                maximum=1_000,
            ),
            public_manual_analysis_cooldown_hours=_bounded_int(
                os.getenv("PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_COOLDOWN_HOURS"),
                default=24,
                minimum=1,
                maximum=168,
            ),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("PAI_LOOP_OPENAI_MODEL", "gpt-5.6-luna"),
            llm_provider=os.getenv("PAI_LOOP_LLM_PROVIDER", "openai").strip().casefold(),
            llm_gateway_base_url=(
                os.getenv("PAI_LOOP_LLM_GATEWAY_BASE_URL", "").strip().rstrip("/")
                or None
            ),
            claude_model=os.getenv("PAI_LOOP_CLAUDE_MODEL", "claude-sonnet-5"),
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
        if self.private_evidence_token and not self.private_evidence_token_valid:
            raise RuntimeError(
                "PAI_LOOP_PRIVATE_EVIDENCE_TOKEN must be a 32-512 character ASCII secret"
            )
        if self.llm_provider not in {"openai", "n8n_claude"}:
            raise RuntimeError("PAI_LOOP_LLM_PROVIDER must be openai or n8n_claude")
        if self.llm_provider == "n8n_claude":
            if self.claude_model != "claude-sonnet-5":
                raise RuntimeError(
                    "PAI_LOOP_CLAUDE_MODEL must match the deployed Claude gateway contract"
                )
            parsed = urlsplit(self.llm_gateway_base_url or "")
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise RuntimeError(
                    "PAI_LOOP_LLM_GATEWAY_BASE_URL must be an absolute credential-free HTTP(S) URL"
                )
        if self.environment.casefold() != "production":
            return
        if self.llm_provider != "n8n_claude":
            raise RuntimeError(
                "production requires PAI_LOOP_LLM_PROVIDER=n8n_claude; direct OpenAI is disabled"
            )
        if self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY must not be configured in production")
        gateway = urlsplit(self.llm_gateway_base_url or "")
        if (
            gateway.scheme != "https"
            or gateway.hostname != "n8n.kma.or.kr"
            or gateway.port not in {None, 443}
            or gateway.path.rstrip("/") != "/webhook/pai-loop-claude"
        ):
            raise RuntimeError(
                "production Claude gateway must be https://n8n.kma.or.kr/webhook/pai-loop-claude"
            )
        if self.llm_provider == "n8n_claude" and urlsplit(
            self.llm_gateway_base_url or ""
        ).scheme != "https":
            raise RuntimeError("PAI_LOOP_LLM_GATEWAY_BASE_URL must use HTTPS in production")
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
        if self.public_manual_analysis_enabled and not self.public_read_only:
            raise RuntimeError(
                "PAI_LOOP_PUBLIC_MANUAL_ANALYSIS_ENABLED requires PAI_LOOP_PUBLIC_READ_ONLY=true"
            )

    def ensure_local_directories(self) -> None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix) or self.database_url.endswith(":memory:"):
            return
        raw_path = self.database_url.removeprefix(prefix)
        if raw_path and raw_path != ":memory:":
            Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
