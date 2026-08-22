#!/usr/bin/env python
"""Run an ingestion pass. The operational entry point until Celery Beat schedules it.

python scripts/ingest.py --max-files 1
python scripts/ingest.py --since 2026-08-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas", "workers/ingestion"):
    sys.path.insert(0, str(ROOT / pkg))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=dt.date.fromisoformat,
        default=None,
        help="only process delta files on or after this date",
    )
    parser.add_argument(
        "--max-files", type=int, default=None, help="process at most N of the newest pending files"
    )
    parser.add_argument(
        "--trigger", default="MANUAL", choices=["SCHEDULED", "MANUAL", "BACKFILL", "RETRY"]
    )
    parser.add_argument(
        "--reclaim-only",
        action="store_true",
        help="release abandoned RUNNING rows and exit; ingest nothing",
    )
    args = parser.parse_args()

    from jobplatform_schemas import SyncTrigger
    from jobplatform_shared import configure_logging, get_settings
    from jobplatform_shared.db import get_sync_engine

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format, service="ingest")

    from ingestion.connectors.openjobdata import OpenJobDataConnector
    from ingestion.pipeline import IngestionPipeline

    connector = OpenJobDataConnector(settings)
    engine = get_sync_engine(settings)

    # Lock hygiene on its own, reachable without doing any work.
    #
    # IngestionPipeline.run() reclaims abandoned runs before it starts, which is useless
    # on exactly the passes that need it most: the watch guard answers "no new file" and
    # returns before a pipeline is ever built, so nothing calls the reclaim. A worker
    # killed mid-run therefore held the lock not for the reclaim window but until the
    # next pass that actually ingested -- the following day's scheduled run. Shortening
    # the window did nothing for that, because the window was never being consulted.
    if args.reclaim_only:
        from ingestion.repositories import SyncRepository

        released = SyncRepository(engine).reclaim_stale_runs(
            connector.get_source_name(),
            older_than_minutes=settings.ingest_stale_run_reclaim_minutes,
        )
        print(f"reclaimed {released} abandoned run(s)")
        return 0

    pipeline = IngestionPipeline(connector, engine, settings=settings)

    started = time.perf_counter()
    report = pipeline.run(
        since=args.since,
        max_files=args.max_files,
        trigger=SyncTrigger[args.trigger],
    )
    elapsed = time.perf_counter() - started

    print("\n" + "=" * 66)
    print(f"  sync run   : {report.sync_run_id}   status: {report.status.value}")
    print(
        f"  files      : {report.files_processed} processed, "
        f"{report.files_skipped} already done, {report.files_discovered} discovered"
    )
    print(f"  rows       : {report.rows_processed:,} processed")
    print(f"    accepted : {report.rows_accepted:,}")
    print(f"    rejected : {report.rows_rejected:,}")
    print(
        f"  writes     : {report.rows_inserted:,} inserted, "
        f"{report.rows_updated:,} updated, {report.rows_unchanged:,} unchanged, "
        f"{report.rows_merged:,} merged"
    )
    print(
        f"  elapsed    : {elapsed:,.1f}s  ({report.rows_processed / elapsed:,.0f} rows/s)"
        if elapsed
        else ""
    )
    print("=" * 66)
    if report.status.value in {"SUCCEEDED", "PARTIAL"}:
        return 0
    # 3 == "another run holds the per-source lock", the only cause of CANCELLED. It is
    # not a failure and, more practically, it is not retryable on a human timescale:
    # an abandoned run is only reclaimed after 120 minutes, so a caller that retries in
    # seconds can never succeed. Distinguishing it lets a scheduler skip instead of
    # burning attempts and reporting a failure that never happened.
    if report.status.value == "CANCELLED":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
