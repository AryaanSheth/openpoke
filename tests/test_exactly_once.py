"""Exactly-once, proven under real concurrency against real Postgres.

A mocked exactly-once test proves nothing: the property being asserted *is* the
behaviour of ``FOR UPDATE SKIP LOCKED`` under concurrent connections, so
replacing the database replaces the thing under test. Everything here runs
against the compose Postgres with one connection per claimer.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from server.jobs import KIND_TRIGGER_FIRE, claim, dedupe_key_for, enqueue
from tests._jobs_support import seed_jobs

# Fixtures (sessionmaker, user_id, the leaked-worker guard) come from here.
# Registered as a plugin rather than imported: importing a fixture shadows the
# argument name in every test that requests it, which is what ruff F811 flags.
pytest_plugins = ("tests._jobs_support",)


async def test_eight_concurrent_claimers_partition_one_hundred_jobs(sessionmaker, user_id):
    """8 claimers, 100 due jobs: every job claimed exactly once, union == 100.

    The failure this guards against is a double claim, which is invisible without
    concurrency — a sequential test passes against a plain SELECT-then-UPDATE.
    """
    expected = set(await seed_jobs(sessionmaker, user_id, 100))

    async def claimer(worker: str) -> list[uuid.UUID]:
        taken: list[uuid.UUID] = []
        while True:
            async with sessionmaker() as session:
                jobs = await claim(session, worker_id=worker, batch=7)
                await session.commit()
            if not jobs:
                return taken
            taken.extend(job.id for job in jobs)
            await asyncio.sleep(0)  # yield, so the claimers really interleave

    results = await asyncio.gather(*(claimer(f"w{i}") for i in range(8)))

    claimed = [job_id for batch in results for job_id in batch]
    assert len(claimed) == 100, "a job was claimed more than once"
    assert set(claimed) == expected
    assert len(set(claimed)) == len(claimed)

    # And every claimer got some work — otherwise SKIP LOCKED degenerated into
    # serialisation and the test would pass for the wrong reason.
    assert sum(1 for batch in results if batch) >= 2


async def test_dedupe_key_collision_is_a_no_op(sessionmaker, user_id):
    """The second enqueue with a used dedupe_key returns None and inserts nothing."""
    key = dedupe_key_for(user_id, "trigger", 42, "2026-07-21T09:00:00Z")

    async with sessionmaker() as session:
        first = await enqueue(
            session, user_id=user_id, kind=KIND_TRIGGER_FIRE, payload={}, dedupe_key=key
        )
        await session.commit()
    assert first is not None

    async with sessionmaker() as session:
        second = await enqueue(
            session, user_id=user_id, kind=KIND_TRIGGER_FIRE, payload={}, dedupe_key=key
        )
        await session.commit()
    assert second is None

    async with sessionmaker() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM jobs WHERE dedupe_key = :k"), {"k": key}
        )
    assert count == 1


async def test_concurrent_enqueue_of_the_same_key_yields_one_job(sessionmaker, user_id):
    """Two pollers racing on one occurrence produce one job, not two.

    ON CONFLICT DO NOTHING, not check-then-insert: the loser's INSERT blocks on
    the winner's uncommitted row and then affects zero rows.
    """
    key = dedupe_key_for(user_id, "trigger", 7, "2026-07-21T10:00:00Z")

    async def attempt():
        async with sessionmaker() as session:
            job = await enqueue(
                session, user_id=user_id, kind=KIND_TRIGGER_FIRE, payload={}, dedupe_key=key
            )
            await session.commit()
            return job

    jobs = await asyncio.gather(*(attempt() for _ in range(6)))
    assert sum(1 for job in jobs if job is not None) == 1


async def test_dedupe_keys_are_namespaced_per_tenant(sessionmaker, user_id):
    """``jobs.dedupe_key`` is globally unique, so two tenants building the same
    logical key must not collide. This is Phase 0 note 6, made executable."""
    other = uuid.uuid4()
    mine = dedupe_key_for(user_id, "email_poll", "2026-07-21")
    theirs = dedupe_key_for(other, "email_poll", "2026-07-21")
    assert mine != theirs


async def test_two_schedulers_on_one_database_produce_one_job_per_occurrence(
    sessionmaker, user_id
):
    """The headline trigger property, with two live TriggerScheduler instances.

    This is the exact scenario the old ``self._in_flight: Set[int]`` could not
    handle — the set is per-process, so a second replica fired every reminder a
    second time.
    """
    from server.db.models import Trigger
    from server.services.trigger_scheduler import TriggerScheduler

    async with sessionmaker() as session:
        trigger = Trigger(
            user_id=user_id,
            agent_name="reminder agent",
            payload="ping",
            start_time=None,
            next_trigger=None,
            recurrence_rule=None,
            timezone="UTC",
            status="active",
        )
        session.add(trigger)
        await session.flush()
        # Due five minutes ago, per the database clock rather than ours.
        await session.execute(
            text("UPDATE triggers SET next_trigger = now() - interval '5 minutes' WHERE id = :i"),
            {"i": trigger.id},
        )
        await session.commit()
        trigger_id = trigger.id

    a = TriggerScheduler(worker_id="sched-a", sessionmaker=sessionmaker)
    b = TriggerScheduler(worker_id="sched-b", sessionmaker=sessionmaker)
    enqueued = await asyncio.gather(a.poll_once(), b.poll_once())

    assert sum(enqueued) == 1, f"both schedulers enqueued: {enqueued}"

    async with sessionmaker() as session:
        rows = await session.execute(
            text("SELECT count(*) FROM jobs WHERE kind = :k AND user_id = :u"),
            {"k": KIND_TRIGGER_FIRE, "u": user_id},
        )
        assert rows.scalar_one() == 1

        # And the schedule advanced: a one-shot trigger is completed, so neither
        # scheduler can pick it up again on the next tick.
        row = await session.get(Trigger, trigger_id)
        assert row is not None
        assert row.status == "completed"
        assert row.next_trigger is None
        assert row.claimed_at is None


async def test_recurring_trigger_advances_and_does_not_refire_the_same_occurrence(
    sessionmaker, user_id
):
    """A recurring trigger produces one job per occurrence, and polling again
    immediately produces none — the schedule moved, the dedupe key changed."""
    from server.db.models import Trigger
    from server.services.trigger_scheduler import TriggerScheduler

    async with sessionmaker() as session:
        trigger = Trigger(
            user_id=user_id,
            agent_name="hourly agent",
            payload="tick",
            # The stored form: TriggerService.build_recurrence always embeds
            # DTSTART. A rule without one is naive and cannot be compared to an
            # aware "now" — see test_malformed_recurrence_pauses_rather_than_wedging.
            recurrence_rule="DTSTART:20260101T000000Z\nRRULE:FREQ=HOURLY",
            timezone="UTC",
            status="active",
        )
        session.add(trigger)
        await session.flush()
        await session.execute(
            text("UPDATE triggers SET next_trigger = now() - interval '1 minute' WHERE id = :i"),
            {"i": trigger.id},
        )
        await session.commit()
        trigger_id = trigger.id

    scheduler = TriggerScheduler(worker_id="sched", sessionmaker=sessionmaker)
    assert await scheduler.poll_once() == 1
    assert await scheduler.poll_once() == 0

    async with sessionmaker() as session:
        row = await session.get(Trigger, trigger_id)
        assert row is not None
        assert row.status == "active"
        assert row.next_trigger is not None
        count = await session.scalar(
            text("SELECT count(*) FROM jobs WHERE user_id = :u AND kind = :k"),
            {"u": user_id, "k": KIND_TRIGGER_FIRE},
        )
        assert count == 1


async def test_malformed_recurrence_pauses_rather_than_wedging_the_poller(
    sessionmaker, user_id
):
    """An unparseable rule must not make the poller re-claim the row every tick.

    Found by writing the test above with a DTSTART-less RRULE, which dateutil
    parses fine and then cannot compare against an aware ``now``. Real triggers
    always carry DTSTART (``TriggerService.build_recurrence``), so this is a
    corrupted-row path — it parks the trigger with the reason recorded.
    """
    from server.db.models import Trigger
    from server.services.trigger_scheduler import TriggerScheduler

    async with sessionmaker() as session:
        trigger = Trigger(
            user_id=user_id,
            agent_name="broken agent",
            payload="tick",
            recurrence_rule="RRULE:FREQ=HOURLY",  # no DTSTART
            timezone="UTC",
            status="active",
        )
        session.add(trigger)
        await session.flush()
        await session.execute(
            text("UPDATE triggers SET next_trigger = now() - interval '1 minute' WHERE id = :i"),
            {"i": trigger.id},
        )
        await session.commit()
        trigger_id = trigger.id

    scheduler = TriggerScheduler(worker_id="sched", sessionmaker=sessionmaker)
    assert await scheduler.poll_once() == 1
    assert await scheduler.poll_once() == 0

    async with sessionmaker() as session:
        row = await session.get(Trigger, trigger_id)
        assert row is not None
        assert row.status == "paused"
        assert row.next_trigger is None
        assert "unparseable recurrence_rule" in (row.last_error or "")


async def test_lease_heartbeat_stops_a_reaper_from_stealing_a_live_job(sessionmaker, user_id):
    """The hostile objection, made runnable.

    ``SKIP LOCKED`` guarantees one *claimant* per claim. It does not guarantee one
    *execution* per job: if a handler outlives ``job_stale_after_s``, a reaper on a
    different worker returns the row to 'pending' while the first handler is still
    running, and the job executes twice. That window is reachable in the shipped
    config — an LLM turn can reach 8 tool iterations x a 60s timeout (480s) against
    a 300s default.

    Deleting ``_heartbeat_loop`` from the worker makes this test fail with runs == 2.
    """
    from server.jobs import KIND_CHAT_TURN, enqueue
    from server.jobs.worker import Worker
    from tests._jobs_support import running

    runs: list[int] = []
    release = asyncio.Event()

    async def slow(job):
        runs.append(1)
        await release.wait()

    async with sessionmaker() as session:
        job = await enqueue(session, user_id=user_id, kind=KIND_CHAT_TURN, payload={})
        await session.commit()
        job_id = job.id

    # Holder runs the job. Its lease would expire after 2s without renewal;
    # the heartbeat interval is stale/3, so it renews at ~1s intervals.
    holder = Worker(
        handlers={KIND_CHAT_TURN: slow},
        worker_id="holder",
        concurrency=1,
        poll_interval_s=0.01,
        reap_interval_s=3600,  # holder never reaps; the thief does
        stale_after_s=2,
        sessionmaker=sessionmaker,
    )
    # A *different* worker, so `exclude_worker` cannot protect the job.
    thief = Worker(
        handlers={KIND_CHAT_TURN: slow},
        worker_id="thief",
        concurrency=1,
        poll_interval_s=0.02,
        reap_interval_s=0.02,
        stale_after_s=2,
        sessionmaker=sessionmaker,
    )
    try:
        async with running(holder):
            await asyncio.sleep(0.3)
            assert runs == [1], "holder never started the job"
            async with running(thief):
                await asyncio.sleep(3.2)  # well past the 2s lease
                assert runs == [1], f"the reaper stole a live job and re-ran it: {runs}"
                async with sessionmaker() as session:
                    owner = await session.scalar(
                        text("SELECT claimed_by FROM jobs WHERE id = :i"), {"i": job_id}
                    )
                assert owner == "holder", f"lease changed hands mid-flight: {owner}"
                release.set()
    finally:
        release.set()
