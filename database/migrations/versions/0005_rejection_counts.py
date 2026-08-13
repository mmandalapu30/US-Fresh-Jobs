"""Store rejection counts instead of one row per rejected record.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-10

The requirement is that no record is silently discarded and every rejection has a stored
reason. Storing 1,235,585 individually identical ``CATEGORY_NOT_ALLOWED`` rows satisfies
that requirement no better than storing the number 1,235,585 alongside a handful of
worked examples — and it reached 2.5M rows / 629 MB in a single day.

A narrow ingest scope rejects far more than it keeps, so this only gets worse: the audit
table would dwarf the jobs table by an order of magnitude.

New shape: up to N detailed examples per (run, reason) with full payloads for debugging,
plus one counted row carrying the remainder. Totals stay exact; storage becomes constant
per run rather than linear in rows rejected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_errors",
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
    )
    # Counting by reason is the dashboard's hot query and now has to sum a column.
    op.execute(
        "CREATE INDEX sync_errors_reason_count_idx ON sync_errors "
        "(sync_run_id, reason) INCLUDE (occurrence_count)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS sync_errors_reason_count_idx")
    op.drop_column("sync_errors", "occurrence_count")
