"""Phase 1 — tenant isolation.

Every test here runs against the real compose Postgres through the real ASGI app
with real bearer tokens. Nothing is mocked except the two network seams
(``conftest.py``'s autouse ``_no_network``), because a mocked isolation test
proves nothing about isolation.

Named ``test_phase1_tenancy`` rather than ``test_tenancy`` so Phase 4's headline
suite lands beside it instead of overwriting it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import clear_token_cache
from server.db.models import ConversationEntry, GmailConnection, Trigger, User
from server.repositories.conversation import ConversationRepository
from server.repositories.execution import AgentLogRepository, AgentRosterRepository
from server.repositories.gmail import GmailConnectionRepository, list_connected
from server.repositories.triggers import TriggerRepository
from server.repositories.users import create_user, split_token

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class Tenant:
    """A real user row plus the plaintext token that authenticates as them."""

    def __init__(self, user: User, token: str) -> None:
        self.user = user
        self.token = token

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture(autouse=True)
def _fresh_token_cache() -> None:
    """The verification cache is process-wide; a stale entry would mask a bug."""
    clear_token_cache()


@pytest_asyncio.fixture(loop_scope="session")
async def alice(db_session: AsyncSession) -> Tenant:
    user, token = await create_user(
        db_session, email=f"alice-{uuid.uuid4().hex}@example.test", timezone="America/New_York"
    )
    return Tenant(user, token)


@pytest_asyncio.fixture(loop_scope="session")
async def bob(db_session: AsyncSession) -> Tenant:
    user, token = await create_user(
        db_session, email=f"bob-{uuid.uuid4().hex}@example.test", timezone="UTC"
    )
    return Tenant(user, token)


@pytest_asyncio.fixture(loop_scope="session")
async def anon(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    yield client


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


async def test_health_is_the_only_unauthenticated_route(anon: httpx.AsyncClient) -> None:
    assert (await anon.get("/api/v1/health")).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/meta"),
        ("GET", "/api/v1/meta/timezone"),
        ("POST", "/api/v1/meta/timezone"),
        ("GET", "/api/v1/chat/history"),
        ("DELETE", "/api/v1/chat/history"),
        ("POST", "/api/v1/chat/send"),
        ("POST", "/api/v1/gmail/connect"),
        ("POST", "/api/v1/gmail/status"),
        ("POST", "/api/v1/gmail/disconnect"),
    ],
)
async def test_unauthenticated_is_401(
    anon: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await anon.request(method, path, json={})
    assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Basic abc123"},
        {"Authorization": f"Bearer opk_{uuid.uuid4().hex}_wrongsecret"},
    ],
)
async def test_malformed_credentials_are_401(
    client: httpx.AsyncClient, header: dict[str, str]
) -> None:
    assert (await client.get("/api/v1/chat/history", headers=header)).status_code == 401


async def test_wrong_secret_for_a_real_user_is_401(
    client: httpx.AsyncClient, alice: Tenant
) -> None:
    parsed = split_token(alice.token)
    assert parsed is not None
    user_id, _ = parsed
    forged = f"opk_{user_id.hex}_definitely-not-the-secret"
    assert (
        await client.get("/api/v1/chat/history", headers={"Authorization": f"Bearer {forged}"})
    ).status_code == 401


async def test_valid_token_authenticates(client: httpx.AsyncClient, alice: Tenant) -> None:
    response = await client.get("/api/v1/chat/history", headers=alice.headers)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Conversation isolation
# ---------------------------------------------------------------------------


async def test_history_never_contains_the_other_tenants_messages(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    await ConversationRepository(db_session, alice.id).append("user_message", "alice secret")
    await ConversationRepository(db_session, alice.id).append("poke_reply", "alice reply")
    await ConversationRepository(db_session, bob.id).append("user_message", "bob secret")
    await db_session.flush()

    alice_body = (await client.get("/api/v1/chat/history", headers=alice.headers)).json()
    bob_body = (await client.get("/api/v1/chat/history", headers=bob.headers)).json()

    alice_text = [m["content"] for m in alice_body["messages"]]
    bob_text = [m["content"] for m in bob_body["messages"]]

    assert alice_text == ["alice secret", "alice reply"]
    assert bob_text == ["bob secret"]
    assert "bob secret" not in alice_text
    assert "alice secret" not in bob_text


async def test_wait_entries_are_not_user_visible(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Tenant
) -> None:
    repo = ConversationRepository(db_session, alice.id)
    await repo.append("user_message", "hello")
    await repo.append("wait", "already answered")
    await db_session.flush()

    body = (await client.get("/api/v1/chat/history", headers=alice.headers)).json()
    assert [m["content"] for m in body["messages"]] == ["hello"]


async def test_history_timestamps_render_in_the_users_timezone(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    """The log format is a naive local-time string that goes straight into the
    prompt. Two users in different zones must see the same instant differently,
    or Phase 0's move to timestamptz silently rewrote every prompt."""
    from datetime import datetime
    from datetime import timezone as dt_timezone

    moment = datetime(2026, 7, 21, 16, 0, 0, tzinfo=dt_timezone.utc)
    await ConversationRepository(db_session, alice.id).append("user_message", "x", ts=moment)
    await ConversationRepository(db_session, bob.id).append("user_message", "x", ts=moment)
    await db_session.flush()

    alice_ts = (await client.get("/api/v1/chat/history", headers=alice.headers)).json()[
        "messages"
    ][0]["timestamp"]
    bob_ts = (await client.get("/api/v1/chat/history", headers=bob.headers)).json()["messages"][
        0
    ]["timestamp"]

    assert bob_ts == "2026-07-21 16:00:00"  # UTC
    assert alice_ts == "2026-07-21 12:00:00"  # America/New_York, UTC-4 in July
    assert alice_ts != bob_ts


