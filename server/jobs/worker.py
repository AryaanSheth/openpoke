"""The worker loop.

Four responsibilities, in the order they matter:

1. **Bind the tenant before dispatch.** Phase 1 found this by firing a real
   trigger: the poller has no tenant of its own, so the execution agent's first
   repository write raised ``LookupError`` and every recurring reminder died —
   fail-closed, but broken. ``jobs.user_id`` is the carrier that fixes it, and
   binding it here is what lets Phase 1's 3-line stopgap in
   ``trigger_scheduler.py`` be deleted outright.
2. **Bound concurrency.** An ``asyncio.Semaphore``, and — more importantly — a
   claim batch sized to *free slots*. Claiming more than you can run marks rows
   'running' while they sit in a queue, and the reaper then correctly decides
   those rows belong to a dead worker.
3. **Retry, then bury.** Handler raises ⇒ ``fail()`` ⇒ backoff ⇒ 'pending'.
   ``attempts >= max_attempts`` ⇒ 'dead'. Never silently gone.
4. **Reap.** ``status='running' AND claimed_at`` older than the stale window ⇒
   back to 'pending'. Nothing else recovers a SIGKILLed worker.

Each unit of work gets its own session. Sharing one session across concurrent
handlers would serialise them onto a single connection and make one handler's
rollback discard another's writes.
"""

from __future__ import annotations

import asyncio
import os
import socket
import traceback
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_settings
from ..db.engine import get_sessionmaker
from ..db.models import Job
from ..logging_config import logger
from .context import reset_current_job_id, set_current_job_id
from .queue import claim, complete, fail, heartbeat, reap

Handler = Callable[[Job], Awaitable[None]]


