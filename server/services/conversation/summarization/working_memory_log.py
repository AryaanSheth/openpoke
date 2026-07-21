"""Working memory — the summary header plus the unsummarised tail, per tenant.

Was ``data/conversation/poke_working_memory.log``: one global file whose first
two lines were a ``<summary_info>`` JSON blob and a ``<conversation_summary>``,
followed by the tail entries. The header is now one ``summary_state`` row and the
tail is ``working_memory_entries``; both carry ``user_id``.

``WorkingMemoryLog`` keeps its method names because
``agents/interaction_agent/runtime.py:56`` and ``summarizer.py:79`` call it and
belong to other phases.
"""

from __future__ import annotations

from html import escape

from ....repositories.context import require_tenant, run_sync
from ....repositories.conversation import WorkingMemoryRepository
from .state import SummaryState


def render_state(state: SummaryState) -> str:
    """Render a state to the exact transcript shape the old file produced."""
    parts: list[str] = []
    summary_text = (state.summary_text or "").strip()
    if summary_text:
        parts.append(
            f"<conversation_summary>{escape(summary_text, quote=False)}</conversation_summary>"
        )
    for entry in state.unsummarized_entries:
        safe_payload = escape(entry.payload, quote=False)
        if entry.timestamp:
            parts.append(
                f'<{entry.tag} timestamp="{entry.timestamp}">{safe_payload}</{entry.tag}>'
            )
        else:
            parts.append(f"<{entry.tag}>{safe_payload}</{entry.tag}>")
    return "\n".join(parts)


class WorkingMemoryLog:
    """Tenant-scoped proxy over ``WorkingMemoryRepository``."""

    def append_entry(self, tag: str, payload: str, timestamp: str | None = None) -> None:
        """*timestamp* is accepted for signature compatibility and ignored.

        The caller only ever has the *rendered* local-time string; the row keeps
        a real ``timestamptz`` and the read path re-renders it through the user's
        zone. ``ConversationLog._record`` passes the authoritative moment
        directly to the repository, so nothing is lost.
        """
        tenant = require_tenant()

        async def _write(session) -> None:
            await WorkingMemoryRepository(session, tenant.user_id).append_entry(tag, payload)

        run_sync(_write)

    def load_summary_state(self) -> SummaryState:
        tenant = require_tenant()

        async def _read(session) -> SummaryState:
            return await WorkingMemoryRepository(session, tenant.user_id).load_summary_state(
                tenant.timezone
            )

        return run_sync(_read)

    def write_summary_state(self, state: SummaryState) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            await WorkingMemoryRepository(session, tenant.user_id).write_summary_state(
                state, tenant.timezone
            )

        run_sync(_write)

    def render_transcript(self, state: SummaryState | None = None) -> str:
        return render_state(state or self.load_summary_state())

    def clear(self) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            await WorkingMemoryRepository(session, tenant.user_id).clear()

        run_sync(_write)


_working_memory_log = WorkingMemoryLog()


def get_working_memory_log() -> WorkingMemoryLog:
    return _working_memory_log


__all__ = ["WorkingMemoryLog", "get_working_memory_log", "render_state"]
