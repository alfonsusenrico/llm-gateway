"""FastAPI application factory: middleware, error handling, and health routes."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from llm_gateway.api.auth import Authenticator
from llm_gateway.api.problems import (
    install_problem_handlers,
    problem_response,
    unhandled_error_response,
)
from llm_gateway.api.routes_allocation import GrantDirectory
from llm_gateway.api.routes_allocation import router as allocation_router
from llm_gateway.api.routes_projects import router as projects_router
from llm_gateway.db.pool import create_pool, database_ready
from llm_gateway.logging_setup import configure_logging, request_id_var
from llm_gateway.notifier.events import wait_for_background_alerts
from llm_gateway.settings import Settings, load_settings

REQUEST_ID_HEADER = "X-Request-ID"
# A caller-supplied request ID is kept only when it is short and plain, so it is
# safe to log and echo.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_READINESS_RETRY_AFTER_SECONDS = 5


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = create_pool(settings)
        # Opening without waiting lets the process start while the database is
        # still unavailable; readiness reports it.
        await pool.open(wait=False)
        app.state.db_pool = pool
        app.state.authenticator = Authenticator(settings, pool)
        app.state.grant_directory = GrantDirectory(pool)
        try:
            yield
        finally:
            await wait_for_background_alerts()
            await pool.close()

    app = FastAPI(
        title="LLM Gateway",
        version=settings.app_revision,
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env == "development" else None,
        redoc_url=None,
    )
    install_problem_handlers(app)
    app.include_router(projects_router)
    app.include_router(allocation_router)

    @app.middleware("http")
    async def assign_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = supplied if _SAFE_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        except Exception as exc:
            # Handled here rather than by an outer handler so the problem body and
            # header still carry this request's ID.
            response = unhandled_error_response(exc)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness: the process is serving requests."""
        return {"status": "ok", "revision": settings.app_revision}

    @app.get(
        "/health/ready",
        tags=["health"],
        responses={503: {"description": "A dependency is unavailable."}},
    )
    async def ready(request: Request) -> JSONResponse:
        """Readiness: the database answers."""
        if not await database_ready(request.app.state.db_pool):
            return problem_response(
                code="dependency_unavailable",
                status=503,
                title="Database is unavailable",
                retry_after_seconds=_READINESS_RETRY_AFTER_SECONDS,
            )
        return JSONResponse({"status": "ready", "revision": settings.app_revision})

    return app
