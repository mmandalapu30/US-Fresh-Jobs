"""Repo-root pytest configuration.

Lives at the root (not under tests/) so its fixtures are visible to every testpath:
``tests/``, ``apps/api/tests/`` and ``workers/ingestion/tests/``.

The environment bootstrap below runs at import time, before any test module is
collected. This matters because ``app.main`` builds the FastAPI app at module scope, so
settings must already be resolvable when pytest imports it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent

# Make the workspace packages importable without an editable install, so a bare
# `pytest` works in a fresh checkout.
for _pkg in ("packages/shared", "packages/schemas", "apps/api", "workers/ingestion", "scripts"):
    _path = str(REPO_ROOT / _pkg)
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Baseline configuration for import-time app construction. These are non-secret test
# values pointing at hosts that intentionally do not exist, so any test that accidentally
# performs real I/O fails loudly instead of touching a developer's real database.
_TEST_ENV_DEFAULTS = {
    "ENVIRONMENT": "test",
    "DEBUG": "false",
    "LOG_LEVEL": "WARNING",
    "LOG_FORMAT": "console",
    "DATABASE_URL": "postgresql+psycopg://testuser:testpass@127.0.0.1:1/testdb",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "CELERY_BROKER_URL": "memory://",
    "CELERY_RESULT_BACKEND": "cache+memory://",
    "OBJECT_STORAGE_URL": "http://127.0.0.1:1",
    "OBJECT_STORAGE_BUCKET": "test-bucket",
    "OBJECT_STORAGE_ACCESS_KEY": "test-access-key-not-a-real-credential",
    "OBJECT_STORAGE_SECRET_KEY": "test-secret-key-not-a-real-credential",
    "SECRET_KEY": "test-only-secret-key-0123456789abcdef0123456789abcdef",
    "JWT_SECRET": "test-only-jwt-secret-fedcba9876543210fedcba9876543210",
    "CORS_ALLOW_ORIGINS": "http://localhost:3000",
    "RATE_LIMIT_ENABLED": "false",
    "OPENJOBDATA_ENABLED": "false",
    "METRICS_ENABLED": "true",
}
for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


def _admin_dsn() -> str:
    """Connection string for creating and dropping throwaway test databases."""
    return os.environ.get(
        "TEST_ADMIN_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/postgres",
    )


def _postgres_available() -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(_admin_dsn(), connect_timeout=3):
            return True
    except Exception:
        return False


requires_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason="no PostgreSQL reachable (set TEST_ADMIN_DATABASE_URL or run `make up`)",
)


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """Create a uniquely named database, migrate it to head, yield its DSN, then drop it.

    Session-scoped: migrating the full schema costs about a second, and every test that
    needs it is read-only or cleans up after itself.
    """
    if not _postgres_available():
        pytest.skip("no PostgreSQL reachable")

    import psycopg

    db_name = f"jobplatform_test_{uuid.uuid4().hex[:12]}"
    admin = _admin_dsn()

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')

    target = admin.rsplit("/", 1)[0] + f"/{db_name}"
    sqlalchemy_url = target.replace("postgresql://", "postgresql+psycopg://", 1)

    try:
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(REPO_ROOT / "database" / "migrations" / "alembic.ini"),
                "-x",
                f"url={sqlalchemy_url}",
                "upgrade",
                "head",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=300,
        )
        if result.returncode != 0:
            pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")

        yield sqlalchemy_url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            # Terminate stragglers so DROP DATABASE cannot hang the suite.
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


@pytest.fixture
def db_connection(migrated_database: str) -> Iterator[object]:
    """A psycopg connection wrapped in a transaction that is always rolled back.

    Rolling back rather than truncating keeps tests independent without paying to
    re-migrate, and means a failing test leaves no residue for the next one.
    """
    import psycopg

    dsn = migrated_database.replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip inherited config so Settings tests start from a known-empty environment."""
    from jobplatform_shared.config import get_settings

    for key in list(os.environ):
        if key.split("_")[0] in {
            "DATABASE",
            "REDIS",
            "CELERY",
            "OBJECT",
            "SECRET",
            "JWT",
            "OPENJOBDATA",
            "INGEST",
            "RATE",
            "API",
            "CORS",
            "ENVIRONMENT",
        }:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def minimal_env(monkeypatch: pytest.MonkeyPatch, clean_settings_env: None) -> None:
    """The smallest environment that produces a valid Settings object."""
    # Port 1 is never bound. Pointing at an unreachable host is deliberate: a unit test
    # must never connect to the developer's real Postgres or Redis, and a test that
    # accidentally performs I/O should fail loudly rather than mutate live data.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://testuser:testpass@127.0.0.1:1/testdb")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("CELERY_BROKER_URL", "memory://")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "cache+memory://")
    monkeypatch.setenv("OBJECT_STORAGE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "test-bucket")
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", "a" * 40)
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "b" * 40)
    monkeypatch.setenv("SECRET_KEY", "s" * 64)
    monkeypatch.setenv("JWT_SECRET", "j" * 64)
