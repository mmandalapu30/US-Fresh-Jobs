#!/usr/bin/env python
"""Backfill role category and seniority level on existing jobs.

New ingestion classifies as it loads. This exists so an already-populated database gains
the columns without re-ingesting from the source — a full re-ingest costs an hour of
network and CPU to recompute something derivable from data already stored.

Also the way to re-run after a taxonomy change: adjust ``JobClassifier``, run this, and
every stored job is reclassified in place.

    python scripts/backfill_categories.py            # only unclassified rows
    python scripts/backfill_categories.py --all      # reclassify everything
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas", "workers/ingestion"):
    sys.path.insert(0, str(ROOT / pkg))

#: Rows per UPDATE. Large enough to amortise round trips, small enough to keep each
#: transaction short so the table is not locked for long stretches.
BATCH = 5_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="reclassify every job, not just rows with no category yet",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    from sqlalchemy import text

    from ingestion.services.classify import CATEGORY_BY_SLUG, JobClassifier
    from jobplatform_shared import get_settings
    from jobplatform_shared.db import get_sync_engine

    classifier = JobClassifier()
    engine = get_sync_engine(get_settings())

    where = "" if args.all else "WHERE category_slug IS NULL"
    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT count(*) FROM jobs {where}")).scalar_one()  # noqa: S608

    if total == 0:
        print("Nothing to classify.")
        return 0

    print(f"Classifying {total:,} jobs (batch {BATCH:,})...")
    started = time.perf_counter()
    categories: Counter[str] = Counter()
    levels: Counter[str] = Counter()
    done = 0
    last_id = 0

    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, title, department, seniority
                      FROM jobs
                     {"WHERE" if args.all else "WHERE category_slug IS NULL AND"} id > :last_id
                     ORDER BY id
                     LIMIT :limit
                    """  # noqa: S608 - the fragment is a literal branch, not input
                ),
                {"last_id": last_id, "limit": BATCH},
            ).all()

        if not rows:
            break

        updates = []
        for row in rows:
            result = classifier.classify(
                row.title, department=row.department, source_seniority=row.seniority
            )
            categories[result.category_slug] += 1
            levels[result.seniority_level] += 1
            updates.append(
                {
                    "job_id": row.id,
                    "category_slug": result.category_slug,
                    "seniority_level": result.seniority_level,
                }
            )

        last_id = rows[-1].id
        done += len(rows)

        if not args.dry_run:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE jobs
                           SET category_slug = :category_slug,
                               seniority_level = CAST(:seniority_level AS seniority_level)
                         WHERE id = :job_id
                        """
                    ),
                    updates,
                )

        print(f"  {done:,}/{total:,} ({100 * done / total:.0f}%)", end="\r", flush=True)

    elapsed = time.perf_counter() - started
    print(f"\n\nClassified {done:,} jobs in {elapsed:,.1f}s ({done / max(elapsed, 0.01):,.0f}/s)")
    if args.dry_run:
        print("(dry run — nothing written)")

    print(f"\n{'CATEGORY':32} {'COUNT':>9} {'%':>7}")
    print("-" * 52)
    for slug, count in categories.most_common():
        name = CATEGORY_BY_SLUG[slug].name
        print(f"{name[:31]:32} {count:>9,} {100 * count / done:>6.1f}%")

    print(f"\n{'LEVEL':32} {'COUNT':>9} {'%':>7}")
    print("-" * 52)
    for level, count in levels.most_common():
        print(f"{level:32} {count:>9,} {100 * count / done:>6.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
