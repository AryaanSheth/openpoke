"""Phase 1 — the importance watcher, per tenant.

Exercises the real ``ImportantEmailWatcher.poll_once`` loop against the real
Postgres: tenant enumeration, per-tenant context binding, per-tenant seen ledger,
and the classify-failure retry semantics. Only the Composio HTTP call, the LLM
call and the interaction-agent dispatch are stubbed, because those are the three
seams that leave the process.

The property that matters: **what the watcher polls for A is a function of A
alone.** The original read one process global (original ``:113``), so B
connecting Gmail redirected A's polling.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from server.repositories.gmail import GmailConnectionRepository, GmailSeenRepository
from server.repositories.users import create_user
from server.services.gmail import importance_watcher as watcher_module
from server.services.gmail.importance_classifier import Classification
from server.services.gmail.importance_watcher import ImportantEmailWatcher
from server.services.gmail.processing import ProcessedEmail


class _NonClosingSessionmaker:
    """Hand the watcher the test's transaction-scoped session.

    The watcher opens ``async with self._session()``; the test session must
    survive that block so the outer rollback still owns it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _NonClosingSessionmaker:
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _email(message_id: str, *, minutes_ago: float = 0.1) -> ProcessedEmail:
    return ProcessedEmail(
        id=message_id,
        thread_id=None,
        query="label:INBOX",
        subject="subject",
        sender="someone@example.test",
        recipient="me@example.test",
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        label_ids=["INBOX"],
        clean_text="body",
        has_attachments=False,
        attachment_count=0,
        attachment_filenames=[],
    )


@pytest_asyncio.fixture(loop_scope="session")
async def two_connected_tenants(db_session: AsyncSession):
    alice, _ = await create_user(
        db_session, email=f"w-alice-{uuid.uuid4().hex}@example.test", timezone="America/New_York"
    )
    bob, _ = await create_user(
        db_session, email=f"w-bob-{uuid.uuid4().hex}@example.test", timezone="UTC"
    )
    await GmailConnectionRepository(db_session, alice.id).upsert(status="ACTIVE")
    await GmailConnectionRepository(db_session, bob.id).upsert(status="ACTIVE")
    await db_session.flush()
    return alice, bob


@pytest.fixture
def stubbed_watcher(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession):
    """A watcher whose three external seams are recorded instead of called."""
    calls: dict[str, list] = {"fetch": [], "classify": [], "dispatch": []}
    inbox: dict[str, list[ProcessedEmail]] = {}
    verdicts: dict[str, Classification] = {}

    async def _fetch(tool_name, composio_user_id, *, arguments=None):
        calls["fetch"].append((tool_name, composio_user_id))
        return {"_composio_user_id": composio_user_id}

    def _parse(raw_result, *, query, cleaner):
        # Restamp to "just arrived". The watcher suppresses anything older than
        # the previous poll, and these tests poll back-to-back — age suppression
        # is pre-existing behaviour, not what is under test here.
        return [
            replace(email, timestamp=datetime.now(timezone.utc))
            for email in inbox.get(raw_result["_composio_user_id"], [])
        ], None

    async def _classify(email):
        calls["classify"].append(email.id)
        return verdicts.get(email.id, Classification.not_important())

    monkeypatch.setattr(watcher_module, "execute_gmail_tool_async", _fetch)
    monkeypatch.setattr(watcher_module, "parse_gmail_fetch_response", _parse)
    monkeypatch.setattr(watcher_module, "classify_email_importance", _classify)

    watcher = ImportantEmailWatcher(sessionmaker=_NonClosingSessionmaker(db_session))

    async def _dispatch(summary: str) -> None:
        calls["dispatch"].append(summary)

    monkeypatch.setattr(watcher, "_dispatch_summary", _dispatch)
    return watcher, calls, inbox, verdicts


async def test_each_tenant_is_polled_with_its_own_composio_identity(
    two_connected_tenants, stubbed_watcher
) -> None:
    alice, bob = two_connected_tenants
    watcher, calls, _, _ = stubbed_watcher

    assert await watcher.poll_once() == 2

    polled = {composio_id for _, composio_id in calls["fetch"]}
    assert polled == {str(alice.id), str(bob.id)}


async def test_b_connecting_does_not_change_what_a_polls(
    db_session: AsyncSession, stubbed_watcher
) -> None:
    """The ``_ACTIVE_USER_ID`` regression, run through the real loop."""
    watcher, calls, _, _ = stubbed_watcher

    alice, _ = await create_user(db_session, email=f"a-{uuid.uuid4().hex}@example.test")
    await GmailConnectionRepository(db_session, alice.id).upsert(status="ACTIVE")
    await db_session.flush()

    await watcher.poll_once()
    a_identity = {cid for _, cid in calls["fetch"]}
    assert a_identity == {str(alice.id)}

    # B connects. Under the old global this is the moment A's polling moved.
    bob, _ = await create_user(db_session, email=f"b-{uuid.uuid4().hex}@example.test")
    await GmailConnectionRepository(db_session, bob.id).upsert(status="ACTIVE")
    await db_session.flush()

    calls["fetch"].clear()
    await watcher.poll_once()
    assert {cid for _, cid in calls["fetch"]} == {str(alice.id), str(bob.id)}


async def test_inactive_connections_are_not_polled(
    db_session: AsyncSession, stubbed_watcher
) -> None:
    watcher, calls, _, _ = stubbed_watcher
    user, _ = await create_user(db_session, email=f"p-{uuid.uuid4().hex}@example.test")
    await GmailConnectionRepository(db_session, user.id).upsert(status="pending")
    await db_session.flush()

    await watcher.poll_once()
    assert [cid for _, cid in calls["fetch"] if cid == str(user.id)] == []


