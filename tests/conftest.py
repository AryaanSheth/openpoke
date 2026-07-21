"""Shared test fixtures.

Three guarantees this file is responsible for:

1. The suite runs against the compose Postgres (``docker compose up -d postgres``),
   migrated with the real Alembic revisions — not ``create_all`` — so a missing
   migration fails the tests rather than hiding.
2. Each test runs inside a transaction that is rolled back. Nothing is truncated
   between tests, so tests stay fast and cannot see each other's writes.
3. **No test can reach OpenRouter or Composio.** Both seams are patched by
   autouse fixtures that raise by default; opting in means scripting the stub,
   not un-patching the network.

Phase 4 extends this file. It does not rewrite it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Point every component at the compose Postgres before server.config is imported.
#
# Deliberately a SEPARATE database from the dev one. Several components are global
# by design — the email watcher polls every connected tenant, `claim()` has no
# WHERE user_id because a worker services all tenants — so any row committed by a
# running dev app leaks into those tests and breaks them. Sharing one database made
# the suite fail the moment a real Gmail account was connected locally.
# Override with TEST_DATABASE_URL if you need a different target.
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://openpoke:openpoke@localhost:5432/openpoke_test",
    ),
)
# A test that reaches the real network would need these; make sure it can't
# accidentally succeed using the developer's live .env credentials.
os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
os.environ["COMPOSIO_API_KEY"] = "test-composio-key"
os.environ["COMPOSIO_GMAIL_AUTH_CONFIG_ID"] = "test-auth-config"

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from server.config import get_settings  # noqa: E402

get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    """Bring the test database to head once per session, synchronously.

    Alembic's async env calls ``asyncio.run``, so this must happen before any
    event loop is running — hence a plain (non-async) session fixture.
    """
    _ensure_database_exists()

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")


def _ensure_database_exists() -> None:
    """Create the test database if it isn't there yet.

    Keeps `pytest` a one-command story on a fresh clone: `docker compose up -d
    postgres` gives you a server, and the suite provisions its own database on it.
    """
    import asyncio
    import re

    url = get_settings().database_url
    name = url.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):  # never interpolate a weird name
        raise RuntimeError(f"refusing to create database with unsafe name: {name!r}")
    admin_url = url.rsplit("/", 1)[0] + "/postgres"

    async def _create() -> None:
        eng = create_async_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with eng.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
                )
                if not exists:
                    await conn.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            await eng.dispose()

    asyncio.run(_create())


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(_migrated_database: None):
    """Session-scoped async engine. NullPool keeps connections from outliving tests."""
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(engine) -> AsyncIterator[AsyncSession]:
    """A session inside an outer transaction that is rolled back afterwards.

    ``join_transaction_mode="create_savepoint"`` means a ``session.commit()``
    inside the code under test releases a savepoint instead of committing for
    real, so application code can commit normally and still leave no trace.
    """
    conn = await engine.connect()
    trans = await conn.begin()
    session = AsyncSession(
        bind=conn,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """In-process client. ASGITransport does not run lifespan events, so the
    app's background loops (trigger scheduler, email watcher) never start."""
    from server.app import app
    from server.db.session import get_session

    async def _override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_session, None)


# ---------------------------------------------------------------------------
# LLM stub — no test reaches OpenRouter
# ---------------------------------------------------------------------------


def _chat_completion(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    """Build an OpenRouter-shaped chat completion payload."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-stub",
        "model": "stub/model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class LLMStub:
    """Records every LLM call and replays scripted responses in order.

    ``stub.push(...)`` queues a response; the last queued response repeats once
    the queue drains, so a tool loop of unknown length cannot hang the test.
    """

    response = staticmethod(_chat_completion)

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._scripted: list[dict] = []
        self._default: dict | None = None

    def push(self, response: dict) -> LLMStub:
        self._scripted.append(response)
        return self

    def push_text(self, content: str) -> LLMStub:
        return self.push(_chat_completion(content))

    def set_default(self, response: dict) -> LLMStub:
        self._default = response
        return self

    @property
    def models(self) -> list[str]:
        return [call["model"] for call in self.calls]

    async def __call__(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self._scripted:
            return self._scripted.pop(0)
        if self._default is not None:
            return self._default
        return _chat_completion("")


@cache
def _openrouter_binding_modules() -> tuple[Any, ...]:
    """Every module that holds a reference to ``request_chat_completion``.

    Call sites do ``from ...openrouter_client import request_chat_completion``,
    which binds the function into their own namespace — patching only the
    defining module would miss all of them. Discovered by scanning rather than
    hardcoded so a new call site cannot silently escape the stub.

    Cached, and first evaluated before anything is patched, so the identity
    comparison is always against the genuine function.

    ``server.app`` does *not* transitively import the agent runtimes:
    ``server/jobs/handlers.py::handle_chat_turn`` and
    ``server/services/gmail/importance_watcher.py::_resolve_interaction_runtime``
    both import ``agents.interaction_agent.runtime`` lazily, inside a function
    body, specifically so the module still imports if Phase 1/2 internals move.
    Left alone, that module (and ``agents.execution_agent.{runtime,
    batch_manager}``, pulled in transitively via ``interaction_agent.tools``)
    would not exist in ``sys.modules`` yet when this function is first called
    (at the first test's fixture setup) and would be missing from the cached
    tuple below — permanently, since ``@cache`` never re-scans. A behavioral
    test that only imports them mid-test-body (by actually running a job or a
    watcher poll, as tests/test_behavioral.py does) would then find
    ``request_chat_completion`` bound to whatever the *first* test to trigger
    that lazy import happened to patch it to — a stale stub from a different
    test, silently swallowing every call with an empty response. Verified: a
    two-scenario run without this import showed exactly that — the second
    scenario's interaction-agent dispatch produced zero recorded calls because
    it was still talking to the first scenario's already-torn-down stub.
    Importing them here, before any module gets patched, is what the cache's
    own docstring promises.
    """
    import server.agents.interaction_agent.runtime  # noqa: F401
    import server.agents.interaction_agent.tools  # noqa: F401  (pulls in execution_agent.{runtime,batch_manager})
    import server.app  # noqa: F401  (imports the whole tree)
    from server.openrouter_client import client as _client

    real = _client.request_chat_completion
    return tuple(
        mod
        for mod in list(sys.modules.values())
        if mod is not None and getattr(mod, "request_chat_completion", None) is real
    )


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> LLMStub:
    """Install an LLMStub over every OpenRouter binding."""
    stub = LLMStub()
    for mod in _openrouter_binding_modules():
        monkeypatch.setattr(mod, "request_chat_completion", stub, raising=False)
    return stub


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Structural block: OpenRouter and Composio both raise unless stubbed.

    Requesting the ``llm`` fixture re-patches the OpenRouter bindings on top of
    this one, so opting in is explicit.
    """

    async def _blocked_llm(**kwargs: Any) -> dict:
        raise AssertionError(
            "test attempted a real OpenRouter call; request the `llm` fixture instead"
        )

    for mod in _openrouter_binding_modules():
        monkeypatch.setattr(mod, "request_chat_completion", _blocked_llm, raising=False)

    def _blocked_composio(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("test attempted a real Composio call; patch the seam instead")

    from server.services.gmail import client as gmail_client

    monkeypatch.setattr(gmail_client, "_get_composio_client", _blocked_composio)
    yield
