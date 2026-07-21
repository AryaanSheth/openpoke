"""Trigger poller: turns due triggers into durable jobs, exactly once.

**What was wrong.** Three separate defects in the same 40 lines:

1. ``self._in_flight: Set[int]`` (original ``:34``) was the *only* dedupe. It is
   per-process, so two replicas each fired every reminder — the failure that gets
   worse with horizontal scale, not better.
2. ``fetch_due`` was a plain ``SELECT`` that never marked a row claimed, so even
   one process racing itself across two ticks could double-fire.
3. The ``try`` wrapped the *whole* loop (original ``:58-66``). One
   ``OperationalError`` ended the poller for the process lifetime while
   ``/health`` kept returning 200 — silent death, no alert, reminders simply stop.

**What replaces it.** The poller claims due triggers with the same
``FOR UPDATE SKIP LOCKED`` pattern the job queue uses, enqueues one
``trigger_fire`` job per occurrence with a tenant-namespaced ``dedupe_key``, and
**advances the schedule in the same transaction**.

Advancing at *enqueue* time rather than at *completion* time is the load-bearing
choice. If the schedule only advanced on success, a job that exhausted its
retries would leave ``next_trigger`` pinned at the old occurrence — and because
the dedupe key is derived from that occurrence, the trigger could never be
re-enqueued either. A recurring reminder would go permanently silent after one
bad afternoon. Advancing at enqueue makes the poller's job "schedule", the
worker's job "execute", and neither can wedge the other.

Exactly-once then rests on two independent mechanisms, either of which suffices:
the row claim (``SKIP LOCKED``), and ``UNIQUE(dedupe_key)`` on the job.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import get_settings
from ..db.engine import get_sessionmaker
from ..db.models import Trigger
from ..jobs import KIND_TRIGGER_FIRE, dedupe_key_for, enqueue
from ..logging_config import logger
from .triggers.utils import load_rrule, resolve_timezone

UTC = timezone.utc

#: Trigger occurrences carry a real side effect (an email can be sent), so they
#: get fewer attempts than the queue default of 5. Retry buys recovery from a
#: transient 429; it also buys a second send if the agent failed *after* acting.
#: Three is the compromise, and the tail-preserving agent history (agent.py) is
#: what lets a retried agent see that it already acted.
TRIGGER_JOB_MAX_ATTEMPTS = 3


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# Same shape as the job claim. A trigger already claimed by a live poller is
# skipped; one claimed by a poller that then died becomes eligible again after
# the stale window, and the job's dedupe_key makes that re-claim a no-op if the
# original enqueue actually committed.
_CLAIM_DUE_SQL = text(
    """
    UPDATE triggers SET claimed_at=now(), claimed_by=:worker
    WHERE id IN (
        SELECT id FROM triggers
        WHERE status='active' AND next_trigger IS NOT NULL AND next_trigger <= now()
          AND (claimed_at IS NULL OR claimed_at < now() - (:stale * interval '1 second'))
        ORDER BY next_trigger
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    RETURNING *
    """
)


async def claim_due_triggers(
    session: AsyncSession, *, worker_id: str, batch: int = 50, stale_after_s: int = 300
) -> list[Trigger]:
    """Claim due triggers for this poller. No other poller can see them."""
    if batch <= 0:
        return []
    stmt = (
        select(Trigger)
        .from_statement(_CLAIM_DUE_SQL)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(
        stmt, {"worker": worker_id, "batch": batch, "stale": stale_after_s}
    )
    return list(result.scalars().all())


def _next_occurrence_after(recurrence_rule: str, after: datetime, timezone_name: str | None):
    """The next fire strictly after *after*, or None when the rule is exhausted."""
    tz = resolve_timezone(timezone_name)
    rule = load_rrule(recurrence_rule)
    nxt = rule.after(after.astimezone(tz), inc=False)
    if nxt is None:
        return None
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(UTC)


def _advance(row: Trigger, occurrence: datetime) -> None:
    """Move the trigger past *occurrence*, in place on the ORM row."""
    row.claimed_at = None
    row.claimed_by = None
    if not row.recurrence_rule:
        row.next_trigger = None
        row.status = "completed"
        return
    try:
        nxt = _next_occurrence_after(row.recurrence_rule, occurrence, row.timezone)
    except Exception as exc:
        # A malformed RRULE must not wedge the poller into re-claiming this row
        # every tick forever. Park it and make the reason visible.
        logger.error(
            "trigger has an unparseable recurrence rule; pausing it",
            extra={"trigger_id": row.id, "error": str(exc)},
        )
        row.next_trigger = None
        row.status = "paused"
        row.last_error = f"unparseable recurrence_rule: {exc}"
        return
    row.next_trigger = nxt
    if nxt is None:
        row.status = "completed"


class TriggerScheduler:
    """Polls for due triggers and enqueues them. Stateless; N are safe."""

    def __init__(
        self,
        poll_interval_seconds: float | None = None,
        *,
        worker_id: str | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        batch: int = 50,
    ) -> None:
        settings = get_settings()
        self._poll_interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else settings.trigger_poll_interval_s
        )
        self._stale_after_s = settings.job_stale_after_s
        self._batch = batch
        self._sessionmaker = sessionmaker
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._lock = asyncio.Lock()
        if worker_id is None:
            from ..jobs.worker import default_worker_id

            worker_id = default_worker_id()
        self._worker_id = worker_id
        # NOTE: no _in_flight set. It was the bug, not the mechanism.

    def _session(self) -> AsyncSession:
        maker = self._sessionmaker or get_sessionmaker()
        return maker()

    async def start(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            self._running = True
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="trigger-scheduler"
            )
            logger.info("Trigger scheduler started", extra={"interval": self._poll_interval})

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
                logger.info("Trigger scheduler stopped")

    async def _run(self) -> None:
        while self._running:
            # The try is inside the loop. The original had it outside, so the
            # first transient DB error ended trigger delivery permanently while
            # /health stayed green.
            try:
                await self.poll_once()
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception as exc:
                logger.exception("Trigger poll failed", extra={"error": str(exc)})
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise

    async def poll_once(self) -> int:
        """Claim due triggers, enqueue one job each, advance the schedule.

        Returns the number of jobs enqueued (claims that deduped return 0 for
        that row but still advance the schedule).
        """
        enqueued = 0
        async with self._session() as session:
            rows = await claim_due_triggers(
                session,
                worker_id=self._worker_id,
                batch=self._batch,
                stale_after_s=self._stale_after_s,
            )
            for row in rows:
                occurrence = row.next_trigger
                if occurrence is None:  # pragma: no cover - the claim filters these
                    continue
                occurrence_iso = _isoformat(occurrence)
                job = await enqueue(
                    session,
                    user_id=row.user_id,
                    kind=KIND_TRIGGER_FIRE,
                    payload={"trigger_id": row.id, "occurrence": occurrence_iso},
                    dedupe_key=dedupe_key_for(row.user_id, "trigger", row.id, occurrence_iso),
                    max_attempts=TRIGGER_JOB_MAX_ATTEMPTS,
                )
                if job is not None:
                    enqueued += 1
                    logger.info(
                        "Trigger enqueued",
                        extra={
                            "trigger_id": row.id,
                            "agent": row.agent_name,
                            "occurrence": occurrence_iso,
                            "job_id": str(job.id),
                        },
                    )
                _advance(row, occurrence)
            # Enqueue and schedule advance land together or not at all.
            await session.commit()
        return enqueued


# ---------------------------------------------------------------------------
# Execution — runs in the worker, with the tenant already bound from jobs.user_id
# ---------------------------------------------------------------------------


def _format_instructions(row: Trigger, occurrence_iso: str, fired_at: datetime) -> str:
    metadata_lines = [f"Trigger ID: {row.id}"]
    if row.recurrence_rule:
        metadata_lines.append(f"Recurrence: {row.recurrence_rule}")
    if row.timezone:
        metadata_lines.append(f"Timezone: {row.timezone}")
    if row.start_time:
        metadata_lines.append(f"Start Time (UTC): {_isoformat(row.start_time)}")

    metadata = "\n".join(f"- {line}" for line in metadata_lines)
    return (
        f"Trigger fired at {_isoformat(fired_at)} (UTC).\n"
        f"Scheduled occurrence time: {occurrence_iso}.\n\n"
        f"Metadata:\n{metadata}\n\n"
        f"Payload:\n{row.payload}"
    )


async def execute_trigger_occurrence(
    trigger_id: int,
    *,
    occurrence: str | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Run one trigger occurrence. Raises on failure so the job retries.

    The tenant is bound by ``jobs/worker.py`` from ``jobs.user_id`` before this
    is called. Phase 1's ``tenant_scope_for`` stopgap in this file — which
    resolved the owner from the row because the poller had no tenant — is
    deleted; the job row carries the tenant now.
    """
    from ..agents.execution_agent.batch_manager import ExecutionBatchManager

    maker = sessionmaker or get_sessionmaker()
    async with maker() as session:
        row = await session.get(Trigger, trigger_id)
        if row is None:
            # Deleted between enqueue and execution. Not an error, and retrying
            # will not bring it back.
            logger.warning("trigger vanished before execution", extra={"trigger_id": trigger_id})
            return
        occurrence_iso = occurrence or (
            _isoformat(row.next_trigger) if row.next_trigger else _isoformat(_utc_now())
        )
        instructions = _format_instructions(row, occurrence_iso, _utc_now())
        agent_name = row.agent_name

    logger.info(
        "Dispatching trigger",
        extra={"trigger_id": trigger_id, "agent": agent_name, "occurrence": occurrence_iso},
    )
    result = await ExecutionBatchManager().execute_agent(agent_name, instructions)

    error_text = None if result.success else (result.error or result.response)
    async with maker() as session:
        row = await session.get(Trigger, trigger_id)
        if row is not None:
            row.last_error = error_text
        await session.commit()

    if error_text:
        # Raising is what buys retry. The schedule has already advanced, so a
        # permanent failure costs one occurrence, not the whole trigger.
        raise RuntimeError(f"trigger {trigger_id} execution failed: {error_text}")


_scheduler_instance: TriggerScheduler | None = None


def get_trigger_scheduler() -> TriggerScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = TriggerScheduler()
    return _scheduler_instance


__all__ = [
    "TRIGGER_JOB_MAX_ATTEMPTS",
    "TriggerScheduler",
    "claim_due_triggers",
    "execute_trigger_occurrence",
    "get_trigger_scheduler",
]
