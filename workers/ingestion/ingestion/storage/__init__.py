"""Object storage for raw source archives, snapshots and ingestion artifacts."""

from .base import ObjectMetadata, ObjectStore, StorageError, build_object_key
from .local import LocalObjectStore
from .s3 import S3ObjectStore

__all__ = [
    "LocalObjectStore",
    "ObjectMetadata",
    "ObjectStore",
    "S3ObjectStore",
    "StorageError",
    "build_object_key",
    "get_object_store",
]


def get_object_store(settings=None):  # type: ignore[no-untyped-def]
    """Return the configured object store.

    A ``file://`` object storage URL selects the local backend; anything else is treated
    as S3-compatible. This lets a developer without Docker run the full pipeline by
    setting OBJECT_STORAGE_URL=file:///some/path.
    """
    from jobplatform_shared import get_settings

    settings = settings or get_settings()
    url = settings.object_storage_url or ""
    if url.startswith("file://"):
        return LocalObjectStore(url.removeprefix("file://"))
    return S3ObjectStore(settings)
