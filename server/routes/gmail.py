from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..config import Settings, get_settings
from ..db.session import get_session
from ..models import GmailConnectPayload, GmailDisconnectPayload, GmailStatusPayload
from ..services import disconnect_account, fetch_status, initiate_connect

router = APIRouter(prefix="/gmail", tags=["gmail"])


@router.post("/connect")
async def gmail_connect(
    payload: GmailConnectPayload,
    user: CurrentUser,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Start the Composio OAuth flow for the caller.

    ``payload.user_id`` is ignored — the Composio identity is derived from the
    authenticated user. See ``services/gmail/client.py``.
    """
    return await initiate_connect(payload, settings, session, user.id)


@router.post("/status")
async def gmail_status(
    payload: GmailStatusPayload,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Report and persist the caller's connection state. The original overwrote a
    process global here (``client.py:306``), which is how B's connection
    redirected A's inbox polling."""
    return await fetch_status(payload, session, user.id)


@router.post("/disconnect")
async def gmail_disconnect(
    payload: GmailDisconnectPayload,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    return await disconnect_account(payload, session, user.id)


__all__ = ["router"]
