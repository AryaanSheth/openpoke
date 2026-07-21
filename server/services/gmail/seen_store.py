"""Ledger of Gmail message ids already handled, per tenant.

Was ``data/gmail_seen.json`` — a single bounded deque shared by every user, so
one tenant's poll could mark another tenant's message id as handled.

The only consumer is the importance watcher, which is async and owned by this
phase, so this is a thin async wrapper over ``GmailSeenRepository`` rather than a
sync bridge proxy. The class name is kept because
``services/gmail/__init__.py`` and ``services/__init__.py`` re-export it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from ...repositories.gmail import CLASSIFY_RETRY_BUDGET, GmailSeenRepository


class GmailSeenStore:
    """Tenant-scoped seen ledger.

    ``classified=False`` records an *attempt*; only ``classified=True`` is a
    verdict. That distinction is what stops a transient OpenRouter failure from
    dropping an email permanently — see ``importance_watcher.py``.
    """

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._repo = GmailSeenRepository(session, user_id)

    async def unprocessed(self, message_ids: Iterable[str]) -> list[str]:
        return await self._repo.unprocessed(message_ids)

    async def mark_classified(self, message_ids: Iterable[str]) -> int:
        """Record a verdict. These ids are done."""
        return await self._repo.mark(message_ids, classified=True)

    async def mark_attempted(self, message_ids: Iterable[str]) -> int:
        """Record an attempt. Eligible for retry until the budget expires."""
        return await self._repo.mark(message_ids, classified=False)

    async def expire_stale(self) -> list[str]:
        """Settle attempts older than the retry budget so they stop retrying."""
        return await self._repo.expire_stale()

    async def is_seen(self, message_id: str) -> bool:
        return await self._repo.is_seen(message_id)

    async def clear(self) -> None:
        await self._repo.clear()


__all__ = ["CLASSIFY_RETRY_BUDGET", "GmailSeenStore"]
