"""Filesystem-backed object store.

Used by the test suite and by local development without Docker. It implements the same
Protocol as ``S3ObjectStore``, so the ingestion pipeline cannot tell them apart — which is
the point: the storage-dependent code paths get exercised either way.

Not for production. ``S3ObjectStore`` is what runs in every deployed environment.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .base import ObjectMetadata, StorageError

__all__ = ["LocalObjectStore"]


class LocalObjectStore:
    """Stores objects as files under ``root``."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        """Resolve a key to a path, refusing anything that escapes the root."""
        if not key or key.startswith("/"):
            raise StorageError(f"invalid object key: {key!r}")
        candidate = (self._root / key).resolve()
        # A key such as "../../etc/passwd" must not write outside the store.
        if not candidate.is_relative_to(self._root):
            raise StorageError(f"object key escapes the storage root: {key!r}")
        return candidate

    def put(
        self,
        key: str,
        data: BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.md5(usedforsecurity=False)  # etag parity with S3, not a security control
        size = 0

        # Write to a temp file in the same directory, then atomically rename. A crash
        # mid-write therefore cannot leave a truncated object that later looks complete.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".partial")
        try:
            with os.fdopen(fd, "wb") as tmp:
                while chunk := data.read(1024 * 1024):
                    tmp.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            os.replace(tmp_name, path)
        except Exception as exc:
            Path(tmp_name).unlink(missing_ok=True)
            raise StorageError(f"failed to write {key!r}: {exc}") from exc

        return ObjectMetadata(
            key=key,
            size_bytes=size,
            etag=digest.hexdigest(),
            last_modified=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            content_type=content_type,
        )

    def get(self, key: str) -> BinaryIO:
        path = self._path_for(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key!r}")
        return path.open("rb")

    def exists(self, key: str) -> bool:
        try:
            return self._path_for(key).is_file()
        except StorageError:
            return False

    def stat(self, key: str) -> ObjectMetadata | None:
        try:
            path = self._path_for(key)
        except StorageError:
            return None
        if not path.is_file():
            return None
        stat = path.stat()
        return ObjectMetadata(
            key=key,
            size_bytes=stat.st_size,
            etag=self._etag(path),
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def list(self, prefix: str) -> Iterator[ObjectMetadata]:
        # Sorted so callers get deterministic, date-ordered results for date-named files.
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.name.endswith(".partial"):
                continue
            key = path.relative_to(self._root).as_posix()
            if key.startswith(prefix):
                stat = path.stat()
                yield ObjectMetadata(
                    key=key,
                    size_bytes=stat.st_size,
                    etag=self._etag(path),
                    last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )

    def delete(self, key: str) -> bool:
        try:
            path = self._path_for(key)
        except StorageError:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True

    def clear(self) -> None:
        """Remove everything. Test helper; not part of the ObjectStore Protocol."""
        shutil.rmtree(self._root, ignore_errors=True)
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _etag(path: Path) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
