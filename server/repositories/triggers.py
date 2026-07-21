"""Triggers, scoped to one tenant. Replaces the global ``data/triggers.db``.

The SQLite table stored ``start_time`` / ``next_trigger`` as ISO-8601 ``...Z``
strings; the Postgres columns are ``timestamptz``. ``TriggerRecord``
(``services/triggers/models.py``) still declares them as ``str``, and the
frontend and the agent tool schemas read that shape — so this module converts on
both edges via ``to_storage_timestamp``/``parse_iso`` and the API response is
byte-identical to before.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Trigger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..services.triggers.models import TriggerRecord

#: Fields the service layer hands us as ISO strings but the DB stores as timestamptz.
_DATETIME_FIELDS = {"start_time", "next_trigger", "created_at", "updated_at"}


def _trigger_helpers():
    """Lazy import: ``server/services/__init__.py`` pulls in this module, so a
    module-level import of the services tree would close the cycle."""
    from ..services.triggers.models import TriggerRecord
    from ..services.triggers.utils import parse_iso, to_storage_timestamp, utc_now

    return TriggerRecord, parse_iso, to_storage_timestamp, utc_now


def _coerce_in(field: str, value: Any) -> Any:
    if field in _DATETIME_FIELDS and isinstance(value, str):
        _, parse_iso, _, _ = _trigger_helpers()
        return parse_iso(value)
    return value


def to_record(row: Trigger) -> TriggerRecord:
    """Render a row back into the string-timestamp shape callers expect."""
    TriggerRecord, _, to_storage_timestamp, _ = _trigger_helpers()
    return TriggerRecord(
        id=row.id,
        agent_name=row.agent_name,
        payload=row.payload,
        start_time=to_storage_timestamp(row.start_time) if row.start_time else None,
        next_trigger=to_storage_timestamp(row.next_trigger) if row.next_trigger else None,
        recurrence_rule=row.recurrence_rule,
        timezone=row.timezone,
        status=row.status,
        last_error=row.last_error,
        created_at=to_storage_timestamp(row.created_at),
        updated_at=to_storage_timestamp(row.updated_at),
    )


class TriggerRepository:
    """All reads and writes carry ``WHERE user_id = :user_id``.

    A cross-tenant id therefore returns ``None``, which the route turns into a
    404 — never a 403, which would confirm the row exists.
    """

    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self._session = session
        self._user_id = user_id

    async def insert(self, payload: dict[str, Any]) -> int:
        values = {key: _coerce_in(key, value) for key, value in payload.items()}
        values["user_id"] = self._user_id
        row = Trigger(**values)
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def _get(self, trigger_id: int, agent_name: str | None = None) -> Trigger | None:
        stmt = select(Trigger).where(
            Trigger.id == trigger_id, Trigger.user_id == self._user_id
        )
        if agent_name is not None:
            stmt = stmt.where(Trigger.agent_name == agent_name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def fetch_one(
        self, trigger_id: int, agent_name: str | None = None
    ) -> TriggerRecord | None:
        row = await self._get(trigger_id, agent_name)
        return to_record(row) if row else None

    async def update(
        self, trigger_id: int, agent_name: str | None, fields: dict[str, Any]
    ) -> bool:
        if not fields:
            return False
        row = await self._get(trigger_id, agent_name)
        if row is None:
            return False
        for key, value in fields.items():
            setattr(row, key, _coerce_in(key, value))
        _, _, _, utc_now = _trigger_helpers()
        row.updated_at = utc_now()
        await self._session.flush()
        return True

    async def list_for_agent(self, agent_name: str) -> list[TriggerRecord]:
        result = await self._session.execute(
            select(Trigger)
            .where(Trigger.user_id == self._user_id, Trigger.agent_name == agent_name)
            .order_by(Trigger.next_trigger.is_(None), Trigger.next_trigger)
        )
        return [to_record(row) for row in result.scalars().all()]

    async def list_all(self) -> list[TriggerRecord]:
        result = await self._session.execute(
            select(Trigger)
            .where(Trigger.user_id == self._user_id)
            .order_by(Trigger.next_trigger.is_(None), Trigger.next_trigger)
        )
        return [to_record(row) for row in result.scalars().all()]

    async def fetch_due(
        self, before: datetime, agent_name: str | None = None
    ) -> list[TriggerRecord]:
        stmt = select(Trigger).where(
            Trigger.user_id == self._user_id,
            Trigger.status == "active",
            Trigger.next_trigger.is_not(None),
            Trigger.next_trigger <= before,
        )
        if agent_name:
            stmt = stmt.where(Trigger.agent_name == agent_name)
        result = await self._session.execute(stmt.order_by(Trigger.next_trigger, Trigger.id))
        return [to_record(row) for row in result.scalars().all()]

    async def clear_all(self) -> None:
        await self._session.execute(delete(Trigger).where(Trigger.user_id == self._user_id))


# ---------------------------------------------------------------------------
# Cross-tenant helpers — for the scheduler, never for a request
# ---------------------------------------------------------------------------


async def fetch_due_all(
    session: AsyncSession, before: datetime, agent_name: str | None = None
) -> list[TriggerRecord]:
    """Every tenant's due triggers.

    The background poller legitimately has no tenant: it services all of them.
    This is the only unscoped read in the module and it is not reachable from any
    HTTP route.
    """
    stmt = select(Trigger).where(
        Trigger.status == "active",
        Trigger.next_trigger.is_not(None),
        Trigger.next_trigger <= before,
    )
    if agent_name:
        stmt = stmt.where(Trigger.agent_name == agent_name)
    result = await session.execute(stmt.order_by(Trigger.next_trigger, Trigger.id))
    return [to_record(row) for row in result.scalars().all()]


async def owner_of(session: AsyncSession, trigger_id: int) -> uuid.UUID | None:
    """Resolve a trigger id to its tenant.

    ``TriggerRecord`` has no ``user_id`` field and lives in a file this phase does
    not own, so the scheduler — which holds only a record — gets the owner back
    this way before doing any scoped write.
    """
    result = await session.execute(select(Trigger.user_id).where(Trigger.id == trigger_id))
    return result.scalar_one_or_none()


__all__ = ["TriggerRepository", "fetch_due_all", "owner_of", "to_record"]
