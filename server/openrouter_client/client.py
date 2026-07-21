"""OpenRouter chat-completions client.

Four changes here, all of them cheap and all of them things that were costing
real money or real latency:

1. **One shared ``AsyncClient``** (was ``:70``: ``async with httpx.AsyncClient()``
   per call). Every LLM request paid a fresh TCP connect and TLS handshake —
   roughly 100-200 ms of pure overhead against ``openrouter.ai``, on a path the
   agent loops hit up to 8 times per turn.
2. **``Retry-After`` on 429.** One retry, honouring the header. Not a general
   retry policy — the job queue's retry already covers the data-loss case
   (plan.md Problem 3) — this exists so we stop hammering an upstream that has
   explicitly told us to wait.
3. **``max_tokens``.** Never set before, so OpenRouter reserved the model's full
   64k output ceiling on every request and a small credit balance 402'd
   immediately with *"requested up to 64000 tokens, but can only afford N"*
   (BASELINE.md gotcha 1). Now ``OPENPOKE_LLM_MAX_TOKENS``, default 4096.
4. **Usage metering.** ``usage`` came back on every response and was discarded
   (``:82``). It now lands in ``llm_usage`` with the tenant and the job, which is
   what turns every cost claim in plan.md from assertion into measurement — and
   is the regression signal for prompt bloat.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ..config import get_settings
from ..logging_config import logger

OpenRouterBaseURL = "https://openrouter.ai/api/v1"

DEFAULT_TIMEOUT_S = 60.0

# One pool for the process. Built lazily rather than at import so that importing
# this module still opens no sockets, and so the pool binds to the event loop
# that actually uses it.
#
# # ponytail: a single module-level client assumes one event loop per process.
# That holds for the API and the worker; Phase 1's sync bridge runs a second
# loop but makes no LLM calls.
_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=DEFAULT_TIMEOUT_S,
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                )
    return _client


async def close_http_client() -> None:
    """Close the shared pool. Called on worker shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class OpenRouterError(RuntimeError):
    """Raised when the OpenRouter API returns an error response."""


def _headers(*, api_key: str | None = None) -> dict[str, str]:
    settings = get_settings()
    key = (api_key or settings.openrouter_api_key or "").strip()
    if not key:
        raise OpenRouterError("Missing OpenRouter API key")

    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _build_messages(messages: list[dict[str, str]], system: str | None) -> list[dict[str, str]]:
    if system:
        return [{"role": "system", "content": system}, *messages]
    return messages


def _handle_response_error(exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    detail: str
    try:
        payload = response.json()
        detail = payload.get("error") or payload.get("message") or json.dumps(payload)
    except Exception:
        detail = response.text
    raise OpenRouterError(f"OpenRouter request failed ({response.status_code}): {detail}") from exc


def _retry_after_seconds(response: httpx.Response, cap: float) -> float | None:
    """Parse ``Retry-After``. Returns None when absent or unusable.

    Only the delta-seconds form is honoured. The HTTP-date form is legal but
    OpenRouter does not send it, and parsing a date against a possibly-skewed
    local clock is a worse failure than not retrying.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, cap)


async def _record_usage(payload: dict[str, Any], model: str) -> None:
    """Write one ``llm_usage`` row. Never raises into the caller.

    Attribution comes from two ContextVars: the tenant (Phase 1) and the job
    (Phase 2). Both can legitimately be absent — a CLI call, a request before
    auth — and ``llm_usage.user_id`` is NOT NULL, so no tenant means no row.
    """
    usage = payload.get("usage") or {}
    if not usage:
        return
    try:
        # TODO(phase-1-integration): reads Phase 1's tenant ContextVar. If tenancy
        # ever moves off a ContextVar, this needs the user id threaded in instead.
        from ..repositories.context import get_tenant

        tenant = get_tenant()
        if tenant is None:
            return

        from ..db.engine import get_sessionmaker
        from ..db.models import LlmUsage
        from ..jobs.context import get_current_job_id

        async with get_sessionmaker()() as session:
            session.add(
                LlmUsage(
                    user_id=tenant.user_id,
                    job_id=get_current_job_id(),
                    model=str(payload.get("model") or model)[:128],
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                )
            )
            await session.commit()
    except Exception as exc:
        # Metering is observability. It must never be the reason a turn fails.
        logger.warning("failed to record llm usage", extra={"error": str(exc)})


async def request_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    system: str | None = None,
    api_key: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    base_url: str = OpenRouterBaseURL,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Request a chat completion and return the raw JSON payload."""

    settings = get_settings()
    payload: dict[str, object] = {
        "model": model,
        "messages": _build_messages(messages, system),
        "stream": False,
        "max_tokens": max_tokens if max_tokens is not None else settings.llm_max_tokens,
    }
    if tools:
        payload["tools"] = tools

    url = f"{base_url.rstrip('/')}/chat/completions"
    client = await _get_client()
    headers = _headers(api_key=api_key)

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT_S)

        if response.status_code == 429:
            delay = _retry_after_seconds(response, settings.llm_retry_after_max_s)
            if delay is not None:
                logger.warning(
                    "OpenRouter rate limited; honouring Retry-After",
                    extra={"delay_s": delay, "model": model},
                )
                await asyncio.sleep(delay)
                response = await client.post(
                    url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT_S
                )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _handle_response_error(exc)

        body = response.json()
    except OpenRouterError:
        raise
    except httpx.HTTPError as exc:
        raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

    await _record_usage(body, model)
    return body


__all__ = [
    "OpenRouterBaseURL",
    "OpenRouterError",
    "close_http_client",
    "request_chat_completion",
]
