"""Split `software-it` into `data`, `software` and `it-ops`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-10

A data role and a programming role are different products to a job seeker, and IT support
is a third thing again. One combined category made it impossible for the ingestion scope
to name what it actually wanted.

Existing rows are remapped rather than dropped: the FK on ``jobs.category_slug`` means the
old slug cannot be deleted while rows still reference it. Remapping here is a coarse
holding position -- ``scripts/backfill_categories.py`` reclassifies every job properly
against the new rules straight after.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_NEW = [
    ("data", "Data & Analytics", "📊", 25),
    ("software", "Software Engineering", "💻", 30),
    ("it-ops", "IT & Infrastructure", "🖥️", 35),
]


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
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
        [
            {"slug": slug, "name": name, "icon": icon, "sort_order": order}
            for slug, name, icon, order in _NEW
        ],
    )

    # Coarse remap so the FK stays satisfied. Titles are re-examined properly by the
    # backfill script; this only has to be valid, not accurate.
    conn.execute(
        sa.text(
            """
            UPDATE jobs SET category_slug = CASE
                WHEN title ~* '(data|analytic|machine learning|business intelligence)'
                    THEN 'data'
                WHEN title ~* '(support|help ?desk|administrator|infrastructure|salesforce)'
                    THEN 'it-ops'
                ELSE 'software'
            END
            WHERE category_slug = 'software-it'
            """
        )
    )

    conn.execute(sa.text("DELETE FROM job_categories WHERE slug = 'software-it'"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO job_categories (name, slug, icon, sort_order)
            VALUES ('Software & IT', 'software-it', '💻', 30)
            ON CONFLICT (slug) DO NOTHING
            """
        )
    )
    conn.execute(
        sa.text(
            "UPDATE jobs SET category_slug = 'software-it' "
            "WHERE category_slug IN ('data', 'software', 'it-ops')"
        )
    )
    conn.execute(
        sa.text("DELETE FROM job_categories WHERE slug IN ('data', 'software', 'it-ops')")
    )
