"""S3-compatible object store.

Works against AWS S3, MinIO, Cloudflare R2 and anything else speaking the S3 API. The
endpoint, credentials and TLS flag all come from settings, so moving from local MinIO to
production S3 is a configuration change and nothing more.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, BinaryIO

from jobplatform_shared import get_logger, get_settings

from .base import ObjectMetadata, StorageError

if TYPE_CHECKING:
    from jobplatform_shared.config import Settings

__all__ = ["S3ObjectStore"]

logger = get_logger(__name__)

#: Multipart threshold. Daily deltas are ~120 MB projected, so anything above this is
#: uploaded in parts and a failed part is retried rather than the whole object.
_MULTIPART_THRESHOLD = 32 * 1024 * 1024


class S3ObjectStore:
    """``ObjectStore`` backed by an S3-compatible service."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        bucket: str | None = None,
    ) -> None:
        settings = settings or get_settings()
        self._bucket = bucket or settings.object_storage_bucket
        self._client = client if client is not None else self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=settings.object_storage_url or None,
            aws_access_key_id=settings.object_storage_access_key.get_secret_value(),
            aws_secret_access_key=settings.object_storage_secret_key.get_secret_value(),
            region_name=settings.object_storage_region,
            use_ssl=settings.object_storage_use_ssl,
            config=Config(
                # MinIO and R2 require path-style addressing; S3 accepts it too.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=15,
                read_timeout=300,
            ),
        )

    # ---- ObjectStore ---------------------------------------------------------

    def put(
        self,
        key: str,
        data: BinaryIO,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMetadata:
        from boto3.s3.transfer import TransferConfig
        from botocore.exceptions import BotoCoreError, ClientError

        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        if metadata:
            # S3 user metadata must be ASCII strings.
            extra["Metadata"] = {k: str(v) for k, v in metadata.items()}

        try:
            self._client.upload_fileobj(
                data,
                self._bucket,
                key,
                ExtraArgs=extra or None,
                Config=TransferConfig(multipart_threshold=_MULTIPART_THRESHOLD),
            )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"failed to upload {key!r}: {exc}") from exc

        stored = self.stat(key)
        if stored is None:
            raise StorageError(f"upload of {key!r} reported success but the object is absent")
        return stored

    def get(self, key: str) -> BinaryIO:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise StorageError(f"object not found: {key!r}") from exc
            raise StorageError(f"failed to read {key!r}: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"failed to read {key!r}: {exc}") from exc
        return response["Body"]

    def exists(self, key: str) -> bool:
        return self.stat(key) is not None

    def stat(self, key: str) -> ObjectMetadata | None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise StorageError(f"failed to stat {key!r}: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"failed to stat {key!r}: {exc}") from exc

        return ObjectMetadata(
            key=key,
            size_bytes=head["ContentLength"],
            etag=head.get("ETag", "").strip('"') or None,
            last_modified=head.get("LastModified"),
            content_type=head.get("ContentType"),
        )

    def list(self, prefix: str) -> Iterator[ObjectMetadata]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for item in page.get("Contents", ()):
                    yield ObjectMetadata(
                        key=item["Key"],
                        size_bytes=item["Size"],
                        etag=item.get("ETag", "").strip('"') or None,
                        last_modified=item.get("LastModified"),
                    )
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"failed to list prefix {prefix!r}: {exc}") from exc

    def delete(self, key: str) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        if not self.exists(key):
            return False
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"failed to delete {key!r}: {exc}") from exc
        return True

    # ---- helpers -------------------------------------------------------------

    def ensure_bucket(self) -> None:
        """Create the bucket if it is missing. Used by local/dev bootstrap only."""
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            logger.info("storage.creating_bucket", bucket=self._bucket)
            self._client.create_bucket(Bucket=self._bucket)
