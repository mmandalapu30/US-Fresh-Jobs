#!/usr/bin/env python
"""Has the source published a file we have not ingested? Exit status is the answer.

The publisher uploads one file per day somewhere between 06:33 and 14:15 UTC, and the
hour genuinely varies (``docs/00-source-verification.md`` §5). A calendar schedule can
only trade one cost against the other: few slots and the day's jobs sit unfetched for
hours, many slots and most passes run a full ingest to learn there was nothing to fetch.
This asks the source directly, so a frequent timer costs one directory listing while
idle and the ingest starts within one interval of publication.

    0  nothing new -- the newest published file is already ingested
    1  the newest published file is not ingested -- the caller should run the ingest
    2  could not tell (source or database unreachable) -- the caller should ingest anyway

The codes match scripts/have_todays_file.py deliberately: 0 means "no action", and both
1 and 2 mean "run it". Exit 2 is not fatal for the same reason it is not there -- an
unknown answer must lead to ingesting, never to skipping, because the ingest is
idempotent while a silently skipped day is not repaired by the next pass.

It asks about the NEWEST file, not about every unprocessed one, and the difference
matters. The scheduled ingest is bounded to the newest few files (INGEST_MAX_FILES in
scripts/daily.sh), so files older than that window stay unprocessed permanently by
design -- 59 were pending on one deployment. A watcher that fired whenever *anything*
was unprocessed would therefore fire on every single pass forever and never converge.
Raise --window to also notice a gap the publisher backfills behind the newest file; keep
it at 1 to track only what "a new file has appeared" ordinarily means.

This deliberately does not open a sync run. IngestionPipeline.run() inserts its
sync_runs row before it discovers anything, so polling through that path would leave a
near-empty run for every idle pass -- roughly fifty a day at a ten-minute interval --
and would hold the per-source lock each time, blocking the admin console's fetch button
for as long as it held it. Both sides of this check are read-only.

    python scripts/has_new_file.py
    python scripts/has_new_file.py --window 3    # also catch a recently backfilled gap
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas", "workers/ingestion"):
    sys.path.insert(0, str(ROOT / pkg))

NONE_NEW, NEW, UNKNOWN = 0, 1, 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window",
        type=int,
        default=1,
        help="consider the newest N published files, not just the newest one (default 1)",
    )
    parser.add_argument("--quiet", action="store_true", help="exit status only, no output")
    args = parser.parse_args()

    if args.window < 1:
        print("--window must be at least 1", file=sys.stderr)
        return UNKNOWN

    def say(message: str) -> None:
        if not args.quiet:
            print(message)

    try:
        from sqlalchemy import text

        from ingestion.connectors.openjobdata import OpenJobDataConnector
        from jobplatform_shared import get_settings
        from jobplatform_shared.db import get_sync_engine

        settings = get_settings()
        connector = OpenJobDataConnector(settings)
        # Ask the connector for its own name rather than hardcoding one, as
        # have_todays_file.py does and for the same reason: `scripts/` is exempt from the
        # layering guard, but the exemption covers wiring, not spreading the provider's
        # identifier into queries.
        source = connector.get_source_name()

        # discover() returns the real listing sorted by file date, so the tail is newest.
        discovered = list(connector.discover())

        # The same comparison _filter_pending makes: a file counts as done only at the
        # exact version we ingested. etag is the publisher's content hash, so a corrected
        # republish under an unchanged filename reads as new work rather than as done.
        with get_sync_engine(settings).connect() as conn:
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
    except Exception as exc:  # any failure means "unknown" -- see the docstring
        say(f"could not determine whether the source has a new file: {exc}")
        return UNKNOWN

    if not discovered:
        # An empty listing is the source misbehaving, not a quiet day. Treat it as
        # unknown so the caller ingests and surfaces the real error, rather than
        # reporting "nothing new" every ten minutes while the feed is broken.
        say("the source listed no files at all")
        return UNKNOWN

    done = {(r.remote_path, r.etag) for r in rows}
    watched = discovered[-args.window :]
    pending = [f for f in watched if (f.remote_path, f.etag or "") not in done]

    latest = watched[-1].file_date
    if not pending:
        if args.window == 1:
            say(f"nothing new: the newest file ({latest}) is ingested")
        else:
            say(f"nothing new: the newest {args.window} files are ingested (latest {latest})")
        return NONE_NEW

    if args.window == 1:
        say(f"new file to ingest: {latest}")
    else:
        say(f"{len(pending)} of the newest {args.window} files to ingest, newest {latest}")
    return NEW


if __name__ == "__main__":
    raise SystemExit(main())
