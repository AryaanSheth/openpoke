"""Durable job queue: the thing that makes accepted work survive a deploy.

``server/jobs/queue.py``   claim/enqueue/complete/fail/reap — the SQL
``server/jobs/worker.py``  the loop that drives them
``server/jobs/handlers.py`` kind -> coroutine
``server/jobs/context.py`` the job id, for cost attribution

Kinds are string constants because they live in a ``VARCHAR`` column and an enum
would need a migration every time one is added.
"""

from __future__ import annotations

KIND_CHAT_TURN = "chat_turn"
KIND_TRIGGER_FIRE = "trigger_fire"
KIND_EMAIL_POLL = "email_poll"

#: Terminal and non-terminal job states, as stored in ``jobs.status``.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_DEAD = "dead"

from .queue import claim, complete, dedupe_key_for, enqueue, fail, reap  # noqa: E402

__all__ = [
    "KIND_CHAT_TURN",
    "KIND_EMAIL_POLL",
    "KIND_TRIGGER_FIRE",
    "STATUS_DEAD",
    "STATUS_DONE",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "claim",
    "complete",
    "dedupe_key_for",
    "enqueue",
    "fail",
    "reap",
]
