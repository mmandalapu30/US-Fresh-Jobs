"""Denormalized industry on jobs, for filtering without a company join.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-10

Industry is a **second axis** to role category, not a substitute: a Registered Nurse at a
hospital and a Registered Nurse at a school share a category but not an industry, and users
filter on both.

The value comes from the source's own company registry, which is 90% populated — far better
coverage than anything inferable from a job title. It was already being read from the
source and then discarded before this change.

Denormalized onto ``jobs`` for the same reason as ``category_slug``: the feed query is
single-table, and joining companies on every filtered page would undo that.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("industry", sa.Text(), nullable=True))

    # Partial on ACTIVE, matching the other feed indexes: about half the table is closed,
    # so the index that actually serves browsing is half the size.
    op.execute(
        "CREATE INDEX jobs_industry_feed_idx ON jobs "
        "(industry, country_code, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )
    # The common combined query: "healthcare roles in the hospital industry".
    op.execute(
        "CREATE INDEX jobs_industry_category_idx ON jobs "
        "(industry, category_slug, posted_at DESC, id DESC) "
        "WHERE status = 'ACTIVE'"
    )
    op.create_index("companies_industry_idx", "companies", ["industry"])


def downgrade() -> None:
    op.drop_index("companies_industry_idx", table_name="companies")
    op.execute("DROP INDEX IF EXISTS jobs_industry_category_idx")
    op.execute("DROP INDEX IF EXISTS jobs_industry_feed_idx")
    op.drop_column("jobs", "industry")
