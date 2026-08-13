"""Role categories and seniority levels for position filtering.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10

Adds two denormalized, indexed columns to ``jobs`` rather than relying on the existing
``job_category_map`` join table.

Why denormalized: the feed query is single-table by design (see the index strategy in
``docs/01-architecture.md``). Joining a M:N table on every filtered page would undo that,
and a job's *primary* category is 1:1 by definition. ``job_category_map`` stays for
secondary categories, which is genuinely many-to-many.

The taxonomy itself is seeded here so the API can list categories without importing the
worker package.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


#: (slug, name, icon, sort order). Mirrors ingestion.services.classify.CATEGORIES.
#: Duplicated deliberately: a migration must be a frozen historical record and cannot
#: import application code that may change under it. `tests/test_taxonomy.py` asserts the
#: two stay in sync.
_CATEGORIES = [
    ("healthcare", "Healthcare & Nursing", "🩺", 10),
    ("education", "Education & Training", "🎓", 20),
    ("software-it", "Software & IT", "💻", 30),
    ("engineering", "Engineering & Technical", "⚙️", 40),
    ("science", "Science & Research", "🔬", 50),
    ("skilled-trades", "Skilled Trades & Maintenance", "🔧", 60),
    ("manufacturing", "Manufacturing & Production", "🏭", 70),
    ("construction", "Construction & Facilities", "🏗️", 80),
    ("transport", "Transportation & Logistics", "🚚", 90),
    ("food-hospitality", "Food Service & Hospitality", "🍽️", 100),
    ("retail-customer", "Retail & Customer Service", "🛍️", 110),
    ("sales", "Sales & Business Development", "📈", 120),
    ("marketing", "Marketing & Communications", "📣", 130),
    ("finance", "Finance & Accounting", "💰", 140),
    ("legal", "Legal & Compliance", "⚖️", 150),
    ("hr", "Human Resources", "👥", 160),
    ("social-services", "Social Services & Nonprofit", "🤝", 170),
    ("security", "Security & Protective Services", "🛡️", 180),
    ("admin", "Administrative & Office", "🗂️", 190),
    ("management", "Management & Operations", "📋", 200),
    ("other", "Other", "•", 999),
]


def upgrade() -> None:
    op.execute(
        "CREATE TYPE seniority_level AS ENUM ("
        "'INTERNSHIP', 'ENTRY', 'MID', 'SENIOR', 'LEAD', "
        "'MANAGER', 'DIRECTOR', 'EXECUTIVE', 'UNKNOWN')"
    )

    # Presentation metadata on the taxonomy so the frontend needs no lookup table.
    op.add_column("job_categories", sa.Column("icon", sa.Text(), nullable=True))
    op.add_column(
        "job_categories",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="500"),
    )

    op.add_column("jobs", sa.Column("category_slug", sa.Text(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column(
            "seniority_level",
            sa.Enum(name="seniority_level", create_type=False),
            nullable=False,
            server_default="UNKNOWN",
        ),
    )

    # Seed the taxonomy.
    #
    # Via the bound connection rather than op.execute(): op.execute() takes a single
    # statement and ignores a parameter list, which silently inserted nothing.
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
        [
            {"slug": slug, "name": name, "icon": icon, "sort_order": order}
            for slug, name, icon, order in _CATEGORIES
        ],
    )

    # ---- indexes -------------------------------------------------------------
    # Partial on ACTIVE for the same reason as the other feed indexes: roughly half the
    # table is closed, so the index that serves browsing is half the size.
    op.execute(
        "CREATE INDEX jobs_category_feed_idx ON jobs "
        "(category_slug, country_code, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )
    op.execute(
        "CREATE INDEX jobs_category_seen_idx ON jobs "
        "(category_slug, first_seen_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )
    op.execute(
        "CREATE INDEX jobs_seniority_idx ON jobs "
        "(seniority_level, country_code, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )
    # Category + state is the "nursing jobs in Texas" query, which is the whole point of
    # this feature.
    op.execute(
        "CREATE INDEX jobs_category_state_idx ON jobs "
        "(category_slug, state_code, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )

    op.create_foreign_key(
        "jobs_category_slug_fkey",
        "jobs",
        "job_categories",
        ["category_slug"],
        ["slug"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("jobs_category_slug_fkey", "jobs", type_="foreignkey")
    for index in (
        "jobs_category_feed_idx",
        "jobs_category_seen_idx",
        "jobs_seniority_idx",
        "jobs_category_state_idx",
    ):
        op.execute(f"DROP INDEX IF EXISTS {index}")

    op.drop_column("jobs", "seniority_level")
    op.drop_column("jobs", "category_slug")
    op.drop_column("job_categories", "sort_order")
    op.drop_column("job_categories", "icon")
    op.execute("DELETE FROM job_categories")
    op.execute("DROP TYPE IF EXISTS seniority_level")
