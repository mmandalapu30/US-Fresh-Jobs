"""Every category the classifier can assign must exist in ``job_categories``.

``jobs.category_slug`` has a foreign key to ``job_categories.slug``, so a category the
classifier emits but the migrations never seeded would fail the insert at ingestion time —
in production, on a real job, after the network fetch has already been paid for.

Checked against the **migrated database** rather than by re-reading a single migration
file: the taxonomy is built up across several revisions (0002 seeds it, 0004 splits the
tech categories), so only the end state is meaningful.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_slugs(migrated_database: str) -> set[str]:
    engine = create_engine(migrated_database)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT slug FROM job_categories")).all()
    finally:
        engine.dispose()
    return {row.slug for row in rows}


@pytest.fixture
def classifier_slugs() -> set[str]:
    from ingestion.services.classify import CATEGORIES

    return {category.slug for category in CATEGORIES}


def test_every_assignable_category_is_seeded(
    seeded_slugs: set[str], classifier_slugs: set[str]
) -> None:
    """A missing row here is a foreign-key violation waiting to happen at ingestion."""
    missing = classifier_slugs - seeded_slugs
    assert not missing, (
        f"classifier can assign categories the migrations never seeded: {sorted(missing)}"
    )


def test_no_orphaned_categories(seeded_slugs: set[str], classifier_slugs: set[str]) -> None:
    """A seeded category nothing can assign shows up as a permanently empty filter chip."""
    orphans = seeded_slugs - classifier_slugs
    assert not orphans, f"seeded categories the classifier never assigns: {sorted(orphans)}"


def test_display_names_are_present(migrated_database: str) -> None:
    engine = create_engine(migrated_database)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT slug, name, icon FROM job_categories")).all()
    finally:
        engine.dispose()

    for row in rows:
        assert row.name, f"{row.slug} has no display name"
        assert row.icon, f"{row.slug} has no icon"


def test_foreign_key_is_enforced(migrated_database: str) -> None:
    """The constraint that makes the sync matter in the first place."""
    import psycopg

    dsn = migrated_database.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO jobs (title, title_normalized, source, content_hash, "
                "dedupe_fingerprint, category_slug) "
                "VALUES ('t','t','s','\\x01','\\xaa','no-such-category')"
            )
        conn.rollback()
