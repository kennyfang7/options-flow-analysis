from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from loguru import logger
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.storage.models import Base


def _adapt_url(url: str) -> str:
    """Add the async driver prefix to a SQLAlchemy URL if not already present.

    SQLite requires the 'aiosqlite' driver; PostgreSQL requires 'asyncpg'.
    Leaves already-adapted URLs unchanged.

    Args:
        url: A standard SQLAlchemy database URL string.

    Returns:
        URL with the appropriate async driver prefix inserted.
    """
    if url.startswith("sqlite") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def make_engine(database_url: str | None = None) -> AsyncEngine:
    """Create an async SQLAlchemy engine from the given URL or settings.

    Args:
        database_url: Optional explicit URL. If None, reads from settings.

    Returns:
        A configured AsyncEngine instance.
    """
    if database_url is None:
        from config.settings import settings
        database_url = settings.database_url

    url = _adapt_url(database_url)
    logger.debug("Creating async engine: {}", url)
    return create_async_engine(url, echo=False)


def _strip_async_prefix(url: str) -> str:
    """Remove async driver prefix from a SQLAlchemy URL string.

    Inverse of _adapt_url(). Used to build a synchronous engine URL
    from the same database_url setting used by the async engine.

    Args:
        url: A SQLAlchemy URL, possibly with an async driver prefix.

    Returns:
        URL with any async driver prefix removed. Unrecognised driver
        prefixes are passed through unchanged.
    """
    return (
        url.replace("sqlite+aiosqlite://", "sqlite://", 1)
           .replace("postgresql+asyncpg://", "postgresql://", 1)
    )


def make_sync_engine(database_url: str | None = None) -> Engine:
    """Create a synchronous SQLAlchemy engine from the given URL or settings.

    Used by Dash callbacks (Flask/sync context) to query the same database
    as the async engine without conflicting connection pool settings.
    SQLAlchemy models are engine-agnostic and work identically with both.

    Args:
        database_url: Optional explicit URL. If None, reads from settings.

    Returns:
        A configured synchronous Engine instance.
    """
    if database_url is None:
        from config.settings import settings
        database_url = settings.database_url

    url = _strip_async_prefix(_adapt_url(database_url))
    logger.debug("Creating sync engine: {}", url)
    return create_engine(url, echo=False)


_sync_engine_lock = threading.Lock()
_sync_engine: Engine | None = None

_async_engine_lock = threading.Lock()


def get_sync_engine() -> Engine:
    """Return the module-level synchronous engine singleton.

    Created lazily on first call. Thread-safe via double-checked locking —
    safe to call concurrently from Flask/Dash worker threads.
    Used exclusively by Dash callbacks for read-only DB queries.

    Returns:
        The shared synchronous Engine instance.
    """
    global _sync_engine
    if _sync_engine is None:
        with _sync_engine_lock:
            if _sync_engine is None:
                _sync_engine = make_sync_engine()
    return _sync_engine


# Module-level singletons — created lazily on first use so that
# importing this module never touches the database or settings at test time.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        with _async_engine_lock:
            if _engine is None:
                _engine = make_engine()
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        with _async_engine_lock:
            if _session_factory is None:
                _session_factory = async_sessionmaker(_get_engine(), expire_on_commit=False)
    return _session_factory


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Create all tables defined in models.py if they do not already exist.

    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS semantics.

    Args:
        engine: Optional engine to use. Defaults to the module-level engine
            (reads DATABASE_URL from settings). Pass a test engine in tests.
    """
    e = engine or _get_engine()
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if str(e.url).startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            logger.debug("init_db: SQLite WAL mode enabled")
    logger.info("init_db: all tables created/verified")


@asynccontextmanager
async def get_session(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a database session.

    Commits on clean exit, rolls back on exception.

    Args:
        session_factory: Optional factory to use. Defaults to the module-level
            factory. Pass a test factory in tests.

    Yields:
        An AsyncSession ready for use.

    Example:
        async with get_session() as session:
            await insert_tick(session, tick)
    """
    factory = session_factory or _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
