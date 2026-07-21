"""OpenRouter client: pooling, max_tokens, Retry-After, and usage metering.

Driven through ``httpx.MockTransport`` rather than by patching the client, so the
assertions are about the bytes actually put on the wire.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import text

from server.openrouter_client import client as orc

# Captured before conftest's `_no_network` autouse fixture replaces the module
# attribute with a raising stub. This module deliberately exercises the real
# client; the network itself is replaced one layer lower, by MockTransport.
# Fixtures (sessionmaker, user_id, the leaked-worker guard) come from here.
# Registered as a plugin rather than imported: importing a fixture shadows the
# argument name in every test that requests it, which is what ruff F811 flags.
pytest_plugins = ("tests._jobs_support",)

_REAL = orc.request_chat_completion


def _completion(usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "model": "anthropic/claude-sonnet-4",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 7},
    }


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Install a scripted transport behind the shared client."""
    seen: list[httpx.Request] = []
    script: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return script.pop(0) if script else httpx.Response(200, json=_completion())

    fake = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(orc, "_client", fake)

    class Harness:
        requests = seen
        responses = script

    yield Harness


async def test_max_tokens_is_sent_on_every_request(transport):
    """Never set before, so OpenRouter reserved the model's full 64k output
    ceiling and a small credit balance 402'd immediately."""
    await _REAL(model="m", messages=[{"role": "user", "content": "x"}])

    import json

    from server.config import get_settings

    body = json.loads(transport.requests[0].read())
    assert body["max_tokens"] == get_settings().llm_max_tokens


async def test_max_tokens_is_overridable_per_call(transport):
    import json

    await _REAL(model="m", messages=[{"role": "user", "content": "x"}], max_tokens=128)
    assert json.loads(transport.requests[0].read())["max_tokens"] == 128


async def test_429_with_retry_after_is_honoured_once(transport, monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(orc.asyncio, "sleep", fake_sleep)
    transport.responses.append(httpx.Response(429, headers={"Retry-After": "3"}, json={}))
    transport.responses.append(httpx.Response(200, json=_completion()))

    result = await _REAL(
        model="m", messages=[{"role": "user", "content": "x"}]
    )

    assert result["choices"][0]["message"]["content"] == "hi"
    assert slept == [3.0]
    assert len(transport.requests) == 2


async def test_retry_after_is_capped(transport, monkeypatch: pytest.MonkeyPatch):
    """A hostile or buggy upstream must not be able to park a worker for an hour."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(orc.asyncio, "sleep", fake_sleep)
    transport.responses.append(httpx.Response(429, headers={"Retry-After": "99999"}, json={}))
    transport.responses.append(httpx.Response(200, json=_completion()))

    await _REAL(model="m", messages=[{"role": "user", "content": "x"}])

    from server.config import get_settings

    assert slept == [get_settings().llm_retry_after_max_s]


async def test_429_without_retry_after_is_not_retried(transport):
    """This is deliberately *not* a general retry policy — the job queue owns
    retry (plan.md Problem 3). Retry-After exists only to stop us hammering an
    upstream that told us to wait."""
    transport.responses.append(httpx.Response(429, json={"error": "slow down"}))

    with pytest.raises(orc.OpenRouterError) as exc:
        await _REAL(model="m", messages=[{"role": "user", "content": "x"}])

    assert "429" in str(exc.value)
    assert len(transport.requests) == 1


async def test_unparseable_retry_after_is_ignored(transport):
    """The HTTP-date form is legal but unsupported; parsing it against a skewed
    local clock is a worse failure than not retrying."""
    transport.responses.append(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, json={})
    )
    with pytest.raises(orc.OpenRouterError):
        await _REAL(model="m", messages=[{"role": "user", "content": "x"}])
    assert len(transport.requests) == 1


async def test_the_http_client_is_shared_across_calls(monkeypatch: pytest.MonkeyPatch):
    """The original opened a new AsyncClient per call — a fresh TLS handshake on
    every LLM request, on a path agent loops hit up to 8 times per turn."""
    monkeypatch.setattr(orc, "_client", None)
    first = await orc._get_client()
    second = await orc._get_client()
    assert first is second
    await orc.close_http_client()
    assert orc._client is None


async def test_usage_is_recorded_with_tenant_and_job(transport, sessionmaker, user_id):
    """The row that makes every cost claim in plan.md a measurement."""
    from server.jobs.context import reset_current_job_id, set_current_job_id
    from server.repositories.context import TenantContext, tenant_scope

    async with sessionmaker() as session:
        job_id = (
            await session.execute(
                text(
                    "INSERT INTO jobs (id, user_id, kind, payload) "
                    "VALUES (:i, :u, 'chat_turn', '{}'::jsonb) RETURNING id"
                ),
                {"i": uuid.uuid4(), "u": user_id},
            )
        ).scalar_one()
        await session.commit()

    transport.responses.append(
        httpx.Response(200, json=_completion({"prompt_tokens": 1234, "completion_tokens": 56}))
    )

    token = set_current_job_id(job_id)
    try:
        with tenant_scope(TenantContext(user_id=user_id, timezone="UTC")):
            await _REAL(
                model="anthropic/claude-sonnet-4", messages=[{"role": "user", "content": "x"}]
            )
    finally:
        reset_current_job_id(token)

    async with sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT model, prompt_tokens, completion_tokens, job_id "
                    "FROM llm_usage WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).mappings().all()

    assert len(row) == 1, row
    assert row[0]["prompt_tokens"] == 1234
    assert row[0]["completion_tokens"] == 56
    assert row[0]["model"] == "anthropic/claude-sonnet-4"
    assert row[0]["job_id"] == job_id


async def test_usage_is_skipped_without_a_tenant(transport, sessionmaker, user_id):
    """``llm_usage.user_id`` is NOT NULL, so no tenant means no row — and never
    an exception into the caller. Metering must not be able to fail a turn."""
    await _REAL(model="m", messages=[{"role": "user", "content": "x"}])

    async with sessionmaker() as session:
        count = await session.scalar(text("SELECT count(*) FROM llm_usage"))
    assert count == 0
