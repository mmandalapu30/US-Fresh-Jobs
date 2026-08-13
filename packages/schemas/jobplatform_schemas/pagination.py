"""Cursor pagination primitives.

OFFSET pagination degrades linearly: ``OFFSET 500000`` makes Postgres walk half a million
rows before returning anything. Keyset pagination on ``(sort_key, id)`` stays O(log n) at
any depth, which is the only way the spec's "50M+ historical jobs" target survives contact
with a deep result page.

The cursor is an opaque base64 token. It is deliberately NOT signed: it encodes only
sort-key values that the client could have observed anyway, so signing would add key
management for no confidentiality gain. It IS strictly validated, because a malformed
cursor must never reach SQL.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

__all__ = ["Cursor", "CursorPage", "PageMeta", "decode_cursor", "encode_cursor"]

#: Guard against a hostile client sending a megabyte of base64.
MAX_CURSOR_LENGTH = 512


class Cursor(BaseModel):
    """Decoded keyset position: the sort value plus the tiebreaker id."""

    model_config = {"extra": "forbid"}

    # ISO-8601 string, numeric, or null depending on the sort field.
    value: str | int | float | None = None
    #: Always present. Guarantees a total order even when ``value`` ties.
    id: int = Field(ge=0)
    #: Which sort the cursor was minted for. A cursor is invalid if the sort changes.
    sort: str = Field(min_length=1, max_length=64)


def encode_cursor(cursor: Cursor) -> str:
    """Serialise a cursor to a URL-safe opaque token."""
    payload = json.dumps(cursor.model_dump(), separators=(",", ":"), default=str)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(token: str | None) -> Cursor | None:
    """Parse a cursor token.

    Returns ``None`` for absent input. Raises ``ValueError`` for malformed input so the
    API layer can answer 422 rather than silently paginating from the beginning -- a
    silent reset would look to the client like duplicated results.
    """
    if not token:
        return None
    if len(token) > MAX_CURSOR_LENGTH:
        raise ValueError("cursor token too long")

    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("malformed cursor") from exc

    if not isinstance(data, dict):
        raise ValueError("malformed cursor")
    return Cursor.model_validate(data)


class PageMeta(BaseModel):
    """Pagination envelope.

    ``total`` is intentionally optional: an exact ``COUNT(*)`` over tens of millions of
    rows costs more than the page itself. Endpoints return it only when it is cheap or
    explicitly requested.
    """

    next_cursor: str | None = None
    has_more: bool = False
    page_size: int
    total: int | None = None


class CursorPage[T](BaseModel):
    items: list[T]
    meta: PageMeta

    @field_validator("items")
    @classmethod
    def _no_none_items(cls, v: list[Any]) -> list[Any]:
        if any(item is None for item in v):
            raise ValueError("page items must not contain null")
        return v
