"""On-demand ingest requests, so an administrator can fetch without shell access.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-16

The API cannot start an ingest itself. It runs in a container with no Docker socket, and
giving it one would hand any request-handling bug the ability to start privileged
containers on the host -- a far worse trade than waiting a few seconds for a button.

So a request is a row. The API writes one; a short-interval timer on the host claims it
and runs the ingest exactly as the daily schedule does. The queue is the interface between
an unprivileged web tier and a privileged host job, and it is durable: a request survives
an API restart, and the runner crashing mid-job leaves a row that says so rather than a
silence.

`sync_run_id` is nullable and filled in once the run exists, so the UI can follow a request
through to the run it produced without guessing which run was "probably" the right one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
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
        # RESTRICT: the record of who asked for a fetch should not vanish because the
        # account was later deleted.
        sa.Column(
            "requested_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
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
        # Bounded on purpose. A run's memory grows with the number of files it processes --
        # measured hitting 2 GB and 3 GB ceilings exactly -- so the button asks for a
        # handful of files, not "everything outstanding".
        sa.Column("max_files", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    # The runner's only question is "is anything waiting", asked every minute; the console's
    # is "what happened recently". A partial index keeps the first one tiny however long the
    # history grows.
    op.create_index(
        "ingest_requests_queued_idx",
        "ingest_requests",
        ["created_at"],
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_index(
        "ingest_requests_created_idx", "ingest_requests", [sa.text("created_at DESC")]
    )


def downgrade() -> None:
    op.drop_index("ingest_requests_created_idx", table_name="ingest_requests")
    op.drop_index("ingest_requests_queued_idx", table_name="ingest_requests")
    op.drop_table("ingest_requests")
    op.execute("DROP TYPE ingest_request_status")
