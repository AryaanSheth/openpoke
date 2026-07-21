"""The interaction agent's conversation transcript, per tenant.

Was a single append-only file bound at import time
(``ConversationLog(_CONVERSATION_LOG_PATH)`` at the old ``log.py:214``), which is
why two users shared one transcript and ``DELETE /chat/history`` wiped everyone's.

``ConversationLog`` is now a **stateless proxy**: it resolves the caller's tenant
from the context var at call time and delegates to
``ConversationRepository``/``WorkingMemoryRepository``. The class and the
``get_conversation_log()`` accessor keep their names and signatures because
``agents/interaction_agent/{runtime,tools}.py`` and ``routes`` call them and
belong to other phases.

Routes should prefer the repositories directly — they already hold the request's
session, so they avoid the sync bridge entirely.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...config import get_settings
from ...logging_config import logger
from ...models import ChatMessage
from ...repositories.context import require_tenant, run_sync
from ...repositories.conversation import ConversationRepository, WorkingMemoryRepository


class ConversationLog:
    """Tenant-scoped conversation transcript."""

    # -- writes ----------------------------------------------------------

    def _record(self, tag: str, payload: str) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            moment = await ConversationRepository(session, tenant.user_id).append(tag, payload)
            await WorkingMemoryRepository(session, tenant.user_id).append_entry(
                tag, payload, ts=moment
            )

        run_sync(_write)
        self._notify_summarization()

    def record_user_message(self, content: str) -> None:
        self._record("user_message", content)

    def record_agent_message(self, content: str) -> None:
        self._record("agent_message", content)

    def record_reply(self, content: str) -> None:
        self._record("poke_reply", content)

    def record_wait(self, reason: str) -> None:
        """A wait marker: orchestration metadata that must not reach the user."""
        self._record("wait", reason)

    # -- reads -----------------------------------------------------------

    def iter_entries(self) -> Iterator[tuple[str, str, str]]:
        tenant = require_tenant()

        async def _read(session):
            return await ConversationRepository(session, tenant.user_id).iter_entries(
                tenant.timezone
            )

        return iter(run_sync(_read))

    def load_transcript(self) -> str:
        tenant = require_tenant()

        async def _read(session) -> str:
            return await ConversationRepository(session, tenant.user_id).load_transcript(
                tenant.timezone
            )

        return run_sync(_read)

    def to_chat_messages(self) -> list[ChatMessage]:
        tenant = require_tenant()

        async def _read(session):
            return await ConversationRepository(session, tenant.user_id).to_chat_messages(
                tenant.timezone
            )

        return run_sync(_read)

    def clear(self) -> None:
        tenant = require_tenant()

        async def _write(session) -> None:
            await ConversationRepository(session, tenant.user_id).clear()
            await WorkingMemoryRepository(session, tenant.user_id).clear()

        run_sync(_write)

    # -- summarization hook ----------------------------------------------

    def _notify_summarization(self) -> None:
        settings = get_settings()
        if not settings.summarization_enabled:
            return
        try:
            from .summarization import schedule_summarization
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("summarization scheduler unavailable", extra={"error": str(exc)})
            return
        try:
            schedule_summarization()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("failed to schedule summarization", extra={"error": str(exc)})


_conversation_log = ConversationLog()


def get_conversation_log() -> ConversationLog:
    """The tenant-resolving proxy. Not a per-user instance — it must stay valid
    for callers that captured it at import time."""
    return _conversation_log


__all__ = ["ConversationLog", "get_conversation_log"]
