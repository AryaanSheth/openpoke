"""Durability: work survives a crashed worker, retries with backoff, and is
buried rather than lost when it cannot succeed.

Real Postgres, real commits, real ``Worker`` instances — the point of the phase
is what happens across process death, and an in-memory queue cannot fail that way.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from server.jobs import KIND_CHAT_TURN, enqueue
from server.jobs.worker import Worker
from tests._jobs_support import running, wait_for

# Fixtures (sessionmaker, user_id, the leaked-worker guard) come from here.
# Registered as a plugin rather than imported: importing a fixture shadows the
# argument name in every test that requests it, which is what ruff F811 flags.
pytest_plugins = ("tests._jobs_support",)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch):
    """Zero the retry backoff. The backoff itself is asserted separately."""
    from server.config import get_settings

    monkeypatch.setenv("OPENPOKE_JOB_BACKOFF_BASE_S", "0")
    monkeypatch.setenv("OPENPOKE_JOB_BACKOFF_MAX_S", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture(loop_scope="session")
async def enqueue_job(sessionmaker, user_id):
    async def _enqueue(kind: str = KIND_CHAT_TURN, *, max_attempts: int = 5, **payload):
        async with sessionmaker() as session:
            job = await enqueue(
                session,
                user_id=user_id,
                kind=kind,
                payload=payload,
                max_attempts=max_attempts,
            )
            await session.commit()
            assert job is not None
            return job.id

    return _enqueue


async def _row(sessionmaker, job_id: uuid.UUID) -> dict:
    async with sessionmaker() as session:
        row = await session.execute(
            text(
                "SELECT status, attempts, max_attempts, claimed_by, last_error "
                "FROM jobs WHERE id = :i"
            ),
            {"i": job_id},
        )
        return dict(row.mappings().one())


async def _run_until(worker: Worker, predicate, timeout: float = 30.0) -> None:
    """Drive a worker until *predicate* holds, then guarantee it is stopped."""
    async with running(worker):
        await wait_for(predicate, timeout=timeout, message="job to reach its final state")


# ---------------------------------------------------------------------------


async def test_handler_failing_twice_then_succeeding_runs_exactly_three_attempts(
    sessionmaker, user_id, enqueue_job
):
    """attempts == 3, and the successful side effect happens exactly once.

    The "exactly once" half is the one that matters: retry that re-runs a
    partially-succeeded handler is not a fix, it is a different bug.
    """
    side_effects: list[int] = []
    calls = {"n": 0}

    async def handler(job):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError(f"transient failure {calls['n']}")
        side_effects.append(calls["n"])

    job_id = await enqueue_job()
    worker = Worker(
        handlers={KIND_CHAT_TURN: handler},
        worker_id="retry-worker",
        concurrency=2,
        poll_interval_s=0.02,
        sessionmaker=sessionmaker,
    )

    async def done():
        return (await _row(sessionmaker, job_id))["status"] == "done"

    await _run_until(worker, done)

    row = await _row(sessionmaker, job_id)
    assert row["status"] == "done"
    assert row["attempts"] == 3, row
    assert side_effects == [3], "the successful handler ran more than once"


async def test_exhausted_retries_end_as_dead_not_missing(sessionmaker, user_id, enqueue_job):
    """A permanently failing job reaches 'dead' with its error, and stops being
    claimed. Deleting it instead is how a broken handler gets discovered by a
    customer rather than a dashboard."""
    attempts = {"n": 0}

    async def always_fails(job):
        attempts["n"] += 1
        raise RuntimeError("permanent failure")

    job_id = await enqueue_job(max_attempts=3)
    worker = Worker(
        handlers={KIND_CHAT_TURN: always_fails},
        worker_id="dead-worker",
        concurrency=2,
        poll_interval_s=0.02,
        sessionmaker=sessionmaker,
    )

    async def dead():
        return (await _row(sessionmaker, job_id))["status"] == "dead"

    await _run_until(worker, dead)

    row = await _row(sessionmaker, job_id)
    assert row["status"] == "dead"
    assert row["attempts"] == 3
    assert "permanent failure" in (row["last_error"] or "")
    assert attempts["n"] == 3, "a dead job was claimed again"


async def test_killed_worker_leaves_a_job_the_reaper_recovers(sessionmaker, user_id, enqueue_job):
    """Enqueue -> kill the worker mid-job -> reaper returns it -> a new worker
    completes it.

    Without the reaper the row stays 'running' with a stale claimed_at forever:
    the claim query only sees 'pending', so nothing ever picks it up again and
    the work is silently gone.
    """
    started = asyncio.Event()
    completed: list[str] = []

    async def hangs(job):
        started.set()
        await asyncio.sleep(3600)

    async def succeeds(job):
        completed.append("ok")

    job_id = await enqueue_job()

    victim = Worker(
        handlers={KIND_CHAT_TURN: hangs},
        worker_id="victim",
        concurrency=2,
        poll_interval_s=0.02,
        reap_interval_s=3600,  # its own reaper must not save it
        sessionmaker=sessionmaker,
    )
    victim_task = asyncio.create_task(victim.run(), name="worker-run-victim")
    await asyncio.wait_for(started.wait(), timeout=10)

    # SIGKILL analogue: cancel without draining, so the in-flight handler never
    # reports and the row stays 'running'.
    victim_task.cancel()
    await asyncio.gather(victim_task, return_exceptions=True)

    row = await _row(sessionmaker, job_id)
    assert row["status"] == "running"
    assert row["claimed_by"] == "victim"

    # The reaper is what makes the row claimable again. Without this line the
    # job stays 'running' forever and the assertions below time out.
    from server.jobs import reap

    async with sessionmaker() as session:
        assert await reap(session, stale_after_s=0, exclude_worker="rescuer") == 1
        await session.commit()

    assert (await _row(sessionmaker, job_id))["status"] == "pending"

    rescuer = Worker(
        handlers={KIND_CHAT_TURN: succeeds},
        worker_id="rescuer",
        concurrency=2,
        poll_interval_s=0.02,
        reap_interval_s=3600,
        sessionmaker=sessionmaker,
    )

    async def done():
        return (await _row(sessionmaker, job_id))["status"] == "done"

    await _run_until(rescuer, done)

    row = await _row(sessionmaker, job_id)
    assert row["status"] == "done"
    assert row["claimed_by"] is None
    assert completed == ["ok"]
    assert row["attempts"] == 2, "the recovered job should be on its second attempt"


async def test_reaper_buries_a_job_that_already_exhausted_its_attempts(
    sessionmaker, user_id, enqueue_job
):
    """A handler that kills its worker every time must not be an infinite crash
    loop: fail() never gets to run, so the reaper has to make the 'dead'
    decision itself."""
    from server.jobs import claim, reap

    job_id = await enqueue_job(max_attempts=1)

    async with sessionmaker() as session:
        claimed = await claim(session, worker_id="doomed", batch=1)
        await session.commit()
    assert [j.id for j in claimed] == [job_id]

    async with sessionmaker() as session:
        assert await reap(session, stale_after_s=0) == 1
        await session.commit()

    row = await _row(sessionmaker, job_id)
    assert row["status"] == "dead"
    assert "reaped" in (row["last_error"] or "")


async def test_a_worker_never_reaps_its_own_in_flight_job(sessionmaker, user_id, enqueue_job):
    """The self-reap regression, pinned.

    With ``stale_after_s=0`` and a fast reap loop, a worker used to reap the job
    it was itself still running, re-claim it, and execute the handler twice — a
    duplicate side effect produced entirely by the recovery mechanism.
    """
    runs: list[int] = []
    release = asyncio.Event()

    async def slow(job):
        runs.append(1)
        await release.wait()

    job_id = await enqueue_job()
    worker = Worker(
        handlers={KIND_CHAT_TURN: slow},
        worker_id="self-reaper",
        concurrency=2,
        poll_interval_s=0.01,
        reap_interval_s=0.01,
        stale_after_s=0,
        sessionmaker=sessionmaker,
    )
    try:
        async with running(worker):
            await asyncio.sleep(0.4)  # many reap cycles
            assert runs == [1], f"the worker reaped and re-ran its own job: {runs}"
            assert (await _row(sessionmaker, job_id))["status"] == "running"
            release.set()
    finally:
        release.set()


async def test_backoff_pushes_run_at_into_the_future(sessionmaker, user_id, enqueue_job):
    """With a real backoff base, a failed job is not immediately re-claimable —
    otherwise a failing handler spins the worker at full speed."""
    from server.jobs import claim, fail

    job_id = await enqueue_job()

    async with sessionmaker() as session:
        await claim(session, worker_id="w", batch=1)
        await session.commit()

    async with sessionmaker() as session:
        await fail(session, job_id, "boom", backoff_base_s=60.0, backoff_max_s=600.0)
        await session.commit()

    async with sessionmaker() as session:
        run_at_future = await session.scalar(
            text("SELECT run_at > now() FROM jobs WHERE id = :i"), {"i": job_id}
        )
        assert run_at_future is True
        again = await claim(session, worker_id="w2", batch=10)
        await session.commit()
    assert job_id not in [j.id for j in again]


async def test_unknown_kind_fails_the_job_rather_than_the_worker(
    sessionmaker, user_id, enqueue_job
):
    """A job whose kind has no handler must not stall the loop or vanish."""
    job_id = await enqueue_job("no_such_kind", max_attempts=1)
    worker = Worker(
        handlers={},
        worker_id="empty-registry",
        concurrency=2,
        poll_interval_s=0.02,
        sessionmaker=sessionmaker,
    )

    async def dead():
        return (await _row(sessionmaker, job_id))["status"] == "dead"

    await _run_until(worker, dead)
    assert "no handler registered" in (await _row(sessionmaker, job_id))["last_error"]


async def test_worker_binds_the_jobs_tenant_before_dispatch(sessionmaker, user_id, enqueue_job):
    """The invariant Phase 1 paid for in production: a repository write inside a
    handler must find a tenant bound, or it raises LookupError and every
    recurring reminder dies."""
    from server.repositories.context import require_tenant

    seen: list[uuid.UUID] = []

    async def handler(job):
        seen.append(require_tenant().user_id)

    job_id = await enqueue_job()
    worker = Worker(
        handlers={KIND_CHAT_TURN: handler},
        worker_id="tenant-worker",
        concurrency=2,
        poll_interval_s=0.02,
        sessionmaker=sessionmaker,
    )

    async def done():
        return (await _row(sessionmaker, job_id))["status"] == "done"

    await _run_until(worker, done)
    assert seen == [user_id]
