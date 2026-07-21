"""Execution-agent roster, per tenant. Replaces ``data/execution_agents/roster.json``.

The old implementation had a data-loss bug worth naming, because it is the reason
not to port the pattern: ``save()`` opened the roster with ``'w'`` — which
truncates on open — and only *then* tried to take ``flock`` (original
``roster.py:44-45``). On contention the file was already zeroed before the lock
was even contested, and the ``BlockingIOError`` retry then rewrote it from
in-memory state that may have been stale. It disappears with the file.

``AgentRoster`` is a stateless proxy: it resolves the tenant at call time, so
callers that captured it at import time keep working.
"""

from __future__ import annotations

from ...repositories.context import require_tenant, run_sync
from ...repositories.execution import AgentRosterRepository


class AgentRoster:
    """Tenant-scoped roster of execution agent names."""

    def load(self) -> None:
        """No-op. Kept because ``interaction_agent/tools.py:115`` calls it; the
        roster is read straight from Postgres now, so there is nothing to reload."""

    def get_agents(self) -> list[str]:
        tenant = require_tenant()

        async def _read(session) -> list[str]:
            return await AgentRosterRepository(session, tenant.user_id).get_agents()

        return run_sync(_read)

    def add_agent(self, agent_name: str) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            await AgentRosterRepository(session, tenant.user_id).add_agent(agent_name)

        run_sync(_write)

    def clear(self) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            await AgentRosterRepository(session, tenant.user_id).clear()

        run_sync(_write)


_agent_roster = AgentRoster()


def get_agent_roster() -> AgentRoster:
    return _agent_roster


__all__ = ["AgentRoster", "get_agent_roster"]
