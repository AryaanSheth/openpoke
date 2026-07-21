"""Gmail connection state and the seen-message ledger, scoped to one tenant.

``gmail_connections`` is what replaces the ``_ACTIVE_USER_ID`` process global
(``services/gmail/client.py:23-40``). That global was written unconditionally by
any caller of ``/gmail/status`` (``client.py:306``), so user B connecting Gmail
redirected user A's inbox polling into A's transcript. The Composio identity now
hangs off the authenticated user's row and nothing else.

``gmail_seen`` replaces the bounded JSON deque in ``data/gmail_seen.json``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import GmailConnection, GmailSeen
from ..logging_config import logger
from . import crypto

#: How long an unclassified message stays eligible for another attempt.
#: Replaces the "attempt counter" the plan asked for — see docs/phase-1-notes.md.
CLASSIFY_RETRY_BUDGET = timedelta(hours=1)

ACTIVE_STATUSES = {"CONNECTED", "SUCCESS", "SUCCESSFUL", "ACTIVE", "COMPLETED"}


class GmailConnectionRepository:
    """One tenant's Composio Gmail binding."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def get(self) -> GmailConnection | None:
        result = await self._session.execute(
            select(GmailConnection)
            .where(GmailConnection.user_id == self._user_id)
            .order_by(GmailConnection.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def composio_user_id(self) -> str:
        """The Composio identity for this tenant.

        Derived from ``users.id``, never from the request body. A client-supplied
        value would let A claim an identity B has not connected yet and inherit
        B's mailbox once B completes OAuth.
        """
        return str(self._user_id)

    async def upsert(
        self,
        *,
        status: str,
        email: str | None = None,
        connection_id: str | None = None,
    ) -> GmailConnection:
        row = await self.get()
        if row is None:
            row = GmailConnection(
                user_id=self._user_id,
                composio_user_id=await self.composio_user_id(),
            )
            self._session.add(row)
        row.status = status
        if email:
            row.email = email
        if connection_id:
            self._store_connection_id(row, connection_id)
        await self._session.flush()
        return row

    def _store_connection_id(self, row: GmailConnection, connection_id: str) -> None:
        try:
            ciphertext, key_version = crypto.encrypt(connection_id)
        except crypto.DataKeyMissing:
            # Fail closed on the credential, open on the feature: the connection
            # id is only needed to revoke, and Composio can list it back.
            logger.warning(
                "OPENPOKE_DATA_KEY unset; storing Gmail connection without the "
                "connection id (revocation will fall back to a Composio lookup)",
                extra={"user_id": str(self._user_id)},
            )
            row.connection_id_encrypted = None
            return
        row.connection_id_encrypted = ciphertext
        row.key_version = key_version

    async def connection_id(self) -> str | None:
        row = await self.get()
        if row is None or not row.connection_id_encrypted:
            return None
        return crypto.decrypt(row.connection_id_encrypted, row.key_version)

    async def is_connected(self) -> bool:
        row = await self.get()
        return bool(row and (row.status or "").upper() in ACTIVE_STATUSES)

    async def clear(self) -> None:
        await self._session.execute(
            delete(GmailConnection).where(GmailConnection.user_id == self._user_id)
        )


async def list_connected(session: AsyncSession) -> list[tuple[uuid.UUID, str, str]]:
    """``(user_id, composio_user_id, timezone)`` for every actively connected tenant.

    The importance watcher iterates this instead of reading one process global.
    Not reachable from any HTTP route.
    """
    from ..db.models import User

    result = await session.execute(
        select(GmailConnection.user_id, GmailConnection.composio_user_id, User.timezone)
        .join(User, User.id == GmailConnection.user_id)
        .where(func.upper(GmailConnection.status).in_(ACTIVE_STATUSES))
    )
    return [(row[0], row[1], row[2] or "UTC") for row in result.all()]


class GmailSeenRepository:
    """Per-tenant ledger of processed message ids.

    ``classified`` is the fix for the permanent-drop bug: the old watcher marked
    every id seen whether or not the classifier succeeded
    (``importance_watcher.py:210`` against ``importance_classifier.py:102-113``),
    so one transient OpenRouter blip dropped that email forever. A row with
    ``classified=False`` is an *attempt*, not a verdict, and stays eligible for
    retry until ``CLASSIFY_RETRY_BUDGET`` runs out.
    """

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def unprocessed(self, message_ids: Iterable[str]) -> list[str]:
        """Ids with no verdict yet: never seen, or attempted and still in budget."""
        ids = [mid for mid in (str(m).strip() for m in message_ids) if mid]
        if not ids:
            return []
        result = await self._session.execute(
            select(GmailSeen.message_id, GmailSeen.classified, GmailSeen.seen_at).where(
                GmailSeen.user_id == self._user_id, GmailSeen.message_id.in_(ids)
            )
        )
        cutoff = datetime.now(timezone.utc) - CLASSIFY_RETRY_BUDGET
        known: dict[str, bool] = {}
        for message_id, classified, seen_at in result.all():
            if seen_at is not None and seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=timezone.utc)
            # Settled if we have a verdict, or if the retry budget expired.
            known[message_id] = bool(classified) or (seen_at is not None and seen_at < cutoff)
        return [mid for mid in ids if not known.get(mid, False)]

    async def mark(self, message_ids: Iterable[str], *, classified: bool) -> int:
        """Upsert rows. ``classified=True`` is a verdict; ``False`` records an attempt.

        An attempt never downgrades an existing verdict, and never refreshes
        ``seen_at`` — the retry budget must start at the *first* attempt or a
        poison message would retry forever.
        """
        ids = sorted({mid for mid in (str(m).strip() for m in message_ids) if mid})
        if not ids:
            return 0
        stmt = pg_insert(GmailSeen).values(
            [
                {"user_id": self._user_id, "message_id": mid, "classified": classified}
                for mid in ids
            ]
        )
        if classified:
            stmt = stmt.on_conflict_do_update(
                constraint="uq_gmail_seen_user_message", set_={"classified": True}
            )
        else:
            stmt = stmt.on_conflict_do_nothing(constraint="uq_gmail_seen_user_message")
        await self._session.execute(stmt)
        return len(ids)

    async def expire_stale(self) -> list[str]:
        """Settle attempts that outlived the retry budget, so they stop retrying."""
        cutoff = datetime.now(timezone.utc) - CLASSIFY_RETRY_BUDGET
        result = await self._session.execute(
            update(GmailSeen)
            .where(
                GmailSeen.user_id == self._user_id,
                GmailSeen.classified.is_(False),
                GmailSeen.seen_at < cutoff,
            )
            .values(classified=True)
            .returning(GmailSeen.message_id)
        )
        return list(result.scalars().all())

    async def is_seen(self, message_id: str) -> bool:
        result = await self._session.execute(
            select(GmailSeen.classified).where(
                GmailSeen.user_id == self._user_id,
                GmailSeen.message_id == str(message_id).strip(),
            )
        )
        return bool(result.scalar_one_or_none())

    async def clear(self) -> None:
        await self._session.execute(
            delete(GmailSeen).where(GmailSeen.user_id == self._user_id)
        )


__all__ = [
    "ACTIVE_STATUSES",
    "CLASSIFY_RETRY_BUDGET",
    "GmailConnectionRepository",
    "GmailSeenRepository",
    "list_connected",
]
