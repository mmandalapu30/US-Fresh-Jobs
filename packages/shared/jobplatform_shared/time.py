"""Time helpers.

Every timestamp in this platform is timezone-aware UTC. The source publishes
``timestamp[us, tz=UTC]`` and the database stores ``timestamptz``; a naive datetime
anywhere in between is a bug, so the helpers here refuse to produce one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["days_ago", "ensure_utc", "hours_ago", "is_future", "utc_now"]


def utc_now() -> datetime:
    """Current time, timezone-aware UTC.

    Used everywhere instead of ``datetime.utcnow()`` (which returns a naive value and is
    deprecated). Patchable in tests as a single seam.
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to UTC, treating naive input as already-UTC.

    Naive input is assumed UTC rather than local: the source documents its timestamps as
    UTC, and guessing the host timezone would silently shift every posted_at.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_future(value: datetime | None, *, tolerance_hours: int = 0) -> bool:
    """True when ``value`` is beyond now plus a tolerance window.

    The source really does publish future ``posted_at`` values (verified: up to 34 days
    ahead), so this gate is load-bearing, not defensive decoration.
    """
    coerced = ensure_utc(value)
    if coerced is None:
        return False
    return coerced > utc_now() + timedelta(hours=tolerance_hours)


def days_ago(days: int) -> datetime:
    return utc_now() - timedelta(days=days)


def hours_ago(hours: int) -> datetime:
    return utc_now() - timedelta(hours=hours)