async def test_first_poll_is_warmup_and_the_second_classifies(
    two_connected_tenants, stubbed_watcher, db_session: AsyncSession
) -> None:
    alice, _bob = two_connected_tenants
    watcher, calls, inbox, verdicts = stubbed_watcher

    inbox[str(alice.id)] = [_email("m1")]
    await watcher.poll_once()
    assert calls["classify"] == [], "warmup must not classify pre-existing mail"

    inbox[str(alice.id)] = [_email("m1"), _email("m2")]
    await watcher.poll_once()
    assert calls["classify"] == ["m2"], "only the new message is classified"


async def test_a_failed_classification_is_retried_not_dropped(
    two_connected_tenants, stubbed_watcher
) -> None:
    """The permanent-drop bug. A transient OpenRouter failure used to mark the
    id seen anyway (original ``:210``), losing the email forever."""
    alice, _bob = two_connected_tenants
    watcher, calls, inbox, verdicts = stubbed_watcher

    inbox[str(alice.id)] = []
    await watcher.poll_once()  # warmup

    inbox[str(alice.id)] = [_email("flaky")]
    verdicts["flaky"] = Classification.failed("openrouter 429")
    await watcher.poll_once()
    assert calls["classify"] == ["flaky"]
    assert calls["dispatch"] == []

    # Next poll: still eligible, and this time it succeeds and is surfaced.
    verdicts["flaky"] = Classification.surfaced("your flight was cancelled")
    await watcher.poll_once()
    assert calls["classify"] == ["flaky", "flaky"]
    assert calls["dispatch"] == ["your flight was cancelled"]

    # And now it is settled — a third poll must not re-classify it.
    await watcher.poll_once()
    assert calls["classify"] == ["flaky", "flaky"]


async def test_a_poison_message_stops_retrying(
    two_connected_tenants, stubbed_watcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, _bob = two_connected_tenants
    watcher, calls, inbox, verdicts = stubbed_watcher

    inbox[str(alice.id)] = []
    await watcher.poll_once()  # warmup

    inbox[str(alice.id)] = [_email("poison")]
    verdicts["poison"] = Classification.failed("always broken")
    await watcher.poll_once()
    assert calls["classify"] == ["poison"]

    from server.repositories import gmail as gmail_repo

    monkeypatch.setattr(gmail_repo, "CLASSIFY_RETRY_BUDGET", timedelta(seconds=-1))
    await watcher.poll_once()
    assert calls["classify"] == ["poison"], "budget exhausted; must stop retrying"


async def test_summaries_land_in_the_right_tenants_conversation(
    two_connected_tenants, stubbed_watcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop the dispatch stub and record the tenant bound at dispatch time."""
    alice, bob = two_connected_tenants
    watcher, calls, inbox, verdicts = stubbed_watcher

    from server.repositories.context import get_tenant

    observed: list[tuple[uuid.UUID, str]] = []

    async def _dispatch(summary: str) -> None:
        tenant = get_tenant()
        assert tenant is not None, "dispatch ran outside a tenant scope"
        observed.append((tenant.user_id, summary))

    monkeypatch.setattr(watcher, "_dispatch_summary", _dispatch)

    inbox[str(alice.id)] = []
    inbox[str(bob.id)] = []
    await watcher.poll_once()  # warmup both

    inbox[str(alice.id)] = [_email("a1")]
    inbox[str(bob.id)] = [_email("b1")]
    verdicts["a1"] = Classification.surfaced("alice's flight cancelled")
    verdicts["b1"] = Classification.surfaced("bob's package delayed")
    await watcher.poll_once()

    assert sorted(observed) == sorted(
        [(alice.id, "alice's flight cancelled"), (bob.id, "bob's package delayed")]
    )


async def test_seen_ledgers_do_not_bleed_between_tenants(
    two_connected_tenants, stubbed_watcher, db_session: AsyncSession
) -> None:
    """Both tenants receive a message with the *same* id. The old JSON deque was
    global, so one tenant's warmup suppressed the other's mail."""
    alice, bob = two_connected_tenants
    watcher, calls, inbox, verdicts = stubbed_watcher

    inbox[str(alice.id)] = [_email("shared")]
    inbox[str(bob.id)] = []
    await watcher.poll_once()  # alice warms up on 'shared'; bob sees nothing

    inbox[str(bob.id)] = [_email("shared")]
    await watcher.poll_once()

    assert calls["classify"] == ["shared"], "bob must still see the id alice consumed"
    assert await GmailSeenRepository(db_session, alice.id).is_seen("shared") is True
    assert await GmailSeenRepository(db_session, bob.id).is_seen("shared") is True


async def test_one_tenants_failure_does_not_stop_the_others(
    two_connected_tenants, stubbed_watcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, bob = two_connected_tenants
    watcher, calls, inbox, _ = stubbed_watcher

    async def _fetch(tool_name, composio_user_id, *, arguments=None):
        calls["fetch"].append((tool_name, composio_user_id))
        if composio_user_id == str(alice.id):
            raise RuntimeError("GMAIL_FETCH_EMAILS timed out after 30s")
        return {"_composio_user_id": composio_user_id}

    monkeypatch.setattr(watcher_module, "execute_gmail_tool_async", _fetch)

    assert await watcher.poll_once() == 2
    assert {cid for _, cid in calls["fetch"]} == {str(alice.id), str(bob.id)}
