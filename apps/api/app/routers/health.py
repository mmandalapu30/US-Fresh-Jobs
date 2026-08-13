"""Operational endpoints: /health, /ready, /metrics.

The distinction matters to an orchestrator:

* ``/health``  — liveness. Is the process itself healthy? Never touches a dependency, so a
  database blip cannot trigger a restart loop that makes the outage worse.
* ``/ready``   — readiness. Can this instance serve traffic *right now*? Checks
  dependencies and returns 503 when they are down, so the load balancer drains it.
* ``/metrics`` — Prometheus exposition.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from jobplatform_shared import get_logger, get_settings
from jobplatform_shared.db import check_database_health
from jobplatform_shared.time import utc_now

from ..core.metrics import render_metrics

router = APIRouter()
logger = get_logger(__name__)

_PROCESS_STARTED_AT = time.time()

#: A dependency probe must never hang a health check.
_DEPENDENCY_TIMEOUT_SECONDS = 3.0


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str
    uptime_seconds: float
    timestamp: str


class DependencyStatus(BaseModel):
    name: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: list[DependencyStatus]
    timestamp: str


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Liveness. Intentionally dependency-free — see module docstring."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service="api",
        version="0.1.0",
        environment=settings.environment,
        uptime_seconds=round(time.time() - _PROCESS_STARTED_AT, 3),
        timestamp=utc_now().isoformat(),
    )


async def _probe(name: str, coro_factory: Any) -> DependencyStatus:
    """Run one dependency check with a hard timeout and never raise."""
    started = time.perf_counter()
    try:
        await asyncio.wait_for(coro_factory(), timeout=_DEPENDENCY_TIMEOUT_SECONDS)
        return DependencyStatus(
            name=name, healthy=True, latency_ms=round((time.perf_counter() - started) * 1000, 2)
        )
    except TimeoutError:
        return DependencyStatus(
            name=name,
            healthy=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error=f"timeout after {_DEPENDENCY_TIMEOUT_SECONDS}s",
        )
    except Exception as exc:
        return DependencyStatus(
            name=name,
            healthy=False,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            # Type name only: an exception string can embed a DSN with credentials.
            error=type(exc).__name__,
        )


async def _check_redis() -> None:
    """Ping Redis. Imported lazily so the API can boot without the redis package."""
    from redis.asyncio import Redis

    settings = get_settings()
    client = Redis.from_url(str(settings.redis_url), socket_connect_timeout=2)
    try:
        await client.ping()
    finally:
        await client.aclose()


@router.get("/ready", response_model=ReadyResponse, summary="Readiness probe")
async def ready(response: Response) -> ReadyResponse:
    """Readiness. Returns 503 when any hard dependency is unavailable."""
    checks = await asyncio.gather(
        _probe("postgres", check_database_health),
        _probe("redis", _check_redis),
    )

    all_healthy = all(check.healthy for check in checks)
    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.warning(
            "api.not_ready",
            unhealthy=[c.name for c in checks if not c.healthy],
        )

    return ReadyResponse(
        status="ready" if all_healthy else "not_ready",
        dependencies=list(checks),
        timestamp=utc_now().isoformat(),
    )


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    settings = get_settings()
    if not settings.metrics_enabled:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
