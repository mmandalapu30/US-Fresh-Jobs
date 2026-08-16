"""On-demand ingest requests, so a fetch can be triggered from the UI.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-16

The API cannot start an ingest itself. It runs in a container with no Docker socket, and
giving it one would hand any request-handling bug the ability to start privileged
containers on the host -- a far worse trade than waiting a few seconds for a button.

So a request is a row. The API writes one; a short-interval timer on the host claims it and
runs the ingest exactly as the daily schedule does. The queue is the interface between an
unprivileged web tier and a privileged host job, and it is durable: a request survives an
API restart, and a runner that dies mid-job leaves a row saying so rather than a silence.

`requested_by` is nullable and free text, not a foreign key. There is no authentication in
this deployment, so there is no user to point at -- recording the caller's address is the
most that can honestly be said about who asked.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE ingest_request_status AS ENUM "
        "('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')"
    )

    op.create_table(
        "ingest_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "status",
            # postgresql.ENUM, not sa.Enum: the create_type flag is dialect-specific, and
            # sa.Enum emits CREATE TYPE a second time here and fails as a duplicate.
            postgresql.ENUM(
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "SKIPPED",
                name="ingest_request_status",
                create_type=False,
            ),
            nullable=False,
            server_default="QUEUED",
        ),
        # Bounded deliberately. A run's memory grows with the number of files it processes
        # -- measured hitting 2 GB and 3 GB ceilings exactly -- so the button asks for a
        # handful of files, never "everything outstanding".
        sa.Column("max_files", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("requested_by", sa.Text(), nullable=True),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    # The runner asks "is anything waiting" every minute; a partial index keeps that query
    # tiny however long the history grows. The second serves the console's "what happened
    # recently".
    op.create_index(
        "ingest_requests_queued_idx",
        "ingest_requests",
        ["created_at"],
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_index("ingest_requests_created_idx", "ingest_requests", [sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("ingest_requests_created_idx", table_name="ingest_requests")
    op.drop_index("ingest_requests_queued_idx", table_name="ingest_requests")
    op.drop_table("ingest_requests")
    op.execute("DROP TYPE ingest_request_status")
