"""Job kind -> coroutine.

Every handler runs with the job's tenant already bound (``worker.py``
``_run_handler``), so it may use the repository proxies directly.

**Handlers signal failure by raising.** Both agent runtimes swallow every
exception and return a ``success=False`` result object, which is exactly the
behaviour that made ``chat_handler.py:47``'s detached task lose work silently.
Converting that back into an exception is what connects the queue's retry to the
real failure.
"""

from __future__ import annotations

from ..db.models import Job
from ..logging_config import logger
from . import KIND_CHAT_TURN, KIND_TRIGGER_FIRE


class JobHandlerError(RuntimeError):
    """A handler failed in a way that should be retried."""


async def handle_chat_turn(job: Job) -> None:
    """Run one interaction-agent turn.

    Replaces ``asyncio.create_task(_run_interaction())``. The difference that
    matters is not the retry — it is that the row exists before the 202 is
    returned, so a deploy mid-turn resumes instead of dropping.
    """
    # TODO(phase-1-integration): the interaction runtime reaches Postgres through
    # Phase 1's sync bridge (repositories/context.run_sync). That blocks this
    # worker's event loop for the duration of each log write. See phase-2-notes
    # "New issues" #1 — the fix is making agents/interaction_agent async.
    from ..agents.interaction_agent.runtime import InteractionAgentRuntime

    message = (job.payload or {}).get("message", "")
    if not str(message).strip():
        # Nothing to retry into. Raising would burn five attempts on a payload
        # that will never become valid.
        logger.warning("chat_turn job with empty message", extra={"job_id": str(job.id)})
        return

    result = await InteractionAgentRuntime().execute(user_message=str(message))
    if not result.success:
        raise JobHandlerError(result.error or "interaction agent returned no response")


async def handle_trigger_fire(job: Job) -> None:
    """Fire one trigger occurrence and schedule the next."""
    from ..services.trigger_scheduler import execute_trigger_occurrence

    payload = job.payload or {}
    trigger_id = payload.get("trigger_id")
    if trigger_id is None:
        raise JobHandlerError("trigger_fire job has no trigger_id")
    await execute_trigger_occurrence(int(trigger_id), occurrence=payload.get("occurrence"))


def default_handlers() -> dict[str, object]:
    """The registry the worker runs with.

    ``email_poll`` is deliberately absent. See docs/phase-2-notes.md "New issues":
    ``ImportantEmailWatcher`` keeps ``_seeded`` / ``_last_poll`` per tenant *in
    process memory*, so turning a poll into a job would let two workers each
    perform their own warmup and re-classify the same inbox. Making it a job
    needs that state in Postgres, and the watcher is Phase 1's file. Until then
    the watcher runs as a single-replica loop inside the worker process.
    """
    return {
        KIND_CHAT_TURN: handle_chat_turn,
        KIND_TRIGGER_FIRE: handle_trigger_fire,
    }


__all__ = [
    "JobHandlerError",
    "default_handlers",
    "handle_chat_turn",
    "handle_trigger_fire",
]
