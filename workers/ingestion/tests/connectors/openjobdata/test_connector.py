"""OpenJobDataConnector tests.

Two layers:

* **Offline** (default) — a synthetic Parquet file built to match the verified real schema
  exactly, including the double-encoded ``job_model_json`` and the awkward real-world
  values (empty ``country``, ``tbc`` workplace, NULL and future ``posted_at``).
* **Live** (``-m network``) — runs against the real bucket. Excluded from normal runs so
  the suite stays fast and offline, but available to prove the connector still works.

The offline fixtures encode measured facts, so if the source changes shape the live tests
fail and these keep passing — which is exactly how the difference gets noticed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ingestion.connectors import RawRecord, SourceConnector, SourceFile
from ingestion.connectors.openjobdata import OpenJobDataConnector, decode_nested_json
from ingestion.connectors.openjobdata.schema import (
    JOB_COLUMNS_FULL,
    JOB_COLUMNS_MINIMAL,
    SOURCE_NAME,
    OpenJobDataPaths,
    parse_delta_date,
)
from ingestion.storage import LocalObjectStore
from jobplatform_schemas import RejectionReason

# Import the shared contract suite so the real connector inherits every check.
from ...test_connector_contract import SourceConnectorContract

BUCKET = "buckets/Invicto69/Jobs-Dataset-bucket"
CHANGES = f"{BUCKET}/data/full/changes"


# --------------------------------------------------------------------------- fixtures


def _job_model(**overrides: Any) -> str:
    """Build a job_model_json value with the verified structure and double encoding."""
    model = {
        "apply_url": "https://jobs.example.com/job/abc",
        "ats_provider": "jobvite",
        "compensation": {
            "benefits": [],
            "currency": "USD",
            "interval": "annually",
            "max_amount": 170000.0,
            "min_amount": 140000.0,
        },
        "department": "Engineering",
        "description_html": "<p>Build things</p>",
        "description_plain": "Build things",
        "employment_type": "Full-time",
        "expires_at": "2026-09-01T00:00:00+00:00",
        "job_id": "abc",
        "location": {
            "city": "Detroit",
            "country": "United States",
            "is_remote": False,
            "postal_code": None,
            "raw_location_text": "            Detroit",
            "state": "Michigan",
            "workplace_type": "onsite",
        },
        "metadata": {},
        "posted_at": "2026-08-08T12:00:00+00:00",
        "requirements": ["python"],
        "responsibilities": ["ship"],
        "seniority": "senior",
        "title": "Senior Software Engineer",
    }
    model.update(overrides)
    # The source double-encodes: the Parquet value is a JSON string containing JSON.
    return json.dumps(json.dumps(model))


def _rows() -> list[dict[str, Any]]:
    """Rows mirroring the real distribution, including the awkward cases."""
    return [
        # 0: complete, valid US job
        {
            "id": "jobvite:acme/abc",
            "job_id": "abc",
            "company_id": 753,
            "title": "Senior Software Engineer",
            "department": "Engineering",
            "employment_type": "Full time",
            "workplace_type": "onsite",
            "country": "United States",
            "is_remote": False,
            "posted_at": datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
            "apply_url": "https://jobs.example.com/job/abc",
            "fetched_time": datetime(2026, 8, 8, 14, 0, tzinfo=UTC),
            "status": "active",
            "close_time": None,
            "job_model_json": _job_model(),
        },
        # 1: closed job -- close_time set, status closed
        {
            "id": "workday:beta/def",
            "job_id": "def",
            "company_id": 999,
            "title": "Data Analyst",
            "department": None,
            "employment_type": "Part-time",
            "workplace_type": "tbc",  # ~41% of real rows
            "country": "",  # 3,331 empty strings in the real file
            "is_remote": False,
            "posted_at": None,  # 19.3% of real rows
            "apply_url": "https://jobs.example.com/job/def",
            "fetched_time": datetime(2026, 8, 8, 14, 0, tzinfo=UTC),
            "status": "closed",
            "close_time": datetime(2026, 8, 8, 13, 0, tzinfo=UTC),
            "job_model_json": _job_model(
                location={
                    "city": None,
                    "country": None,
                    "is_remote": False,
                    "postal_code": None,
                    "raw_location_text": "            South Carolina",
                    "state": None,
                    "workplace_type": "onsite",
                },
                compensation=None,
                expires_at=None,
            ),
        },
        # 2: future posted_at (the real max was 34 days ahead) + state as a code
        {
            "id": "greenhouse:gamma/ghi",
            "job_id": "ghi",
            "company_id": 753,
            "title": "Remote Engineer",
            "department": None,
            "employment_type": None,
            "workplace_type": "remote",
            "country": "REMOTE",  # junk value, 270 real rows
            "is_remote": True,
            "posted_at": datetime(2026, 9, 11, tzinfo=UTC),
            "apply_url": "https://jobs.example.com/job/ghi",
            "fetched_time": datetime(2026, 8, 8, 14, 0, tzinfo=UTC),
            "status": "active",
            "close_time": None,
            "job_model_json": _job_model(
                location={
                    "city": "Fairborn",
                    "country": "United States",
                    "is_remote": True,
                    "postal_code": "45324",
                    "raw_location_text": "Fairborn, OH",
                    "state": "OH",  # code, not name -- both forms occur
                    "workplace_type": "remote",
                },
                compensation={
                    "benefits": [],
                    "currency": "USD",
                    "interval": "hourly",
                    "max_amount": None,
                    "min_amount": 40.0,
                },
            ),
        },
        # 3: invalid -- no title, no apply_url
        {
            "id": "adp:delta/jkl",
            "job_id": "jkl",
            "company_id": 1,
            "title": "",
            "department": None,
            "employment_type": None,
            "workplace_type": "onsite",
            "country": "Canada",
            "is_remote": False,
            "posted_at": datetime(2026, 8, 1, tzinfo=UTC),
            "apply_url": None,
            "fetched_time": datetime(2026, 8, 8, 14, 0, tzinfo=UTC),
            "status": "active",
            "close_time": None,
            "job_model_json": _job_model(),
        },
        # 4: unparseable job model
        {
            "id": "lever:eps/mno",
            "job_id": "mno",
            "company_id": 2,
            "title": "Broken Payload",
            "department": None,
            "employment_type": None,
            "workplace_type": "onsite",
            "country": "United States",
            "is_remote": False,
            "posted_at": datetime(2026, 8, 1, tzinfo=UTC),
            "apply_url": "https://jobs.example.com/job/mno",
            "fetched_time": datetime(2026, 8, 8, 14, 0, tzinfo=UTC),
            "status": "active",
            "close_time": None,
            "job_model_json": "{not valid json at all",
        },
    ]


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns = {name: [row.get(name) for row in rows] for name in JOB_COLUMNS_FULL}
    table = pa.table(columns)
    # Many small row groups, as in the real files (~95 groups for 81k rows).
    pq.write_table(table, path, row_group_size=2)


def _write_companies(path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(
        pa.table(
            {
                "id": [753, 999],
                "name": ["ABC Corporation", "Beta LLC"],
                "website": ["abc.com", "beta.com"],
                "ats": ["jobvite", "workday"],
                "career_url": ["https://abc.com/careers", "https://beta.com/jobs"],
                "industry": ["software", "finance"],
                "size": ["201-500", "51-200"],
                "locality": ["detroit", "austin"],
                "region": ["michigan", "texas"],
                "country": ["united states", "united states"],
                "linkedin_url": ["linkedin.com/company/abc", "linkedin.com/company/beta"],
            }
        ),
        path,
    )


class FakeHfFileSystem:
    """In-memory stand-in for HfFileSystem covering only what the connector uses."""

    def __init__(self, entries: list[dict[str, Any]], files: dict[str, Path]) -> None:
        self._entries = entries
        self._files = files
        self.opened: list[str] = []

    def ls(self, path: str, detail: bool = True) -> list[dict[str, Any]]:
        if path != CHANGES:
            raise FileNotFoundError(path)
        return self._entries

    def open(self, path: str, mode: str = "rb") -> Any:
        self.opened.append(path)
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path].open("rb")


@pytest.fixture
def parquet_dir(tmp_path: Path) -> Path:
    _write_parquet(tmp_path / "2026-08-07.parquet", _rows())
    _write_parquet(tmp_path / "2026-08-08.parquet", _rows())
    _write_parquet(tmp_path / "2026-08-10.parquet", _rows()[:1])
    _write_companies(tmp_path / "companies.parquet")
    return tmp_path


@pytest.fixture
def fake_fs(parquet_dir: Path) -> FakeHfFileSystem:
    entries = [
        {
            "name": f"{CHANGES}/2026-08-07.parquet",
            "size": 100,
            "type": "file",
            "xet_hash": "hash-07",
            "uploaded_at": "2026-08-07 10:40:36+00:00",
        },
        {
            "name": f"{CHANGES}/2026-08-08.parquet",
            "size": 200,
            "type": "file",
            "xet_hash": "hash-08",
            "uploaded_at": "2026-08-08 14:15:48+00:00",
        },
        # 2026-08-09 deliberately absent: the publisher really does skip days.
        {
            "name": f"{CHANGES}/2026-08-10.parquet",
            "size": 300,
            "type": "file",
            "xet_hash": "hash-10",
            "uploaded_at": "2026-08-10 06:33:14+00:00",
        },
        # Non-delta entries must be ignored rather than crash discovery.
        {"name": f"{CHANGES}/README.md", "size": 10, "type": "file"},
        {"name": f"{CHANGES}/partial.parquet.tmp", "size": 10, "type": "file"},
    ]
    files = {
        f"{CHANGES}/2026-08-07.parquet": parquet_dir / "2026-08-07.parquet",
        f"{CHANGES}/2026-08-08.parquet": parquet_dir / "2026-08-08.parquet",
        f"{CHANGES}/2026-08-10.parquet": parquet_dir / "2026-08-10.parquet",
        f"{BUCKET}/data/companies/companies.parquet": parquet_dir / "companies.parquet",
    }
    return FakeHfFileSystem(entries, files)


@pytest.fixture
def connector(fake_fs: FakeHfFileSystem, minimal_env: None) -> OpenJobDataConnector:
    from jobplatform_shared import get_settings

    get_settings.cache_clear()
    return OpenJobDataConnector(filesystem=fake_fs)


@pytest.fixture
def records(connector: OpenJobDataConnector) -> list[RawRecord]:
    source_file = SourceFile(
        remote_path=f"{CHANGES}/2026-08-08.parquet", file_date=date(2026, 8, 8)
    )
    return list(connector.fetch(source_file))


# ----------------------------------------------------------------------------- tests


class TestPaths:
    def test_strips_the_hf_scheme(self) -> None:
        paths = OpenJobDataPaths(bucket_uri="hf://buckets/Owner/Name", variant="full")
        assert paths.changes_dir == "buckets/Owner/Name/data/full/changes"
        assert paths.companies_path == "buckets/Owner/Name/data/companies/companies.parquet"

    def test_variant_is_part_of_the_path(self) -> None:
        minimal = OpenJobDataPaths(bucket_uri="hf://b", variant="minimal")
        assert minimal.changes_dir == "b/data/minimal/changes"

    def test_rejects_an_unknown_variant(self) -> None:
        with pytest.raises(ValueError, match="must be 'full' or 'minimal'"):
            OpenJobDataPaths(bucket_uri="hf://b", variant="medium")

    def test_delta_path(self) -> None:
        paths = OpenJobDataPaths(bucket_uri="hf://b", variant="full")
        assert paths.delta_path(date(2026, 8, 8)) == "b/data/full/changes/2026-08-08.parquet"


class TestDeltaFilenames:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("2026-08-08.parquet", date(2026, 8, 8)),
            ("2026-01-01.parquet", date(2026, 1, 1)),
            ("README.md", None),
            ("part-0.parquet", None),
            ("2026-08-08.parquet.tmp", None),
            ("2026-13-45.parquet", None),  # syntactically right, not a real date
            ("", None),
        ],
    )
    def test_parse(self, name: str, expected: date | None) -> None:
        assert parse_delta_date(name) == expected


class TestNestedJsonDecoding:
    def test_decodes_double_encoded(self) -> None:
        """The Parquet value is a JSON string containing JSON; one loads() is not enough."""
        encoded = json.dumps(json.dumps({"a": 1}))
        assert json.loads(encoded) == '{"a": 1}'  # a str, not a dict
        assert decode_nested_json(encoded) == {"a": 1}

    def test_decodes_single_encoded(self) -> None:
        assert decode_nested_json(json.dumps({"a": 1})) == {"a": 1}

    def test_passes_through_a_dict(self) -> None:
        assert decode_nested_json({"a": 1}) == {"a": 1}

    @pytest.mark.parametrize("value", [None, "", "not json", "[1,2,3]", '"just a string"', 42])
    def test_returns_none_for_undecodable(self, value: object) -> None:
        """Returning None lets the caller reject with a reason instead of crashing."""
        assert decode_nested_json(value) is None


class TestDiscover:
    def test_lists_real_files_only(self, connector: OpenJobDataConnector) -> None:
        files = connector.discover()
        assert [f.file_date for f in files] == [
            date(2026, 8, 7),
            date(2026, 8, 8),
            date(2026, 8, 10),
        ]

    def test_ignores_non_delta_entries(self, connector: OpenJobDataConnector) -> None:
        """A README or a partial upload must not break discovery."""
        names = [f.remote_path for f in connector.discover()]
        assert not any("README" in n or ".tmp" in n for n in names)

    def test_does_not_invent_the_skipped_day(self, connector: OpenJobDataConnector) -> None:
        """The publisher skips days. Discovery reports what exists, never a generated range."""
        assert date(2026, 8, 9) not in [f.file_date for f in connector.discover()]

    def test_results_are_chronological(self, connector: OpenJobDataConnector) -> None:
        dates = [f.file_date for f in connector.discover()]
        assert dates == sorted(dates)

    def test_since_filters(self, connector: OpenJobDataConnector) -> None:
        files = connector.discover(since=date(2026, 8, 8))
        assert [f.file_date for f in files] == [date(2026, 8, 8), date(2026, 8, 10)]

    def test_captures_the_content_hash_as_the_version(
        self, connector: OpenJobDataConnector
    ) -> None:
        """xet_hash is a real content hash, so a corrected republish is detected."""
        by_date = {f.file_date: f for f in connector.discover()}
        assert by_date[date(2026, 8, 8)].etag == "hash-08"
        assert by_date[date(2026, 8, 8)].checkpoint_key() == (
            f"{CHANGES}/2026-08-08.parquet",
            "hash-08",
        )

    def test_captures_size_and_upload_time(self, connector: OpenJobDataConnector) -> None:
        newest = connector.discover()[-1]
        assert newest.size_bytes == 300
        assert newest.last_modified is not None

    def test_unreachable_source_raises_typed_error(self, minimal_env: None) -> None:
        from jobplatform_shared import SourceUnavailableError, get_settings

        class Broken:
            def ls(self, *_a: object, **_k: object) -> None:
                raise OSError("connection reset")

        get_settings.cache_clear()
        with pytest.raises(SourceUnavailableError):
            OpenJobDataConnector(filesystem=Broken()).discover()

    def test_a_transient_listing_failure_is_retried(
        self, minimal_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One reset must not end a scheduled run before it reads a row.

        Observed in production: three consecutive daily runs each died here in 0.1s,
        having ingested nothing, because the listing was the one source call with no
        retry while downloads had several.
        """
        from ingestion.connectors.openjobdata import connector as connector_module
        from jobplatform_shared import get_settings

        monkeypatch.setattr(connector_module.time, "sleep", lambda _s: None)

        class FlakyOnce:
            def __init__(self) -> None:
                self.calls = 0

            def ls(self, *_a: object, **_k: object) -> list[dict[str, object]]:
                self.calls += 1
                if self.calls == 1:
                    raise OSError("[WinError 10054] connection forcibly closed")
                return []

        flaky = FlakyOnce()
        get_settings.cache_clear()
        assert OpenJobDataConnector(filesystem=flaky).discover() == []
        assert flaky.calls == 2, "the listing should have been retried exactly once"

    def test_a_missing_directory_is_not_retried(
        self, minimal_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing path is a settled answer. Retrying it only delays the report."""
        from ingestion.connectors.openjobdata import connector as connector_module
        from jobplatform_shared import SourceUnavailableError, get_settings

        monkeypatch.setattr(connector_module.time, "sleep", lambda _s: None)

        class Missing:
            def __init__(self) -> None:
                self.calls = 0

            def ls(self, *_a: object, **_k: object) -> None:
                self.calls += 1
                raise FileNotFoundError("no such directory")

        missing = Missing()
        get_settings.cache_clear()
        with pytest.raises(SourceUnavailableError, match="delta directory not found"):
            OpenJobDataConnector(filesystem=missing).discover()
        assert missing.calls == 1


class TestColumnProjection:
    def test_entire_json_is_excluded_by_default(self, connector: OpenJobDataConnector) -> None:
        """entire_json is 49.5% of the file and this platform never reads it."""
        assert "entire_json" not in connector._columns
        assert "job_model_json" in connector._columns

    def test_projection_matches_the_verified_schema(self, connector: OpenJobDataConnector) -> None:
        assert set(connector._columns) == set(JOB_COLUMNS_FULL)

    def test_minimal_variant_omits_the_job_model(
        self, fake_fs: FakeHfFileSystem, minimal_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jobplatform_shared import get_settings

        monkeypatch.setenv("OPENJOBDATA_VARIANT", "minimal")
        get_settings.cache_clear()
        assert set(OpenJobDataConnector(filesystem=fake_fs)._columns) == set(JOB_COLUMNS_MINIMAL)

    def test_entire_json_can_be_opted_into(
        self, fake_fs: FakeHfFileSystem, minimal_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jobplatform_shared import get_settings

        monkeypatch.setenv("OPENJOBDATA_INCLUDE_ENTIRE_JSON", "true")
        get_settings.cache_clear()
        assert "entire_json" in OpenJobDataConnector(filesystem=fake_fs)._columns


class TestFetch:
    def test_streams_all_rows(self, records: list[RawRecord]) -> None:
        assert len(records) == 5

    def test_yields_lazily(self, connector: OpenJobDataConnector) -> None:
        """One real file holds ~81,000 rows; fetch() must not materialise them."""
        result = connector.fetch(SourceFile(remote_path=f"{CHANGES}/2026-08-08.parquet"))
        assert isinstance(result, Iterator)

    def test_preserves_row_index_and_path(self, records: list[RawRecord]) -> None:
        assert [r.row_index for r in records] == [0, 1, 2, 3, 4]
        assert all(r.source_path.endswith("2026-08-08.parquet") for r in records)

    def test_external_id_uses_the_source_primary_key(self, records: list[RawRecord]) -> None:
        """`id` is unique (0 dupes in 81,149 rows); `job_id` is NOT (2,705 dupes)."""
        assert records[0].external_id == "jobvite:acme/abc"
        assert len({r.external_id for r in records}) == len(records)

    def test_missing_file_raises_typed_error(self, connector: OpenJobDataConnector) -> None:
        from jobplatform_shared import SourceUnavailableError

        with pytest.raises(SourceUnavailableError):
            list(connector.fetch(SourceFile(remote_path=f"{CHANGES}/2026-01-01.parquet")))


class TestValidate:
    def test_accepts_a_good_row(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        assert connector.validate(records[0]).is_valid

    def test_accepts_a_row_with_no_posted_at(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """19.3% of real rows have no posted_at. Rejecting them would discard a fifth of
        the dataset; freshness handling is a downstream concern, not a validity one."""
        assert records[1].payload["posted_at"] is None
        assert connector.validate(records[1]).is_valid

    def test_accepts_a_future_posted_at(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """Structural validation must not apply freshness policy -- that belongs to the
        shared FreshnessService so every source is judged identically."""
        assert connector.validate(records[2]).is_valid

    def test_rejects_missing_title_and_apply_url(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        result = connector.validate(records[3])
        assert not result.is_valid
        assert RejectionReason.MISSING_TITLE in result.reasons
        assert RejectionReason.MISSING_APPLY_URL in result.reasons

    def test_rejects_an_unparseable_job_model(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        result = connector.validate(records[4])
        assert not result.is_valid
        assert RejectionReason.UNPARSEABLE_PAYLOAD in result.reasons

    def test_rejects_a_non_http_apply_url(self, connector: OpenJobDataConnector) -> None:
        record = RawRecord(
            external_id="x:1",
            payload={
                "id": "x:1",
                "title": "T",
                "company_id": 1,
                "apply_url": "javascript:alert(1)",
                "job_model_json": _job_model(),
            },
        )
        result = connector.validate(record)
        assert RejectionReason.INVALID_URL in result.reasons

    def test_rejection_names_the_row(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """A rejection must be traceable back to the exact source row."""
        assert "row 3" in connector.validate(records[3]).detail


class TestNormalize:
    def test_maps_core_fields(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        job = connector.normalize(records[0])
        assert job.source == SOURCE_NAME
        assert job.external_id == "jobvite:acme/abc"
        assert job.title == "Senior Software Engineer"
        assert job.description_text == "Build things"
        assert job.seniority == "senior"

    def test_resolves_the_company_name(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        job = connector.normalize(records[0])
        assert job.company_name == "ABC Corporation"
        assert job.company_external_id == "753"

    def test_keeps_location_raw(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """The connector reports what the source said. Deciding that 'Michigan' means MI,
        or that this is a US job, belongs to LocationNormalizer."""
        job = connector.normalize(records[0])
        assert job.raw_state == "Michigan"
        assert job.raw_city == "Detroit"
        assert job.raw_country == "United States"
        assert not hasattr(job, "state_code")

    def test_strips_padding_from_raw_location_text(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """Real values carry leading whitespace: '            South Carolina'."""
        job = connector.normalize(records[1])
        assert job.raw_location_text == "South Carolina"

    def test_state_code_form_is_preserved_untouched(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """The source emits both 'Michigan' and 'OH'. Normalising here would hide that
        from the service whose job it is to resolve them."""
        assert connector.normalize(records[2]).raw_state == "OH"

    def test_empty_country_becomes_none(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """Empty strings must not be stored as data; '' and NULL both mean unknown."""
        assert connector.normalize(records[1]).raw_country is None

    def test_junk_country_is_passed_through_for_the_normalizer_to_reject(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """'REMOTE' is not a country, but the connector does not silently drop it -- the
        LocationNormalizer resolves it to None and the row is rejected with a reason."""
        assert connector.normalize(records[2]).raw_country == "REMOTE"

    def test_tbc_workplace_becomes_none(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """'tbc' is ~41% of rows and means unknown, not on-site."""
        assert connector.normalize(records[1]).raw_workplace_type is None

    def test_maps_salary(self, connector: OpenJobDataConnector, records: list[RawRecord]) -> None:
        job = connector.normalize(records[0])
        assert (job.salary_min, job.salary_max) == (140000.0, 170000.0)
        assert job.salary_currency == "USD"
        assert job.raw_salary_interval == "annually"

    def test_handles_a_missing_compensation_block(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """Only 18.7% of real rows carry compensation."""
        job = connector.normalize(records[1])
        assert job.salary_min is None and job.salary_max is None

    def test_open_ended_salary(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        job = connector.normalize(records[2])
        assert job.salary_min == 40.0 and job.salary_max is None

    def test_separates_the_two_closure_timestamps(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """close_at is the EMPLOYER's stated expiry; closed_at is when the SOURCE noticed
        the posting was gone. Collapsing them loses a real distinction."""
        active = connector.normalize(records[0])
        assert active.close_at == datetime(2026, 9, 1, tzinfo=UTC)
        assert active.closed_at is None  # still active

        closed = connector.normalize(records[1])
        assert closed.closed_at == datetime(2026, 8, 8, 13, 0, tzinfo=UTC)
        assert closed.close_at is None  # employer never stated one

    def test_keeps_posted_at_and_fetched_time_distinct(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        job = connector.normalize(records[0])
        assert job.posted_at == datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        assert job.source_fetched_at == datetime(2026, 8, 8, 14, 0, tzinfo=UTC)
        assert job.posted_at != job.source_fetched_at

    def test_null_posted_at_is_not_invented(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """The spec forbids fabricating timestamps. A missing posted_at stays missing."""
        job = connector.normalize(records[1])
        assert job.posted_at is None
        assert job.source_fetched_at is not None

    def test_future_posted_at_is_preserved_not_clamped(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """Silently clamping would hide a real data-quality signal; the freshness gate
        quarantines it downstream instead."""
        assert connector.normalize(records[2]).posted_at == datetime(2026, 9, 11, tzinfo=UTC)

    def test_all_timestamps_are_utc_aware(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        job = connector.normalize(records[0])
        for value in (job.posted_at, job.source_fetched_at, job.close_at):
            assert value is not None and value.tzinfo is not None

    def test_recovers_the_ats_provider(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        assert connector.normalize(records[0]).ats_provider == "jobvite"

    def test_unknown_company_does_not_fail_normalization(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """company_id 2 is absent from the lookup; the job still normalises."""
        job = connector.normalize(records[4])
        assert job.company_name is None
        assert job.company_external_id == "2"


class TestCompanyLookup:
    def test_loaded_once_and_cached(
        self, connector: OpenJobDataConnector, records: list[RawRecord]
    ) -> None:
        """A per-row remote lookup over 109k companies would be absurd."""
        connector.normalize(records[0])
        connector.normalize(records[1])
        opens = [p for p in connector.filesystem.opened if "companies" in p]
        assert len(opens) == 1

    def test_missing_lookup_degrades_gracefully(self, parquet_dir: Path, minimal_env: None) -> None:
        """A company-file outage must not abort ingestion; jobs keep their external id."""
        from jobplatform_shared import get_settings

        class NoCompanies(FakeHfFileSystem):
            def open(self, path: str, mode: str = "rb") -> Any:
                if "companies" in path:
                    raise FileNotFoundError(path)
                return super().open(path, mode)

        get_settings.cache_clear()
        fs = NoCompanies(
            [],
            {f"{CHANGES}/2026-08-08.parquet": parquet_dir / "2026-08-08.parquet"},
        )
        conn = OpenJobDataConnector(filesystem=fs)
        record = next(iter(conn.fetch(SourceFile(remote_path=f"{CHANGES}/2026-08-08.parquet"))))
        job = conn.normalize(record)
        assert job.company_name is None
        assert job.company_external_id == "753"


class TestArchiving:
    def test_archive_key_layout(self, connector: OpenJobDataConnector) -> None:
        key = connector.archive_key(
            SourceFile(remote_path=f"{CHANGES}/2026-08-08.parquet", file_date=date(2026, 8, 8))
        )
        assert key == "raw/openjobdata/full/changes/2026-08-08.parquet"

    def test_archives_then_reads_from_the_archive(
        self, fake_fs: FakeHfFileSystem, tmp_path: Path, minimal_env: None
    ) -> None:
        """Reprocessing must not re-download ~120 MB."""
        from jobplatform_shared import get_settings

        get_settings.cache_clear()
        store = LocalObjectStore(tmp_path / "archive")
        conn = OpenJobDataConnector(filesystem=fake_fs, object_store=store)
        source_file = SourceFile(
            remote_path=f"{CHANGES}/2026-08-08.parquet", file_date=date(2026, 8, 8)
        )

        key = conn.archive(source_file)
        assert store.exists(key)

        fake_fs.opened.clear()
        rows = list(conn.fetch(source_file))
        assert len(rows) == 5
        assert not [p for p in fake_fs.opened if p.endswith("2026-08-08.parquet")]

    def test_archiving_twice_is_a_noop(
        self, fake_fs: FakeHfFileSystem, tmp_path: Path, minimal_env: None
    ) -> None:
        from jobplatform_shared import get_settings

        get_settings.cache_clear()
        store = LocalObjectStore(tmp_path / "archive")
        conn = OpenJobDataConnector(filesystem=fake_fs, object_store=store)
        source_file = SourceFile(remote_path=f"{CHANGES}/2026-08-08.parquet")

        assert conn.archive(source_file) == conn.archive(source_file)


class TestCapabilities:
    def test_declares_closure_reporting(self, connector: OpenJobDataConnector) -> None:
        """The source ships status='closed', so absence from a delta is NOT removal.
        Getting this wrong would mass-mark live jobs as removed."""
        assert connector.get_capabilities().reports_closures is True

    def test_cadence_allows_for_skipped_days(self, connector: OpenJobDataConnector) -> None:
        """Publication is same-day but the hour varies and days get skipped, so a 24 h
        staleness alarm would page on every normal gap."""
        assert connector.get_capabilities().expected_cadence_hours > 24

    def test_supports_incremental(self, connector: OpenJobDataConnector) -> None:
        assert connector.get_capabilities().supports_incremental is True


class TestOpenJobDataAgainstContract(SourceConnectorContract):
    """The real connector inherits every check the shared contract suite defines."""

    @pytest.fixture
    def connector(self, fake_fs: FakeHfFileSystem, minimal_env: None) -> SourceConnector:
        from jobplatform_shared import get_settings

        get_settings.cache_clear()
        return OpenJobDataConnector(filesystem=fake_fs)


# ------------------------------------------------------------------- live source tests


@pytest.mark.network
class TestAgainstLiveSource:
    """Runs against the real bucket. Excluded from default runs (`-m network` to include).

    These are the tests that fail when the upstream source changes shape, which is the
    signal to re-run scripts/verify_source.py and update the documentation.
    """

    @pytest.fixture
    def live(self, minimal_env: None) -> OpenJobDataConnector:
        pytest.importorskip("huggingface_hub")
        pytest.importorskip("pyarrow")
        from jobplatform_shared import get_settings

        get_settings.cache_clear()
        return OpenJobDataConnector()

    def test_discovers_real_delta_files(self, live: OpenJobDataConnector) -> None:
        files = live.discover()
        assert len(files) > 50
        assert all(f.file_date and f.etag for f in files)

    def test_real_files_are_chronological_and_dated(self, live: OpenJobDataConnector) -> None:
        dates = [f.file_date for f in live.discover()]
        assert dates == sorted(dates)
        assert len(set(dates)) == len(dates)

    def test_fetch_and_normalize_a_real_row(self, live: OpenJobDataConnector) -> None:
        newest = live.discover()[-1]
        for record in live.fetch(newest):
            if live.validate(record).is_valid:
                job = live.normalize(record)
                assert job.source == SOURCE_NAME
                assert job.title
                assert job.external_id == record.external_id
                return
        pytest.fail("no valid record found in the newest delta")
