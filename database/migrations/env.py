"""Alembic environment.

The database URL always comes from settings (i.e. the environment), never from
``alembic.ini``. That keeps credentials out of tracked files and makes the same migration
command work identically in dev, CI and production.

``autogenerate`` is deliberately not wired up: the spec requires migrations to be authored
and reviewed rather than derived from live schema drift.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the workspace packages importable when alembic runs from the repo root.
ROOT = Path(__file__).resolve().parents[2]
for pkg in ("packages/shared", "packages/schemas"):
    candidate = str(ROOT / pkg)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Resolve the target DSN.

    Precedence: explicit ``-x url=...`` (used by tests against a temp database), then
    ``DATABASE_URL`` from the environment via Settings.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "url" in x_args:
        return x_args["url"]

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError(
            "DATABASE_URL is not set. Source an env file (e.g. .env.development) or pass "
            "-x url=postgresql+psycopg://..."
        )
    # Alembic runs synchronously; coerce an async DSN if one was supplied.
    return raw.replace("postgresql+asyncpg://", "postgresql+psycopg://")


# No declarative metadata: migrations are hand-authored, so autogenerate is off.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. Used for review and for --sql deploys."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # Every migration runs inside one transaction: a failure leaves no partial schema.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
