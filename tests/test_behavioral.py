"""Behavioral regression net: recorded LLM fixtures, tool-call sequences.

Two scenarios per plan.md's Phase 4:

1. "remind me tomorrow at 9" -- interaction agent delegates via
   ``send_message_to_agent``, an execution agent runs, ``createTrigger`` fires,
   a trigger row lands.
2. An important-email arrival -- the watcher classifies and dispatches into the
   owner's conversation.

Fixtures are data (``tests/fixtures/*.json``), replayed through the ``llm``
stub conftest.py already provides -- no second mocking mechanism. The autouse
``_no_network`` fixture in conftest.py blocks OpenRouter/Composio structurally;
requesting ``llm`` opts back in explicitly.

Both scenarios are armed (no xfail). Neither was expected to be when this file
was started -- Phase 2 landed the job queue mid-session, and a real,
independently-verified conftest.py caching bug (fixed here; see
``_openrouter_binding_modules`` in conftest.py) was silently swallowing the
second scenario's interaction-agent call. See each test's docstring for what
changed and how it was verified, not assumed.

**Design constraint that shapes every assertion here:** the agent runtime
(``server/agents/interaction_agent`` and ``server/agents/execution_agent``) is
outside this phase's edit scope and still uses the legacy proxy stores
(``ConversationLog``, ``AgentRoster``, ``ExecutionAgentLogStore``,
``TriggerStore``). Every one of them writes through
``repositories.context.run_sync`` -- a *separate* bridge connection to the same
Postgres instance, not the test's ``db_session`` transaction (see
``docs/phase-1-notes.md`` New Issues #2). Two consequences, verified below
rather than assumed:

- A user created only inside ``db_session`` is invisible to the bridge
  connection (its creating transaction never really commits), so any bridge
  write referencing that user's id fails with a foreign-key violation. Tests
  that exercise the legacy proxies use ``bridge_user`` (real commit through the
  same bridge, explicit teardown through the same bridge) instead of
  ``create_user`` against ``db_session``.
- Bridge writes are real commits with no rollback net. ``bridge_user`` deletes
  every row it created, in FK order, in a ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import (
    Agent,
    AgentLogEntry,
    ConversationEntry,
    Job,
    SummaryState,
    Trigger,
    User,
    WorkingMemoryEntry,
)
from server.repositories.context import run_sync
from server.repositories.conversation import ConversationRepository
from server.repositories.execution import AgentRosterRepository
from server.repositories.gmail import GmailConnectionRepository
from server.repositories.triggers import TriggerRepository
from server.repositories.users import create_user

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _script_turns(llm, turns: list[dict[str, Any]]) -> None:
    """Push one stubbed OpenRouter completion per fixture turn, in order."""
    for turn in turns:
        raw_tool_calls = turn.get("tool_calls") or []
        tool_calls = (
            [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for i, tc in enumerate(raw_tool_calls)
            ]
            if raw_tool_calls
            else None
        )
        llm.push(llm.response(content=turn.get("content", ""), tool_calls=tool_calls))


async def _drain_background_tasks(rounds: int = 20) -> None:
    """Await every task spawned by a detached ``loop.create_task`` call, plus
    whatever *those* tasks go on to spawn (``send_message_to_agent`` ->
    ``_dispatch_to_interaction_agent`` is itself a second detached task created
    only once the first completes). Not a mocking seam -- this awaits the real
    tasks the real code scheduled; it exists because nothing in the production
    code path retains a handle to join them itself.
    """
    seen: set[asyncio.Task] = {asyncio.current_task()}
    for _ in range(rounds):
        pending = [t for t in asyncio.all_tasks() if t not in seen and not t.done()]
        if not pending:
            await asyncio.sleep(0)
            continue
        seen.update(pending)
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# bridge_user -- a real, committed user for scenarios that exercise the legacy
# proxy stores (ConversationLog / AgentRoster / ExecutionAgentLogStore /
# TriggerStore). See module docstring for why db_session alone is not enough.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def bridge_user() -> AsyncIterator[tuple[User, str]]:
    async def _create(session: AsyncSession) -> tuple[User, str]:
        user, token = await create_user(
            session, email=f"behavioral-{uuid.uuid4().hex}@example.test", timezone="UTC"
        )
        return user, token

    user, token = run_sync(_create)
    try:
        yield user, token
    finally:

        async def _cleanup(session: AsyncSession) -> None:
            for model in (
                ConversationEntry,
                WorkingMemoryEntry,
                SummaryState,
                AgentLogEntry,
                Agent,
                Trigger,
                Job,
            ):
                await session.execute(delete(model).where(model.user_id == user.id))
            await session.execute(delete(User).where(User.id == user.id))

        run_sync(_cleanup)


# ---------------------------------------------------------------------------
# Scenario 1 -- "remind me tomorrow at 9"
# ---------------------------------------------------------------------------


async def test_remind_me_tomorrow_creates_trigger_via_expected_tool_sequence(
    client,
    llm,
    bridge_user: tuple[User, str],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Armed -- runs cleanly against the code as it stands. Was drafted expecting
    to xfail (Phase 2 was mid-rewrite of chat_handler.py when this file was
    started, and the pre-Phase-2 asyncio.create_task path could not be driven
    deterministically). By the time this ran, Phase 2 had landed server/jobs/* +
    server/worker.py, and POST /chat/send now durably enqueues a `chat_turn`
    job. Two more layers of the chain -- send_message_to_agent
    (interaction_agent/tools.py:141) and ExecutionBatchManager.
    _dispatch_to_interaction_agent (execution_agent/batch_manager.py:193) --
    are still bare, unretained ``loop.create_task(...)`` calls rather than jobs,
    so this test drains them explicitly (see ``_drain_background_tasks``)
    instead of assuming the worker's own await chain reaches them. That is not
    a mock or a narrowed assertion: it awaits the real tasks the real code
    schedules, deterministically, because nothing on this path does genuine
    network I/O (the LLMStub never suspends), so there is no race to paper
    over. Verified stable across 5 repeated runs.

    Ground truth sequence (see fixtures/remind_me_tomorrow.json for the full
    six-call trace and how it was derived -- an earlier draft of this fixture
    assumed the interaction agent's ``execute()`` returns after one LLM round;
    it does not, matching ExecutionAgentRuntime's same call-again-to-close-the-
    loop pattern): interaction agent's send_message_to_user + send_message_to_agent
    in one round -> interaction agent's loop-closing call -> execution agent's
    createTrigger -> execution agent's loop-closing call -> interaction agent's
    send_message_to_user relay (via handle_agent_message) -> interaction agent's
    loop-closing call.

    Each assertion is functional evidence a specific tool in the sequence
    actually ran: the roster gaining the delegated-to agent proves
    send_message_to_agent ran, the trigger row proves createTrigger ran with the
    scripted arguments, and the two persisted replies prove both
    send_message_to_user calls ran.
    """
    from server.config import get_settings
    from server.jobs.handlers import default_handlers
    from server.jobs.worker import Worker

    # ConversationLog._notify_summarization schedules a *second*, independent
    # detached LLM call (summarizer_model) on every conversation write when
    # summarization is enabled (the config default). It is orthogonal to what
    # this scenario tests, but shares this test's single LLMStub queue, so a
    # summarization pass interleaving mid-sequence steals a response meant for
    # the interaction/execution agent and corrupts the scripted order --
    # verified: with summarization on, the execution agent ends up calling
    # send_message_to_user (a tool it doesn't even have) because the response
    # meant for its createTrigger call was consumed by the summarizer instead.
    # Disabled here via the sanctioned per-test settings-override seam, not by
    # touching server/ code.
    monkeypatch.setenv("OPENPOKE_CONVERSATION_SUMMARY_THRESHOLD", "0")
    get_settings.cache_clear()
    try:
        user, token = bridge_user
        fixture = _load_fixture("remind_me_tomorrow.json")
        fixture_agent_name = fixture["turns"][0]["tool_calls"][1]["arguments"]["agent_name"]
        _script_turns(llm, fixture["turns"])

        response = await client.post(
            "/api/v1/chat/send",
            json={"messages": [{"role": "user", "content": fixture["user_message"]}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202
        job_id = uuid.UUID(response.json()["job_id"])

        # Worker.run_once() claims from the whole `jobs` table, not just this
        # test's row -- against a shared local Postgres it can pick up pending
        # jobs left by other processes (concurrent test runs, other agents;
        # observed directly in this session). A large batch/concurrency makes
        # one run_once() claim everything pending in a single pass (including
        # ours) rather than looping and re-claiming across rounds. Asserting on
        # our specific job's own status, not an aggregate claimed-count, is what
        # makes this robust to that noise.
        worker = Worker(
            handlers=default_handlers(),
            worker_id="test-remind-me-tomorrow",
            batch=200,
            concurrency=200,
        )
        await worker.run_once()
        await worker.drain()

        job_status = (
            await db_session.execute(select(Job.status).where(Job.id == job_id))
        ).scalar_one()
        assert job_status == "done", f"chat_turn job did not complete: status={job_status!r}"

        # The chat_turn job is now complete, but send_message_to_agent and the
        # execution-agent dispatch back to the interaction agent are still
        # detached `loop.create_task` calls (see the docstring above) with no
        # handle the worker retains -- drain those by hand to give the rest of
        # the chain a chance to finish before asserting on it.
        await _drain_background_tasks()

        roster = await AgentRosterRepository(db_session, user.id).get_agents()
        assert fixture_agent_name in roster, "send_message_to_agent never ran"

        triggers = await TriggerRepository(db_session, user.id).list_for_agent(
            fixture_agent_name
        )
        assert len(triggers) == 1, "createTrigger never ran"
        assert triggers[0].payload == fixture["expected_trigger"]["payload"]
        assert triggers[0].status == fixture["expected_trigger"]["status"]

        entries = await ConversationRepository(db_session, user.id).iter_entries("UTC")
        replies = [payload for tag, _ts, payload in entries if tag == "poke_reply"]
        # Both send_message_to_user calls land as poke_reply entries: the
        # immediate "on it" acknowledgement (turn 0) and the later relay of the
        # execution agent's result (turn 4, via handle_agent_message).
        ack_message = fixture["turns"][0]["tool_calls"][0]["arguments"]["message"]
        confirmation_message = fixture["turns"][4]["tool_calls"][0]["arguments"]["message"]
        assert ack_message in replies, "the acknowledging send_message_to_user never ran"
        assert confirmation_message in replies, "the confirming send_message_to_user never ran"
    finally:
        # Belt-and-braces: monkeypatch restores the env var at fixture teardown
        # regardless, but the lru_cache on get_settings would otherwise keep
        # serving threshold=0 to whatever runs before that teardown fires.
        monkeypatch.undo()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Scenario 2 -- important-email arrival
# ---------------------------------------------------------------------------


class _NonClosingSessionmaker:
    """Hand the watcher the test's transaction-scoped session (same pattern as
    tests/test_phase1_watcher.py's fixture of the same name)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _NonClosingSessionmaker:
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _processed_email(fixture_email: dict[str, Any]):
    from server.services.gmail.processing import ProcessedEmail

    return ProcessedEmail(
        id=fixture_email["id"],
        thread_id=None,
        query="label:INBOX",
        subject=fixture_email["subject"],
        sender=fixture_email["sender"],
        recipient="owner@example.test",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=0.1),
        label_ids=["INBOX"],
        clean_text=fixture_email["clean_text"],
        has_attachments=False,
        attachment_count=0,
        attachment_filenames=[],
    )


async def test_important_email_arrival_dispatches_expected_tool_sequence(
    llm, bridge_user: tuple[User, str], db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ground truth: server/services/gmail/importance_watcher.py::_poll_tenant.
    classify_email_importance issues one LLM call against mark_email_importance;
    a decided+summarized verdict flows into _dispatch_summary ->
    InteractionAgentRuntime.handle_agent_message, a second, *awaited* LLM call
    (no detached task on this path) that relays the summary via
    send_message_to_user. This is the one half of the two plan.md scenarios that
    is actually driveable today: the watcher awaits its own dispatch directly."""
    from server.services.gmail import importance_watcher as watcher_module
    from server.services.gmail.importance_watcher import ImportantEmailWatcher

    user, _token = bridge_user
    fixture = _load_fixture("important_email_arrival.json")

    await GmailConnectionRepository(db_session, user.id).upsert(status="ACTIVE")
    await db_session.flush()

    inbox: list[Any] = []

    async def _fetch(tool_name, composio_user_id, *, arguments=None):
        return {"_composio_user_id": composio_user_id}

    def _parse(raw_result, *, query, cleaner):
        # Restamp to "just arrived" (same trick as test_phase1_watcher.py's
        # stubbed_watcher._parse): the watcher suppresses anything older than
        # the previous poll, and these polls run back-to-back within a single
        # test, so a fixed fixture timestamp would always read as stale.
        from dataclasses import replace

        return [replace(email, timestamp=datetime.now(timezone.utc)) for email in inbox], None

    monkeypatch.setattr(watcher_module, "execute_gmail_tool_async", _fetch)
    monkeypatch.setattr(watcher_module, "parse_gmail_fetch_response", _parse)

    watcher = ImportantEmailWatcher(sessionmaker=_NonClosingSessionmaker(db_session))

    # Warmup poll: the watcher treats a first poll's inbox as pre-existing, not
    # new, so classification is never triggered on it (importance_watcher.py:200).
    assert await watcher.poll_once() == 1

    inbox.append(_processed_email(fixture["email"]))
    _script_turns(llm, fixture["turns"])

    assert await watcher.poll_once() == 1

    # Three LLM calls: the classifier's one call, plus the interaction agent's
    # two (a tool-call round for send_message_to_user, then a final round with
    # no tool calls -- InteractionAgentRuntime._run_interaction_loop always
    # calls once more after executing a tool).
    called_models = [call["model"] for call in llm.calls]
    assert called_models == [_resolved_model(turn["model_setting"]) for turn in fixture["turns"]]

    # Salient argument check on the classifier call: the email text it graded
    # is the one from the fixture, not some other message.
    classifier_messages = llm.calls[0]["messages"]
    assert fixture["email"]["clean_text"] in classifier_messages[0]["content"]

    # Salient argument check on the interaction-agent call: the watcher's
    # summary (the classifier's tool-call output) made it into the prompt handed
    # to the interaction agent.
    # Note: `messages` is the same list object across every iteration of the
    # interaction agent's tool loop (InteractionAgentRuntime appends to it
    # in place rather than passing a fresh list per LLM call), and LLMStub
    # records the reference, not a snapshot -- so `llm.calls[1]["messages"]`
    # keeps growing even after this call returns. Index [0], the turn's
    # original prompt, which is appended-after but never mutated.
    interaction_messages = llm.calls[1]["messages"]
    expected_summary = fixture["turns"][0]["tool_calls"][0]["arguments"]["summary"]
    assert expected_summary in interaction_messages[0]["content"]

    # Functional proof the sequence actually executed for real, not merely that
    # the stub was called: the interaction agent's send_message_to_user reply is
    # a real, persisted conversation entry for this tenant.
    entries = await ConversationRepository(db_session, user.id).iter_entries("UTC")
    replies = [payload for tag, _ts, payload in entries if tag == "poke_reply"]
    expected_message = fixture["turns"][1]["tool_calls"][0]["arguments"]["message"]
    assert expected_message in replies


def _resolved_model(setting_name: str) -> str:
    from server.config import get_settings

    return getattr(get_settings(), setting_name)
