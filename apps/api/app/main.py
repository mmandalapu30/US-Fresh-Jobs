"""FastAPI application factory.

Milestone 1 ships the operational surface only — health, readiness, metrics and the error
contract. Job endpoints arrive in Milestone 9 on top of this same skeleton, so the
middleware, logging and error handling below are the ones that will run in production.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from jobplatform_shared import JobPlatformError, configure_logging, get_logger, get_settings
from jobplatform_shared.db import dispose_async_engine
from jobplatform_shared.logging import bind_contextvars, clear_contextvars

from .core.errors import register_exception_handlers
from .routers import health, jobs, meta

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shutdown.

    Deliberately does NOT block start-up on a database connection: a pod that cannot reach
    Postgres should still start and report ``/ready`` as false, so an orchestrator can
    surface the real problem instead of crash-looping with no diagnostics.
    """
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format, service="api")
    logger.info(
        "api.starting",
        environment=settings.environment,
        version=app.version,
        metrics_enabled=settings.metrics_enabled,
    )
    yield
    logger.info("api.stopping")
    await dispose_async_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="US Fresh Jobs Platform API",
        description=(
            "Continuously updated U.S. job data platform. See /docs for the interactive schema."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs are useful in dev but are attack surface in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # ---- middleware (executes bottom-up on the request path) ------------------

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER, "X-CSRF-Token"],
            expose_headers=[REQUEST_ID_HEADER, "Retry-After"],
            max_age=600,
        )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach a request id, time the request, and emit one structured access log."""
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        clear_contextvars()
        bind_contextvars(
            service="api",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        request.state.request_id = request_id

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            # exc_info makes the traceback part of the structured record
            logger.exception("http.request.failed", duration_ms=round(duration_ms, 2))
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        # Health checks fire constantly; logging them at INFO drowns the signal.
        log = logger.debug if request.url.path in {"/health", "/metrics"} else logger.info
        log(
            "http.request",
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    register_exception_handlers(app)

    # ---- routes ---------------------------------------------------------------
    # Operational endpoints are unversioned by convention: probes must not break on a
    # v2 rollout.
    app.include_router(health.router, tags=["operations"])
    app.include_router(meta.router, prefix=settings.api_v1_prefix, tags=["meta"])
    app.include_router(jobs.router, prefix=settings.api_v1_prefix, tags=["jobs"])

    return app


app = create_app()

__all__ = ["JSONResponse", "JobPlatformError", "app", "create_app"]
