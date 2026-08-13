"""Connector interface tests.

No real source is involved. A deliberately minimal fake connector proves the Protocol is
implementable and that the pipeline can be written against it — which is what keeps the
platform source-agnostic before any provider code exists.

The reusable contract suite at the bottom is the important part: every future connector
(Greenhouse, Ashby, Lever, ...) subclasses it and inherits these checks.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime

import pytest

from ingestion.connectors import (
    ConnectorCapabilities,
    NormalizedJob,
    RawRecord,
    SourceConnector,
    SourceFile,
    ValidationResult,
)
from jobplatform_schemas import RejectionReason


class FakeConnector:
    """A source that yields two records from one file. Structural test double only."""

    def __init__(self, *, records: list[dict] | None = None) -> None:
        self._records = (
            records
            if records is not None
            else [
                {"id": "fake:1", "title": "Engineer", "country": "United States"},
                {"id": "fake:2", "title": "Designer", "country": "Canada"},
            ]
        )

    def get_source_name(self) -> str:
        return "fake"

    def get_capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            supports_incremental=True,
            supports_full_refresh=True,
            reports_closures=True,
            expected_cadence_hours=24,
        )

    def discover(self, *, since: date | None = None) -> Sequence[SourceFile]:
        files = [
            SourceFile(
                remote_path="changes/2026-08-07.parquet", file_date=date(2026, 8, 7), etag="a"
            ),
            SourceFile(
                remote_path="changes/2026-08-08.parquet", file_date=date(2026, 8, 8), etag="b"
            ),
        ]
        if since is not None:
            files = [f for f in files if f.file_date and f.file_date >= since]
        return files

    def fetch(self, source_file: SourceFile) -> Iterator[RawRecord]:
        for index, payload in enumerate(self._records):
            yield RawRecord(
                external_id=payload["id"],
                payload=payload,
                source_path=source_file.remote_path,
                row_index=index,
            )

    def fetch_incremental(self, *, since: date | None = None) -> Iterator[RawRecord]:
        for source_file in self.discover(since=since):
            yield from self.fetch(source_file)

    def validate(self, record: RawRecord) -> ValidationResult:
        if not record.payload.get("title"):
            return ValidationResult.reject(RejectionReason.MISSING_TITLE)
        return ValidationResult.ok()

    def normalize(self, record: RawRecord) -> NormalizedJob:
        return NormalizedJob(
            source=self.get_source_name(),
            external_id=record.external_id,
            title=record.payload["title"],
            raw_country=record.payload.get("country"),
            source_path=record.source_path,
        )


class TestProtocolConformance:
    def test_fake_connector_satisfies_the_protocol(self) -> None:
        assert isinstance(FakeConnector(), SourceConnector)

    def test_protocol_requires_the_specified_methods(self) -> None:
        """The spec names this exact method set."""
        for name in (
            "discover",
            "fetch",
            "fetch_incremental",
            "normalize",
            "validate",
            "get_source_name",
        ):
            assert hasattr(FakeConnector(), name), f"connector is missing {name}()"

    def test_an_incomplete_implementation_is_rejected(self) -> None:
        class Incomplete:
            def get_source_name(self) -> str:
                return "incomplete"

        assert not isinstance(Incomplete(), SourceConnector)


class TestSourceFile:
    def test_checkpoint_key_includes_the_version(self) -> None:
        """Path alone would skip a corrected republish of the same filename."""
        v1 = SourceFile(remote_path="changes/a.parquet", etag="v1")
        v2 = SourceFile(remote_path="changes/a.parquet", etag="v2")
        assert v1.checkpoint_key() != v2.checkpoint_key()

    def test_checkpoint_key_falls_back_to_size(self) -> None:
        """Not every source exposes an etag; size is the next best version signal."""
        file = SourceFile(remote_path="changes/a.parquet", size_bytes=1234)
        assert file.checkpoint_key() == ("changes/a.parquet", "1234")

    def test_identical_files_share_a_checkpoint_key(self) -> None:
        a = SourceFile(remote_path="changes/a.parquet", etag="x")
        b = SourceFile(remote_path="changes/a.parquet", etag="x")
        assert a.checkpoint_key() == b.checkpoint_key()

    def test_is_hashable(self) -> None:
        """Frozen so discovery results can be de-duplicated with a set."""
        assert len({SourceFile(remote_path="a"), SourceFile(remote_path="a")}) == 1


class TestValidationResult:
    def test_ok(self) -> None:
        result = ValidationResult.ok()
        assert result.is_valid and result.reasons == []

    def test_rejection_carries_reasons(self) -> None:
        result = ValidationResult.reject(
            RejectionReason.MISSING_TITLE, RejectionReason.INVALID_URL, detail="both bad"
        )
        assert not result.is_valid
        assert result.reasons == [RejectionReason.MISSING_TITLE, RejectionReason.INVALID_URL]
        assert result.detail == "both bad"

    def test_a_rejection_without_a_reason_is_refused(self) -> None:
        """Rejecting a row without saying why is exactly what the spec forbids."""
        with pytest.raises(ValueError, match="at least one reason"):
            ValidationResult.reject()


class TestNormalizedJob:
    def test_timestamps_stay_distinct(self) -> None:
        """The four source-provided timestamps must remain independently addressable."""
        job = NormalizedJob(
            source="fake",
            external_id="1",
            title="Engineer",
            posted_at=datetime(2026, 8, 1, tzinfo=UTC),
            source_fetched_at=datetime(2026, 8, 8, tzinfo=UTC),
            close_at=datetime(2026, 9, 1, tzinfo=UTC),
            closed_at=datetime(2026, 8, 9, tzinfo=UTC),
        )
        assert job.posted_at != job.source_fetched_at != job.close_at != job.closed_at

    def test_location_is_kept_raw(self) -> None:
        """A connector reports what the source said; it does not decide US-ness.

        Classification belongs to LocationNormalizer so every source is judged identically.
        """
        job = NormalizedJob(
            source="fake", external_id="1", title="t", raw_state="Massachusetts", raw_country="US"
        )
        assert job.raw_state == "Massachusetts"
        assert not hasattr(job, "state_code")
        assert not hasattr(job, "country_code")

    def test_defaults_are_not_shared_between_instances(self) -> None:
        a = NormalizedJob(source="s", external_id="1", title="t")
        a.skills.append("python")
        b = NormalizedJob(source="s", external_id="2", title="t")
        assert b.skills == []


class SourceConnectorContract:
    """Reusable contract suite.

    Every real connector subclasses this and supplies ``connector``, inheriting the checks
    that must hold for all sources. Adding a provider therefore cannot skip them.
    """

    @pytest.fixture
    def connector(self) -> SourceConnector:
        raise NotImplementedError("supply a connector instance")

    def test_source_name_is_stable_and_nonempty(self, connector: SourceConnector) -> None:
        name = connector.get_source_name()
        assert name and name == connector.get_source_name()
        assert name.strip() == name

    def test_discover_returns_source_files(self, connector: SourceConnector) -> None:
        for item in connector.discover():
            assert isinstance(item, SourceFile)
            assert item.remote_path

    def test_discovered_units_have_unique_checkpoint_keys(self, connector: SourceConnector) -> None:
        """Colliding keys would make one unit silently mask another."""
        keys = [f.checkpoint_key() for f in connector.discover()]
        assert len(keys) == len(set(keys))

    def test_fetch_yields_raw_records(self, connector: SourceConnector) -> None:
        files = connector.discover()
        if not files:
            pytest.skip("source currently exposes no units")
        for record in connector.fetch(files[0]):
            assert isinstance(record, RawRecord)
            assert record.external_id
            break

    def test_validate_then_normalize(self, connector: SourceConnector) -> None:
        files = connector.discover()
        if not files:
            pytest.skip("source currently exposes no units")
        for record in connector.fetch(files[0]):
            if connector.validate(record).is_valid:
                job = connector.normalize(record)
                assert job.source == connector.get_source_name()
                assert job.external_id == record.external_id
                assert job.title
            break


class TestFakeConnectorAgainstContract(SourceConnectorContract):
    """Proves the contract suite itself works before a real connector uses it."""

    @pytest.fixture
    def connector(self) -> SourceConnector:
        return FakeConnector()


class TestFakeConnectorBehaviour:
    def test_discover_since_filters(self) -> None:
        files = FakeConnector().discover(since=date(2026, 8, 8))
        assert [f.remote_path for f in files] == ["changes/2026-08-08.parquet"]

    def test_fetch_is_lazy(self) -> None:
        """fetch() must stream: one unit can hold ~81,000 records."""
        result = FakeConnector().fetch(SourceFile(remote_path="x"))
        assert isinstance(result, Iterator)

    def test_validation_rejects_missing_title(self) -> None:
        connector = FakeConnector(records=[{"id": "fake:3", "title": ""}])
        record = next(iter(connector.fetch(SourceFile(remote_path="x"))))
        result = connector.validate(record)
        assert not result.is_valid
        assert RejectionReason.MISSING_TITLE in result.reasons

    def test_row_index_is_preserved_for_traceability(self) -> None:
        records = list(FakeConnector().fetch(SourceFile(remote_path="changes/a.parquet")))
        assert [r.row_index for r in records] == [0, 1]
        assert all(r.source_path == "changes/a.parquet" for r in records)
