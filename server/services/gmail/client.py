"""Composio Gmail client, bound to the authenticated tenant.

**What was wrong.** The Composio identity lived in a process global,
``_ACTIVE_USER_ID`` (original ``client.py:23-40``), defaulted to the PID
(``user_id = payload.user_id or f"web-{os.getpid()}"``, original ``:215``), and
was overwritten *unconditionally* by any caller of ``/gmail/status`` (original
``:306``). So user B connecting Gmail redirected user A's inbox polling into A's
transcript, and any second process silently believed Gmail was disconnected.

**What replaces it.** ``gmail_connections`` keyed on ``users.id``. The Composio
identifier is derived from the authenticated user and never read from the request
body — a client-supplied value would let A claim an identity B has not connected
yet and inherit B's mailbox once B completes OAuth.

**Timeouts.** Every Composio call is blocking I/O made from an async server. The
original had *no timeout at all* on the two hottest ones (original
``:481-494``), so one hung upstream call froze the event loop for every tenant,
including ``/health``. They now run in a worker thread under an explicit
deadline.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, TypeVar

from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import Settings, get_settings
from ...logging_config import logger
from ...models import GmailConnectPayload, GmailDisconnectPayload, GmailStatusPayload
from ...repositories.context import get_tenant
from ...repositories.gmail import ACTIVE_STATUSES, GmailConnectionRepository
from ...utils import error_response

T = TypeVar("T")

#: Deadline for any single Composio call. Env-overridable because a slow mailbox
#: is a support ticket, not a code change.
COMPOSIO_TIMEOUT_S = float(os.getenv("OPENPOKE_COMPOSIO_TIMEOUT_S", "30"))

_CLIENT_LOCK = threading.Lock()
_CLIENT: Any | None = None

#: Bounded pool for the sync call path. Bounded on purpose: an unbounded pool
#: turns a hung upstream into unbounded thread growth.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="composio")

_PROFILE_CACHE: dict[str, dict[str, Any]] = {}
_PROFILE_CACHE_LOCK = threading.Lock()


def _normalized(value: str | None) -> str:
    return (value or "").strip()


def get_active_gmail_user_id() -> str | None:
    """The Composio identity of the tenant bound to this context.

    Replaces the ``_ACTIVE_USER_ID`` process global. Callers in
    ``agents/execution_agent/**`` capture nothing — they call this per tool
    invocation — so resolving from the context var is a drop-in.
    """
    tenant = get_tenant()
    return tenant.composio_user_id if tenant else None


def _gmail_import_client():
    from composio import Composio  # type: ignore

    return Composio


def _get_composio_client(settings: Settings | None = None):
    """Process-wide Composio client. One platform key, never per-user."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    with _CLIENT_LOCK:
        if _CLIENT is None:
            resolved_settings = settings or get_settings()
            Composio = _gmail_import_client()
            api_key = resolved_settings.composio_api_key
            try:
                _CLIENT = Composio(api_key=api_key) if api_key else Composio()
            except TypeError as exc:
                if api_key:
                    raise RuntimeError(
                        "Installed Composio SDK does not accept the api_key argument; "
                        "upgrade the SDK or remove COMPOSIO_API_KEY."
                    ) from exc
                _CLIENT = Composio()
    return _CLIENT


# ---------------------------------------------------------------------------
# Blocking-call isolation
# ---------------------------------------------------------------------------


async def _call(fn: Callable[[], T], *, what: str, timeout: float = COMPOSIO_TIMEOUT_S) -> T:
    """Run a blocking Composio call off the event loop, under a deadline."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
    except asyncio.TimeoutError as exc:
        # The worker thread is still blocked on the socket; we stop waiting for
        # it. The pool is bounded, so a persistent upstream hang degrades this
        # feature rather than the process.
        raise RuntimeError(f"{what} timed out after {timeout:.0f}s") from exc


def _call_sync(fn: Callable[[], T], *, what: str, timeout: float = COMPOSIO_TIMEOUT_S) -> T:
    """Same deadline for the synchronous call path used by the agent tools."""
    future = _EXECUTOR.submit(fn)
    try:
        return future.result(timeout=timeout)
    except TimeoutError as exc:
        future.cancel()
        raise RuntimeError(f"{what} timed out after {timeout:.0f}s") from exc


# ---------------------------------------------------------------------------
# Response shape helpers
# ---------------------------------------------------------------------------


def _list_payload(items: Any) -> Any:
    """Composio renamed the list payload ``data`` -> ``items`` in >=0.18.

    Accepting both is what keeps a version bump from silently reporting
    "disconnected" (see BASELINE.md).
    """
    data = getattr(items, "data", None) or getattr(items, "items", None)
    if data is None and isinstance(items, dict):
        data = items.get("data") or items.get("items")
    return data


def _extract_email(obj: Any) -> str | None:
    if obj is None:
        return None
    direct_keys = (
        "email",
        "email_address",
        "emailAddress",
        "user_email",
        "provider_email",
        "account_email",
    )
    for key in direct_keys:
        try:
            val = getattr(obj, key)
            if isinstance(val, str) and "@" in val:
                return val
        except Exception:
            pass
        if isinstance(obj, dict):
            val = obj.get(key)
            if isinstance(val, str) and "@" in val:
                return val
    if isinstance(obj, dict):
        email_addresses = obj.get("emailAddresses")
        if isinstance(email_addresses, (list, tuple)):
            for entry in email_addresses:
                if isinstance(entry, dict):
                    candidate = (
                        entry.get("value") or entry.get("email") or entry.get("emailAddress")
                    )
                    if isinstance(candidate, str) and "@" in candidate:
                        return candidate
                elif isinstance(entry, str) and "@" in entry:
                    return entry
        nested_paths = (
            ("profile", "email"),
            ("profile", "emailAddress"),
            ("user", "email"),
            ("data", "email"),
            ("data", "user", "email"),
            ("provider_profile", "email"),
        )
        for path in nested_paths:
            current: Any = obj
            for segment in path:
                if isinstance(current, dict) and segment in current:
                    current = current[segment]
                else:
                    current = None
                    break
            if isinstance(current, str) and "@" in current:
                return current
    return None


def _connection_id_of(obj: Any) -> str | None:
    if obj is None:
        return None
    candidate = getattr(obj, "id", None)
    if candidate is None and isinstance(obj, dict):
        candidate = obj.get("id")
    return _normalized(candidate) or None


def _status_of(obj: Any) -> str | None:
    value = getattr(obj, "status", None)
    if value is None and isinstance(obj, dict):
        value = obj.get("status")
    return value


# ---------------------------------------------------------------------------
# Profile cache — keyed on the Composio user id, which is per-tenant
# ---------------------------------------------------------------------------


def _cache_profile(composio_user_id: str, profile: dict[str, Any]) -> None:
    sanitized = _normalized(composio_user_id)
    if not sanitized or not isinstance(profile, dict):
        return
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE[sanitized] = {
            "profile": profile,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }


def _get_cached_profile(composio_user_id: str | None) -> dict[str, Any] | None:
    sanitized = _normalized(composio_user_id)
    if not sanitized:
        return None
    with _PROFILE_CACHE_LOCK:
        payload = _PROFILE_CACHE.get(sanitized)
        if payload and isinstance(payload.get("profile"), dict):
            return payload["profile"]
    return None


def _clear_cached_profile(composio_user_id: str | None = None) -> None:
    with _PROFILE_CACHE_LOCK:
        if composio_user_id:
            _PROFILE_CACHE.pop(_normalized(composio_user_id), None)
        else:
            _PROFILE_CACHE.clear()


async def _fetch_profile(composio_user_id: str) -> dict[str, Any] | None:
    sanitized = _normalized(composio_user_id)
    if not sanitized:
        return None
    try:
        result = await execute_gmail_tool_async(
            "GMAIL_GET_PROFILE", sanitized, arguments={"user_id": "me"}
        )
    except Exception as exc:
        logger.warning(
            "GMAIL_GET_PROFILE invocation failed",
            extra={"composio_user_id": sanitized, "error": str(exc)},
        )
        return None

    profile: dict[str, Any] | None = None
    if isinstance(result, dict):
        if isinstance(result.get("data"), dict):
            profile = result["data"]
        elif isinstance(result.get("profile"), dict):
            profile = result["profile"]
        elif isinstance(result.get("response_data"), dict):
            profile = result["response_data"]
        elif isinstance(result.get("items"), list):
            for item in result["items"]:
                if not isinstance(item, dict):
                    continue
                data_dict = item.get("data")
                if isinstance(data_dict, dict):
                    profile = (
                        data_dict.get("response_data")
                        or data_dict.get("profile")
                        or data_dict
                    )
                elif isinstance(item.get("response_data"), dict):
                    profile = item["response_data"]
                elif isinstance(item.get("profile"), dict):
                    profile = item["profile"]
                if isinstance(profile, dict):
                    break
        elif result.get("successful") is True and isinstance(result.get("result"), dict):
            profile = result.get("result")  # type: ignore[assignment]
        elif all(not isinstance(result.get(key), dict) for key in ("data", "profile", "result")):
            profile = result or None

    if isinstance(profile, dict):
        _cache_profile(sanitized, profile)
        return profile

    # Deliberately does NOT log the payload: a Gmail profile carries the user's
    # address and, on some shapes, message snippets.
    logger.warning(
        "unexpected Gmail profile payload shape", extra={"composio_user_id": sanitized}
    )
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def initiate_connect(
    payload: GmailConnectPayload,
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
) -> JSONResponse:
    """Start the Composio OAuth flow for the authenticated tenant."""
    auth_config_id = payload.auth_config_id or settings.composio_gmail_auth_config_id or ""
    if not auth_config_id:
        return error_response(
            "Missing auth_config_id. Set COMPOSIO_GMAIL_AUTH_CONFIG_ID or pass auth_config_id.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    repo = GmailConnectionRepository(session, user_id)
    composio_user_id = await repo.composio_user_id()
    _clear_cached_profile(composio_user_id)

    try:
        client = _get_composio_client(settings)
        # Composio deprecated `initiate` for Composio-managed OAuth auth configs:
        # it now 400s with "use POST /api/v3/connected_accounts/link instead".
        # `link` is the supported path and returns the same ConnectionRequest shape.
        # Fall back to `initiate` so a self-managed auth config (or an older SDK
        # without `link`) keeps working rather than hard-failing.
        if hasattr(client.connected_accounts, "link"):
            req = await _call(
                lambda: client.connected_accounts.link(
                    user_id=composio_user_id, auth_config_id=auth_config_id
                ),
                what="connected_accounts.link",
            )
        else:
            req = await _call(
                lambda: client.connected_accounts.initiate(
                    user_id=composio_user_id, auth_config_id=auth_config_id
                ),
                what="connected_accounts.initiate",
            )
    except Exception as exc:
        logger.exception("gmail connect failed", extra={"user_id": str(user_id)})
        return error_response(
            "Failed to initiate Gmail connect",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    await repo.upsert(status="pending")
    return JSONResponse(
        {
            "ok": True,
            "redirect_url": getattr(req, "redirect_url", None)
            or getattr(req, "redirectUrl", None),
            "connection_request_id": getattr(req, "id", None),
            "user_id": composio_user_id,
        }
    )


async def fetch_status(
    payload: GmailStatusPayload, session: AsyncSession, user_id: uuid.UUID
) -> JSONResponse:
    """Report — and persist — this tenant's Gmail connection state.

    The original wrote a process global here, which is the exact line
    (``client.py:306``) that let one user's connection hijack another's polling.
    """
    repo = GmailConnectionRepository(session, user_id)
    composio_user_id = await repo.composio_user_id()
    connection_request_id = _normalized(payload.connection_request_id)

    try:
        client = _get_composio_client()
        account: Any = None

        if connection_request_id:
            try:
                account = await _call(
                    lambda: client.connected_accounts.wait_for_connection(
                        connection_request_id, timeout=2.0
                    ),
                    what="connected_accounts.wait_for_connection",
                )
            except Exception:
                try:
                    account = await _call(
                        lambda: client.connected_accounts.get(connection_request_id),
                        what="connected_accounts.get",
                    )
                except Exception:
                    account = None
            # A connection request is only ours if Composio agrees it belongs to
            # this tenant's identity. Otherwise a guessed id would attach another
            # user's mailbox to this account.
            if account is not None:
                owner = getattr(account, "user_id", None)
                if owner is None and isinstance(account, dict):
                    owner = account.get("user_id")
                if _normalized(owner) and _normalized(owner) != composio_user_id:
                    logger.warning(
                        "gmail status: connection_request_id belongs to another identity",
                        extra={"user_id": str(user_id)},
                    )
                    account = None

        if account is None:
            try:
                items = await _call(
                    lambda: client.connected_accounts.list(
                        user_ids=[composio_user_id],
                        toolkit_slugs=["GMAIL"],
                        statuses=["ACTIVE"],
                    ),
                    what="connected_accounts.list",
                )
                data = _list_payload(items)
                if data:
                    account = data[0]
            except Exception as exc:
                logger.warning(
                    "gmail connected-account lookup failed",
                    extra={"user_id": str(user_id), "error": str(exc)},
                )
                account = None

        status_value = _status_of(account) if account is not None else None
        normalized_status = (status_value or "").upper()
        connected = normalized_status in ACTIVE_STATUSES
        email = _extract_email(account) if account is not None else None
        profile: dict[str, Any] | None = None
        profile_source = "none"

        if connected:
            profile = _get_cached_profile(composio_user_id)
            if profile:
                profile_source = "cache"
            else:
                profile = await _fetch_profile(composio_user_id)
                if profile:
                    profile_source = "fetched"
            if profile and not email:
                email = _extract_email(profile)
        else:
            _clear_cached_profile(composio_user_id)

        await repo.upsert(
            status=normalized_status or "UNKNOWN",
            email=email,
            connection_id=_connection_id_of(account) if connected else None,
        )

        return JSONResponse(
            {
                "ok": True,
                "connected": bool(connected),
                "status": status_value or "UNKNOWN",
                "email": email,
                "user_id": composio_user_id,
                "profile": profile,
                "profile_source": profile_source,
            }
        )
    except Exception as exc:
        logger.exception("gmail status failed", extra={"user_id": str(user_id)})
        return error_response(
            "Failed to fetch connection status",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


async def disconnect_account(
    payload: GmailDisconnectPayload, session: AsyncSession, user_id: uuid.UUID
) -> JSONResponse:
    """Revoke this tenant's Gmail connection.

    ``payload.connection_id`` is deliberately ignored: Composio connection ids are
    opaque bearer-ish identifiers, and honouring a client-supplied one would let
    any authenticated user delete any other user's connection. Candidates come
    from our own row, or from a Composio listing scoped to this tenant's identity.
    """
    repo = GmailConnectionRepository(session, user_id)
    composio_user_id = await repo.composio_user_id()

    try:
        client = _get_composio_client()
    except Exception as exc:
        logger.exception("gmail disconnect failed: client init", extra={"user_id": str(user_id)})
        return error_response(
            "Failed to disconnect Gmail",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    candidates: list[str] = []
    stored = await repo.connection_id()
    if stored:
        candidates.append(stored)

    try:
        items = await _call(
            lambda: client.connected_accounts.list(
                user_ids=[composio_user_id], toolkit_slugs=["GMAIL"]
            ),
            what="connected_accounts.list",
        )
        for entry in _list_payload(items) or []:
            candidate = _connection_id_of(entry)
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    except Exception as exc:
        logger.warning(
            "failed to list Gmail connections",
            extra={"user_id": str(user_id), "error": str(exc)},
        )

    removed_ids: list[str] = []
    errors: list[str] = []
    for identifier in candidates:
        try:
            await _call(
                lambda cid=identifier: client.connected_accounts.delete(cid),
                what="connected_accounts.delete",
            )
            removed_ids.append(identifier)
        except Exception as exc:  # pragma: no cover - depends on remote state
            logger.warning(
                "failed to remove Gmail connection",
                extra={"user_id": str(user_id), "error": str(exc)},
            )
            errors.append(str(exc))

    _clear_cached_profile(composio_user_id)
    await repo.clear()

    if errors and not removed_ids:
        return error_response(
            "Failed to disconnect Gmail",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="; ".join(errors),
        )

    body: dict[str, Any] = {
        "ok": True,
        "disconnected": bool(removed_ids),
        "removed_connection_ids": removed_ids,
    }
    if not removed_ids:
        body["message"] = "No Gmail connection found"
    if errors:
        body["warnings"] = errors
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _normalize_tool_response(result: Any) -> dict[str, Any]:
    payload_dict: dict[str, Any] | None = None
    try:
        if hasattr(result, "model_dump"):
            payload_dict = result.model_dump()  # type: ignore[assignment]
        elif hasattr(result, "dict"):
            payload_dict = result.dict()  # type: ignore[assignment]
    except Exception:
        payload_dict = None

    if payload_dict is None:
        try:
            if hasattr(result, "model_dump_json"):
                payload_dict = json.loads(result.model_dump_json())
        except Exception:
            payload_dict = None

    if payload_dict is None:
        if isinstance(result, dict):
            payload_dict = result
        elif isinstance(result, list):
            payload_dict = {"items": result}
        else:
            payload_dict = {"repr": str(result)}

    return payload_dict


def _prepare_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if value is not None:
                prepared[key] = value
    prepared.setdefault("user_id", "me")
    return prepared


def execute_gmail_tool(
    tool_name: str,
    composio_user_id: str,
    *,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronous tool execution, for the agent tool callables.

    Bounded by ``COMPOSIO_TIMEOUT_S``. The original had no timeout at all.
    """
    prepared = _prepare_arguments(arguments)
    try:
        client = _get_composio_client()
        result = _call_sync(
            lambda: client.client.tools.execute(
                tool_name, user_id=composio_user_id, arguments=prepared
            ),
            what=tool_name,
        )
        return _normalize_tool_response(result)
    except Exception as exc:
        # Never log `arguments` or the response: both carry email bodies.
        logger.warning(
            "gmail tool execution failed",
            extra={"tool": tool_name, "error": str(exc)},
        )
        raise RuntimeError(f"{tool_name} invocation failed: {exc}") from exc


async def execute_gmail_tool_async(
    tool_name: str,
    composio_user_id: str,
    *,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Async tool execution — the hot path used by the importance watcher.

    ``asyncio.to_thread`` + an explicit deadline, so a hung Composio call can no
    longer freeze the event loop for every tenant.
    """
    prepared = _prepare_arguments(arguments)
    try:
        client = _get_composio_client()
        result = await _call(
            lambda: client.client.tools.execute(
                tool_name, user_id=composio_user_id, arguments=prepared
            ),
            what=tool_name,
        )
        return _normalize_tool_response(result)
    except Exception as exc:
        logger.warning(
            "gmail tool execution failed",
            extra={"tool": tool_name, "error": str(exc)},
        )
        raise RuntimeError(f"{tool_name} invocation failed: {exc}") from exc


__all__ = [
    "COMPOSIO_TIMEOUT_S",
    "disconnect_account",
    "execute_gmail_tool",
    "execute_gmail_tool_async",
    "fetch_status",
    "get_active_gmail_user_id",
    "initiate_connect",
]
