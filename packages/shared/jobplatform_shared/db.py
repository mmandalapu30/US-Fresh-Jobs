"""Database engine and session management.

Both an async engine (API request path) and a sync engine (ingestion workers, Alembic) are
provided. They intentionally share nothing but the URL: the ingestion workload uses large
transactions with ``COPY``, and mixing it into the request-path pool would let a slow bulk
load starve user searches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker[Session] | None = None


def _asyncify(dsn: str) -> str:
    """Return the asyncpg form of a psycopg DSN."""
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql+psycopg://"):
        return dsn.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return dsn


def _syncify(dsn: str) -> str:
    """Return the psycopg (sync) form of a DSN."""
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_timeout": settings.database_pool_timeout,
        # Recycle below typical proxy/idle timeouts so we never hand out a dead connection.
        "pool_recycle": 1800,
        "pool_pre_ping": True,
        "echo": False,
    }


# --------------------------------------------------------------------------- async


def get_async_engine(settings: Settings | None = None) -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        settings = settings or get_settings()
        _async_engine = create_async_engine(
            _asyncify(str(settings.database_url)),
            **_engine_kwargs(settings),
            connect_args={
                "server_settings": {
                    # A runaway query cannot pin a connection forever.
                    "statement_timeout": str(settings.database_statement_timeout_ms),
                    "application_name": "jobplatform-api",
                }
            },
        )
    return _async_engine


def get_async_session_factory(
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_async_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_session_factory


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency.

    Read endpoints do not need an explicit commit, so this yields the session and only
    guarantees rollback and close. Endpoints that write use ``async_session_scope``.
    """
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database_health(settings: Settings | None = None) -> bool:
    """Cheap liveness probe used by ``/ready``."""
    engine = get_async_engine(settings)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return result.scalar_one() == 1


async def dispose_async_engine() -> None:
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
    _async_engine = None
    _async_session_factory = None


# ---------------------------------------------------------------------------- sync


def get_sync_engine(settings: Settings | None = None) -> Engine:
    global _sync_engine
    if _sync_engine is None:
        settings = settings or get_settings()
        _sync_engine = create_engine(
            _syncify(str(settings.database_url)),
            **_engine_kwargs(settings),
            connect_args={"application_name": "jobplatform-worker"},
        )

        @event.listens_for(_sync_engine, "connect")
        def _set_worker_timeouts(dbapi_conn: Any, _record: Any) -> None:
            # Ingestion transactions are long by design; a request-path timeout would
            # abort legitimate bulk loads.
            with dbapi_conn.cursor() as cur:
                cur.execute("SET statement_timeout = 0")
                cur.execute("SET idle_in_transaction_session_timeout = '10min'")

    return _sync_engine


def get_sync_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _sync_session_factory
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(
            bind=get_sync_engine(settings), expire_on_commit=False, autoflush=False
        )
    return _sync_session_factory


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_sync_engine() -> None:
    global _sync_engine, _sync_session_factory
    if _sync_engine is not None:
        _sync_engine.dispose()
    _sync_engine = None
    _sync_session_factory = None


def reset_engines() -> None:
    """Drop cached engines. Used by tests that swap the database URL."""
    global _async_engine, _async_session_factory
    dispose_sync_engine()
    _async_engine = None
    _async_session_factory = None
