from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..db.session import get_session
from ..models import ChatHistoryClearResponse, ChatHistoryResponse, ChatRequest
from ..repositories.conversation import ConversationRepository, WorkingMemoryRepository
from ..repositories.execution import AgentLogRepository, AgentRosterRepository
from ..repositories.triggers import TriggerRepository
from ..services import handle_chat_request

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "/send",
    response_class=JSONResponse,
    summary="Submit a chat message and receive a completion",
)
async def chat_send(payload: ChatRequest, user: CurrentUser) -> JSONResponse:
    """The tenant is bound by ``get_current_user`` before this runs, so the
    detached interaction task inherits it through the context var."""
    return await handle_chat_request(payload)


@router.get("/history", response_model=ChatHistoryResponse)
async def chat_history(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> ChatHistoryResponse:
    """This user's transcript. Reads through the request session rather than the
    legacy proxy, so it never touches the sync bridge."""
    repo = ConversationRepository(session, user.id)
    return ChatHistoryResponse(messages=await repo.to_chat_messages(user.timezone or "UTC"))


@router.delete("/history", response_model=ChatHistoryClearResponse)
async def clear_history(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> ChatHistoryClearResponse:
    """Clear **this caller's** conversation, working memory, agents, agent logs
    and triggers — in one transaction.

    The original (``routes/chat.py:26-45``) was unauthenticated and wiped the
    global conversation log, the roster, every execution log and **every trigger
    in the system**, in four independent non-transactional steps.
    """
    await ConversationRepository(session, user.id).clear()
    await WorkingMemoryRepository(session, user.id).clear()
    await AgentLogRepository(session, user.id).clear_all()
    await AgentRosterRepository(session, user.id).clear()
    await TriggerRepository(session, user.id).clear_all()
    return ChatHistoryClearResponse()


__all__ = ["router"]
