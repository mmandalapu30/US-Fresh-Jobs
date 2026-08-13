"""Object storage tests.

The same suite runs against both backends. That is the point: if ``LocalObjectStore`` and
``S3ObjectStore`` diverge in behaviour, code that works in tests will fail in production.
The S3 backend is exercised against ``moto``, which implements the real S3 API surface.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ingestion.storage import LocalObjectStore, ObjectStore, StorageError, build_object_key
from ingestion.storage.s3 import S3ObjectStore

BUCKET = "test-archive"


@pytest.fixture
def local_store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture
def s3_store() -> Iterator[S3ObjectStore]:
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")

    with moto.mock_aws():
        client: Any = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield S3ObjectStore(client=client, bucket=BUCKET)


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest) -> ObjectStore:
    """Every behavioural test runs against both backends."""
    return request.getfixturevalue(f"{request.param}_store")


class TestObjectStoreContract:
    def test_both_backends_satisfy_the_protocol(self, store: ObjectStore) -> None:
        assert isinstance(store, ObjectStore)

    def test_put_then_get_round_trip(self, store: ObjectStore) -> None:
        payload = b"parquet-bytes-would-go-here"
        meta = store.put("raw/src/changes/2026-08-08.parquet", io.BytesIO(payload))

        assert meta.size_bytes == len(payload)
        with store.get("raw/src/changes/2026-08-08.parquet") as handle:
            assert handle.read() == payload

    def test_exists(self, store: ObjectStore) -> None:
        assert not store.exists("raw/src/missing.parquet")
        store.put("raw/src/present.parquet", io.BytesIO(b"x"))
        assert store.exists("raw/src/present.parquet")

    def test_stat_returns_none_when_absent(self, store: ObjectStore) -> None:
        assert store.stat("raw/src/nope.parquet") is None

    def test_stat_reports_size_and_etag(self, store: ObjectStore) -> None:
        store.put("raw/src/a.parquet", io.BytesIO(b"hello world"))
        meta = store.stat("raw/src/a.parquet")
        assert meta is not None
        assert meta.size_bytes == 11
        assert meta.etag  # both backends expose an md5-style etag

    def test_get_missing_raises(self, store: ObjectStore) -> None:
        with pytest.raises(StorageError, match="not found"):
            store.get("raw/src/missing.parquet")

    def test_list_by_prefix(self, store: ObjectStore) -> None:
        for name in ("2026-08-06", "2026-08-07", "2026-08-08"):
            store.put(f"raw/src/changes/{name}.parquet", io.BytesIO(b"x"))
        store.put("raw/other/thing.parquet", io.BytesIO(b"x"))

        keys = sorted(m.key for m in store.list("raw/src/changes/"))
        assert keys == [
            "raw/src/changes/2026-08-06.parquet",
            "raw/src/changes/2026-08-07.parquet",
            "raw/src/changes/2026-08-08.parquet",
        ]

    def test_list_empty_prefix_yields_nothing(self, store: ObjectStore) -> None:
        assert list(store.list("raw/nothing/")) == []

    def test_delete(self, store: ObjectStore) -> None:
        store.put("raw/src/gone.parquet", io.BytesIO(b"x"))
        assert store.delete("raw/src/gone.parquet") is True
        assert store.exists("raw/src/gone.parquet") is False

    def test_delete_missing_returns_false(self, store: ObjectStore) -> None:
        assert store.delete("raw/src/never-existed.parquet") is False

    def test_put_overwrites(self, store: ObjectStore) -> None:
        """Re-archiving a corrected file must replace it, not append or fail."""
        store.put("raw/src/a.parquet", io.BytesIO(b"first"))
        store.put("raw/src/a.parquet", io.BytesIO(b"second-longer"))
        with store.get("raw/src/a.parquet") as handle:
            assert handle.read() == b"second-longer"

    def test_large_object_streams(self, store: ObjectStore) -> None:
        """Daily deltas are ~120 MB; the write path must not assume a small payload."""
        payload = b"a" * (5 * 1024 * 1024)
        meta = store.put("raw/src/big.parquet", io.BytesIO(payload))
        assert meta.size_bytes == len(payload)


class TestLocalStoreSafety:
    """Path traversal matters here: archive keys are partly source-derived."""

    @pytest.mark.parametrize("key", ["../escape.txt", "a/../../escape.txt", "/abs/path.txt"])
    def test_rejects_keys_escaping_the_root(self, local_store: LocalObjectStore, key: str) -> None:
        with pytest.raises(StorageError):
            local_store.put(key, io.BytesIO(b"x"))

    def test_partial_files_are_not_listed(
        self, local_store: LocalObjectStore, tmp_path: Path
    ) -> None:
        """A crashed write leaves a .partial file; it must never look like a real object."""
        local_store.put("raw/src/a.parquet", io.BytesIO(b"x"))
        (Path(local_store._path_for("raw/src/a.parquet")).parent / "b.partial").write_bytes(b"junk")

        keys = [m.key for m in local_store.list("raw/")]
        assert keys == ["raw/src/a.parquet"]

    def test_write_is_atomic(self, local_store: LocalObjectStore) -> None:
        """A failing read mid-put must not leave a half-written object behind."""

        class Exploding(io.RawIOBase):
            def read(self, _size: int = -1) -> bytes:
                raise OSError("network died")

        with pytest.raises(StorageError):
            local_store.put("raw/src/doomed.parquet", Exploding())  # type: ignore[arg-type]
        assert not local_store.exists("raw/src/doomed.parquet")


class TestObjectKeys:
    def test_layout(self) -> None:
        key = build_object_key("acme", "changes", "2026-08-08.parquet", variant="full")
        assert key == "raw/acme/full/changes/2026-08-08.parquet"

    def test_variant_optional(self) -> None:
        assert build_object_key("acme", "base", "part-0.parquet") == "raw/acme/base/part-0.parquet"

    def test_date_named_keys_sort_chronologically(self) -> None:
        """Lexical order must equal chronological order, so prefix listing is ordered."""
        keys = [
            build_object_key("s", "changes", f"2026-08-{day:02d}.parquet") for day in (8, 6, 10, 7)
        ]
        assert sorted(keys) == [
            "raw/s/changes/2026-08-06.parquet",
            "raw/s/changes/2026-08-07.parquet",
            "raw/s/changes/2026-08-08.parquet",
            "raw/s/changes/2026-08-10.parquet",
        ]

    def test_rejects_traversal(self) -> None:
        with pytest.raises(ValueError, match=r"\.\."):
            build_object_key("acme", "changes", "../../../etc/passwd")
