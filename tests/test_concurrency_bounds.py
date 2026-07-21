"""The concurrency bound is real, not decorative.

The path this replaces had none: ``asyncio.create_task`` per chat turn meant a
burst of 200 requests produced 200 concurrent LLM calls inside the API process.
"""

from __future__ import annotations

import asyncio

from server.jobs import KIND_CHAT_TURN
from server.jobs.worker import Worker
from tests._jobs_support import job_statuses, running, seed_jobs, wait_for

# Fixtures (sessionmaker, user_id, the leaked-worker guard) come from here.
# Registered as a plugin rather than imported: importing a fixture shadows the
# argument name in every test that requests it, which is what ruff F811 flags.
pytest_plugins = ("tests._jobs_support",)


async def _drain_queue(worker: Worker, sessionmaker, user_id, expected: int, timeout=120.0):
    async def drained():
        counts = await job_statuses(sessionmaker, user_id)
        return counts if counts.get("done", 0) >= expected else None

    async with running(worker):
        return await wait_for(drained, timeout=timeout, message=f"{expected} jobs to finish")


async def test_two_hundred_jobs_never_exceed_a_semaphore_of_ten(sessionmaker, user_id):
    """200 jobs, concurrency 10: observed max in-flight <= 10, all 200 complete.

    ``observed`` is counted inside the handler, so it measures what actually ran
    concurrently rather than what the worker believes about itself.
    """
    observed = {"now": 0, "peak": 0}

    async def handler(job):
        observed["now"] += 1
        observed["peak"] = max(observed["peak"], observed["now"])
        try:
            await asyncio.sleep(0.01)  # long enough for overlap to be real
        finally:
            observed["now"] -= 1

    await seed_jobs(sessionmaker, user_id, 200, kind=KIND_CHAT_TURN)

    worker = Worker(
        handlers={KIND_CHAT_TURN: handler},
        worker_id="bounded",
        concurrency=10,
        batch=25,  # deliberately larger than the concurrency
        poll_interval_s=0.01,
        sessionmaker=sessionmaker,
    )
    counts = await _drain_queue(worker, sessionmaker, user_id, expected=200)

    assert counts.get("done") == 200, counts
    assert observed["peak"] <= 10, f"in-flight peaked at {observed['peak']}"
    assert observed["peak"] > 1, "nothing ran concurrently; the test proves nothing"
    assert worker.peak_in_flight <= 10


async def test_claim_batch_is_capped_by_free_slots(sessionmaker, user_id):
    """Claiming more than can be run marks rows 'running' while they queue, and
    the reaper then correctly decides they belong to a dead worker."""
    gate = asyncio.Event()

    async def blocked(job):
        await gate.wait()

    await seed_jobs(sessionmaker, user_id, 50, kind=KIND_CHAT_TURN)

    worker = Worker(
        handlers={KIND_CHAT_TURN: blocked},
        worker_id="capped",
        concurrency=4,
        batch=50,
        poll_interval_s=0.01,
        sessionmaker=sessionmaker,
    )
    try:
        async with running(worker):
            await asyncio.sleep(0.5)
            counts = await job_statuses(sessionmaker, user_id)
            assert counts.get("running", 0) == 4, counts
            assert counts.get("pending", 0) == 46, counts
            gate.set()
    finally:
        gate.set()


async def test_two_workers_split_the_queue_without_overlap(sessionmaker, user_id):
    """Two live Worker instances against one database: 100 jobs, 100 executions,
    no job run twice."""
    executed_a: list[str] = []
    executed_b: list[str] = []

    def make(sink):
        async def handler(job):
            sink.append(str(job.id))
            await asyncio.sleep(0.005)

        return handler

    await seed_jobs(sessionmaker, user_id, 100, kind=KIND_CHAT_TURN)

    a = Worker(
        handlers={KIND_CHAT_TURN: make(executed_a)},
        worker_id="pair-a",
        concurrency=5,
        batch=5,
        poll_interval_s=0.01,
        sessionmaker=sessionmaker,
    )
    b = Worker(
        handlers={KIND_CHAT_TURN: make(executed_b)},
        worker_id="pair-b",
        concurrency=5,
        batch=5,
        poll_interval_s=0.01,
        sessionmaker=sessionmaker,
    )

    async def drained():
        return (await job_statuses(sessionmaker, user_id)).get("done", 0) >= 100

    async with running(a), running(b):
        await wait_for(drained, timeout=120, message="both workers to drain the queue")

    all_executed = executed_a + executed_b
    assert len(all_executed) == 100, f"a job ran twice: {len(all_executed)}"
    assert len(set(all_executed)) == 100
    assert executed_a and executed_b, "one worker did all the work; no real overlap"
