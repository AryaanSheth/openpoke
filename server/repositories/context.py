"""Per-tenant request context, plus the sync->async bridge the legacy stores use.

Two things live here, both consequences of the same constraint:

**1. A tenant context.** Phase 1 replaces the module-level singletons with
repositories that take ``(session, user_id)``. But several call sites that use
those stores live in files owned by Phase 2 (``chat_handler.py``,
``trigger_scheduler.py``) or in the agent runtime, and *capture the store at
import time* (``agents/execution_agent/tools/triggers.py:93-94``). Those names
have to keep working. So the module-level ``get_x()`` accessors survive as
**proxies** that resolve the current tenant from a ``ContextVar`` at call time.

**2. A sync bridge.** The legacy store API is synchronous
(``log.record_user_message(...)``) and is called from inside ``async def``
functions in files this phase does not own. Making it async would require
editing them. Instead, ``run_sync`` hands the coroutine to a dedicated event
loop running on its own daemon thread with its own engine, and blocks the
calling thread for the result.

# ponytail: this bridge exists only because Phase 1 may not edit the agent
# runtime. Blocking the API event loop is no worse than the blocking file I/O it
# replaces, but it is still blocking. Phase 2 moves this work into the worker,
# where the caller is already async and the bridge can be deleted outright.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import get_settings

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Tenant context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantContext:
    """Everything the legacy proxies need to resolve a tenant without a DB hit.

    ``timezone`` is carried inline because ``utils/timezones.py`` asks for it on
    *every* log append; a round trip per append would be absurd.
    ``composio_user_id`` replaces the deleted ``_ACTIVE_USER_ID`` process global.
    """

    user_id: uuid.UUID
    timezone: str = "UTC"
    composio_user_id: str | None = None


_current: ContextVar[TenantContext | None] = ContextVar("openpoke_tenant", default=None)


def get_tenant() -> TenantContext | None:
    """Return the tenant bound to this context, if any."""
    return _current.get()


def require_tenant() -> TenantContext:
    """Return the bound tenant or raise. Used where an unscoped write would be a leak."""
    ctx = _current.get()
    if ctx is None:
        raise LookupError(
            "no tenant bound to this context; a per-user store was used outside a request"
        )
    return ctx


def set_tenant(ctx: TenantContext | None) -> Token:
    return _current.set(ctx)


def reset_tenant(token: Token) -> None:
    _current.reset(token)


def update_tenant(**changes: object) -> None:
    """Patch fields on the bound tenant in place (e.g. after a timezone change)."""
    ctx = _current.get()
    if ctx is not None:
        _current.set(replace(ctx, **changes))  # type: ignore[arg-type]


@contextmanager
def tenant_scope(ctx: TenantContext) -> Iterator[TenantContext]:
    """Bind *ctx* for the duration of the block. Used by the importance watcher
    to poll each connected user under that user's own identity."""
    token = set_tenant(ctx)
    try:
        yield ctx
    finally:
        reset_tenant(token)


# ---------------------------------------------------------------------------
# Sync bridge
# ---------------------------------------------------------------------------

BRIDGE_TIMEOUT_S = 30.0

_bridge_lock = threading.Lock()
_bridge_loop: asyncio.AbstractEventLoop | None = None
_bridge_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _ensure_bridge() -> asyncio.AbstractEventLoop:
    global _bridge_loop, _bridge_sessionmaker
    if _bridge_loop is not None:
        return _bridge_loop
    with _bridge_lock:
        if _bridge_loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="openpoke-db-bridge", daemon=True
            )
            thread.start()
            # A separate engine, not server.db.engine's: asyncpg connections are
            # bound to the loop that created them, so sharing a pool across two
            # loops corrupts it.
            engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
            _bridge_sessionmaker = async_sessionmaker(
                bind=engine, expire_on_commit=False, autoflush=False
            )
            _bridge_loop = loop
    return _bridge_loop


def run_sync(
    fn: Callable[[AsyncSession], Awaitable[T]], *, timeout: float = BRIDGE_TIMEOUT_S
) -> T:
    """Run *fn* against a fresh session on the bridge loop and block for the result.

    Commits on success, rolls back on error. The unit of work is one call, which
    matches the legacy stores' semantics (each append was its own ``write()``).
    """
    loop = _ensure_bridge()

    async def _wrapper() -> T:
        assert _bridge_sessionmaker is not None
        async with _bridge_sessionmaker() as session:
            try:
                result = await fn(session)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise

    future = asyncio.run_coroutine_threadsafe(_wrapper(), loop)
    return future.result(timeout)


__all__ = [
    "BRIDGE_TIMEOUT_S",
    "TenantContext",
    "get_tenant",
    "require_tenant",
    "reset_tenant",
    "run_sync",
    "set_tenant",
    "tenant_scope",
    "update_tenant",
]
