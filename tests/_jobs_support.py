"""Shared helpers for the Phase 2 durability tests.

These tests cannot use ``conftest.py``'s ``db_session``. That fixture runs
everything inside one outer transaction that is rolled back, which is exactly
right for isolation and exactly wrong here: ``SKIP LOCKED`` is a claim about what
*concurrent connections* observe, and a test that shares one connection proves
nothing about it. Every claimer below gets its own session on its own connection,
and every write is really committed.

The price is that cleanup is explicit. Each test owns a fresh user and deletes
the whole ``jobs`` table on entry — the compose Postgres is dedicated to this
suite, and no other test in the tree commits job rows.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture(loop_scope="session")
async def sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    """A sessionmaker over the session engine. Real connections, real commits."""
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _no_worker_outlives_its_test() -> AsyncIterator[None]:
    """``claim()`` is global by design — it has no ``WHERE user_id``, because a
    worker services every tenant. That makes a leaked ``Worker`` from one test
    invisible poison for the next: it claims and runs the next test's jobs with
    the *previous* test's handler. This fixture makes that failure loud and
    immediate instead of a confusing count mismatch three tests later.
    """
    yield
    leaked = [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and task.get_name().startswith(("job-", "worker-run", "trigger-scheduler"))
    ]
    for task in leaked:
        task.cancel()
    assert not leaked, f"tasks outlived the test: {[t.get_name() for t in leaked]}"


@pytest_asyncio.fixture(loop_scope="session")
async def user_id(sessionmaker) -> AsyncIterator[uuid.UUID]:
    """A committed tenant, plus a clean queue. Dropped afterwards (CASCADE)."""
    from server.repositories.users import create_user

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM llm_usage"))
        await session.execute(text("DELETE FROM jobs"))
        user, _token = await create_user(
            session, email=f"phase2-{uuid.uuid4().hex[:12]}@test.invalid", timezone="UTC"
        )
        await session.commit()
        uid = user.id

    yield uid

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
        await session.commit()


@asynccontextmanager
async def running(worker, *, shutdown_timeout: float = 90.0):
    """Run a Worker for the duration of the block, then guarantee it is gone.

    Named so ``_no_worker_outlives_its_test`` can see it, and hard-cancelled if a
    graceful stop does not finish — a hung shutdown must fail this test, not the
    next one.

    The timeout is generous because conftest's engine uses ``NullPool``: every
    session is a fresh physical connection, so draining tens of queued jobs is
    dominated by connection setup, not by the handlers. A tighter bound was
    flaky when these files run back to back.
    """
    task = asyncio.create_task(worker.run(), name=f"worker-run-{worker.worker_id}")
    try:
        yield task
    finally:
        worker.stop()
        try:
            await asyncio.wait_for(task, timeout=shutdown_timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise AssertionError(
                f"worker {worker.worker_id} did not stop cleanly"
            ) from exc


async def wait_for(predicate, *, timeout: float = 30.0, message: str = "condition"):
    """Poll *predicate* until true. Returns its last value."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


async def job_statuses(sessionmaker, user_id: uuid.UUID) -> dict[str, int]:
    """``status -> count`` for one tenant's jobs."""
    async with sessionmaker() as session:
        rows = await session.execute(
            text("SELECT status, count(*) FROM jobs WHERE user_id = :uid GROUP BY status"),
            {"uid": user_id},
        )
        return {status: int(count) for status, count in rows.all()}


async def seed_jobs(
    sessionmaker, user_id: uuid.UUID, count: int, *, kind: str = "test_noop"
) -> list[uuid.UUID]:
    """Insert *count* immediately-due jobs and return their ids."""
    from server.jobs import enqueue

    ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for i in range(count):
            job = await enqueue(session, user_id=user_id, kind=kind, payload={"n": i})
            assert job is not None
            ids.append(job.id)
        await session.commit()
    return ids
