"""Execution-agent roster and per-agent journals, scoped to one tenant.

Replaces ``data/execution_agents/roster.json`` and
``data/execution_agents/<slug>.log``.

Two bugs disappear with the files:

* ``roster.py:44-58`` opened the roster with ``'w'`` (which truncates) *before*
  taking ``flock``, so on contention the file was already zeroed by the time the
  lock was contested. There is no file to truncate now.
* ``log_store.py:19-24`` slugified agent names onto filenames, so two agents
  whose names slugify identically shared one journal. ``agent_log_entries`` keys
  on the real name.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, insert, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Agent, AgentLogEntry
from .formatting import render_entry, render_timestamp

_SEQ_RETRIES = 3


class AgentRosterRepository:
    """The set of execution agents this user has created."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def get_agents(self) -> list[str]:
        result = await self._session.execute(
            select(Agent.name).where(Agent.user_id == self._user_id).order_by(Agent.id)
        )
        return list(result.scalars().all())

    async def add_agent(self, name: str) -> bool:
        """Insert if absent. Returns True when the agent is new to this user."""
        stmt = (
            pg_insert(Agent)
            .values(user_id=self._user_id, name=name)
            .on_conflict_do_nothing(constraint="uq_agents_user_name")
            .returning(Agent.id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def clear(self) -> None:
        await self._session.execute(delete(Agent).where(Agent.user_id == self._user_id))


class AgentLogRepository:
    """Append-only journal per execution agent, per tenant."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def append(
        self, agent_name: str, tag: str, payload: str, *, ts: datetime | None = None
    ) -> None:
        moment = ts or datetime.now(timezone.utc)
        for attempt in range(_SEQ_RETRIES):
            try:
                async with self._session.begin_nested():
                    await self._session.execute(
                        insert(AgentLogEntry).from_select(
                            ["user_id", "agent_name", "seq", "tag", "payload", "ts"],
                            select(
                                literal(self._user_id),
                                literal(agent_name),
                                func.coalesce(func.max(AgentLogEntry.seq), -1) + 1,
                                literal(tag),
                                literal(str(payload)),
                                literal(moment),
                            ).where(
                                AgentLogEntry.user_id == self._user_id,
                                AgentLogEntry.agent_name == agent_name,
                            ),
                        )
                    )
                return
            except IntegrityError:
                if attempt == _SEQ_RETRIES - 1:
                    raise

    async def iter_entries(
        self, agent_name: str, timezone_name: str
    ) -> list[tuple[str, str, str]]:
        result = await self._session.execute(
            select(AgentLogEntry.tag, AgentLogEntry.ts, AgentLogEntry.payload)
            .where(
                AgentLogEntry.user_id == self._user_id,
                AgentLogEntry.agent_name == agent_name,
            )
            .order_by(AgentLogEntry.seq)
        )
        return [
            (tag, render_timestamp(ts, timezone_name), payload)
            for tag, ts, payload in result.all()
        ]

    async def load_transcript(self, agent_name: str, timezone_name: str) -> str:
        entries = await self.iter_entries(agent_name, timezone_name)
        return "\n".join(render_entry(tag, ts, payload) for tag, ts, payload in entries)

    async def load_recent(
        self, agent_name: str, timezone_name: str, limit: int = 10
    ) -> list[tuple[str, str, str]]:
        entries = await self.iter_entries(agent_name, timezone_name)
        return entries[-limit:] if entries else []

    async def list_agents(self) -> list[str]:
        result = await self._session.execute(
            select(AgentLogEntry.agent_name)
            .where(AgentLogEntry.user_id == self._user_id)
            .distinct()
            .order_by(AgentLogEntry.agent_name)
        )
        return list(result.scalars().all())

    async def clear_all(self) -> None:
        await self._session.execute(
            delete(AgentLogEntry).where(AgentLogEntry.user_id == self._user_id)
        )


__all__ = ["AgentLogRepository", "AgentRosterRepository"]
