#!/usr/bin/env python
"""Delete jobs outside the configured role scope.

**This is destructive and irreversible.** It runs as a dry run unless `--execute` is
passed, and `--execute` additionally requires typing the job count to confirm.

Why it exists: narrowing `INGEST_CATEGORY_ALLOWLIST` stops *future* jobs arriving, but the
rows already stored stay. This removes them so the platform matches its new scope.

Note this deliberately contradicts the platform's normal "never delete, only transition"
rule. That rule protects job *history* — a posting that closed still happened. Purging an
entire category is a different act: a product decision that those roles are not part of
this platform at all. It is a one-off operator action, not something the pipeline does.

    python scripts/purge_out_of_scope.py                    # dry run (default)
    python scripts/purge_out_of_scope.py --execute          # asks for confirmation
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas", "workers/ingestion"):
    sys.path.insert(0, str(ROOT / pkg))

#: Rows per delete. Bounded so each transaction is short and the table is never locked
#: for long; a single DELETE of 180k rows would hold locks for the whole run.
BATCH = 5_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="actually delete (default is a dry run)"
    )
    parser.add_argument(
        "--keep",
        default=None,
        help="comma-separated category slugs to keep; defaults to INGEST_CATEGORY_ALLOWLIST",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the interactive confirmation prompt"
    )
    args = parser.parse_args()

    from sqlalchemy import text

    from jobplatform_shared import get_settings
    from jobplatform_shared.db import get_sync_engine

    settings = get_settings()
    keep = (
        [s.strip().lower() for s in args.keep.split(",") if s.strip()]
        if args.keep
        else list(settings.ingest_category_allowlist)
    )

    if not keep:
        print(
            "No categories to keep.\n"
            "Set INGEST_CATEGORY_ALLOWLIST or pass --keep, otherwise this would delete\n"
            "every job in the database."
        )
        return 2

    engine = get_sync_engine(settings)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT COALESCE(c.name, j.category_slug, 'unclassified') AS name,
                       j.category_slug,
                       count(*) AS n
                  FROM jobs j
                  LEFT JOIN job_categories c ON c.slug = j.category_slug
                 GROUP BY 1, 2
                 ORDER BY n DESC
                """
            )
        ).all()

    keeping = [r for r in rows if r.category_slug in keep]
    deleting = [r for r in rows if r.category_slug not in keep]
    keep_total = sum(r.n for r in keeping)
    delete_total = sum(r.n for r in deleting)

    print(f"\nKeeping: {', '.join(keep)}\n")
    print(f"{'CATEGORY':34} {'JOBS':>9}   ACTION")
    print("-" * 60)
    for row in keeping:
        print(f"{row.name[:33]:34} {row.n:>9,}   KEEP")
    for row in deleting:
        print(f"{row.name[:33]:34} {row.n:>9,}   DELETE")
    print("-" * 60)
    print(f"{'KEEP':34} {keep_total:>9,}")
    print(f"{'DELETE':34} {delete_total:>9,}")

    if not args.execute:
        print("\nDry run — nothing deleted. Re-run with --execute to apply.")
        return 0

    if delete_total == 0:
        print("\nNothing to delete.")
        return 0

    if not args.yes:
        print(
            f"\nThis permanently deletes {delete_total:,} jobs and all their events,"
            f"\nprovenance and saved references. It cannot be undone."
        )
        answer = input(f"Type the number {delete_total} to proceed: ").strip()
        if answer != str(delete_total):
            print("Aborted — input did not match.")
            return 1

    print("\nDeleting...")
    started = time.perf_counter()
    removed = 0
    while True:
        with engine.begin() as conn:
            # Child rows go via ON DELETE CASCADE on job_sources/job_events/job_skills.
            # job_events is partitioned, so the cascade fans out per partition -- another
            # reason to keep each batch small.
            result = conn.execute(
                text(
                    """
                    DELETE FROM jobs
                     WHERE id IN (
                         SELECT id FROM jobs
                          -- The NULL arm matters: `NOT (x = ANY(...))` is NULL, not
                          -- true, when x is NULL, so unclassified rows would survive.
                          WHERE category_slug IS NULL
                             OR NOT (category_slug = ANY(:keep))
                          LIMIT :limit
                     )
                    """
                ),
                {"keep": keep, "limit": BATCH},
            )
        count = result.rowcount or 0
        removed += count
        print(f"  {removed:,}/{delete_total:,}", end="\r", flush=True)
        if count == 0:
            break

    elapsed = time.perf_counter() - started
    print(f"\n\nDeleted {removed:,} jobs in {elapsed:,.1f}s")

    with engine.connect() as conn:
        remaining = conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()
        by_cat = conn.execute(
            text("SELECT category_slug, count(*) AS n FROM jobs GROUP BY 1 ORDER BY n DESC")
        ).all()

    print(f"\nRemaining: {remaining:,} jobs")
    for row in by_cat:
        print(f"  {row.category_slug or 'unclassified':20} {row.n:>8,}")

    print(
        "\nOrphaned companies and locations are left in place: they are cheap, and a\n"
        "later ingest may reference them again."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
