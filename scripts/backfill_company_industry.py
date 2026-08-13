#!/usr/bin/env python
"""Refresh company industry/size from the source registry, then denormalize onto jobs.

Exists because industry was read from the source but never stored until now. A full
re-ingest would recompute everything else for an hour to recover one column; the company
registry is a single 16 MB file, so refreshing just that takes about a minute.

Also the right tool whenever the source's company metadata changes: it is a lookup table,
not an event stream, so re-reading it is cheap and always safe.

    python scripts/backfill_company_industry.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas", "workers/ingestion"):
    sys.path.insert(0, str(ROOT / pkg))

CHUNK = 2_000


def main() -> int:
    from sqlalchemy import text

    from ingestion.connectors.openjobdata import OpenJobDataConnector
    from ingestion.pipeline.process import _normalize_industry
    from jobplatform_shared import configure_logging, get_settings
    from jobplatform_shared.db import get_sync_engine

    settings = get_settings()
    configure_logging(level="WARNING", fmt="console", service="backfill")

    connector = OpenJobDataConnector(settings)
    engine = get_sync_engine(settings)

    print("Loading company registry from the source...")
    started = time.perf_counter()
    companies = connector._load_companies()
    print(f"  {len(companies):,} companies in {time.perf_counter() - started:.1f}s")

    rows = [
        {
            "external_company_id": str(identifier),
            "industry": _normalize_industry(record.get("industry")),
            "size_range": record.get("size"),
        }
        for identifier, record in companies.items()
        if record.get("industry") or record.get("size")
    ]
    print(f"  {len(rows):,} carry industry or size")

    source = connector.get_source_name()
    updated = 0
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start : start + CHUNK]
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE companies c
                       SET industry = COALESCE(:industry, c.industry),
                           size_range = COALESCE(:size_range, c.size_range)
                      FROM company_sources cs
                     WHERE cs.company_id = c.id
                       AND cs.source = :source
                       AND cs.external_company_id = :external_company_id
                    """
                ),
                [{**row, "source": source} for row in chunk],
            )
            updated += result.rowcount or 0
        print(f"  companies updated: {min(start + CHUNK, len(rows)):,}/{len(rows):,}", end="\r")

    print(f"\n  {updated:,} company rows matched\n")

    # Denormalize onto jobs so the feed query stays single-table.
    print("Copying industry onto jobs...")
    with engine.begin() as conn:
        filled = conn.execute(
            text(
                """
                UPDATE jobs j
                   SET industry = c.industry
                  FROM companies c
                 WHERE c.id = j.company_id
                   AND c.industry IS NOT NULL
                   AND j.industry IS DISTINCT FROM c.industry
                """
            )
        ).rowcount

    with engine.connect() as conn:
        coverage = conn.execute(
            text(
                "SELECT round(100.0 * count(*) FILTER (WHERE industry IS NOT NULL) "
                "/ NULLIF(count(*), 0), 1) FROM jobs"
            )
        ).scalar_one()
        top = conn.execute(
            text(
                "SELECT industry, count(*) AS n FROM jobs WHERE industry IS NOT NULL "
                "GROUP BY 1 ORDER BY n DESC LIMIT 10"
            )
        ).all()

    print(f"  {filled:,} jobs updated — industry coverage now {coverage}%\n")
    print(f"{'INDUSTRY':40} {'JOBS':>9}")
    print("-" * 50)
    for row in top:
        print(f"{row.industry[:39]:40} {row.n:>9,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
