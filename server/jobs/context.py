"""The currently-executing job id, for cost attribution.

``llm_usage.job_id`` answers "which job spent this". The OpenRouter client is
three call layers below the worker and threading a job id through every agent
signature would touch files this phase does not own, so it rides a ``ContextVar``
— the same mechanism Phase 1 uses for the tenant, and for the same reason.

``None`` outside a job (a synchronous request path); the column is nullable.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_current_job_id: ContextVar[uuid.UUID | None] = ContextVar("openpoke_job_id", default=None)


def get_current_job_id() -> uuid.UUID | None:
    return _current_job_id.get()


def set_current_job_id(job_id: uuid.UUID | None) -> Token:
    return _current_job_id.set(job_id)


def reset_current_job_id(token: Token) -> None:
    _current_job_id.reset(token)


__all__ = ["get_current_job_id", "reset_current_job_id", "set_current_job_id"]
