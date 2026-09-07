from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import __version__
from .analysis_api import router as analysis_persistence_router
from .api import router
from .accounts import router as accounts_router
from .company_awards import router as company_awards_router
from .config import Settings
from .database import Base, build_engine, build_session_factory
from .daily_operations import router as daily_operations_router
from .demo import seed_synthetic_replay
from .migrations import apply_additive_migrations
from .manual_analysis import router as manual_analysis_router
from .outcomes_api import router as bid_outcomes_router
from .outcome_feedback import router as outcome_feedback_router
from .operator_decisions import router as operator_decisions_router
from .performance_records import router as performance_records_router
from .prespec_api import router as prespec_router
from .private_company_evidence import router as private_company_evidence_router
from .pps_discovery import router as pps_discovery_router
from .public_performance import public_performance_router
from .quantitative_scoring import quantitative_scoring_router
from .reference_api import router as reference_data_router
from .reference_registry import sync_packaged_reference_data, sync_public_company_profile
from .result_learning import router as result_learning_router
from .recovery_diagnostics import PATH as recovery_diagnostics_path, router as recovery_diagnostics_router
from .schemas import HealthResponse
from .teams_readiness import router as teams_readiness_router


def _scrub_private_performance_search_query(request: Request) -> None:
    """Remove private free text before Uvicorn formats its access-log line."""

    path = str(request.scope.get("path") or "").rstrip("/")
    if path != "/api/v1/performance-records" or not (
        request.headers.get("x-pai-loop-api-key")
        or request.headers.get("x-pai-private-evidence-token")
    ):
        return
    raw_query = request.scope.get("query_string", b"")
    if not isinstance(raw_query, bytes) or not raw_query:
        return
    try:
        query_items = parse_qsl(raw_query.decode("ascii"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return
    if not any(name == "q" for name, _value in query_items):
        return
    # The route rejects this request after authenticating the supplied strong
    # credential. Clearing the scope here also clears the string Uvicorn reads
    # after the response when it writes the access log.
    request.state.private_performance_query_blocked = True
    request.scope["query_string"] = b""


def create_app(*, database_url: str | None = None, seed_synthetic: bool | None = None) -> FastAPI:
    settings = Settings.from_env(database_url=database_url)
    if seed_synthetic is not None:
        settings = Settings(
            environment=settings.environment,
            database_url=settings.database_url,
            seed_synthetic=seed_synthetic,
            cors_origins=settings.cors_origins,
            log_level=settings.log_level,
            api_key=settings.api_key,
            private_evidence_token=settings.private_evidence_token,
            public_read_only=settings.public_read_only,
            department_accounts_enabled=settings.department_accounts_enabled,
            public_manual_analysis_enabled=settings.public_manual_analysis_enabled,
            public_manual_analysis_token=settings.public_manual_analysis_token,
            public_manual_analysis_hourly_limit=settings.public_manual_analysis_hourly_limit,
            public_manual_analysis_cooldown_hours=settings.public_manual_analysis_cooldown_hours,
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model,
            llm_provider=settings.llm_provider,
            llm_gateway_base_url=settings.llm_gateway_base_url,
            claude_model=settings.claude_model,
            pps_api_key=settings.pps_api_key,
            pps_base_url=settings.pps_base_url,
            pps_notice_operation=settings.pps_notice_operation,
            pps_award_operation=settings.pps_award_operation,
        )
    settings.validate_security()
    settings.ensure_local_directories()
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        apply_additive_migrations(engine)
        with session_factory() as session:
            sync_packaged_reference_data(session)
            sync_public_company_profile(session)
            if settings.seed_synthetic:
                seed_synthetic_replay(session)
            session.commit()
        yield
        engine.dispose()

    application = FastAPI(
        title="PAI LOOP API",
        summary="근거 우선 공공입찰 의사결정 지원",
        description=(
            "LLM은 조건을 구조화하고, 이 API의 결정론적 규칙 엔진이 "
            "마감일 기준 증빙으로 PASS/REVIEW/FAIL을 판정하며, 기본 실패 사유는 DF-000으로 구분합니다."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def teams_tab_security_headers(request: Request, call_next):
        _scrub_private_performance_search_query(request)
        response = await call_next(request)
        if request.url.path == recovery_diagnostics_path:
            response.headers["Cache-Control"] = "no-store"
        # Teams tabs are first-party HTTPS pages rendered by Microsoft inside
        # an iframe. CSP is the standards-based allowlist; X-Frame-Options is
        # intentionally omitted because DENY/SAMEORIGIN would block Teams.
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'self' https://teams.microsoft.com "
            "https://*.teams.microsoft.com https://*.cloud.microsoft"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.url.path.startswith(
            ("/api/v1/performance-records", "/api/v1/operator-evidence", "/api/v1/accounts", "/api/v1/operator-decisions", "/api/v1/result-learning")
        ):
            response.headers["Cache-Control"] = "no-store"
        if "x-frame-options" in response.headers:
            del response.headers["x-frame-options"]
        return response

    application.include_router(router)
    application.include_router(accounts_router)
    application.include_router(public_performance_router)
    application.include_router(daily_operations_router)
    application.include_router(quantitative_scoring_router)
    application.include_router(reference_data_router)
    application.include_router(bid_outcomes_router)
    application.include_router(outcome_feedback_router)
    application.include_router(performance_records_router)
    application.include_router(result_learning_router)
    application.include_router(operator_decisions_router)
    application.include_router(manual_analysis_router)
    application.include_router(recovery_diagnostics_router)
    application.include_router(pps_discovery_router)
    application.include_router(prespec_router)
    application.include_router(private_company_evidence_router)
    application.include_router(company_awards_router)
    application.include_router(analysis_persistence_router)
    application.include_router(teams_readiness_router)

    @application.get("/healthz", response_model=HealthResponse, tags=["operations"])
    def health(request: Request) -> HealthResponse:
        database_status = "ok"
        try:
            with request.app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception:  # pragma: no cover - operational dependency status
            logging.exception("database health check failed")
            database_status = "error"
        status_text = "ok" if database_status == "ok" else "degraded"
        return HealthResponse(
            status=status_text,
            service="pai-loop-api",
            version=__version__,
            database=database_status,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_exception(request: Request, exc: RequestValidationError):
        if request.url.path == recovery_diagnostics_path:
            return JSONResponse(status_code=422, headers={"Cache-Control": "no-store"}, content={"detail": "진단 공고 목록을 확인해 주세요.", "code": "DIAGNOSTIC_VALIDATION_ERROR"})
        if request.url.path.startswith("/api/v1/accounts"):
            return JSONResponse(status_code=422, headers={"Cache-Control": "no-store"}, content={"detail": "계정 입력 형식을 확인해 주세요.", "code": "ACCOUNT_VALIDATION_ERROR"})
        if (
            request.url.path == "/api/v1/performance-records/private-import"
            or request.url.path.startswith("/api/v1/operator-evidence")
        ):
            return JSONResponse(
                status_code=422,
                headers={"Cache-Control": "no-store"},
                content={
                    "detail": "비공개 증빙 입력 형식을 확인해 주세요.",
                    "code": "PRIVATE_EVIDENCE_VALIDATION_ERROR",
                },
            )
        return await request_validation_exception_handler(request, exc)

    @application.exception_handler(Exception)
    async def unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
        logging.exception("unhandled API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "요청을 처리하지 못했습니다.", "code": "INTERNAL_ERROR"},
        )

    static_dir = Path(__file__).parent / "static"
    if (static_dir / "index.html").exists():
        index_file = static_dir / "index.html"

        def frontend_index() -> FileResponse:
            return FileResponse(index_file)

        frontend_routes = (
            "/",
            "/notices",
            "/reviews",
            "/urgent",
            "/fail",
            "/cancelled",
            "/result-missing",
            "/decisions",
            "/results",
            "/awards",
            "/prespec",
            "/performance",
        )
        for frontend_route in frontend_routes:
            route_name = frontend_route.strip("/").replace("/", "-") or "dashboard"
            application.add_api_route(
                frontend_route,
                frontend_index,
                methods=["GET"],
                include_in_schema=False,
                name=f"frontend-{route_name}",
            )

        application.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:
        @application.get("/", include_in_schema=False)
        def index() -> dict[str, str]:
            return {
                "service": "PAI LOOP",
                "docs": "/api/docs",
                "health": "/healthz",
                "demo": "POST /api/v1/ingestion/replay",
            }

    return application


app = create_app()


def run() -> None:
    uvicorn.run("pai_loop.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
