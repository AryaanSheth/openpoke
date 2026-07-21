"""Accept a chat turn by making it durable, not by detaching it.

**What was wrong** (original ``:47``)::

    asyncio.create_task(_run_interaction())
    return PlainTextResponse("", status_code=202)

Four problems in two lines. No reference was retained, so the task was
garbage-collectable mid-flight. Nothing was persisted, so a deploy dropped every
in-flight turn — and the client had already been told 202, so it waited forever
for a reply that no longer existed. There was no retry, so a transient
OpenRouter 429 was permanent data loss. And there was no concurrency bound, so a
burst spawned unbounded concurrent LLM calls inside the API process.

The replacement writes a row and returns its id. The 202 now means "durably
accepted", which is the only thing a 202 should ever mean.
"""

from __future__ import annotations

from fastapi import status
from fastapi.responses import JSONResponse, PlainTextResponse

from ...logging_config import logger
from ...models import ChatMessage, ChatRequest
from ...utils import error_response


def _extract_latest_user_message(payload: ChatRequest) -> ChatMessage | None:
    for message in reversed(payload.messages):
        if message.role.lower().strip() == "user" and message.content.strip():
            return message
    return None


async def handle_chat_request(payload: ChatRequest) -> PlainTextResponse | JSONResponse:
    """Enqueue one interaction-agent turn and return 202 with its job id."""

    user_message = _extract_latest_user_message(payload)
    if user_message is None:
        return error_response("Missing user message", status_code=status.HTTP_400_BAD_REQUEST)

    user_content = user_message.content.strip()

    # TODO(phase-1-integration): the tenant is resolved from the ContextVar that
    # `get_current_user` binds, rather than taken as an argument, so
    # `routes/chat.py` (Phase 1's file) needs no signature change. If routes ever
    # pass the user explicitly, take it as a parameter and delete this.
    from ...repositories.context import require_tenant

    try:
        tenant = require_tenant()
    except LookupError:
        logger.error("chat request with no bound tenant")
        return error_response(
            "Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED
        )

    from ...db.engine import get_sessionmaker
    from ...jobs import KIND_CHAT_TURN, enqueue

    logger.info(
        "chat request",
        extra={"message_length": len(user_content), "user_id": str(tenant.user_id)},
    )

    async with get_sessionmaker()() as session:
        job = await enqueue(
            session,
            user_id=tenant.user_id,
            kind=KIND_CHAT_TURN,
            payload={"message": user_content},
        )
        await session.commit()

    if job is None:  # pragma: no cover - only reachable if a dedupe_key is added
        return error_response(
            "Duplicate chat turn", status_code=status.HTTP_409_CONFLICT
        )

    # The frontend proxy (web/app/api/chat/route.ts) forwards the body verbatim
    # and does not parse it, so adding the job id is backwards-compatible; the
    # status code it keys off is unchanged.
    return JSONResponse({"job_id": str(job.id)}, status_code=status.HTTP_202_ACCEPTED)


__all__ = ["handle_chat_request"]
