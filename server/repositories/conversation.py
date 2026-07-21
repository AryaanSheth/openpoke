"""Conversation log and working memory, scoped to one tenant.

Replaces ``data/conversation/poke_conversation.log`` and
``data/conversation/poke_working_memory.log``, both of which were single global
files bound at import time.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationEntry, WorkingMemoryEntry
from ..db.models import SummaryState as SummaryStateRow
from ..models import ChatMessage
from .formatting import parse_timestamp, render_entry, render_timestamp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..services.conversation.summarization.state import LogEntry, SummaryState


def _state_types() -> tuple[type, type]:
    """Import the summary dataclasses lazily.

    ``server/services/__init__.py`` eagerly imports the whole service tree, which
    imports this module; a module-level import here would close the cycle.
    """
    from ..services.conversation.summarization.state import LogEntry, SummaryState

    return LogEntry, SummaryState

#: Tags that never surface to the user (orchestration metadata).
_HIDDEN_TAGS = {"wait"}

_SEQ_RETRIES = 3


class ConversationRepository:
    """Append-only conversation transcript for one user."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    # -- writes ----------------------------------------------------------

    async def append(self, tag: str, payload: str, *, ts: datetime | None = None) -> datetime:
        """Append an entry and return its timestamp.

        ``seq`` is allocated inside the INSERT so two concurrent appends for the
        same user cannot read the same max. The unique ``(user_id, seq)`` is the
        backstop; on collision we retry rather than lose the entry.
        """
        moment = ts or datetime.now(timezone.utc)
        for attempt in range(_SEQ_RETRIES):
            try:
                async with self._session.begin_nested():
                    await self._session.execute(
                        insert(ConversationEntry).from_select(
                            ["user_id", "seq", "tag", "payload", "ts"],
                            select(
                                literal(self._user_id),
                                func.coalesce(func.max(ConversationEntry.seq), -1) + 1,
                                literal(tag),
                                literal(str(payload)),
                                literal(moment),
                            ).where(ConversationEntry.user_id == self._user_id),
                        )
                    )
                return moment
            except IntegrityError:
                if attempt == _SEQ_RETRIES - 1:
                    raise
        raise AssertionError("unreachable")

    async def clear(self) -> None:
        await self._session.execute(
            delete(ConversationEntry).where(ConversationEntry.user_id == self._user_id)
        )

    # -- reads -----------------------------------------------------------

    async def iter_entries(self, timezone_name: str) -> list[tuple[str, str, str]]:
        """``(tag, rendered_timestamp, payload)`` in insertion order."""
        result = await self._session.execute(
            select(ConversationEntry.tag, ConversationEntry.ts, ConversationEntry.payload)
            .where(ConversationEntry.user_id == self._user_id)
            .order_by(ConversationEntry.seq)
        )
        return [
            (tag, render_timestamp(ts, timezone_name), payload)
            for tag, ts, payload in result.all()
        ]

    async def load_transcript(self, timezone_name: str) -> str:
        entries = await self.iter_entries(timezone_name)
        return "\n".join(render_entry(tag, ts, payload) for tag, ts, payload in entries)

    async def to_chat_messages(self, timezone_name: str) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for tag, ts, payload in await self.iter_entries(timezone_name):
            if tag in _HIDDEN_TAGS:
                continue
            if tag == "user_message":
                messages.append(ChatMessage(role="user", content=payload, timestamp=ts or None))
            elif tag == "poke_reply":
                messages.append(
                    ChatMessage(role="assistant", content=payload, timestamp=ts or None)
                )
        return messages

    async def count(self) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(ConversationEntry)
            .where(ConversationEntry.user_id == self._user_id)
        )
        return int(result.scalar_one())


class WorkingMemoryRepository:
    """Summary header + unsummarised tail for one user.

    The old file kept both in one document: a ``<summary_info>`` line, a
    ``<conversation_summary>`` line, then the tail entries. Here the header is a
    single ``summary_state`` row and the tail is ``working_memory_entries``.
    """

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def append_entry(
        self, tag: str, payload: str, *, ts: datetime | None = None
    ) -> None:
        moment = ts or datetime.now(timezone.utc)
        for attempt in range(_SEQ_RETRIES):
            try:
                async with self._session.begin_nested():
                    await self._session.execute(
                        insert(WorkingMemoryEntry).from_select(
                            ["user_id", "seq", "tag", "payload", "ts"],
                            select(
                                literal(self._user_id),
                                func.coalesce(func.max(WorkingMemoryEntry.seq), -1) + 1,
                                literal(tag),
                                literal(str(payload)),
                                literal(moment),
                            ).where(WorkingMemoryEntry.user_id == self._user_id),
                        )
                    )
                return
            except IntegrityError:
                if attempt == _SEQ_RETRIES - 1:
                    raise

    async def _load_header(self) -> SummaryStateRow | None:
        result = await self._session.execute(
            select(SummaryStateRow).where(SummaryStateRow.user_id == self._user_id)
        )
        return result.scalar_one_or_none()

    async def load_summary_state(self, timezone_name: str) -> SummaryState:
        LogEntry, SummaryState = _state_types()
        header = await self._load_header()
        rows = await self._session.execute(
            select(WorkingMemoryEntry.tag, WorkingMemoryEntry.ts, WorkingMemoryEntry.payload)
            .where(WorkingMemoryEntry.user_id == self._user_id)
            .order_by(WorkingMemoryEntry.seq)
        )
        entries = [
            LogEntry(tag=tag, payload=payload, timestamp=render_timestamp(ts, timezone_name) or None)
            for tag, ts, payload in rows.all()
        ]
        if header is None:
            return SummaryState(unsummarized_entries=entries)
        return SummaryState(
            summary_text=header.summary_text or "",
            last_index=header.last_index,
            updated_at=header.updated_at,
            unsummarized_entries=entries,
        )

    async def write_summary_state(self, state: SummaryState, timezone_name: str) -> None:
        """Replace the header and the whole tail. Mirrors the old atomic rewrite."""
        header = await self._load_header()
        if header is None:
            header = SummaryStateRow(user_id=self._user_id)
            self._session.add(header)
        header.summary_text = state.summary_text or ""
        header.last_index = state.last_index
        header.updated_at = state.updated_at

        await self._session.execute(
            delete(WorkingMemoryEntry).where(WorkingMemoryEntry.user_id == self._user_id)
        )
        await self._session.flush()
        await self._replace_entries(state.unsummarized_entries, timezone_name)

    async def _replace_entries(
        self, entries: Sequence[LogEntry], timezone_name: str
    ) -> None:
        """Re-materialise the tail.

        ``LogEntry.timestamp`` is the *rendered* local-time string, because that
        is all the summarizer ever sees. Parsing it back through the same zone is
        an exact round trip at second precision — writing ``now()`` instead would
        silently restamp the whole tail with the summarization time and change
        every subsequent prompt.
        """
        if not entries:
            return
        self._session.add_all(
            [
                WorkingMemoryEntry(
                    user_id=self._user_id,
                    seq=index,
                    tag=entry.tag,
                    payload=entry.payload,
                    ts=parse_timestamp(entry.timestamp, timezone_name),
                )
                for index, entry in enumerate(entries)
            ]
        )
        await self._session.flush()

    async def clear(self) -> None:
        await self._session.execute(
            delete(WorkingMemoryEntry).where(WorkingMemoryEntry.user_id == self._user_id)
        )
        await self._session.execute(
            delete(SummaryStateRow).where(SummaryStateRow.user_id == self._user_id)
        )


__all__ = ["ConversationRepository", "WorkingMemoryRepository"]
