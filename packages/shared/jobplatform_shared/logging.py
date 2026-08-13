"""Structured JSON logging.

One configuration shared by the API and the workers so a single log pipeline can parse
everything. Every record carries a ``request_id``/``sync_run_id`` when one is bound, which
is what makes an ingestion run traceable end to end.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars

__all__ = [
    "bind_contextvars",
    "clear_contextvars",
    "configure_logging",
    "get_logger",
    "unbind_contextvars",
]

#: Keys whose values are scrubbed before a record is emitted. Cheap defence against a
#: secret reaching the log pipeline through a stray ``log.info("...", **payload)``.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "secret_key",
        "jwt_secret",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "set-cookie",
        "cookie",
    }
)

_REDACTED = "***REDACTED***"


#: Substrings that mark a key as secret regardless of its prefix. Pattern matching rather
#: than an exhaustive list means any new credential (``*_token``, ``*_secret_key``, a
#: future source's API key) is redacted the day it is added, and the logger needs no
#: knowledge of which providers exist.
_SENSITIVE_PATTERNS = ("password", "secret", "token", "api_key", "credential", "private_key")


def _redact_sensitive(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict):
        lowered = key.lower()
        if lowered in _SENSITIVE_KEYS or any(p in lowered for p in _SENSITIVE_PATTERNS):
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(*, level: str = "INFO", fmt: str = "json", service: str = "unknown") -> None:
    """Configure structlog and route the stdlib logging tree through it.

    Args:
        level: standard logging level name.
        fmt: ``json`` for production/aggregation, ``console`` for human-readable local dev.
        service: value bound to every record, so API and worker logs are separable.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _redact_sensitive,
    ]

    if fmt == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib loggers (uvicorn, sqlalchemy, celery) through the same renderer so the
    # output stream stays uniformly parseable.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # uvicorn installs its own handlers; drop them so records are not emitted twice.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(noisy)
        lg.handlers.clear()
        lg.propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[return-value]
