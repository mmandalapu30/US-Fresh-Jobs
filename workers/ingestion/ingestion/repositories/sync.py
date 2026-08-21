"""Sync run, file checkpoint and error tracking.

This is the durability layer for ingestion. Three guarantees live here:

1. **One active run per source.** Enforced by a partial UNIQUE index, so two schedulers
   cannot double-ingest the same file. ``start_run`` surfaces that as a typed error.
2. **Per-file checkpoints.** ``sync_files`` records progress per unit of work. A worker
   killed mid-file resumes from the last committed row group instead of restarting the run.
3. **No silent drops.** Every rejected record lands in ``sync_errors`` with a reason from
   a closed enum.

Uses SQLAlchemy Core with bound parameters throughout — no string-built SQL.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from jobplatform_schemas import RejectionReason, SyncStatus, SyncTrigger
from jobplatform_shared import IngestionError, get_logger

__all__ = [
    "RejectedRecord",
    "SyncFileRecord",
    "SyncRepository",
    "SyncRunActiveError",
    "SyncRunRecord",
]

logger = get_logger(__name__)

#: Rejected rows are buffered and flushed in batches; a per-row INSERT would dominate the
#: cost of a bad file (a schema change upstream can reject tens of thousands of rows).
_ERROR_FLUSH_SIZE = 500

#: Truncate stored payloads so one pathological record cannot bloat the errors table.
_MAX_STORED_PAYLOAD_CHARS = 8_000

#: Detailed examples kept per (run, reason). Beyond this only the count is recorded.
#: A narrow ingest scope rejects millions of rows for the same reason; storing each one
#: adds no diagnostic value over a sample plus an accurate total.
_MAX_EXAMPLES_PER_REASON = 100


def _encode_payload(payload: dict[str, Any] | None) -> str | None:
    """Serialise a rejected row's payload, bounding its size.

    Truncating the JSON *text* would produce a syntactically invalid document that the
    ``jsonb`` column rejects outright -- losing the rejection record entirely, which is
    the opposite of the requirement. So an oversized payload is wrapped in a valid
    envelope that keeps a readable prefix plus the original size.
    """
    import json

    if not payload:
        return None

    encoded = json.dumps(payload, default=str)
    if len(encoded) <= _MAX_STORED_PAYLOAD_CHARS:
        return encoded

    return json.dumps(
        {
            "_truncated": True,
            "_original_chars": len(encoded),
            "_preview": encoded[:_MAX_STORED_PAYLOAD_CHARS],
        }
    )


class SyncRunActiveError(IngestionError):
    """Another run for this source is already PENDING or RUNNING."""

    code = "sync_run_active"


@dataclass(slots=True)
class SyncRunRecord:
    id: int
    sync_uuid: str
    source: str
    status: SyncStatus
    started_at: datetime


@dataclass(slots=True)
class SyncFileRecord:
    id: int
    remote_path: str
    status: SyncStatus
    row_groups_done: int
    rows_committed: int


@dataclass(slots=True)
class RejectedRecord:
    """One row that did not become a job, with the reason it did not."""

    reason: RejectionReason
    external_job_id: str | None = None
    error_message: str | None = None
    payload: dict[str, Any] | None = None
    sync_file_id: int | None = None


@dataclass(slots=True)
class _RunCounters:
    """Accumulated in memory, flushed to the row once per file rather than per record."""

    files_discovered: int = 0
    files_processed: int = 0
    files_failed: int = 0
    rows_processed: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    duplicates_found: int = 0
    bytes_downloaded: int = 0
    error_count: int = 0

    def as_params(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in dataclass_fields(self)}


class SyncRepository:
    """Persistence for ingestion bookkeeping."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---- runs ----------------------------------------------------------------

    def start_run(
        self,
        source: str,
        *,
        trigger: SyncTrigger = SyncTrigger.SCHEDULED,
        worker_id: str | None = None,
        config_snapshot: dict[str, Any] | None = None,
    ) -> SyncRunRecord:
        """Open a RUNNING sync run.

        Raises ``SyncRunActiveError`` when one is already open for this source. That is a
        real condition, not a race to retry blindly: it usually means a previous run
        crashed without finalising, and reclaim_stale_runs handles that explicitly.
        """
        import json

        statement = text(
            """
            INSERT INTO sync_runs (source, trigger, status, worker_id, config_snapshot)
            VALUES (:source, CAST(:trigger AS sync_trigger), CAST('RUNNING' AS sync_status),
                    :worker_id, CAST(:config AS jsonb))
            RETURNING id, sync_uuid, status, started_at
            """
        )
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    statement,
                    {
                        "source": source,
                        "trigger": trigger.value,
                        "worker_id": worker_id,
                        "config": json.dumps(config_snapshot) if config_snapshot else None,
                    },
                ).one()
        except IntegrityError as exc:
            if "sync_runs_one_active_per_source" in str(exc.orig):
                raise SyncRunActiveError(
                    f"a sync run is already active for source {source!r}", source=source
                ) from exc
            raise

        logger.info("sync.run.started", sync_run_id=row.id, source=source, trigger=trigger.value)
        return SyncRunRecord(
            id=row.id,
            sync_uuid=str(row.sync_uuid),
            source=source,
            status=SyncStatus.RUNNING,
            started_at=row.started_at,
        )

    def finish_run(
        self, run_id: int, *, status: SyncStatus, counters: _RunCounters | None = None
    ) -> None:
        """Close a run and stamp its duration.

        ``duration_seconds`` is computed in the database from ``started_at`` so it stays
        correct even if the worker's clock drifted mid-run.
        """
        params: dict[str, Any] = {"run_id": run_id, "status": status.value}
        counter_sql = ""
        if counters is not None:
            counter_params = counters.as_params()
            params.update(counter_params)
            counter_sql = "".join(f", {name} = :{name}" for name in counter_params)

        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    UPDATE sync_runs
                       SET status = CAST(:status AS sync_status),
                           finished_at = now(),
                           duration_seconds = EXTRACT(EPOCH FROM (now() - started_at))
                           {counter_sql}
                     WHERE id = :run_id
                    """  # noqa: S608 - counter_sql is built from dataclass field names, never input
                ),
                params,
            )
        logger.info("sync.run.finished", sync_run_id=run_id, status=status.value)

    def reclaim_stale_runs(self, source: str, *, older_than_minutes: int = 30) -> int:
        """Mark long-abandoned RUNNING rows as FAILED.

        A worker killed by OOM or a node restart leaves a RUNNING row that would block
        every future run through the one-active-per-source index. This releases the lock
        without hiding the failure: the row becomes FAILED, so the incident stays visible
        in the admin dashboard.

        The window is the dead time a crash costs, so the pipeline passes
        ``ingest_stale_run_reclaim_minutes`` rather than relying on this default. It must
        stay comfortably above the longest legitimate run: reclaiming a live worker's row
        frees the lock for a second run that will then collide on the provenance UNIQUE.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE sync_runs
                       SET status = CAST('FAILED' AS sync_status),
                           finished_at = now(),
                           duration_seconds = EXTRACT(EPOCH FROM (now() - started_at))
                     WHERE source = :source
                       AND status IN ('PENDING', 'RUNNING')
                       AND started_at < now() - make_interval(mins => :minutes)
                    """
                ),
                {"source": source, "minutes": older_than_minutes},
            )
        count = result.rowcount or 0
        if count:
            logger.warning("sync.run.reclaimed_stale", source=source, count=count)
        return count

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM sync_runs WHERE id = :id"), {"id": run_id})
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def last_successful_run(self, source: str) -> dict[str, Any] | None:
        """Most recent SUCCEEDED or PARTIAL run. Drives staleness alerting."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT * FROM sync_runs
                     WHERE source = :source AND status IN ('SUCCEEDED', 'PARTIAL')
                     ORDER BY started_at DESC
                     LIMIT 1
                    """
                    ),
                    {"source": source},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    # ---- file checkpoints ----------------------------------------------------

    def already_processed(self, source: str, remote_path: str, etag: str | None) -> bool:
        """Whether this exact version of a unit has already completed successfully.

        Version-aware on purpose: a source may correct a file in place. Matching on path
        alone would skip the corrected copy; matching on path plus version re-ingests it,
        and the idempotent upsert makes that safe.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT 1 FROM sync_files
                     WHERE source = :source
                       AND remote_path = :remote_path
                       AND COALESCE(remote_etag, '') = COALESCE(:etag, '')
                       AND status = 'SUCCEEDED'
                     LIMIT 1
                    """
                ),
                {"source": source, "remote_path": remote_path, "etag": etag},
            ).first()
        return row is not None

    def filter_unprocessed(
        self, source: str, candidates: Iterable[tuple[str, str | None]]
    ) -> list[tuple[str, str | None]]:
        """Reduce discovered units to those still needing work. One query, not N."""
        items = list(candidates)
        if not items:
            return []

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT remote_path, COALESCE(remote_etag, '') AS etag
                      FROM sync_files
                     WHERE source = :source AND status = 'SUCCEEDED'
                    """
                ),
                {"source": source},
            ).all()

        done = {(r.remote_path, r.etag) for r in rows}
        return [(path, etag) for path, etag in items if (path, etag or "") not in done]

    def start_file(
        self,
        run_id: int,
        source: str,
        *,
        remote_path: str,
        remote_size_bytes: int | None = None,
        remote_etag: str | None = None,
        file_date: date | None = None,
        row_groups_total: int | None = None,
        archived_object_key: str | None = None,
    ) -> SyncFileRecord:
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO sync_files (
                        sync_run_id, source, remote_path, remote_size_bytes, remote_etag,
                        file_date, row_groups_total, archived_object_key, status, started_at
                    ) VALUES (
                        :run_id, :source, :remote_path, :size, :etag,
                        :file_date, :row_groups_total, :object_key,
                        CAST('RUNNING' AS sync_status), now()
                    )
                    RETURNING id, status, row_groups_done, rows_committed
                    """
                ),
                {
                    "run_id": run_id,
                    "source": source,
                    "remote_path": remote_path,
                    "size": remote_size_bytes,
                    "etag": remote_etag,
                    "file_date": file_date,
                    "row_groups_total": row_groups_total,
                    "object_key": archived_object_key,
                },
            ).one()

        return SyncFileRecord(
            id=row.id,
            remote_path=remote_path,
            status=SyncStatus.RUNNING,
            row_groups_done=row.row_groups_done,
            rows_committed=row.rows_committed,
        )

    def checkpoint_file(
        self,
        file_id: int,
        *,
        row_groups_done: int,
        rows_committed: int,
        conn: Connection | None = None,
    ) -> None:
        """Record progress within a unit of work.

        Accepts an existing connection so the checkpoint can be committed **in the same
        transaction** as the data it describes. That atomicity is what makes a crash
        recoverable: the checkpoint can never claim rows that were rolled back.
        """
        statement = text(
            """
            UPDATE sync_files
               SET row_groups_done = :row_groups_done,
                   rows_committed = :rows_committed
             WHERE id = :file_id
            """
        )
        params = {
            "file_id": file_id,
            "row_groups_done": row_groups_done,
            "rows_committed": rows_committed,
        }
        if conn is not None:
            conn.execute(statement, params)
        else:
            with self._engine.begin() as own_conn:
                own_conn.execute(statement, params)

    def finish_file(
        self,
        file_id: int,
        *,
        status: SyncStatus,
        rows_committed: int | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE sync_files
                       SET status = CAST(:status AS sync_status),
                           finished_at = now(),
                           rows_committed = COALESCE(:rows_committed, rows_committed),
                           error_message = :error_message
                     WHERE id = :file_id
                    """
                ),
                {
                    "file_id": file_id,
                    "status": status.value,
                    "rows_committed": rows_committed,
                    # Truncate: a driver traceback can be enormous.
                    "error_message": error_message[:4000] if error_message else None,
                },
            )

    def get_file_progress(self, source: str, remote_path: str) -> SyncFileRecord | None:
        """Latest checkpoint for a unit, so a retry can resume mid-file."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id, remote_path, status, row_groups_done, rows_committed
                      FROM sync_files
                     WHERE source = :source AND remote_path = :remote_path
                     ORDER BY id DESC
                     LIMIT 1
                    """
                ),
                {"source": source, "remote_path": remote_path},
            ).first()

        if row is None:
            return None
        return SyncFileRecord(
            id=row.id,
            remote_path=row.remote_path,
            status=SyncStatus(row.status),
            row_groups_done=row.row_groups_done,
            rows_committed=row.rows_committed,
        )

    # ---- rejections ----------------------------------------------------------

    def record_rejections(
        self, run_id: int, source: str, rejections: Sequence[RejectedRecord]
    ) -> int:
        """Persist rejected rows with their reasons. Returns how many were stored."""

        if not rejections:
            return 0

        rows = [
            {
                "run_id": run_id,
                "file_id": r.sync_file_id,
                "source": source,
                "external_job_id": r.external_job_id,
                "reason": r.reason.value,
                "error_message": (r.error_message or "")[:2000] or None,
                "payload": _encode_payload(r.payload),
            }
            for r in rejections
        ]

        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sync_errors (
                        sync_run_id, sync_file_id, source, external_job_id,
                        reason, error_message, row_payload
                    ) VALUES (
                        :run_id, :file_id, :source, :external_job_id,
                        :reason, :error_message, CAST(:payload AS jsonb)
                    )
                    """
                ),
                rows,
            )
        return len(rows)

    def record_rejection_counts(self, run_id: int, source: str, counts: Counter[str]) -> None:
        """Store one counted row per reason for rejections beyond the sample limit."""
        if not counts:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sync_errors
                        (sync_run_id, source, reason, error_message, occurrence_count)
                    VALUES (:run_id, :source, :reason,
                            'aggregated: additional occurrences beyond the sample limit',
                            :occurrence_count)
                    """
                ),
                [
                    {
                        "run_id": run_id,
                        "source": source,
                        "reason": reason,
                        "occurrence_count": count,
                    }
                    for reason, count in counts.items()
                ],
            )

    def rejection_summary(self, run_id: int) -> dict[str, int]:
        """Counts by reason. Feeds the data-quality panel of the admin dashboard."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT reason, sum(occurrence_count)::bigint AS n
                      FROM sync_errors
                     WHERE sync_run_id = :run_id
                     GROUP BY reason
                     ORDER BY n DESC
                    """
                ),
                {"run_id": run_id},
            ).all()
        return {row.reason: row.n for row in rows}

    @contextmanager
    def rejection_buffer(self, run_id: int, source: str) -> Iterator[_RejectionBuffer]:
        """Batch rejections, flushing periodically and on exit.

        Guarantees a flush even when the body raises, so a crash mid-file still leaves the
        reasons for everything rejected up to that point.
        """
        buffer = _RejectionBuffer(self, run_id, source)
        try:
            yield buffer
        finally:
            # finalize(), not flush(): a crash mid-file must still record the tallied
            # remainder, otherwise the totals silently under-report.
            buffer.finalize()


