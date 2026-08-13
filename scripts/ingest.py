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
    args = parser.parse_args()

    from jobplatform_schemas import SyncTrigger
    from jobplatform_shared import configure_logging, get_settings
    from jobplatform_shared.db import get_sync_engine

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format, service="ingest")

    from ingestion.connectors.openjobdata import OpenJobDataConnector
    from ingestion.pipeline import IngestionPipeline

    connector = OpenJobDataConnector(settings)
    pipeline = IngestionPipeline(connector, get_sync_engine(settings), settings=settings)

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
