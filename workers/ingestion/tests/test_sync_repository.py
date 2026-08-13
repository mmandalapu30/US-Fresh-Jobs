"""Sync tracking tests — the durability guarantees behind resumable ingestion.

Run against real PostgreSQL. The behaviours asserted here are precisely the spec's
reliability requirements: idempotent reprocessing, safe resumption after a crash, and
never silently discarding a bad record.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ingestion.repositories import RejectedRecord, SyncRepository, SyncRunActiveError
from jobplatform_schemas import RejectionReason, SyncStatus, SyncTrigger

pytestmark = pytest.mark.integration

SOURCE = "source-a"


@pytest.fixture
def engine(migrated_database: str) -> Iterator[Engine]:
    eng = create_engine(migrated_database)
    yield eng
    # Leave the schema clean for the next test without paying to re-migrate.
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE sync_errors, sync_files, sync_runs RESTART IDENTITY CASCADE"))
    eng.dispose()


@pytest.fixture
def repo(engine: Engine) -> SyncRepository:
    return SyncRepository(engine)


class TestRunLifecycle:
    def test_start_and_finish(self, repo: SyncRepository) -> None:
        run = repo.start_run(SOURCE, trigger=SyncTrigger.MANUAL)
        assert run.status is SyncStatus.RUNNING
        assert run.sync_uuid

        repo.finish_run(run.id, status=SyncStatus.SUCCEEDED)
        stored = repo.get_run(run.id)
        assert stored is not None
        assert stored["status"] == "SUCCEEDED"
        assert stored["finished_at"] is not None
        assert stored["duration_seconds"] >= 0

    def test_second_concurrent_run_is_refused(self, repo: SyncRepository) -> None:
        """Two schedulers must not ingest the same file simultaneously."""
        repo.start_run(SOURCE)
        with pytest.raises(SyncRunActiveError):
            repo.start_run(SOURCE)

    def test_new_run_allowed_after_previous_finishes(self, repo: SyncRepository) -> None:
        first = repo.start_run(SOURCE)
        repo.finish_run(first.id, status=SyncStatus.SUCCEEDED)
        second = repo.start_run(SOURCE)
        assert second.id != first.id

    def test_different_sources_run_concurrently(self, repo: SyncRepository) -> None:
        """The lock is per source, not global."""
        repo.start_run("source-a")
        repo.start_run("source-b")  # must not raise

    def test_reclaim_stale_run_releases_the_lock(
        self, repo: SyncRepository, engine: Engine
    ) -> None:
        """A worker killed by OOM leaves a RUNNING row that would block every future run."""
        run = repo.start_run(SOURCE)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE sync_runs SET started_at = now() - interval '5 hours' WHERE id = :i"),
                {"i": run.id},
            )

        assert repo.reclaim_stale_runs(SOURCE, older_than_minutes=120) == 1
        assert repo.get_run(run.id)["status"] == "FAILED"  # visible, not hidden
        repo.start_run(SOURCE)  # lock released

    def test_reclaim_leaves_fresh_runs_alone(self, repo: SyncRepository) -> None:
        run = repo.start_run(SOURCE)
        assert repo.reclaim_stale_runs(SOURCE, older_than_minutes=120) == 0
        assert repo.get_run(run.id)["status"] == "RUNNING"

    def test_last_successful_run(self, repo: SyncRepository) -> None:
        failed = repo.start_run(SOURCE)
        repo.finish_run(failed.id, status=SyncStatus.FAILED)
        good = repo.start_run(SOURCE)
        repo.finish_run(good.id, status=SyncStatus.SUCCEEDED)

        latest = repo.last_successful_run(SOURCE)
        assert latest is not None and latest["id"] == good.id

    def test_last_successful_run_none_when_never_succeeded(self, repo: SyncRepository) -> None:
        run = repo.start_run(SOURCE)
        repo.finish_run(run.id, status=SyncStatus.FAILED)
        assert repo.last_successful_run(SOURCE) is None


class TestFileCheckpoints:
    def test_completed_file_is_skipped_on_rerun(self, repo: SyncRepository) -> None:
        """Spec test case 12: reprocessing a source file must be a no-op."""
        run = repo.start_run(SOURCE)
        file = repo.start_file(
            run.id, SOURCE, remote_path="changes/2026-08-08.parquet", remote_etag="abc"
        )
        repo.finish_file(file.id, status=SyncStatus.SUCCEEDED, rows_committed=81_149)

        assert repo.already_processed(SOURCE, "changes/2026-08-08.parquet", "abc") is True

    def test_republished_file_is_reprocessed(self, repo: SyncRepository) -> None:
        """A corrected file has a new etag and must NOT be skipped."""
        run = repo.start_run(SOURCE)
        file = repo.start_file(
            run.id, SOURCE, remote_path="changes/2026-08-08.parquet", remote_etag="v1"
        )
        repo.finish_file(file.id, status=SyncStatus.SUCCEEDED)

        assert repo.already_processed(SOURCE, "changes/2026-08-08.parquet", "v2") is False

    def test_failed_file_is_not_marked_processed(self, repo: SyncRepository) -> None:
        run = repo.start_run(SOURCE)
        file = repo.start_file(run.id, SOURCE, remote_path="changes/x.parquet", remote_etag="a")
        repo.finish_file(file.id, status=SyncStatus.FAILED, error_message="connection reset")

        assert repo.already_processed(SOURCE, "changes/x.parquet", "a") is False

    def test_filter_unprocessed_reduces_the_worklist(self, repo: SyncRepository) -> None:
        """Discovery returns everything; only new or changed units should be worked."""
        run = repo.start_run(SOURCE)
        done = repo.start_file(run.id, SOURCE, remote_path="changes/a.parquet", remote_etag="1")
        repo.finish_file(done.id, status=SyncStatus.SUCCEEDED)

        candidates = [
            ("changes/a.parquet", "1"),  # already done
            ("changes/a.parquet", "2"),  # republished -> redo
            ("changes/b.parquet", "1"),  # new
        ]
        assert repo.filter_unprocessed(SOURCE, candidates) == [
            ("changes/a.parquet", "2"),
            ("changes/b.parquet", "1"),
        ]

    def test_filter_unprocessed_handles_empty_input(self, repo: SyncRepository) -> None:
        assert repo.filter_unprocessed(SOURCE, []) == []

    def test_progress_survives_for_resumption(self, repo: SyncRepository) -> None:
        """Spec test case 13: a worker crashing mid-file must resume, not restart."""
        run = repo.start_run(SOURCE)
        file = repo.start_file(
            run.id,
            SOURCE,
            remote_path="changes/2026-08-08.parquet",
            row_groups_total=95,
            file_date=date(2026, 8, 8),
        )
        repo.checkpoint_file(file.id, row_groups_done=40, rows_committed=34_000)
        # ...worker dies here; no finish_file call...

        progress = repo.get_file_progress(SOURCE, "changes/2026-08-08.parquet")
        assert progress is not None
        assert progress.status is SyncStatus.RUNNING
        assert progress.row_groups_done == 40
        assert progress.rows_committed == 34_000

    def test_checkpoint_commits_with_its_data(self, repo: SyncRepository, engine: Engine) -> None:
        """The checkpoint must roll back with the rows it describes.

        If a checkpoint could commit independently, a crash would leave it claiming rows
        that were rolled back, and the resumed run would skip them permanently.
        """
        run = repo.start_run(SOURCE)
        file = repo.start_file(run.id, SOURCE, remote_path="changes/c.parquet")

        try:
            with engine.begin() as conn:
                repo.checkpoint_file(file.id, row_groups_done=10, rows_committed=500, conn=conn)
                raise RuntimeError("simulated crash after checkpoint, before commit")
        except RuntimeError:
            pass

        progress = repo.get_file_progress(SOURCE, "changes/c.parquet")
        assert progress is not None
        assert progress.row_groups_done == 0  # rolled back with the transaction
        assert progress.rows_committed == 0

    def test_progress_none_for_unknown_file(self, repo: SyncRepository) -> None:
        assert repo.get_file_progress(SOURCE, "changes/never-seen.parquet") is None


class TestRejections:
    def test_rejections_are_stored_with_reasons(self, repo: SyncRepository) -> None:
        """Spec: 'Do not silently discard bad records. Record rejection reasons.'"""
        run = repo.start_run(SOURCE)
        stored = repo.record_rejections(
            run.id,
            SOURCE,
            [
                RejectedRecord(reason=RejectionReason.MISSING_TITLE, external_job_id="a:1"),
                RejectedRecord(
                    reason=RejectionReason.FUTURE_POSTED_AT,
                    external_job_id="a:2",
                    error_message="posted_at is 34 days ahead",
                    payload={"posted_at": "2026-09-11T00:00:00Z"},
                ),
            ],
        )
        assert stored == 2
        assert repo.rejection_summary(run.id) == {"MISSING_TITLE": 1, "FUTURE_POSTED_AT": 1}

    def test_every_reason_is_storable(self, repo: SyncRepository) -> None:
        """The enum and the column must not drift apart."""
        run = repo.start_run(SOURCE)
        repo.record_rejections(
            run.id, SOURCE, [RejectedRecord(reason=reason) for reason in RejectionReason]
        )
        assert sum(repo.rejection_summary(run.id).values()) == len(list(RejectionReason))

    def test_oversized_payload_is_truncated(self, repo: SyncRepository, engine: Engine) -> None:
        """One pathological record must not bloat the errors table."""
        run = repo.start_run(SOURCE)
        repo.record_rejections(
            run.id,
            SOURCE,
            [
                RejectedRecord(
                    reason=RejectionReason.SCHEMA_VIOLATION,
                    payload={"blob": "x" * 100_000},
                )
            ],
        )
        with engine.connect() as conn:
            length = conn.execute(
                text("SELECT length(row_payload::text) FROM sync_errors WHERE sync_run_id = :i"),
                {"i": run.id},
            ).scalar_one()
        assert length < 20_000

    def test_buffer_flushes_on_exit(self, repo: SyncRepository) -> None:
        run = repo.start_run(SOURCE)
        with repo.rejection_buffer(run.id, SOURCE) as buffer:
            for i in range(10):
                buffer.add(
                    RejectedRecord(reason=RejectionReason.INVALID_URL, external_job_id=f"a:{i}")
                )
        assert repo.rejection_summary(run.id) == {"INVALID_URL": 10}

    def test_buffer_flushes_even_when_the_body_raises(self, repo: SyncRepository) -> None:
        """A crash mid-file must still leave the reasons for everything rejected so far."""
        run = repo.start_run(SOURCE)
        with pytest.raises(RuntimeError), repo.rejection_buffer(run.id, SOURCE) as buffer:
            buffer.add(RejectedRecord(reason=RejectionReason.MISSING_COMPANY))
            raise RuntimeError("worker died")

        assert repo.rejection_summary(run.id) == {"MISSING_COMPANY": 1}

    def test_buffer_batches_large_volumes(self, repo: SyncRepository) -> None:
        """An upstream schema change can reject tens of thousands of rows at once."""
        run = repo.start_run(SOURCE)
        with repo.rejection_buffer(run.id, SOURCE) as buffer:
            for i in range(1_200):
                buffer.add(
                    RejectedRecord(reason=RejectionReason.INVALID_COUNTRY, external_job_id=str(i))
                )
        assert repo.rejection_summary(run.id) == {"INVALID_COUNTRY": 1_200}

    def test_empty_rejection_list_is_a_noop(self, repo: SyncRepository) -> None:
        run = repo.start_run(SOURCE)
        assert repo.record_rejections(run.id, SOURCE, []) == 0


class TestCounters:
    def test_counters_persist_on_finish(self, repo: SyncRepository) -> None:
        from ingestion.repositories.sync import _RunCounters

        run = repo.start_run(SOURCE)
        counters = _RunCounters(
            files_discovered=3,
            files_processed=3,
            rows_processed=81_149,
            rows_accepted=62_425,
            rows_rejected=18_724,
            duplicates_found=2_705,
            bytes_downloaded=120_000_000,
        )
        repo.finish_run(run.id, status=SyncStatus.SUCCEEDED, counters=counters)

        stored = repo.get_run(run.id)
        assert stored is not None
        assert stored["rows_processed"] == 81_149
        assert stored["rows_accepted"] == 62_425
        assert stored["rows_rejected"] == 18_724
        assert stored["duplicates_found"] == 2_705
        assert stored["bytes_downloaded"] == 120_000_000
