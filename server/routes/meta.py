from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..config import Settings, get_settings
from ..db.session import get_session
from ..models import (
    HealthResponse,
    RootResponse,
    SetTimezoneRequest,
    SetTimezoneResponse,
)
from ..services.timezone_store import get_timezone_async, set_timezone_async

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """The **only** unauthenticated route. Load balancers need it and it exposes
    nothing tenant-specific."""
    return HealthResponse(ok=True, service="openpoke", version=settings.app_version)


@router.get("/meta", response_model=RootResponse)
def meta(
    request: Request,
    user: CurrentUser,
    settings: Settings = Depends(get_settings),
) -> RootResponse:
    endpoints = sorted(
        {
            route.path
            for route in request.app.routes
            if getattr(route, "include_in_schema", False) and route.path.startswith("/api/")
        }
    )
    return RootResponse(
        status="ok",
        service="openpoke",
        version=settings.app_version,
        endpoints=endpoints,
    )


@router.post("/meta/timezone", response_model=SetTimezoneResponse)
async def set_timezone(
    payload: SetTimezoneRequest,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> SetTimezoneResponse:
    """Per-user preference, on ``users.timezone``. Was one global file."""
    try:
        timezone_name = await set_timezone_async(session, user.id, payload.timezone)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return SetTimezoneResponse(timezone=timezone_name)


@router.get("/meta/timezone", response_model=SetTimezoneResponse)
async def get_timezone(
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> SetTimezoneResponse:
    return SetTimezoneResponse(timezone=await get_timezone_async(session, user.id))


__all__ = ["router"]
