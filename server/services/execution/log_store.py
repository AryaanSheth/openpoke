"""Per-agent execution journals, per tenant.

Replaces ``data/execution_agents/<slug>.log``. Two bugs go away with the files:

* ``_slugify`` (original ``log_store.py:19-24``) mapped agent names onto
  filenames, so "Email: Summary" and "Email Summary" shared one journal. The
  ``(user_id, agent_name, seq)`` key uses the real name.
* ``list_agents()`` returned *slugs*, not agent names, so the value it produced
  could not be fed back into any other method on the class.

``ExecutionAgentLogStore`` is a stateless proxy resolving the tenant at call
time, because ``agents/execution_agent/tools/{gmail,triggers}.py`` and
``tasks/search_email/tool.py`` capture it at import time.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...repositories.context import require_tenant, run_sync
from ...repositories.execution import AgentLogRepository


class ExecutionAgentLogStore:
    """Append-only journal per execution agent, scoped to the caller's tenant."""

    def _append(self, agent_name: str, tag: str, payload: str) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            await AgentLogRepository(session, tenant.user_id).append(agent_name, tag, payload)

        run_sync(_write)

    def record_request(self, agent_name: str, instructions: str) -> None:
        self._append(agent_name, "agent_request", instructions)

    def record_action(self, agent_name: str, description: str) -> None:
        self._append(agent_name, "agent_action", description)

    def record_tool_response(self, agent_name: str, tool_name: str, response: str) -> None:
        self._append(agent_name, "tool_response", f"{tool_name}: {response}")

    def record_agent_response(self, agent_name: str, response: str) -> None:
        self._append(agent_name, "agent_response", response)

    def iter_entries(self, agent_name: str) -> Iterator[tuple[str, str, str]]:
        tenant = require_tenant()

        async def _read(session):
            return await AgentLogRepository(session, tenant.user_id).iter_entries(
                agent_name, tenant.timezone
            )

        return iter(run_sync(_read))

    def load_transcript(self, agent_name: str) -> str:
        tenant = require_tenant()

        async def _read(session) -> str:
            return await AgentLogRepository(session, tenant.user_id).load_transcript(
                agent_name, tenant.timezone
            )

        return run_sync(_read)

    def load_recent(self, agent_name: str, limit: int = 10) -> list[tuple[str, str, str]]:
        tenant = require_tenant()

        async def _read(session):
            return await AgentLogRepository(session, tenant.user_id).load_recent(
                agent_name, tenant.timezone, limit
            )

        return run_sync(_read)

    def list_agents(self) -> list[str]:
        tenant = require_tenant()

        async def _read(session) -> list[str]:
            return await AgentLogRepository(session, tenant.user_id).list_agents()

        return run_sync(_read)

    def clear_all(self) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            await AgentLogRepository(session, tenant.user_id).clear_all()

        run_sync(_write)


_execution_agent_logs = ExecutionAgentLogStore()


def get_execution_agent_logs() -> ExecutionAgentLogStore:
    return _execution_agent_logs


__all__ = ["ExecutionAgentLogStore", "get_execution_agent_logs"]
