"""Split `data` into `data-engineering`, `data-analytics` and `data-science`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-10

"Data roles" is three distinct jobs. A data engineer builds pipelines, an analyst queries
them, a scientist models on top. Someone hiring — or job hunting — for one is usually not
interested in the other two, and a single `data` category made it impossible for the
ingestion scope to say which.

Existing rows are remapped coarsely to satisfy the foreign key;
``scripts/backfill_categories.py`` reclassifies everything properly straight after.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_NEW = [
    ("data-engineering", "Data Engineering", "🔧", 22),
    ("data-analytics", "Data Analytics", "📊", 24),
    ("data-science", "Data Science & ML", "🧪", 26),
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

    conn.execute(
        sa.text(
            """
            UPDATE jobs SET category_slug = CASE
                WHEN title ~* '(data engineer|analytics engineer|etl|data warehouse|data platform|data pipeline)'
                    THEN 'data-engineering'
                WHEN title ~* '(data scien|machine learning|artificial intelligence|deep learning)'
                    THEN 'data-science'
                ELSE 'data-analytics'
            END
            WHERE category_slug = 'data'
            """
        )
    )

    conn.execute(sa.text("DELETE FROM job_categories WHERE slug = 'data'"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO job_categories (name, slug, icon, sort_order)
            VALUES ('Data & Analytics', 'data', '📊', 25)
            ON CONFLICT (slug) DO NOTHING
            """
        )
    )
    conn.execute(
        sa.text(
            "UPDATE jobs SET category_slug = 'data' WHERE category_slug IN "
            "('data-engineering', 'data-analytics', 'data-science')"
        )
    )
    conn.execute(
        sa.text(
            "DELETE FROM job_categories WHERE slug IN "
            "('data-engineering', 'data-analytics', 'data-science')"
        )
    )
