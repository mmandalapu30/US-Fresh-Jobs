"""OpenJobData source facts: paths, columns, and payload decoding.

Every constant here was measured against the live bucket, not assumed. See
``docs/00-source-verification.md`` and re-check with ``python scripts/verify_source.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

__all__ = [
    "COMPANY_COLUMNS",
    "DELTA_FILENAME_RE",
    "JOB_COLUMNS_FULL",
    "JOB_COLUMNS_MINIMAL",
    "SOURCE_NAME",
    "OpenJobDataPaths",
    "decode_nested_json",
    "parse_delta_date",
]

#: Stored in ``jobs.source`` and ``job_sources.source``. Permanent once data exists:
#: changing it would orphan every provenance row.
SOURCE_NAME: Final = "openjobdata"

#: The 14 flat columns present in both variants (VERIFIED against the Parquet footer).
JOB_COLUMNS_MINIMAL: Final[tuple[str, ...]] = (
    "id",
    "job_id",
    "company_id",
    "title",
    "department",
    "employment_type",
    "workplace_type",
    "country",
    "is_remote",
    "posted_at",
    "apply_url",
    "fetched_time",
    "status",
    "close_time",
)

#: The projection actually read from the ``full`` variant.
#:
#: ``entire_json`` is deliberately EXCLUDED. It is the raw scraper payload, is never read
#: by this platform, and accounts for 49.5% of the file (118 MB of 238 MB on 2026-08-08).
#: Omitting it halves the daily download for free, because HfFileSystem is seekable and
#: PyArrow issues HTTP range requests per column.
JOB_COLUMNS_FULL: Final[tuple[str, ...]] = (*JOB_COLUMNS_MINIMAL, "job_model_json")

#: Columns read from ``companies.parquet`` (109,166 rows). Values arrive lowercased.
COMPANY_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "website",
    "ats",
    "career_url",
    "industry",
    "size",
    "locality",
    "region",
    "country",
    "linkedin_url",
)

#: Delta files are named exactly ``YYYY-MM-DD.parquet``.
DELTA_FILENAME_RE: Final = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.parquet$")

#: Source status values. ``closed`` means the source detected the posting was taken down.
STATUS_ACTIVE: Final = "active"
STATUS_CLOSED: Final = "closed"

#: ``workplace_type`` values. ``tbc`` is the source's "unknown" and is ~41% of rows -- it
#: must not be mistaken for on-site.
WORKPLACE_UNKNOWN: Final = "tbc"


@dataclass(frozen=True, slots=True)
class OpenJobDataPaths:
    """Path builder for the bucket layout.

    The bucket URI is configuration (``OPENJOBDATA_BUCKET_URI``) rather than a literal, so
    a mirror or a moved dataset needs no code change.
    """

    bucket_uri: str
    variant: str

    def __post_init__(self) -> None:
        if self.variant not in {"full", "minimal"}:
            raise ValueError(f"variant must be 'full' or 'minimal', got {self.variant!r}")

    @property
    def _root(self) -> str:
        # HfFileSystem paths omit the scheme: "hf://buckets/x" -> "buckets/x"
        return self.bucket_uri.removeprefix("hf://").rstrip("/")

    @property
    def changes_dir(self) -> str:
        """Daily delta files: only new or changed jobs for a given date."""
        return f"{self._root}/data/{self.variant}/changes"

    @property
    def base_dir(self) -> str:
        """Full snapshot shards, ``part-*.parquet``. Used for backfill only."""
        return f"{self._root}/data/{self.variant}"

    @property
    def companies_path(self) -> str:
        return f"{self._root}/data/companies/companies.parquet"

    def delta_path(self, day: date) -> str:
        return f"{self.changes_dir}/{day.isoformat()}.parquet"


def parse_delta_date(filename: str) -> date | None:
    """Extract the logical date from a delta filename, or ``None`` if it is not one.

    Returning ``None`` rather than raising lets ``discover()`` ignore unexpected files
    (a README, a temp upload) without failing the whole run.
    """
    match = DELTA_FILENAME_RE.match(filename)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group("date"))
    except ValueError:
        return None


def decode_nested_json(value: object, *, max_depth: int = 3) -> dict[str, Any] | None:
    """Decode the source's double-encoded JSON columns.

    ``job_model_json`` is a JSON *string* whose content is itself JSON, so a single
    ``json.loads`` returns a ``str`` rather than a dict. Verified against real rows.

    Returns ``None`` for anything that will not decode to a dict, so the caller can reject
    the row with a reason instead of crashing the file.
    """
    current: object = value
    for _ in range(max_depth):
        if isinstance(current, dict):
            return current
        if not isinstance(current, str | bytes):
            return None
        try:
            current = json.loads(current)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    return current if isinstance(current, dict) else None
