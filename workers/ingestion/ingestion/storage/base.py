"""Object storage abstraction.

Raw source files are archived before they are parsed, for three reasons the spec cares
about:

* **Reprocessing** — a re-run reads the archived copy instead of re-downloading ~120 MB.
* **Auditability** — "what exactly did the source give us that day" stays answerable even
  after the publisher deletes or rewrites a file.
* **Debugging** — a rejected row can be traced back to the exact bytes it came from.

Two backends implement one Protocol. ``S3ObjectStore`` targets S3/MinIO/R2/GCS-S3 in every
deployed environment; ``LocalObjectStore`` writes to a directory so tests and a laptop
without Docker exercise the same code path.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Protocol, runtime_checkable

__all__ = ["ObjectMetadata", "ObjectStore", "StorageError", "build_object_key"]


class StorageError(RuntimeError):
    """Raised when the storage backend cannot complete an operation."""


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """What we know about a stored object without downloading it."""

    key: str
    size_bytes: int
    etag: str | None = None
    last_modified: datetime | None = None
    content_type: str | None = None


@runtime_checkable
class ObjectStore(Protocol):
    """Minimal storage surface.

    Deliberately small. Anything richer (lifecycle rules, versioning, presigned URLs)
    belongs in the concrete backend, not in the interface every backend must satisfy.
    """

    def put(
        self,
        key: str,
        data: BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        """Store a stream at ``key``, overwriting any existing object."""
        ...

    def get(self, key: str) -> BinaryIO:
        """Open a stored object for reading. Raises ``StorageError`` if absent."""
        ...

    def exists(self, key: str) -> bool: ...

    def stat(self, key: str) -> ObjectMetadata | None:
        """Metadata for ``key``, or ``None`` when it does not exist."""
        ...

    def list(self, prefix: str) -> Iterator[ObjectMetadata]:
        """Yield metadata for every object under ``prefix``."""
        ...

    def delete(self, key: str) -> bool:
        """Remove ``key``. Returns whether anything was deleted."""
        ...


def build_object_key(
    source: str,
    kind: str,
    filename: str,
    *,
    variant: str | None = None,
) -> str:
    """Compose a stable, sortable archive key.

    Layout::

        raw/{source}/{variant}/{kind}/{filename}
        raw/acme-source/full/changes/2026-08-08.parquet

    Source and kind lead the path so a lifecycle rule can expire one source's raw archive
    without touching another's, and so listing a prefix returns chronological order for
    date-named files.
    """
    parts = ["raw", source]
    if variant:
        parts.append(variant)
    parts.extend([kind, filename])
    key = "/".join(p.strip("/") for p in parts if p)
    if ".." in key:
        # Defence in depth: a source-supplied filename must never escape its prefix.
        raise ValueError(f"object key must not contain '..': {key!r}")
    return key
