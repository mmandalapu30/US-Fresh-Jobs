"""Exception hierarchy shared by the API and the workers."""

from __future__ import annotations

from typing import Any


class JobPlatformError(Exception):
    """Base class. Carries a stable machine-readable code plus structured context."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": self.context}


class ConfigurationError(JobPlatformError):
    code = "configuration_error"


class NotFoundError(JobPlatformError):
    code = "not_found"
    http_status = 404


class ValidationError(JobPlatformError):
    code = "validation_error"
    http_status = 422


class AuthenticationError(JobPlatformError):
    code = "authentication_error"
    http_status = 401


class AuthorizationError(JobPlatformError):
    code = "authorization_error"
    http_status = 403


class RateLimitError(JobPlatformError):
    code = "rate_limited"
    http_status = 429


class SourceUnavailableError(JobPlatformError):
    """A source connector could not reach or read its upstream."""

    code = "source_unavailable"
    http_status = 503


class IngestionError(JobPlatformError):
    code = "ingestion_error"


class DataQualityError(IngestionError):
    """A record failed a quality gate. Always carries the rejection reason."""

    code = "data_quality_error"
