"""Uniform error contract.

Every failure the API emits has the same JSON shape, so clients never have to branch on
where an error came from:

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {...}}}

Unhandled exceptions never leak internals: the traceback goes to the structured log and
the client receives a generic message plus the request id needed to correlate.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobplatform_shared import JobPlatformError, get_logger

logger = get_logger(__name__)


def _payload(
    code: str, message: str, request: Request, details: Any | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        }
    }
    if details:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(JobPlatformError)
    async def _domain_error(request: Request, exc: JobPlatformError) -> JSONResponse:
        logger.warning("api.domain_error", code=exc.code, message=exc.message, **exc.context)
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code, exc.message, request, exc.context or None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Surface which fields failed, but not the submitted values -- a rejected
        # password or token must never be echoed back or logged.
        details = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "issue": err.get("msg")}
            for err in exc.errors()
        ]
        logger.info("api.validation_error", field_count=len(details))
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_payload("validation_error", "Request validation failed", request, details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "authentication_error",
            403: "authorization_error",
            404: "not_found",
            405: "method_not_allowed",
            429: "rate_limited",
        }.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code, str(exc.detail), request),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full detail to the log, nothing sensitive to the client.
        logger.exception("api.unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload(
                "internal_error",
                "An unexpected error occurred. Quote the request_id when reporting this.",
                request,
            ),
        )
