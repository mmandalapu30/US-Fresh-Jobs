#!/usr/bin/env python
"""Remove jobs older than the freshness window. Safe to run on a schedule.

The ingestion gate stops *new* stale jobs arriving. This removes ones already stored that
have since aged out, so the board stays fresh without manual intervention.

Age is judged on the employer's ``posted_at`` where it is trustworthy, and on our own
``first_seen_at`` where it is not. Roughly 17% of source rows carry no posting date at all;
judging those by detection time keeps them for a fair window instead of deleting them for
a fault that is the source's, not the job's.

    python scripts/enforce_retention.py                  # dry run (default)
    python scripts/enforce_retention.py --execute        # delete
    python scripts/enforce_retention.py --execute --expire-only   # mark EXPIRED instead

Schedule it daily:
    0 3 * * *  cd /srv/job-platform && python scripts/enforce_retention.py --execute --yes
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas", "workers/ingestion"):
    sys.path.insert(0, str(ROOT / pkg))

BATCH = 5_000

#: A job is out of window when the best date we have is older than the cutoff.
#: COALESCE order encodes the preference: employer's date first, our detection time
#: second. Both are compared against the same cutoff so the rule is easy to reason about.
_AGE_PREDICATE = """
    COALESCE(
        CASE WHEN posted_at_is_valid THEN posted_at END,
        first_seen_at
    ) < :cutoff
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="apply (default is a dry run)")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="freshness window in days; defaults to RETENTION_MAX_POSTED_AGE_DAYS",
    )
    parser.add_argument(
        "--expire-only",
        action="store_true",
        help="mark jobs EXPIRED and keep them, instead of deleting",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument(
        "--keep-errors-days",
        type=int,
        default=14,
        help="also trim sync_errors older than this many days (0 disables)",
    )
    args = parser.parse_args()

    from sqlalchemy import text

    from jobplatform_shared import get_settings
    from jobplatform_shared.db import get_sync_engine
    from jobplatform_shared.time import utc_now

    settings = get_settings()
    days = args.days if args.days is not None else settings.retention_max_posted_age_days

    if days <= 0:
        print(
            "Retention is disabled (RETENTION_MAX_POSTED_AGE_DAYS=0).\n"
            "Set it, or pass --days N, to enable."
        )
        return 2

    from datetime import timedelta

    cutoff = utc_now() - timedelta(days=days)
    engine = get_sync_engine(settings)

    with engine.connect() as conn:
        summary = conn.execute(
            text(
                f"""
                SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE {_AGE_PREDICATE}) AS out_of_window,
                    count(*) FILTER (WHERE {_AGE_PREDICATE} AND posted_at IS NULL)
                        AS out_of_window_no_date
                  FROM jobs
                """  # noqa: S608 - predicate is a module constant; cutoff is bound
            ),
            {"cutoff": cutoff},
        ).one()

    keeping = summary.total - summary.out_of_window
    print(f"\nFreshness window: {days} days (cutoff {cutoff:%Y-%m-%d %H:%M} UTC)")
    print(f"  total jobs        {summary.total:>9,}")
    print(f"  within window     {keeping:>9,}")
    print(f"  out of window     {summary.out_of_window:>9,}")
    print(f"    of which no posting date, judged on detection: {summary.out_of_window_no_date:,}")

    if not args.execute:
        print("\nDry run — nothing changed. Re-run with --execute to apply.")
        return 0

    if summary.out_of_window == 0:
        print("\nNothing to do.")
        return 0

    action = "mark EXPIRED" if args.expire_only else "DELETE"
    if not args.yes:
        print(f"\nAbout to {action} {summary.out_of_window:,} jobs.")
        if input("Type yes to proceed: ").strip().lower() != "yes":
            print("Aborted.")
            return 1

    print(f"\n{action}...")
    started = time.perf_counter()
    affected = 0

    while True:
        with engine.begin() as conn:
            if args.expire_only:
                # Keeps history, per the platform's usual "transition, never delete" rule.
                result = conn.execute(
                    text(
                        f"""
                        UPDATE jobs SET status = 'EXPIRED',
                                        closed_at = COALESCE(closed_at, now())
                         WHERE id IN (
                             SELECT id FROM jobs
                              WHERE status <> 'EXPIRED' AND {_AGE_PREDICATE}
                              LIMIT :limit
                         )
                        """  # noqa: S608
                    ),
                    {"cutoff": cutoff, "limit": BATCH},
                )
            else:
                result = conn.execute(
                    text(
                        f"""
                        DELETE FROM jobs
                         WHERE id IN (
                             SELECT id FROM jobs WHERE {_AGE_PREDICATE} LIMIT :limit
                         )
                        """  # noqa: S608
                    ),
                    {"cutoff": cutoff, "limit": BATCH},
                )
        count = result.rowcount or 0
        affected += count
        print(f"  {affected:,}/{summary.out_of_window:,}", end="\r", flush=True)
        if count == 0:
            break

    elapsed = time.perf_counter() - started
    print(f"\n\n{action}: {affected:,} jobs in {elapsed:,.1f}s")

    with engine.connect() as conn:
        after = conn.execute(
            text(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status='ACTIVE') AS active,
                       count(*) FILTER (WHERE posted_at_is_valid
                                          AND posted_at >= now()-interval '24 hours')
                           AS posted_24h,
                       count(*) FILTER (WHERE first_seen_at >= now()-interval '24 hours')
                           AS found_24h
                  FROM jobs
                """
            )
        ).one()

    print(
        f"\nRemaining: {after.total:,} jobs ({after.active:,} active)\n"
        f"  posted in last 24h: {after.posted_24h:,}\n"
        f"  found by us in last 24h: {after.found_24h:,}"
    )

    # Rejections are the audit trail behind "nothing is silently discarded", but they
    # accumulate far faster than jobs: a narrow ingest scope rejects far more than it
    # keeps, and the table reached 2.3M rows / 563 MB in a day. Recent ones are what
    # anyone actually investigates; older ones are dead weight.
    if args.keep_errors_days > 0:
        error_cutoff = utc_now() - timedelta(days=args.keep_errors_days)
        with engine.begin() as conn:
            trimmed = (
                conn.execute(
                    text("DELETE FROM sync_errors WHERE occurred_at < :cutoff"),
                    {"cutoff": error_cutoff},
                ).rowcount
                or 0
            )
        if trimmed:
            print(
                f"\nTrimmed {trimmed:,} rejection records older than {args.keep_errors_days} days"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
