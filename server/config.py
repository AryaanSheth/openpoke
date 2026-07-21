"""Application configuration.

Backed by ``pydantic_settings.BaseSettings`` so that values are resolved when a
``Settings`` instance is constructed rather than when this module is imported.
The previous implementation used ``pydantic.BaseModel`` with
``Field(default=os.getenv(...))``, which froze every setting at first import and
made per-test overrides impossible.

Naming convention (per the rewrite contract):
  * app settings use the ``OPENPOKE_`` env prefix
  * third-party credentials keep their conventional plain names
    (``OPENROUTER_API_KEY``, ``COMPOSIO_API_KEY``,
    ``COMPOSIO_GMAIL_AUTH_CONFIG_ID``, ``DATABASE_URL``)

Because the prefix is not uniform, each field declares its env var explicitly via
``validation_alias`` instead of relying on ``env_prefix``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_APP_NAME = "OpenPoke Server"
DEFAULT_APP_VERSION = "0.3.0"

# Repo root .env. In this checkout it is a symlink to .env.local; pydantic-settings
# opens the path normally so the symlink is followed transparently.
_REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = _REPO_ROOT / ".env"

DEFAULT_DATABASE_URL = "postgresql+asyncpg://openpoke:openpoke@localhost:5432/openpoke"
DEFAULT_MODEL = "anthropic/claude-sonnet-4"


class Settings(BaseSettings):
    """Application settings resolved from the environment and the repo-root .env."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App metadata
    app_name: str = DEFAULT_APP_NAME
    app_version: str = DEFAULT_APP_VERSION

    # Server runtime
    server_host: str = Field(default="0.0.0.0", validation_alias="OPENPOKE_HOST")
    server_port: int = Field(default=8001, validation_alias="OPENPOKE_PORT")

    # Persistence
    database_url: str = Field(default=DEFAULT_DATABASE_URL, validation_alias="DATABASE_URL")
    # Measured ceiling: with SQLAlchemy's default 5+10, /chat/send is flat to 14
    # concurrent requests and collapses at 15 — every extra request waits the full
    # 30s pool timeout. See docs/LOADTEST.md. Raise these together with Postgres'
    # max_connections (default 100) divided by your replica count.
    db_pool_size: int = Field(default=20, validation_alias="OPENPOKE_DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, validation_alias="OPENPOKE_DB_MAX_OVERFLOW")
    db_pool_timeout_s: int = Field(default=10, validation_alias="OPENPOKE_DB_POOL_TIMEOUT_S")

    # LLM model selection (all five are env-overridable; they were hardcoded before)
    interaction_agent_model: str = Field(
        default=DEFAULT_MODEL, validation_alias="OPENPOKE_INTERACTION_AGENT_MODEL"
    )
    execution_agent_model: str = Field(
        default=DEFAULT_MODEL, validation_alias="OPENPOKE_EXECUTION_AGENT_MODEL"
    )
    execution_agent_search_model: str = Field(
        default=DEFAULT_MODEL, validation_alias="OPENPOKE_EXECUTION_AGENT_SEARCH_MODEL"
    )
    summarizer_model: str = Field(default=DEFAULT_MODEL, validation_alias="OPENPOKE_SUMMARIZER_MODEL")
    email_classifier_model: str = Field(
        default=DEFAULT_MODEL, validation_alias="OPENPOKE_EMAIL_CLASSIFIER_MODEL"
    )

    # Credentials / integrations
    openrouter_api_key: str | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
    composio_gmail_auth_config_id: str | None = Field(
        default=None, validation_alias="COMPOSIO_GMAIL_AUTH_CONFIG_ID"
    )
    composio_api_key: str | None = Field(default=None, validation_alias="COMPOSIO_API_KEY")

    # HTTP behaviour. Default is an allowlist, not "*" — Phase 1 relies on this.
    cors_allow_origins_raw: str = Field(
        default="http://localhost:3000", validation_alias="OPENPOKE_CORS_ALLOW_ORIGINS"
    )
    enable_docs: bool = Field(default=True, validation_alias="OPENPOKE_ENABLE_DOCS")
    docs_url: str | None = Field(default="/docs", validation_alias="OPENPOKE_DOCS_URL")

    # Summarisation controls
    conversation_summary_threshold: int = Field(
        default=100, validation_alias="OPENPOKE_CONVERSATION_SUMMARY_THRESHOLD"
    )
    conversation_summary_tail_size: int = Field(
        default=10, validation_alias="OPENPOKE_CONVERSATION_SUMMARY_TAIL_SIZE"
    )

    # ------------------------------------------------------------------
    # Phase 2 — durable jobs. Added to this file rather than scattered
    # os.getenv() calls because Settings is already the one config seam.
    # ------------------------------------------------------------------

    #: Max jobs a single worker executes concurrently (the asyncio.Semaphore).
    worker_concurrency: int = Field(default=10, validation_alias="OPENPOKE_WORKER_CONCURRENCY")
    #: Sleep between claim attempts when the queue is empty.
    worker_poll_interval_s: float = Field(
        default=1.0, validation_alias="OPENPOKE_WORKER_POLL_INTERVAL_S"
    )
    #: Upper bound on rows per claim; the real batch is min(this, free slots).
    worker_batch_size: int = Field(default=10, validation_alias="OPENPOKE_WORKER_BATCH_SIZE")
    #: How often the reaper runs. Cheap indexed UPDATE, so this can stay low.
    worker_reap_interval_s: float = Field(
        default=30.0, validation_alias="OPENPOKE_WORKER_REAP_INTERVAL_S"
    )
    #: A 'running' job untouched for this long is assumed to belong to a dead
    #: worker and goes back to 'pending'. Must exceed the longest handler.
    job_stale_after_s: int = Field(default=300, validation_alias="OPENPOKE_JOB_STALE_AFTER_S")
    #: Retry backoff: base * 2^(attempts-1), capped, with 50-100% jitter.
    job_backoff_base_s: float = Field(default=2.0, validation_alias="OPENPOKE_JOB_BACKOFF_BASE_S")
    job_backoff_max_s: float = Field(default=300.0, validation_alias="OPENPOKE_JOB_BACKOFF_MAX_S")
    #: Trigger poller cadence. Stateless + SKIP LOCKED, so N pollers are safe.
    trigger_poll_interval_s: float = Field(
        default=10.0, validation_alias="OPENPOKE_TRIGGER_POLL_INTERVAL_S"
    )
    #: The importance watcher keeps per-tenant state in memory, so exactly one
    #: replica may run it. See docs/phase-2-notes.md "New issues".
    worker_run_email_watcher: bool = Field(
        default=True, validation_alias="OPENPOKE_WORKER_RUN_EMAIL_WATCHER"
    )

    # ------------------------------------------------------------------
    # Phase 2 — LLM cost control (Problem 3 carve-outs)
    # ------------------------------------------------------------------

    #: Sent as `max_tokens` on every OpenRouter call. Without it OpenRouter
    #: reserves the model's full 64k output ceiling and a small credit balance
    #: 402s immediately (BASELINE.md gotcha 1).
    llm_max_tokens: int = Field(default=4096, validation_alias="OPENPOKE_LLM_MAX_TOKENS")
    #: Upper bound on how long we will honour a 429 `Retry-After`.
    llm_retry_after_max_s: float = Field(
        default=30.0, validation_alias="OPENPOKE_LLM_RETRY_AFTER_MAX_S"
    )
    #: Bleed-stop on unbounded execution-agent prompts: most recent N requests.
    execution_agent_conversation_limit: int = Field(
        default=20, validation_alias="OPENPOKE_EXECUTION_AGENT_CONVERSATION_LIMIT"
    )
    #: Hard ceiling on the history block, applied after the request-count window.
    execution_agent_history_char_budget: int = Field(
        default=24000, validation_alias="OPENPOKE_EXECUTION_AGENT_HISTORY_CHAR_BUDGET"
    )

    @property
    def cors_allow_origins(self) -> list[str]:
        """Parse CORS origins from a comma-separated string."""
        if self.cors_allow_origins_raw.strip() in {"", "*"}:
            return ["*"]
        return [origin.strip() for origin in self.cors_allow_origins_raw.split(",") if origin.strip()]

    @property
    def resolved_docs_url(self) -> str | None:
        """Return documentation URL when docs are enabled."""
        return (self.docs_url or "/docs") if self.enable_docs else None

    @property
    def summarization_enabled(self) -> bool:
        """Flag indicating conversation summarisation is active."""
        return self.conversation_summary_threshold > 0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get cached settings instance. Tests call ``get_settings.cache_clear()``."""
    return Settings()


__all__ = ["Settings", "get_settings", "ENV_FILE", "DEFAULT_APP_NAME", "DEFAULT_APP_VERSION"]
