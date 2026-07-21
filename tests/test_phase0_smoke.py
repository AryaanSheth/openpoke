"""Smoke tests for the Phase 0 harness itself.

These assert that the fixtures work, not that the application behaves. Phase 4
owns the real suite; this file exists so `pytest` is meaningful today.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.config import Settings, get_settings
from server.db.models import User

_PROBE_EMAIL = "rollback-probe@example.com"


def test_settings_are_overridable_per_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the pydantic-settings rewrite: no import-time freeze."""
    monkeypatch.setenv("OPENPOKE_INTERACTION_AGENT_MODEL", "test/model-x")
    monkeypatch.setenv("OPENPOKE_CORS_ALLOW_ORIGINS", "https://a.example,https://b.example")
    settings = Settings()
    assert settings.interaction_agent_model == "test/model-x"
    assert settings.cors_allow_origins == ["https://a.example", "https://b.example"]


def test_cors_default_is_an_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENPOKE_CORS_ALLOW_ORIGINS", raising=False)
    assert Settings().cors_allow_origins == ["http://localhost:3000"]


def test_get_settings_cache_is_clearable() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()


async def test_a_db_session_round_trip(db_session) -> None:
    db_session.add(User(id=uuid.uuid4(), email=_PROBE_EMAIL, api_key_hash="x"))
    await db_session.commit()
    found = await db_session.scalar(select(User).where(User.email == _PROBE_EMAIL))
    assert found is not None


async def test_b_previous_test_was_rolled_back(db_session) -> None:
    """Depends on running after test_a_*; proves the transaction rollback works."""
    found = await db_session.scalar(select(User).where(User.email == _PROBE_EMAIL))
    assert found is None


async def test_health_endpoint(client) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_llm_stub_records_calls_and_replays(llm) -> None:
    from server.openrouter_client import request_chat_completion

    llm.push_text("hello")
    result = await request_chat_completion(model="stub/model", messages=[{"role": "user", "content": "hi"}])
    assert result["choices"][0]["message"]["content"] == "hello"
    assert llm.models == ["stub/model"]


async def test_openrouter_is_blocked_without_the_stub() -> None:
    from server.openrouter_client import request_chat_completion

    with pytest.raises(AssertionError, match="real OpenRouter call"):
        await request_chat_completion(model="anything", messages=[])


def test_composio_is_blocked() -> None:
    from server.services.gmail.client import _get_composio_client

    with pytest.raises(AssertionError, match="real Composio call"):
        _get_composio_client()
