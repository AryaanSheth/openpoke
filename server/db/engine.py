"""Async engine and sessionmaker singletons.

Both are built lazily on first use so that importing this module never opens a
connection — that keeps `alembic`, the tests, and the API process free to
configure settings before anything touches the database.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
            # Sized deliberately, not by default. The default 5+10 caps the API at
            # 14 concurrent requests; the 15th waits the full pool timeout and the
            # endpoint goes from ~15ms to ~30s. Measured — docs/LOADTEST.md.
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # Fail fast instead of queueing for 30s: a request that cannot get a
            # connection in 10s should surface as an error, not a hung client.
            pool_timeout=settings.db_pool_timeout_s,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async sessionmaker."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Close all pooled connections and reset the singletons."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


__all__ = ["get_engine", "get_sessionmaker", "dispose_engine"]