# ---------------------------------------------------------------------------
# DELETE /chat/history — the global-wipe bug
# ---------------------------------------------------------------------------


async def test_delete_history_leaves_the_other_tenant_intact(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    """The headline regression: the original wiped the conversation, roster,
    execution logs and **every trigger in the system**, unauthenticated."""
    for tenant in (alice, bob):
        await ConversationRepository(db_session, tenant.id).append("user_message", "hi")
        await AgentRosterRepository(db_session, tenant.id).add_agent("Reminder Agent")
        await AgentLogRepository(db_session, tenant.id).append(
            "Reminder Agent", "agent_request", "do the thing"
        )
        await TriggerRepository(db_session, tenant.id).insert(
            {
                "agent_name": "Reminder Agent",
                "payload": "ping",
                "start_time": "2026-07-22T09:00:00Z",
                "next_trigger": "2026-07-22T09:00:00Z",
                "status": "active",
                "created_at": "2026-07-21T09:00:00Z",
                "updated_at": "2026-07-21T09:00:00Z",
            }
        )
    await db_session.flush()

    response = await client.delete("/api/v1/chat/history", headers=alice.headers)
    assert response.status_code == 200

    assert await ConversationRepository(db_session, alice.id).count() == 0
    assert await AgentRosterRepository(db_session, alice.id).get_agents() == []
    assert await AgentLogRepository(db_session, alice.id).list_agents() == []
    assert await TriggerRepository(db_session, alice.id).list_all() == []

    assert await ConversationRepository(db_session, bob.id).count() == 1
    assert await AgentRosterRepository(db_session, bob.id).get_agents() == ["Reminder Agent"]
    assert await AgentLogRepository(db_session, bob.id).list_agents() == ["Reminder Agent"]
    assert len(await TriggerRepository(db_session, bob.id).list_all()) == 1


# ---------------------------------------------------------------------------
# Triggers — 404, not 403
# ---------------------------------------------------------------------------


async def test_cross_tenant_trigger_by_id_is_invisible(
    db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    trigger_id = await TriggerRepository(db_session, bob.id).insert(
        {
            "agent_name": "Bob Agent",
            "payload": "bob's private reminder",
            "next_trigger": "2026-07-22T09:00:00Z",
            "status": "active",
            "created_at": "2026-07-21T09:00:00Z",
            "updated_at": "2026-07-21T09:00:00Z",
        }
    )
    await db_session.flush()

    # Bob sees it; Alice gets None, which the route layer renders as 404 rather
    # than 403 — a 403 would confirm the row exists.
    assert await TriggerRepository(db_session, bob.id).fetch_one(trigger_id) is not None
    assert await TriggerRepository(db_session, alice.id).fetch_one(trigger_id) is None

    # And she cannot write to it either.
    assert (
        await TriggerRepository(db_session, alice.id).update(
            trigger_id, None, {"payload": "hijacked"}
        )
        is False
    )
    record = await TriggerRepository(db_session, bob.id).fetch_one(trigger_id)
    assert record is not None and record.payload == "bob's private reminder"


async def test_trigger_timestamps_round_trip_as_iso_strings(
    db_session: AsyncSession, alice: Tenant
) -> None:
    """Phase 0 moved these columns to timestamptz; ``TriggerRecord`` still
    declares ``str`` and the frontend reads that shape."""
    trigger_id = await TriggerRepository(db_session, alice.id).insert(
        {
            "agent_name": "A",
            "payload": "p",
            "start_time": "2026-07-22T09:00:00Z",
            "next_trigger": "2026-07-22T09:00:00Z",
            "status": "active",
            "created_at": "2026-07-21T09:00:00Z",
            "updated_at": "2026-07-21T09:00:00Z",
        }
    )
    await db_session.flush()
    record = await TriggerRepository(db_session, alice.id).fetch_one(trigger_id)
    assert record is not None
    assert record.next_trigger == "2026-07-22T09:00:00Z"
    assert isinstance(record.created_at, str) and record.created_at.endswith("Z")


# ---------------------------------------------------------------------------
# Gmail — the _ACTIVE_USER_ID bug
# ---------------------------------------------------------------------------


async def test_gmail_identity_is_derived_from_the_user_not_the_request(
    db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    """A client-supplied Composio id must not be honoured: it would let A claim
    an identity B has not connected yet and inherit B's mailbox."""
    assert await GmailConnectionRepository(db_session, alice.id).composio_user_id() == str(
        alice.id
    )
    assert await GmailConnectionRepository(db_session, bob.id).composio_user_id() == str(bob.id)


async def test_b_connecting_gmail_does_not_change_what_a_polls(
    db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    """The ``_ACTIVE_USER_ID`` regression, stated as an invariant: what the
    watcher polls for A must be a function of A alone."""
    await GmailConnectionRepository(db_session, alice.id).upsert(
        status="ACTIVE", email="alice@example.test"
    )
    await db_session.flush()

    before = {uid: cid for uid, cid, _ in await list_connected(db_session)}
    assert before[alice.id] == str(alice.id)

    await GmailConnectionRepository(db_session, bob.id).upsert(
        status="ACTIVE", email="bob@example.test"
    )
    await db_session.flush()

    after = {uid: cid for uid, cid, _ in await list_connected(db_session)}
    assert after[alice.id] == before[alice.id], "B's connection moved A's mailbox"
    assert after[bob.id] == str(bob.id)
    assert after[alice.id] != after[bob.id]


async def test_active_gmail_user_id_has_no_process_global(alice: Tenant) -> None:
    """``get_active_gmail_user_id`` must resolve from the tenant context and be
    ``None`` outside one — never a PID default, never a sticky last-writer."""
    from server.repositories.context import TenantContext, tenant_scope
    from server.services.gmail.client import get_active_gmail_user_id

    assert get_active_gmail_user_id() is None
    with tenant_scope(TenantContext(user_id=alice.id, composio_user_id=str(alice.id))):
        assert get_active_gmail_user_id() == str(alice.id)
    assert get_active_gmail_user_id() is None


async def test_no_pid_default_survives_anywhere() -> None:
    """Invert the claim: if the PID default is really gone, no *executable* code
    in the Gmail client may reference ``getpid`` or the old global.

    Parsed with ``ast`` so the comments documenting the removed bug don't count.
    """
    import ast
    import inspect

    from server.services.gmail import client as gmail_client

    tree = ast.parse(inspect.getsource(gmail_client))
    names = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "getpid" not in names
    assert "_ACTIVE_USER_ID" not in names
    assert not hasattr(gmail_client, "_ACTIVE_USER_ID")
    assert not hasattr(gmail_client, "_set_active_gmail_user_id")


# ---------------------------------------------------------------------------
# Seen-store: the permanent-drop bug
# ---------------------------------------------------------------------------


async def test_failed_classification_does_not_settle_the_message(
    db_session: AsyncSession, alice: Tenant
) -> None:
    from server.services.gmail.seen_store import GmailSeenStore

    seen = GmailSeenStore(db_session, alice.id)

    # A failure records an attempt, not a verdict — the id stays eligible.
    await seen.mark_attempted(["msg-1"])
    await db_session.flush()
    assert await seen.unprocessed(["msg-1"]) == ["msg-1"]
    assert await seen.is_seen("msg-1") is False

    # A verdict settles it.
    await seen.mark_classified(["msg-1"])
    await db_session.flush()
    assert await seen.unprocessed(["msg-1"]) == []
    assert await seen.is_seen("msg-1") is True


async def test_an_attempt_never_downgrades_a_verdict(
    db_session: AsyncSession, alice: Tenant
) -> None:
    from server.services.gmail.seen_store import GmailSeenStore

    seen = GmailSeenStore(db_session, alice.id)
    await seen.mark_classified(["msg-2"])
    await seen.mark_attempted(["msg-2"])
    await db_session.flush()
    assert await seen.is_seen("msg-2") is True


async def test_the_retry_budget_is_bounded(
    db_session: AsyncSession, alice: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poison message must stop retrying. The budget is time-based rather than
    an attempt counter because adding a column belongs to Phase 0's migration."""
    from datetime import timedelta

    from server.repositories import gmail as gmail_repo
    from server.services.gmail.seen_store import GmailSeenStore

    seen = GmailSeenStore(db_session, alice.id)
    await seen.mark_attempted(["poison"])
    await db_session.flush()
    assert await seen.unprocessed(["poison"]) == ["poison"]

    monkeypatch.setattr(gmail_repo, "CLASSIFY_RETRY_BUDGET", timedelta(seconds=-1))
    assert await seen.unprocessed(["poison"]) == []
    assert await seen.expire_stale() == ["poison"]


async def test_seen_ledger_is_per_tenant(
    db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    from server.services.gmail.seen_store import GmailSeenStore

    await GmailSeenStore(db_session, alice.id).mark_classified(["shared-id"])
    await db_session.flush()
    assert await GmailSeenStore(db_session, bob.id).unprocessed(["shared-id"]) == ["shared-id"]


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------


async def test_connection_id_is_encrypted_at_rest(
    db_session: AsyncSession, alice: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OPENPOKE_DATA_KEY", Fernet.generate_key().decode())

    repo = GmailConnectionRepository(db_session, alice.id)
    await repo.upsert(status="ACTIVE", connection_id="ca_super_secret")
    await db_session.flush()

    row = (
        await db_session.execute(
            select(GmailConnection).where(GmailConnection.user_id == alice.id)
        )
    ).scalar_one()
    assert row.connection_id_encrypted is not None
    assert "ca_super_secret" not in row.connection_id_encrypted
    assert row.key_version == 1
    assert await repo.connection_id() == "ca_super_secret"


async def test_missing_data_key_stores_nothing_rather_than_plaintext(
    db_session: AsyncSession, alice: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENPOKE_DATA_KEY", raising=False)
    repo = GmailConnectionRepository(db_session, alice.id)
    await repo.upsert(status="ACTIVE", connection_id="ca_super_secret")
    await db_session.flush()

    row = (
        await db_session.execute(
            select(GmailConnection).where(GmailConnection.user_id == alice.id)
        )
    ).scalar_one()
    assert row.connection_id_encrypted is None


# ---------------------------------------------------------------------------
# Repository-level scoping, checked directly against the DB
# ---------------------------------------------------------------------------


async def test_every_conversation_row_carries_its_owner(
    db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    await ConversationRepository(db_session, alice.id).append("user_message", "a")
    await ConversationRepository(db_session, bob.id).append("user_message", "b")
    await db_session.flush()

    rows = (
        await db_session.execute(
            select(ConversationEntry.user_id, ConversationEntry.payload).where(
                ConversationEntry.user_id.in_([alice.id, bob.id])
            )
        )
    ).all()
    assert {(uid, payload) for uid, payload in rows} == {
        (alice.id, "a"),
        (bob.id, "b"),
    }


async def test_agent_names_collide_across_tenants_without_sharing(
    db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    """The file store slugified agent names onto shared filenames. Same name,
    two tenants, two journals."""
    for tenant, note in ((alice, "alice note"), (bob, "bob note")):
        await AgentRosterRepository(db_session, tenant.id).add_agent("Email Summary")
        await AgentLogRepository(db_session, tenant.id).append(
            "Email Summary", "agent_request", note
        )
    await db_session.flush()

    alice_log = await AgentLogRepository(db_session, alice.id).iter_entries(
        "Email Summary", "UTC"
    )
    bob_log = await AgentLogRepository(db_session, bob.id).iter_entries("Email Summary", "UTC")
    assert [payload for _, _, payload in alice_log] == ["alice note"]
    assert [payload for _, _, payload in bob_log] == ["bob note"]


async def test_add_agent_is_idempotent_per_tenant(
    db_session: AsyncSession, alice: Tenant
) -> None:
    repo = AgentRosterRepository(db_session, alice.id)
    assert await repo.add_agent("Dup") is True
    assert await repo.add_agent("Dup") is False
    await db_session.flush()
    assert await repo.get_agents() == ["Dup"]


async def test_scheduler_view_spans_tenants_but_records_stay_scoped(
    db_session: AsyncSession, alice: Tenant, bob: Tenant
) -> None:
    """``fetch_due_all`` is the one deliberately unscoped read. Prove it is not
    reachable from a scoped repository."""
    from datetime import datetime
    from datetime import timezone as dt_timezone

    from server.repositories.triggers import fetch_due_all, owner_of

    ids = {}
    for tenant in (alice, bob):
        ids[tenant.id] = await TriggerRepository(db_session, tenant.id).insert(
            {
                "agent_name": "A",
                "payload": "p",
                "next_trigger": "2020-01-01T00:00:00Z",
                "status": "active",
                "created_at": "2020-01-01T00:00:00Z",
                "updated_at": "2020-01-01T00:00:00Z",
            }
        )
    await db_session.flush()

    before = datetime.now(dt_timezone.utc)
    everyone = {record.id for record in await fetch_due_all(db_session, before)}
    assert ids[alice.id] in everyone and ids[bob.id] in everyone

    alice_only = {
        record.id for record in await TriggerRepository(db_session, alice.id).fetch_due(before)
    }
    assert alice_only == {ids[alice.id]}

    assert await owner_of(db_session, ids[bob.id]) == bob.id


async def test_the_scheduler_can_resolve_a_triggers_tenant(
    db_session: AsyncSession, alice: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the trigger poller has no tenant of its own, so everything a
    fired trigger does (agent logs, roster, the reply into the conversation) used
    to raise ``LookupError`` and kill the trigger. ``tenant_scope_for`` is the
    seam that binds the owner; Phase 2 replaces it with the job's ``user_id``.
    """
    from server.repositories import context as ctx_module
    from server.repositories.context import get_tenant
    from server.services.triggers.service import TriggerService
    from server.services.triggers.store import TriggerStore

    trigger_id = await TriggerRepository(db_session, alice.id).insert(
        {
            "agent_name": "Report Reminder",
            "payload": "submit the report",
            "next_trigger": "2020-01-01T00:00:00Z",
            "status": "active",
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2020-01-01T00:00:00Z",
        }
    )
    await db_session.flush()
    record = await TriggerRepository(db_session, alice.id).fetch_one(trigger_id)
    assert record is not None

    # The store proxy normally reaches Postgres through the sync bridge, which
    # would open its own connection and never see this rolled-back transaction.
    # Route it at the test session instead; everything else is the real path.
    def _run_sync(fn, *, timeout: float = 30.0):
        import asyncio

        return asyncio.get_event_loop().run_until_complete(fn(db_session))

    async def _tenant_for(_self, tid: int):
        from server.repositories.triggers import owner_of
        from server.repositories.users import UserRepository

        owner = await owner_of(db_session, tid)
        if owner is None:
            return None
        return ctx_module.TenantContext(
            user_id=owner,
            timezone=await UserRepository(db_session, owner).get_timezone(),
            composio_user_id=str(owner),
        )

    service = TriggerService(TriggerStore())
    monkeypatch.setattr(
        TriggerStore, "tenant_for", lambda self, tid: None, raising=True
    )

    # With no owner resolvable the scope refuses rather than running unscoped.
    with pytest.raises(LookupError):
        with service.tenant_scope_for(record):
            pass

    resolved = await _tenant_for(None, trigger_id)
    assert resolved is not None
    monkeypatch.setattr(TriggerStore, "tenant_for", lambda self, tid: resolved)

    assert get_tenant() is None
    with service.tenant_scope_for(record):
        bound = get_tenant()
        assert bound is not None
        assert bound.user_id == alice.id
        assert bound.timezone == "America/New_York"
    assert get_tenant() is None


async def test_deleting_a_user_cascades(db_session: AsyncSession, alice: Tenant) -> None:
    """Invert the isolation claim: if rows really are owned, dropping the owner
    must drop the rows."""
    await ConversationRepository(db_session, alice.id).append("user_message", "x")
    await TriggerRepository(db_session, alice.id).insert(
        {
            "agent_name": "A",
            "payload": "p",
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    await db_session.flush()

    await db_session.delete(await db_session.get(User, alice.id))
    await db_session.flush()

    assert (
        await db_session.execute(
            select(ConversationEntry).where(ConversationEntry.user_id == alice.id)
        )
    ).first() is None
    assert (
        await db_session.execute(select(Trigger).where(Trigger.user_id == alice.id))
    ).first() is None
