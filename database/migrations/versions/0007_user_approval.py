"""Approval-based access control: user status, approval provenance, admin audit log.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-14

The site stays public; the job data does not. Access becomes something an administrator
grants rather than something registration confers, so `users` needs a state machine
alongside the role it already had, and every administrative decision needs to be
attributable after the fact.

Two columns already here do adjacent jobs and are deliberately left alone:

  `is_active`  operational kill switch, unrelated to approval. A suspended account and a
               deactivated one are different statements, and collapsing them would make
               "why can this person not log in" unanswerable.
  `role`       what a user may do once approved. Status is whether they may do anything
               at all. Keeping them separate is what lets an admin be suspended without
               ceasing to be an admin.

Existing rows become APPROVED rather than PENDING. There are none in practice, but a
migration that silently locks out whoever is already there would be the wrong default for
anyone restoring an older dump into this schema.
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
    op.execute("CREATE TYPE user_status AS ENUM ('PENDING', 'APPROVED', 'REJECTED', 'SUSPENDED')")

    op.add_column(
        "users",
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING",
                "APPROVED",
                "REJECTED",
                "SUSPENDED",
                name="user_status",
                create_type=False,
            ),
            nullable=False,
            server_default="PENDING",
        ),
    )
    op.add_column("users", sa.Column("phone", sa.Text(), nullable=True))

    # Who decided, and when. Nullable because most rows never reach these states, and
    # SET NULL on the actor so removing an administrator never deletes the history of what
    # they approved -- the decision outlives the account that made it.
    for verb in ("approved", "rejected"):
        op.add_column("users", sa.Column(f"{verb}_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("users", sa.Column(f"{verb}_by", sa.BigInteger(), nullable=True))
        op.create_foreign_key(
            f"users_{verb}_by_fkey", "users", "users", [f"{verb}_by"], ["id"], ondelete="SET NULL"
        )
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))

    # Every request re-reads the actor's status, so this index is on the hottest path in
    # the application. Role is indexed for the admin console's filters.
    op.create_index("users_status_idx", "users", ["status"])
    op.create_index("users_role_idx", "users", ["role"])
    # The console's default view is "pending, oldest first", which this serves directly.
    op.create_index("users_status_created_idx", "users", ["status", "created_at"])

    # Anyone already in the table predates approval existing and should not be locked out
    # by its introduction.
    op.execute("UPDATE users SET status = 'APPROVED' WHERE status = 'PENDING'")
    op.alter_column("users", "status", server_default="PENDING")

    op.execute(
        "CREATE TYPE admin_action AS ENUM ("
        "'ADMIN_APPROVED_USER', 'ADMIN_REJECTED_USER', 'ADMIN_SUSPENDED_USER', "
        "'ADMIN_REACTIVATED_USER', 'ADMIN_CHANGED_ROLE')"
    )

    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        # RESTRICT, not CASCADE: an audit row that can be erased by deleting the account it
        # describes is not an audit trail. Deleting an admin who has acted must fail loudly.
        sa.Column(
            "admin_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "action",
            postgresql.ENUM(
                "ADMIN_APPROVED_USER",
                "ADMIN_REJECTED_USER",
                "ADMIN_SUSPENDED_USER",
                "ADMIN_REACTIVATED_USER",
                "ADMIN_CHANGED_ROLE",
                name="admin_action",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("previous_status", sa.Text(), nullable=True),
        sa.Column("new_status", sa.Text(), nullable=True),
        # INET rather than text: it validates on write, so a malformed value cannot be
        # stored and later mislead an investigation.
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The dashboard reads "most recent first"; the per-user index answers "what happened to
    # this account" on the user detail page.
    op.create_index("admin_audit_log_created_idx", "admin_audit_log", [sa.text("created_at DESC")])
    op.create_index("admin_audit_log_target_idx", "admin_audit_log", ["target_user_id"])


def downgrade() -> None:
    op.drop_table("admin_audit_log")
    op.execute("DROP TYPE admin_action")

    op.drop_index("users_status_created_idx", table_name="users")
    op.drop_index("users_role_idx", table_name="users")
    op.drop_index("users_status_idx", table_name="users")

    for verb in ("approved", "rejected"):
        op.drop_constraint(f"users_{verb}_by_fkey", "users", type_="foreignkey")
        op.drop_column("users", f"{verb}_by")
        op.drop_column("users", f"{verb}_at")
    op.drop_column("users", "suspended_at")
    op.drop_column("users", "phone")
    op.drop_column("users", "status")
    op.execute("DROP TYPE user_status")
