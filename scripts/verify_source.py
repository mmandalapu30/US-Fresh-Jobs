#!/usr/bin/env python
"""Re-verify every claim in docs/00-source-verification.md against the live bucket.

Run this before trusting the documentation — an upstream source can change its layout,
schema or cadence at any time, and silently building on a stale assumption is exactly the
failure mode the spec warns about.

    python scripts/verify_source.py            # structure + schema (fast, ~30s)
    python scripts/verify_source.py --deep     # also parses a full-variant delta (slow)

Exit code is non-zero if any check fails, so CI can run it on a schedule.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass, field

BUCKET = "buckets/Invicto69/Jobs-Dataset-bucket"

# Expected values recorded during the 2026-08-10 verification.
EXPECTED_MINIMAL_COLUMNS = [
    "id",
    "job_id",
    "company_id",
    "title",
    "department",
    "employment_type",
    "workplace_type",
    "country",
    "is_remote",
    "posted_at",
    "apply_url",
    "fetched_time",
    "status",
    "close_time",
]
EXPECTED_FULL_EXTRA_COLUMNS = ["entire_json", "job_model_json"]
EXPECTED_LOCATION_KEYS = {
    "city",
    "country",
    "is_remote",
    "postal_code",
    "raw_location_text",
    "state",
    "workplace_type",
}


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, label: str, condition: bool, detail: str = "") -> bool:
        target = self.passed if condition else self.failed
        target.append(f"{label}{f'  [{detail}]' if detail else ''}")
        return condition

    def note(self, text: str) -> None:
        self.notes.append(text)

    def render(self) -> int:
        print("\n" + "=" * 78)
        print("OpenJobData source verification")
        print("=" * 78)
        for line in self.passed:
            print(f"  PASS  {line}")
        for line in self.failed:
            print(f"  FAIL  {line}")
        if self.notes:
            print("\n  Observations:")
            for line in self.notes:
                print(f"    - {line}")
        print("-" * 78)
        print(f"  {len(self.passed)} passed, {len(self.failed)} failed")
        print("=" * 78)
        return 1 if self.failed else 0


def _load_double_encoded(value: object) -> dict | None:
    """The JSON columns are double-encoded: a JSON string containing JSON."""
    current: object = value
    for _ in range(3):
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except json.JSONDecodeError:
                return None
        else:
            break
    return current if isinstance(current, dict) else None


def verify(deep: bool) -> int:
    report = Report()

    try:
        from huggingface_hub import HfFileSystem
    except ImportError:
        print("huggingface_hub is not installed. Run: pip install huggingface-hub", file=sys.stderr)
        return 2

    fs = HfFileSystem()  # public bucket: no token required

    # ---- 1. access + structure ------------------------------------------------
    try:
        root = {entry["name"].split("/")[-1] for entry in fs.ls(BUCKET, detail=True)}
    except Exception as exc:
        print(f"FATAL: cannot list the bucket: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report.check("anonymous access works (no token)", True)
    report.check("README.md present", "README.md" in root)
    report.check("data/ present", "data" in root)

    variants = {e["name"].split("/")[-1] for e in fs.ls(f"{BUCKET}/data", detail=True)}
    report.check(
        "data/full, data/minimal, data/companies present",
        {"full", "minimal", "companies"} <= variants,
        f"found {sorted(variants)}",
    )

    # ---- 2. license -----------------------------------------------------------
    try:
        with fs.open(f"{BUCKET}/README.md", "r") as handle:
            readme = handle.read()
        report.check("license declared as MIT", "license: mit" in readme.lower())
    except Exception as exc:
        report.check("README readable", False, type(exc).__name__)

    # ---- 3. deltas: cadence and gaps -----------------------------------------
    for variant in ("minimal", "full"):
        try:
            entries = fs.ls(f"{BUCKET}/data/{variant}/changes", detail=True)
        except Exception as exc:
            report.check(f"{variant}/changes listable", False, type(exc).__name__)
            continue

        dates = sorted(e["name"].split("/")[-1].removesuffix(".parquet") for e in entries)
        report.check(f"{variant}/changes has daily files", len(dates) > 0, f"{len(dates)} files")

        # Contiguity: the publisher is known to skip days.
        try:
            first, last = dt.date.fromisoformat(dates[0]), dt.date.fromisoformat(dates[-1])
            expected = {
                (first + dt.timedelta(days=i)).isoformat() for i in range((last - first).days + 1)
            }
            missing = sorted(expected - set(dates))
            if missing:
                report.note(
                    f"{variant}: {len(missing)} missing date(s) in range -- "
                    f"discover() must diff the listing, not iterate dates. "
                    f"e.g. {missing[:5]}"
                )
            lag = (dt.datetime.now(dt.UTC).date() - last).days
            report.note(f"{variant}: newest delta {last} (publication lag {lag} day(s))")
        except ValueError:
            report.check(f"{variant} delta filenames are YYYY-MM-DD", False, dates[0])

    # ---- 4. schema ------------------------------------------------------------
    try:
        import pyarrow.parquet as pq
    except ImportError:
        report.note("pyarrow not installed — schema checks skipped")
        return report.render()

    latest = sorted(e["name"] for e in fs.ls(f"{BUCKET}/data/minimal/changes", detail=True))[-1]

    with fs.open(latest, "rb") as handle:
        parquet = pq.ParquetFile(handle)
        minimal_columns = list(parquet.schema_arrow.names)
        minimal_rows = parquet.metadata.num_rows

    report.check(
        "minimal schema matches the documented 14 columns",
        minimal_columns == EXPECTED_MINIMAL_COLUMNS,
        f"got {minimal_columns}" if minimal_columns != EXPECTED_MINIMAL_COLUMNS else "",
    )
    report.check(
        "minimal variant has NO city/state (drives the full-variant decision)",
        not {"city", "state"} & set(minimal_columns),
    )
    report.note(f"newest minimal delta: {latest.split('/')[-1]} with {minimal_rows:,} rows")

    full_latest = latest.replace("/minimal/", "/full/")
    with fs.open(full_latest, "rb") as handle:
        parquet = pq.ParquetFile(handle)
        full_columns = list(parquet.schema_arrow.names)
        metadata = parquet.metadata

    report.check(
        "full schema = minimal + entire_json + job_model_json",
        set(full_columns) == set(EXPECTED_MINIMAL_COLUMNS) | set(EXPECTED_FULL_EXTRA_COLUMNS),
        f"got {full_columns}",
    )

    # Column-level byte cost: proves the projection saving is real.
    sizes: dict[str, int] = {}
    for group_index in range(metadata.num_row_groups):
        group = metadata.row_group(group_index)
        for column_index in range(group.num_columns):
            column = group.column(column_index)
            sizes[column.path_in_schema] = (
                sizes.get(column.path_in_schema, 0) + column.total_compressed_size
            )
    total = sum(sizes.values())
    entire = sizes.get("entire_json", 0)
    report.check(
        "excluding entire_json saves >40% of the download",
        entire / total > 0.40 if total else False,
        f"entire_json is {100 * entire / total:.1f}% of {total / 1e6:.0f} MB",
    )
    report.note(
        f"projected daily read: {(total - entire) / 1e6:.0f} MB "
        f"(vs {total / 1e6:.0f} MB unprojected)"
    )

    # ---- 5. deep checks -------------------------------------------------------
    if deep:
        import collections

        with fs.open(full_latest, "rb") as handle:
            parquet = pq.ParquetFile(handle)
            table = parquet.read_row_group(0, columns=["country", "job_model_json"])

        location_keys: collections.Counter[str] = collections.Counter()
        parsed = 0
        for raw in table.column("job_model_json").to_pylist():
            model = _load_double_encoded(raw)
            if model is None:
                continue
            parsed += 1
            location = model.get("location")
            if isinstance(location, dict):
                location_keys.update(location.keys())

        report.check(
            "job_model_json is double-encoded JSON and parses", parsed > 0, f"{parsed} rows parsed"
        )
        report.check(
            "job_model_json.location carries city/state/postal_code",
            set(location_keys) >= EXPECTED_LOCATION_KEYS,
            f"got {sorted(location_keys)}",
        )

        # Timestamp quality on the full delta.
        with fs.open(latest, "rb") as handle:
            table = pq.ParquetFile(handle).read(columns=["posted_at", "country", "id"])

        posted = table.column("posted_at").to_pylist()
        nulls = sum(1 for value in posted if value is None)
        now = dt.datetime.now(dt.UTC)
        future = sum(
            1 for value in posted if value is not None and value.replace(tzinfo=dt.UTC) > now
        )
        report.note(f"posted_at NULL: {nulls:,}/{len(posted):,} ({100 * nulls / len(posted):.1f}%)")
        report.check(
            "future-dated posted_at exists (freshness gate is required)",
            future > 0,
            f"{future:,} rows dated in the future",
        )

        ids = table.column("id").to_pylist()
        report.check("id is unique within a file", len(ids) == len(set(ids)))

        countries = collections.Counter(table.column("country").to_pylist())
        us_rows = countries.get("United States", 0)
        report.check("US rows dominate the feed", us_rows > 0, f"{us_rows:,} US rows")
        report.note(
            "top countries: "
            + ", ".join(f"{name or 'NULL'}={count:,}" for name, count in countries.most_common(5))
        )

    return report.render()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="also parse a full-variant delta (downloads ~100 MB)",
    )
    args = parser.parse_args()
    return verify(deep=args.deep)


if __name__ == "__main__":
    raise SystemExit(main())