def default_worker_id() -> str:
    """Identify the process in ``jobs.claimed_by``. Host + pid is enough to find
    the container that stranded a job."""
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    """Claims jobs and runs them. Safe to run N of these against one database."""

    def __init__(
        self,
        *,
        handlers: dict[str, Handler],
        worker_id: str | None = None,
        concurrency: int | None = None,
        batch: int | None = None,
        poll_interval_s: float | None = None,
        reap_interval_s: float | None = None,
        stale_after_s: int | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        settings = get_settings()
        self.handlers = handlers
        self.worker_id = worker_id or default_worker_id()
        self.concurrency = concurrency if concurrency is not None else settings.worker_concurrency
        self.batch = batch if batch is not None else settings.worker_batch_size
        self.poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else settings.worker_poll_interval_s
        )
        self.reap_interval_s = (
            reap_interval_s if reap_interval_s is not None else settings.worker_reap_interval_s
        )
        self.stale_after_s = (
            stale_after_s if stale_after_s is not None else settings.job_stale_after_s
        )
        self._sessionmaker = sessionmaker
        self._sem = asyncio.Semaphore(self.concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        #: user_id -> timezone, filled one batch at a time. See _load_timezones.
        self._timezones: dict[uuid.UUID, str] = {}
        self._stop = asyncio.Event()

        #: Observability, and what the concurrency test asserts against.
        self.in_flight = 0
        self.peak_in_flight = 0
        self.completed = 0
        self.failed = 0

    # -- session ---------------------------------------------------------

    def _session(self) -> AsyncSession:
        maker = self._sessionmaker or get_sessionmaker()
        return maker()

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        """Poll until :meth:`stop` is called, then drain in-flight work."""
        logger.info(
            "worker started",
            extra={
                "worker_id": self.worker_id,
                "concurrency": self.concurrency,
                "batch": self.batch,
            },
        )
        reaper = asyncio.create_task(self._reap_loop(), name="job-reaper")
        hard_stop = False
        try:
            while not self._stop.is_set():
                try:
                    claimed = await self.run_once()
                except Exception as exc:
                    # One bad poll must not end the loop. This is the bug from
                    # trigger_scheduler.py:58-66 — a try around the *whole* loop
                    # meant one OperationalError killed the poller for the
                    # process lifetime while /health stayed green.
                    logger.exception("job poll failed", extra={"error": str(exc)})
                    claimed = 0
                if claimed == 0:
                    await self._sleep_or_stop(self.poll_interval_s)
        except asyncio.CancelledError:
            # Hard stop (task cancelled, not stop() called). Do NOT drain:
            # awaiting handlers that were never told to finish is how a shutdown
            # hangs forever. In-flight rows stay 'running' and the reaper takes
            # them — which is exactly the crash path the reaper exists for.
            hard_stop = True
            raise
        finally:
            reaper.cancel()
            if hard_stop:
                for task in list(self._tasks):
                    task.cancel()
            else:
                await asyncio.gather(reaper, return_exceptions=True)
                await self.drain()
            logger.info(
                "worker stopped",
                extra={
                    "worker_id": self.worker_id,
                    "completed": self.completed,
                    "failed": self.failed,
                    "hard_stop": hard_stop,
                },
            )

    def stop(self) -> None:
        self._stop.set()

    async def drain(self) -> None:
        """Wait for in-flight handlers. In-flight work is already marked
        'running', so exiting without draining hands it to the reaper — correct
        but five minutes slower than just waiting."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except (TimeoutError, asyncio.TimeoutError):
            pass

    # -- the loop body ---------------------------------------------------

    async def run_once(self) -> int:
        """Claim up to the free-slot count and dispatch. Returns jobs claimed."""
        free = self.concurrency - len(self._tasks)
        if free <= 0:
            await self._sleep_or_stop(self.poll_interval_s)
            return 0

        async with self._session() as session:
            jobs = await claim(session, worker_id=self.worker_id, batch=min(self.batch, free))
            # Commit immediately: the row locks taken by the claim are held until
            # this transaction ends, and holding them for the job's duration
            # would serialise every worker behind the slowest handler.
            await session.commit()
            if jobs:
                await self._load_timezones(session, {job.user_id for job in jobs})

        for job in jobs:
            task = asyncio.create_task(self._execute(job), name=f"job-{job.id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return len(jobs)

    async def _load_timezones(self, session: AsyncSession, user_ids: set[uuid.UUID]) -> None:
        """Resolve the batch's tenant timezones in one query.

        Doing this per job inside the handler was an N+1: a third session, and a
        third physical connection under a non-pooling engine, for every job. The
        value is only used to render log timestamps, so a short cache is safe —
        a timezone change takes effect on the next batch.
        """
        missing = [uid for uid in user_ids if uid not in self._timezones]
        if not missing:
            return
        from ..db.models import User

        rows = await session.execute(select(User.id, User.timezone).where(User.id.in_(missing)))
        for user_id, timezone_name in rows.all():
            self._timezones[user_id] = timezone_name or "UTC"

    async def _reap_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep_or_stop(self.reap_interval_s)
            if self._stop.is_set():
                return
            try:
                await self.reap_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("reaper failed", extra={"error": str(exc)})

    async def reap_once(self) -> int:
        async with self._session() as session:
            # Never reap our own claims: if this loop is running, so are they.
            count = await reap(
                session, stale_after_s=self.stale_after_s, exclude_worker=self.worker_id
            )
            await session.commit()
        if count:
            logger.warning("reaped stale jobs", extra={"count": count})
        return count

    # -- dispatch --------------------------------------------------------

    async def _execute(self, job: Job) -> None:
        async with self._sem:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                await self._dispatch(job)
            finally:
                self.in_flight -= 1

    async def _heartbeat_loop(self, job_id: uuid.UUID) -> None:
        """Renew the lease while the handler runs, until cancelled.

        Interval is a third of the stale window so two consecutive failed renewals
        still land before a reaper can act.
        """
        interval = max(1.0, self.stale_after_s / 3)
        while True:
            await asyncio.sleep(interval)
            async with self._session() as session:
                held = await heartbeat(session, job_id, self.worker_id)
                await session.commit()
            if not held:
                # Lost the lease: another worker owns this job now. Nothing to do but
                # say so loudly — the duplicate execution has already begun.
                logger.error(
                    "job lease lost while running; duplicate execution possible",
                    extra={"job_id": str(job_id), "worker": self.worker_id},
                )
                return

    async def _dispatch(self, job: Job) -> None:
        job_id = job.id
        beat = asyncio.create_task(self._heartbeat_loop(job_id))
        try:
            handler = self.handlers.get(job.kind)
            if handler is None:
                raise LookupError(f"no handler registered for job kind {job.kind!r}")
            await self._run_handler(handler, job)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "job failed",
                extra={
                    "job_id": str(job_id),
                    "kind": job.kind,
                    "attempts": job.attempts,
                    "error": detail,
                    "traceback": traceback.format_exc(limit=8),
                },
            )
            self.failed += 1
            async with self._session() as session:
                await fail(session, job_id, detail)
                await session.commit()
            return
        finally:
            # Handler is off the CPU either way; stop renewing before the terminal
            # write so a heartbeat can't resurrect claimed_at after complete/fail.
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat

        async with self._session() as session:
            await complete(session, job_id)
            await session.commit()
        self.completed += 1
        logger.info("job done", extra={"job_id": str(job_id), "kind": job.kind})

    async def _run_handler(self, handler: Handler, job: Job) -> None:
        """Bind tenant + job id, then run the handler.

        The tenant binding is the invariant Phase 1 paid for in production: a job
        executing on behalf of a tenant must have that tenant bound before any
        repository write, or the first write raises ``LookupError``.
        """
        # TODO(phase-1-integration): depends on Phase 1 internals that are not in
        # CONTRACT.md — repositories.context.{TenantContext,tenant_scope}.
        # Imported lazily so this module still imports if those move.
        from ..repositories.context import TenantContext, tenant_scope

        ctx = TenantContext(
            user_id=job.user_id,
            timezone=self._timezones.get(job.user_id, "UTC"),
            # Phase 1 made the Composio identity `str(users.id)` by construction.
            composio_user_id=str(job.user_id),
        )
        token = set_current_job_id(job.id)
        try:
            with tenant_scope(ctx):
                await handler(job)
        finally:
            reset_current_job_id(token)


def new_worker(**kwargs: object) -> Worker:
    """Build a Worker with the default handler registry."""
    from .handlers import default_handlers

    kwargs.setdefault("handlers", default_handlers())
    return Worker(**kwargs)  # type: ignore[arg-type]


__all__ = ["Handler", "Worker", "default_worker_id", "new_worker"]
