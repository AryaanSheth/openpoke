"""The durable work queue.

Five functions, all of them one statement. The interesting one is :func:`claim`.

**Why ``FOR UPDATE SKIP LOCKED``.** The job this queue replaces was
``asyncio.create_task`` (``chat_handler.py:47``) plus an in-process ``Set[int]``
(``trigger_scheduler.py:34``). Both are per-process, so a second replica doubles
every reminder and a deploy drops every in-flight turn. The fix has to be a
*database* mutual exclusion, and it has to work with N workers that never talk
to each other — no leader election, no advisory-lock dance, no coordinator to
fail over.

``SELECT ... FOR UPDATE SKIP LOCKED`` gives exactly that. Worker A locks the rows
it is about to claim; worker B's identical query does not block on them, it walks
past them and takes the next ones. The exclusion is the row lock, so it is
correct for any N and it costs one round trip.

The three ways this query is usually written wrong, and why this one is not:

1. ``SELECT`` then ``UPDATE`` in two statements. Two workers both read the same
   pending row before either writes. Double execution, and it only shows up
   under real concurrency.
2. ``UPDATE ... WHERE status='pending' LIMIT`` without the locking subquery.
   Postgres has no ``LIMIT`` on ``UPDATE``, and the workaround people reach for
   (a plain subquery) takes no lock, so it degrades to case 1.
3. ``FOR UPDATE`` without ``SKIP LOCKED``. Correct, but every worker serialises
   behind the first one; throughput collapses to one worker's worth.

Transaction boundaries are the caller's. The worker commits immediately after
claiming — holding the claim transaction open for the whole job would keep the
row locks for the job's duration and re-serialise everything.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Job

# The claim, verbatim from plan.md Phase 2 step 2.
#
# ``RETURNING *`` is mapped back onto the ORM entity via ``from_statement`` so the
# worker gets real ``Job`` objects rather than rows it would have to re-fetch.
_CLAIM_SQL = text(
    """
    UPDATE jobs SET status='running', claimed_at=now(), claimed_by=:worker, attempts=attempts+1
    WHERE id IN (
        SELECT id FROM jobs
        WHERE status='pending' AND run_at <= now()
        ORDER BY run_at
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    RETURNING *
    """
)

# Retry, or bury. Done as one statement rather than read-compute-write so the
# backoff uses the *database* clock (workers' clocks drift) and so a concurrent
# reaper cannot interleave between the read and the write.
#
# Backoff is `base * 2^(attempts-1)`, capped, then multiplied by a random factor
# in [0.5, 1.0) — "equal jitter". Without jitter, a batch of jobs that all failed
# on the same upstream outage retries in lockstep and re-creates the outage.
_FAIL_SQL = text(
    """
    UPDATE jobs
    SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
        run_at = CASE
            WHEN attempts >= max_attempts THEN run_at
            ELSE now() + (
                LEAST(:backoff_max, :backoff_base * power(2, GREATEST(attempts - 1, 0)))
                * (0.5 + random() * 0.5)
            ) * interval '1 second'
        END,
        last_error = :error,
        claimed_at = NULL,
        claimed_by = NULL
    WHERE id = :job_id
    RETURNING status, attempts
    """
)

# The reaper. This is the only thing that recovers a SIGKILLed worker: its rows
# stay 'running' with a stale claimed_at forever otherwise.
#
# A job that died with attempts already at the ceiling goes straight to 'dead'
# rather than back to 'pending' — otherwise a handler that reliably kills its
# worker (OOM, segfault in a C extension) is an infinite crash loop that no
# amount of max_attempts can stop, because the process never lives long enough
# to call fail().
_REAP_SQL = text(
    """
    UPDATE jobs
    SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
        claimed_at = NULL,
        claimed_by = NULL,
        last_error = :reason
    WHERE status = 'running'
      AND claimed_at < now() - (:stale * interval '1 second')
      AND claimed_by IS DISTINCT FROM :exclude_worker
    RETURNING id
    """
)


async def enqueue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: str,
    payload: dict,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> Job | None:
    """Insert a job. Returns ``None`` when *dedupe_key* is already present.

    The dedupe is ``ON CONFLICT DO NOTHING`` against the unique index, not a
    ``SELECT`` first: two pollers racing on the same occurrence must produce one
    job, and a check-then-insert cannot guarantee that. The loser's INSERT blocks
    until the winner commits and then affects zero rows.

    ``dedupe_key`` is globally unique (not per-tenant), so every key a caller
    constructs must be namespaced by ``user_id`` — see :func:`dedupe_key_for`.
    """
    values: dict[str, Any] = {
        "user_id": user_id,
        "kind": kind,
        "payload": payload,
        "status": "pending",
        "attempts": 0,
        "max_attempts": max_attempts,
        "dedupe_key": dedupe_key,
    }
    if run_at is not None:
        values["run_at"] = run_at

    stmt = pg_insert(Job).values(**values)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Job.dedupe_key])

    result = await session.execute(stmt.returning(Job))
    return result.scalars().first()


async def claim(session: AsyncSession, *, worker_id: str, batch: int = 10) -> list[Job]:
    """Atomically take up to *batch* due jobs for *worker_id*.

    Every returned job is guaranteed to have been returned to no other caller.
    The caller must commit; until it does, the rows are locked (so no one else
    can take them) but not durable (a crash returns them to 'pending').
    """
    if batch <= 0:
        return []
    stmt = (
        select(Job)
        .from_statement(_CLAIM_SQL)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(stmt, {"worker": worker_id, "batch": batch})
    return list(result.scalars().all())


async def complete(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Mark a job done. Terminal."""
    await session.execute(
        text("UPDATE jobs SET status='done', claimed_at=NULL, claimed_by=NULL WHERE id=:job_id"),
        {"job_id": job_id},
    )


async def heartbeat(session: AsyncSession, job_id: uuid.UUID, worker_id: str) -> bool:
    """Push a running job's lease forward. Returns False if the lease was lost.

    ``SKIP LOCKED`` guarantees one *claimant* per claim; it does not guarantee one
    *execution* per job. Without this, a handler that outlives ``job_stale_after_s``
    is reaped while still alive and a second worker runs it concurrently — a real
    window, since an LLM turn can reach 8 tool iterations x a 60s timeout (480s)
    against a 300s default.

    The ``claimed_by`` guard is what makes the return value meaningful: if a reaper
    already took the job away, the UPDATE matches nothing and the caller learns its
    work is now duplicated rather than continuing to believe it owns the job.
    """
    held = await session.scalar(
        text(
            "UPDATE jobs SET claimed_at=now() "
            "WHERE id=:job_id AND claimed_by=:worker AND status='running' "
            "RETURNING id"
        ),
        {"job_id": job_id, "worker": worker_id},
    )
    return held is not None


async def fail(
    session: AsyncSession,
    job_id: uuid.UUID,
    error: str,
    *,
    backoff_base_s: float | None = None,
    backoff_max_s: float | None = None,
) -> None:
    """Return a job to 'pending' with backoff, or bury it as 'dead'.

    'dead' rather than deleted: a job that exhausted its retries is the single
    most useful row in the table and silently dropping it is how you find out
    about a broken handler from a customer instead of a dashboard.
    """
    from ..config import get_settings

    settings = get_settings()
    await session.execute(
        _FAIL_SQL,
        {
            "job_id": job_id,
            "error": error,
            "backoff_base": (
                settings.job_backoff_base_s if backoff_base_s is None else backoff_base_s
            ),
            "backoff_max": (
                settings.job_backoff_max_s if backoff_max_s is None else backoff_max_s
            ),
        },
    )


async def reap(
    session: AsyncSession, *, stale_after_s: int = 300, exclude_worker: str | None = None
) -> int:
    """Return jobs abandoned by a dead worker to the queue. Returns the count.

    **The one way to make this queue run a handler twice** is a stale window
    shorter than the slowest handler: the reaper decides a still-running job is
    abandoned, another worker claims it, and the side effect happens twice.
    ``job_stale_after_s`` must exceed the longest handler runtime — with LLM
    turns that is the 300 s default against a 60 s per-call timeout and at most 8
    tool iterations, which is *not* a comfortable margin. Named as a residual
    risk in docs/phase-2-notes.md rather than papered over.

    *exclude_worker* removes the self-inflicted half of that hazard entirely. A
    worker's ``claimed_by`` is ``host:pid``, unique to a process lifetime, so a
    worker that is alive enough to run its own reaper is alive enough to still
    own its own claims — it never has anything of its own to reap. Found by
    ``test_killed_worker_leaves_a_job_the_reaper_recovers``, which caught a
    worker reaping and re-running its own in-flight job.
    """
    result = await session.execute(
        _REAP_SQL,
        {
            "stale": stale_after_s,
            "exclude_worker": exclude_worker,
            "reason": f"reaped: no completion within {stale_after_s}s of claim",
        },
    )
    # RETURNING + len() rather than `.rowcount`: SQLAlchemy types the return of
    # session.execute() as Result, which has no rowcount, and a cast to paper
    # over that would be hiding a real modelling question rather than answering it.
    return len(result.all())


def dedupe_key_for(user_id: uuid.UUID, *parts: object) -> str:
    """Build a dedupe key namespaced by tenant.

    ``jobs.dedupe_key`` is ``UNIQUE`` globally, not ``UNIQUE(user_id, key)``
    (Phase 0 note 6). Any key built from tenant-scoped data — ``email_poll:2026-07-21``
    is the obvious one — would therefore let one tenant's job silently suppress
    another's, with no error anywhere. Every key goes through here.

    **Deviation from CONTRACT.md**, recorded deliberately. The contract fixes the
    trigger key as ``trigger:{trigger_id}:{occurrence_iso}``. That specific key is
    in fact collision-free (trigger ids are a global sequence), but the rule
    "namespace every key with the tenant" is the one that survives the next kind
    being added, so it is applied uniformly. Result for triggers:
    ``{user_id}:trigger:{trigger_id}:{occurrence_iso}``.
    """
    return ":".join([str(user_id), *(str(part) for part in parts)])


__all__ = ["claim", "complete", "dedupe_key_for", "enqueue", "fail", "reap"]
