"""Trigger persistence. Postgres, tenant-scoped, behind the old sync interface.

Was ``data/triggers.db`` — one global SQLite file with no ``user_id`` column at
all, which is why ``DELETE /chat/history`` from any browser destroyed every
trigger in the system.

``TriggerService`` (``service.py``) and ``trigger_scheduler.py`` call this class
synchronously, and both are outside this phase's edit scope for the scheduler, so
the shape is preserved exactly: same method names, same ISO-8601 string
timestamps in and out. The work happens on the sync bridge.

**Scoping rule.** Writes that create rows *require* a tenant. Reads and updates
that address a row by its server-generated id fall back to resolving the owner
from the row itself when no tenant is bound — that is the background poller,
which services all tenants and never sees user input. Anything reachable from an
HTTP route always has a tenant bound by ``get_current_user``.
"""

from __future__ import annotations

from typing import Any

from ...logging_config import logger
from ...repositories.context import TenantContext, get_tenant, require_tenant, run_sync
from ...repositories.triggers import TriggerRepository, fetch_due_all, owner_of
from ...repositories.users import UserRepository
from .models import TriggerRecord
from .utils import parse_iso


class TriggerStore:
    """Tenant-resolving proxy. Stateless; safe to capture at import time."""

    def __init__(self, db_path: Any = None) -> None:
        """*db_path* is accepted and ignored.

        ``services/triggers/__init__.py:12`` still constructs
        ``TriggerStore(_default_db_path)`` and that file is outside this phase's
        edit scope, so the argument survives as a no-op rather than the module
        failing to import. See docs/phase-1-notes.md "New issues".
        """
        if db_path is not None:
            logger.debug("TriggerStore path argument ignored; storage is Postgres now")

    # -- scoped writes ---------------------------------------------------

    def insert(self, payload: dict[str, Any]) -> int:
        tenant = require_tenant()

        async def _write(session) -> int:
            return await TriggerRepository(session, tenant.user_id).insert(payload)

        return run_sync(_write)

    def list_for_agent(self, agent_name: str) -> list[TriggerRecord]:
        tenant = require_tenant()

        async def _read(session):
            return await TriggerRepository(session, tenant.user_id).list_for_agent(agent_name)

        return run_sync(_read)

    def clear_all(self) -> None:
        """Clears **this tenant's** triggers only. The original wiped the table."""
        tenant = require_tenant()

        async def _write(session) -> None:
            await TriggerRepository(session, tenant.user_id).clear_all()

        run_sync(_write)

    # -- id-addressed access ---------------------------------------------

    def fetch_one(self, trigger_id: int, agent_name: str) -> TriggerRecord | None:
        async def _read(session):
            user_id = await self._resolve_owner(session, trigger_id)
            if user_id is None:
                return None
            return await TriggerRepository(session, user_id).fetch_one(trigger_id, agent_name)

        return run_sync(_read)

    def update(self, trigger_id: int, agent_name: str, fields: dict[str, Any]) -> bool:
        if not fields:
            return False

        async def _write(session) -> bool:
            user_id = await self._resolve_owner(session, trigger_id)
            if user_id is None:
                return False
            return await TriggerRepository(session, user_id).update(
                trigger_id, agent_name, fields
            )

        return run_sync(_write)

    # -- due-trigger polling ---------------------------------------------

    def fetch_due(self, agent_name: str | None, before_iso: str) -> list[TriggerRecord]:
        before = parse_iso(before_iso)
        tenant = get_tenant()

        async def _read(session):
            if tenant is not None:
                return await TriggerRepository(session, tenant.user_id).fetch_due(
                    before, agent_name
                )
            # Background poller: all tenants by design. Not route-reachable.
            return await fetch_due_all(session, before, agent_name)

        return run_sync(_read)

    def tenant_for(self, trigger_id: int) -> TenantContext | None:
        """Resolve a trigger to a full tenant context.

        The background scheduler holds only a ``TriggerRecord``, which has no
        ``user_id`` field (``models.py`` is outside this phase). This is how a
        fired trigger's side effects — agent logs, roster entries, the reply
        written back into the conversation — land in the owning tenant instead
        of failing closed.
        """

        async def _read(session) -> TenantContext | None:
            user_id = await owner_of(session, trigger_id)
            if user_id is None:
                return None
            timezone_name = await UserRepository(session, user_id).get_timezone()
            return TenantContext(
                user_id=user_id,
                timezone=timezone_name,
                composio_user_id=str(user_id),
            )

        return run_sync(_read)

    # -- internals -------------------------------------------------------

    @staticmethod
    async def _resolve_owner(session, trigger_id: int):
        """Return the tenant to scope this operation to, or ``None`` to refuse.

        With a tenant bound, a foreign id resolves to ``None`` — the caller turns
        that into a 404 rather than a 403, so existence is not leaked.
        """
        tenant = get_tenant()
        if tenant is not None:
            return tenant.user_id
        owner = await owner_of(session, trigger_id)
        if owner is None:
            return None
        # TODO(phase-2): the trigger scheduler should carry the owning user_id on
        # the job it enqueues instead of relying on this lookup. TriggerRecord has
        # no user_id field and services/triggers/models.py is outside phase 1.
        logger.debug(
            "trigger accessed without a bound tenant; resolved from the row",
            extra={"trigger_id": trigger_id, "user_id": str(owner)},
        )
        return owner


__all__ = ["TriggerStore"]