class _RejectionBuffer:
    """Accumulates rejections, storing a bounded sample plus exact counts.

    Every rejection is still accounted for -- the totals are exact -- but only the first
    ``_MAX_EXAMPLES_PER_REASON`` of each reason keep their full payload. The rest are
    tallied and written as a single counted row when the buffer closes.
    """

    def __init__(self, repo: SyncRepository, run_id: int, source: str) -> None:
        self._repo = repo
        self._run_id = run_id
        self._source = source
        self._pending: list[RejectedRecord] = []
        self._examples: Counter[str] = Counter()
        self._overflow: Counter[str] = Counter()
        self.total = 0

    def add(self, rejection: RejectedRecord) -> None:
        reason = rejection.reason.value
        if self._examples[reason] < _MAX_EXAMPLES_PER_REASON:
            self._examples[reason] += 1
            self._pending.append(rejection)
            if len(self._pending) >= _ERROR_FLUSH_SIZE:
                self.flush()
        else:
            # Past the sample limit this reason only needs counting.
            self._overflow[reason] += 1
        self.total += 1

    def flush(self) -> None:
        if not self._pending:
            return
        self._repo.record_rejections(self._run_id, self._source, self._pending)
        self._pending.clear()

    def finalize(self) -> None:
        """Write the tallied remainder. Called once, when the buffer closes."""
        self.flush()
        if not self._overflow:
            return
        self._repo.record_rejection_counts(self._run_id, self._source, self._overflow)
        self._overflow.clear()
