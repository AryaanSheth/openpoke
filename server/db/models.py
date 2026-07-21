"""SQLAlchemy 2.0 models — the whole schema lives here.

Every tenant-scoped table carries ``user_id`` and a composite index leading with
it, so no query can be cheap unless it is also scoped.

These tables replace the file-backed stores:

============================  ==========================================
table                         replaces
============================  ==========================================
conversation_entries          data/conversation/poke_conversation.log
working_memory_entries        data/conversation/poke_working_memory.log
summary_state                 the <summary_info>/<conversation_summary> header of the same file
agents                        data/execution_agents/roster.json
agent_log_entries             data/execution_agents/<slug>.log
triggers                      data/triggers.db (SQLite)
gmail_seen                    data/gmail_seen.json
users.timezone                data/timezone.txt
============================  ==========================================
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base. Alembic autogenerate compares against this metadata."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _user_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )


_NOW = text("now()")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # argon2 hash of the bearer token. Never the token itself.
    api_key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'UTC'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


class GmailConnection(Base):
    __tablename__ = "gmail_connections"
    __table_args__ = (
        # One live Composio identity maps to exactly one tenant; the importance
        # watcher resolves the other direction with this.
        UniqueConstraint("composio_user_id", name="uq_gmail_connections_composio_user_id"),
        Index("ix_gmail_connections_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    # Composio's own identifier. NEVER the tenant key.
    composio_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet ciphertext of the Composio connection id (a bearer credential).
    connection_id_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bumped on key rotation so re-encryption is in-place, not a migration.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'pending'"))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW, onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Conversation / working memory
# ---------------------------------------------------------------------------


class ConversationEntry(Base):
    """One line of the old poke_conversation.log, per tenant."""

    __tablename__ = "conversation_entries"
    # The unique constraint is itself the (user_id, seq) composite index; a second
    # explicit Index would only double write cost.
    __table_args__ = (UniqueConstraint("user_id", "seq", name="uq_conversation_entries_user_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    # Per-user monotonic ordinal. Replaces "position of the line in the file".
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # user_message | agent_message | poke_reply | wait
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=_NOW)


class WorkingMemoryEntry(Base):
    """Unsummarised tail of the conversation, per tenant."""

    __tablename__ = "working_memory_entries"
    __table_args__ = (UniqueConstraint("user_id", "seq", name="uq_working_memory_entries_user_seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=_NOW)


class SummaryState(Base):
    """The <summary_info> + <conversation_summary> header, one row per tenant."""

    __tablename__ = "summary_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # -1 means "nothing summarised yet" (matches SummaryState.empty()).
    last_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("-1"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Execution agents
# ---------------------------------------------------------------------------


class Agent(Base):
    """Replaces roster.json. The (user_id, name) unique key kills the _slugify
    filename collision — two agents whose names slugify identically used to share
    one .log file."""

    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_agents_user_name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


class AgentLogEntry(Base):
    """Replaces data/execution_agents/<slug>.log."""

    __tablename__ = "agent_log_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "agent_name", "seq", name="uq_agent_log_entries_user_agent_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # agent_request | agent_action | tool_response | agent_response
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=_NOW)


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


class Trigger(Base):
    """Mirrors the SQLite triggers table (services/triggers/store.py) plus
    user_id and the claim columns the exactly-once poller needs.

    ``id`` stays an integer because the contract's trigger_fire payload and the
    existing tool schemas pass ``trigger_id: int``.
    ``start_time`` / ``next_trigger`` were ISO-8601 UTC strings in SQLite; they
    become real timestamptz here so ``next_trigger <= now()`` can use an index.
    """

    __tablename__ = "triggers"
    __table_args__ = (
        Index("ix_triggers_user_agent_next", "user_id", "agent_name", "next_trigger"),
        Index(
            "ix_triggers_due",
            "next_trigger",
            postgresql_where=text("status = 'active' AND next_trigger IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_trigger: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recurrence_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # active | paused | completed (services/triggers/utils.py VALID_STATUSES)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'active'"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW, onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class GmailSeen(Base):
    """Replaces the bounded JSON deque in data/gmail_seen.json. The unique pair is
    what makes "already processed" a constraint rather than a race."""

    __tablename__ = "gmail_seen"
    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_gmail_seen_user_message"),
        Index("ix_gmail_seen_user_seen_at", "user_id", "seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # False until the classifier actually returned a verdict, so a transient
    # OpenRouter failure does not permanently drop the email.
    classified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


# ---------------------------------------------------------------------------
# Jobs + metering
# ---------------------------------------------------------------------------


class Job(Base):
    """Durable work queue. Phase 2 owns the claim/complete/fail/reap logic; this
    is only the shape it needs."""

    __tablename__ = "jobs"
    __table_args__ = (
        # The claim query: WHERE status='pending' AND run_at <= now() ORDER BY run_at.
        # Partial so the index never carries completed/dead rows.
        Index(
            "ix_jobs_pending_run_at",
            "status",
            "run_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_jobs_user_status", "user_id", "status"),
        # Reaper: status='running' AND claimed_at < now() - interval '5 min'.
        Index(
            "ix_jobs_running_claimed_at",
            "claimed_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    # chat_turn | trigger_fire | email_poll (server/jobs/__init__.py)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # pending | running | done | dead
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Globally unique: `trigger:{trigger_id}:{occurrence_iso}` already embeds the
    # trigger, which is itself tenant-scoped. NULLs do not collide in Postgres.
    dedupe_key: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


class LlmUsage(Base):
    """Per-call token accounting. Phase 2 step 9 populates it; without it the
    Problem-3 deferral is unfalsifiable."""

    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_user_ts", "user_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    # Null for LLM calls made outside a job (e.g. a synchronous request path).
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=_NOW)


__all__ = [
    "Agent",
    "AgentLogEntry",
    "Base",
    "ConversationEntry",
    "GmailConnection",
    "GmailSeen",
    "Job",
    "LlmUsage",
    "SummaryState",
    "Trigger",
    "User",
    "WorkingMemoryEntry",
]
