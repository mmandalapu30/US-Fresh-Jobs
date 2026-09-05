"""Add the `workday` role category.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-04

Workday roles were spread across `software`, `data-analytics` and `it-ops` according to
whichever ordinary word their title happened to contain, so the one attribute a Workday
consultant, integration developer and HRIS analyst share was the one attribute the board
could not filter on.

Nothing is remapped here. Unlike 0004 and 0006 this revision splits no existing category
away, so no row is left pointing at a slug that is about to disappear -- the classifier
simply gains a rule that fires earlier than the ones those jobs currently match.
``scripts/backfill_categories.py --all`` moves the stored rows over afterwards, and until
it runs the board is merely as accurate as it was before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

#: Sorts between education (20) and data-engineering (22), mirroring where the rule sits
#: in ``ingestion.services.classify``: ahead of the tech block, behind the two domains
#: that outrank tooling. `tests/test_taxonomy_sync.py` asserts slug, name and icon match.
_CATEGORY = ("workday", "Workday", "🧩", 21)


def upgrade() -> None:
    slug, name, icon, sort_order = _CATEGORY
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO job_categories (name, slug, icon, sort_order)
            VALUES (:name, :slug, :icon, :sort_order)
            ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name,
                    icon = EXCLUDED.icon,
                    sort_order = EXCLUDED.sort_order
            """
        ),
        {"slug": slug, "name": name, "icon": icon, "sort_order": sort_order},
    )


def downgrade() -> None:
    conn = op.get_bind()
    # The FK on jobs.category_slug blocks the delete while rows still point here. Where
    # they belong instead is exactly the question this revision was added to answer, so
    # they go to 'other' rather than being guessed at; re-upgrading and running
    # scripts/backfill_categories.py --all is what restores them.
    conn.execute(sa.text("UPDATE jobs SET category_slug = 'other' WHERE category_slug = 'workday'"))
    conn.execute(sa.text("DELETE FROM job_categories WHERE slug = 'workday'"))
