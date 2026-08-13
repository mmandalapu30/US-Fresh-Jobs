#!/usr/bin/env python
"""Has today's delta file already been ingested? Exit status is the answer.

The publisher uploads one file per day, named by date, somewhere between 06:33 and
14:15 UTC -- and the hour genuinely varies (``docs/00-source-verification.md`` §5). A
single fixed morning slot therefore lands before the file exists on a good fraction of
days, so the schedule pairs one primary run with cheap re-checks through the afternoon.

This is the guard those re-checks ask first, so a catch-up costs one indexed query
rather than a directory listing and a sync_runs row on every pass once the day is done.

    0  today's file is already SUCCEEDED -- nothing to do
    1  not ingested yet -- the caller should run the ingest
    2  could not tell (database unreachable, misconfigured)

Exit 2 is deliberately distinct and deliberately *not* fatal to the caller: an unknown
answer must lead to running the ingest, never to skipping it. Failing closed here would
turn a transient database blip into a silently skipped day.

    python scripts/have_todays_file.py            # today, UTC
    python scripts/have_todays_file.py --date 2026-08-13
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas", "workers/ingestion"):
    sys.path.insert(0, str(ROOT / pkg))

FOUND, MISSING, UNKNOWN = 0, 1, 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=dt.date.fromisoformat,
        default=None,
        help="check this file date instead of today (UTC)",
    )
    parser.add_argument("--quiet", action="store_true", help="exit status only, no output")
    args = parser.parse_args()

    # UTC, not local. The file is named for the publisher's UTC date, and every slot in
    # the shipped schedule (09:00-17:00 America/New_York, i.e. 13:00-22:00 UTC) falls on
    # that same UTC day -- so the two never disagree for a run this schedule makes.
    target = args.date or dt.datetime.now(dt.UTC).date()

    def say(message: str) -> None:
        if not args.quiet:
            print(message)

    try:
        from sqlalchemy import text

        from ingestion.connectors.openjobdata import OpenJobDataConnector
        from jobplatform_shared import get_settings
        from jobplatform_shared.db import get_sync_engine

        settings = get_settings()
        # Ask the connector for its own name rather than hardcoding one. `scripts/` is
        # exempt from the layering guard, but the exemption is for wiring like this
        # import -- not a licence to spread the provider's identifier into queries.
        source = OpenJobDataConnector(settings).get_source_name()

        with get_sync_engine(settings).connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT rows_committed,
                           -- Converted here, not in Python. A timestamptz comes back in
                           -- whatever zone the server session happens to use, which is
                           -- not necessarily UTC -- one development host answers in
                           -- America/Los_Angeles -- and formatting that directly prints
                           -- a local time under a UTC label. AT TIME ZONE settles it at
                           -- the source, so the label is true wherever this runs.
                           finished_at AT TIME ZONE 'UTC' AS finished_at_utc
                      FROM sync_files
                     WHERE source = :source
                       AND file_date = :file_date
                       AND status = 'SUCCEEDED'
                     ORDER BY finished_at DESC
                     LIMIT 1
                    """
                ),
                {"source": source, "file_date": target},
            ).first()
    except Exception as exc:  # any failure means "unknown" -- see the docstring
        say(f"could not determine whether {target} is ingested: {exc}")
        return UNKNOWN

    if row is None:
        say(f"{target}: not ingested yet")
        return MISSING

    say(
        f"{target}: already ingested ({row.rows_committed:,} rows, at {row.finished_at_utc:%H:%M} UTC)"
    )
    return FOUND


if __name__ == "__main__":
    raise SystemExit(main())
