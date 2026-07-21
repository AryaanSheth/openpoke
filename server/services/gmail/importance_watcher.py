"""Background watcher that surfaces important Gmail emails proactively.

**What was wrong.** One watcher read one process global for the Composio identity
(original ``:113``: ``composio_user_id = get_active_gmail_user_id()``) and wrote
every result into the one shared conversation log. With two users that is not a
bug you can paper over — it is the absence of tenancy.

**What it does now.** Each poll enumerates the actively connected tenants from
``gmail_connections``, and for each one binds that tenant's context, polls Gmail
with *that user's* Composio identity, and dispatches into *that user's*
conversation. Per-poll state (warmup flag, last poll time) is keyed by
``user_id`` instead of being a single instance attribute.

**And the drop bug.** The old loop appended every id to ``processed_ids`` before
checking the classifier result (original ``:200-210``), so a transient OpenRouter
failure marked the email seen forever. Ids are now settled only on a decided
classification; a failed attempt is recorded as an attempt and stays eligible
until ``CLASSIFY_RETRY_BUDGET`` expires, which is what stops a poison message
from retrying without bound.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from ...db.engine import get_sessionmaker
from ...logging_config import logger
from ...repositories.context import TenantContext, tenant_scope
from ...repositories.gmail import list_connected
from .client import execute_gmail_tool_async
from .importance_classifier import classify_email_importance
from .processing import EmailTextCleaner, ProcessedEmail, parse_gmail_fetch_response
from .seen_store import GmailSeenStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...agents.interaction_agent.runtime import InteractionAgentRuntime


def _resolve_interaction_runtime() -> InteractionAgentRuntime:
    from ...agents.interaction_agent.runtime import InteractionAgentRuntime

    return InteractionAgentRuntime()


DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_LOOKBACK_MINUTES = 10
DEFAULT_MAX_RESULTS = 50


class ImportantEmailWatcher:
    """Poll every connected tenant's Gmail and surface important messages."""

    def __init__(
        self,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
        *,
        sessionmaker=None,
    ) -> None:
        self._poll_interval = poll_interval_seconds
        self._lookback_minutes = lookback_minutes
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._cleaner = EmailTextCleaner(max_url_length=60)
        self._sessionmaker = sessionmaker
        # Per-tenant poll state. Was a pair of instance attributes describing
        # "the" user, which is exactly the bug.
        self._seeded: set[uuid.UUID] = set()
        self._last_poll: dict[uuid.UUID, datetime] = {}

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            if self._task and not self._task.done():
                return
            loop = asyncio.get_running_loop()
            self._running = True
            self._seeded.clear()
            self._last_poll.clear()
            self._task = loop.create_task(self._run(), name="important-email-watcher")
            logger.info(
                "Important email watcher started",
                extra={
                    "interval_seconds": self._poll_interval,
                    "lookback_minutes": self._lookback_minutes,
                },
            )

    async def stop(self) -> None:
        async with self._lock:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._task = None
                logger.info("Important email watcher stopped")

    async def _run(self) -> None:
        try:
            while self._running:
                try:
                    await self.poll_once()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.exception(
                        "Important email watcher poll failed", extra={"error": str(exc)}
                    )
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            raise

    # -- polling ---------------------------------------------------------

    def _session(self) -> AsyncSession:
        maker = self._sessionmaker or get_sessionmaker()
        return maker()

    async def poll_once(self) -> int:
        """Poll every connected tenant. Returns the number of tenants polled."""
        async with self._session() as session:
            tenants = await list_connected(session)

        if not tenants:
            logger.debug("No Gmail connections; skipping importance poll")
            return 0

        for user_id, composio_user_id, timezone_name in tenants:
            ctx = TenantContext(
                user_id=user_id,
                timezone=timezone_name,
                composio_user_id=composio_user_id,
            )
            try:
                with tenant_scope(ctx):
                    await self._poll_tenant(ctx)
            except Exception as exc:  # pragma: no cover - defensive
                # One tenant's failure must not stop the others.
                logger.warning(
                    "Important email poll failed for tenant",
                    extra={"user_id": str(user_id), "error": str(exc)},
                )
        return len(tenants)

    async def _poll_tenant(self, ctx: TenantContext) -> None:
        now = datetime.now(timezone.utc)
        first_poll = ctx.user_id not in self._seeded
        previous = self._last_poll.get(ctx.user_id)
        interval_cutoff = now - timedelta(seconds=self._poll_interval)
        cutoff_time = previous if previous and previous > interval_cutoff else interval_cutoff

        query = f"label:INBOX newer_than:{self._lookback_minutes}m"
        try:
            raw_result = await execute_gmail_tool_async(
                "GMAIL_FETCH_EMAILS",
                ctx.composio_user_id or "",
                arguments={
                    "query": query,
                    "include_payload": True,
                    "max_results": DEFAULT_MAX_RESULTS,
                },
            )
        except Exception as exc:
            logger.warning(
                "Failed to fetch Gmail messages for watcher",
                extra={"user_id": str(ctx.user_id), "error": str(exc)},
            )
            return

        processed_emails, _ = parse_gmail_fetch_response(
            raw_result, query=query, cleaner=self._cleaner
        )

        async with self._session() as session:
            seen = GmailSeenStore(session, ctx.user_id)

            expired = await seen.expire_stale()
            if expired:
                # Loud on purpose: these are messages we never managed to
                # classify inside the retry budget. Ids only, never bodies.
                logger.error(
                    "Gmail messages abandoned after exhausting the classify retry budget",
                    extra={"user_id": str(ctx.user_id), "message_ids": expired},
                )

            if not processed_emails:
                await session.commit()
                self._complete(ctx.user_id, now)
                return

            if first_poll:
                # Warmup: everything already in the inbox is pre-existing, not new.
                await seen.mark_classified(email.id for email in processed_emails)
                await session.commit()
                logger.info(
                    "Important email watcher completed initial warmup",
                    extra={"user_id": str(ctx.user_id), "skipped_ids": len(processed_emails)},
                )
                self._complete(ctx.user_id, now)
                return

            pending_ids = set(await seen.unprocessed(email.id for email in processed_emails))
            unseen = [email for email in processed_emails if email.id in pending_ids]

            if not unseen:
                await session.commit()
                logger.info(
                    "Important email watcher check complete",
                    extra={"user_id": str(ctx.user_id), "emails_reviewed": 0, "surfaced": 0},
                )
                self._complete(ctx.user_id, now)
                return

            unseen.sort(key=lambda email: email.timestamp or now)
            eligible, aged = self._split_by_age(unseen, cutoff_time)

            # Aged-out messages get a verdict of "too old to surface" — that is a
            # decision, so settling them is correct.
            if aged:
                await seen.mark_classified(email.id for email in aged)

            surfaced = 0
            failed = 0
            summaries: list[str] = []
            for email in eligible:
                result = await classify_email_importance(email)
                if not result.decided:
                    # No verdict: record the attempt so the retry budget starts
                    # ticking, but leave the message eligible for another pass.
                    await seen.mark_attempted([email.id])
                    failed += 1
                    continue
                await seen.mark_classified([email.id])
                if result.summary:
                    summaries.append(result.summary)
                    surfaced += 1

            await session.commit()

        for summary in summaries:
            await self._dispatch_summary(summary)

        logger.info(
            "Important email watcher check complete",
            extra={
                "user_id": str(ctx.user_id),
                "emails_reviewed": len(unseen),
                "surfaced": surfaced,
                "classify_failures": failed,
                "suppressed_for_age": len(aged),
            },
        )
        self._complete(ctx.user_id, now)

    def _split_by_age(
        self, emails: list[ProcessedEmail], cutoff: datetime
    ) -> tuple[list[ProcessedEmail], list[ProcessedEmail]]:
        """Partition into (recent enough to surface, too old).

        Both sides of the comparison are normalised to UTC. The original compared
        against a value converted into the *user's* timezone and patched naive
        timestamps with that zone (original ``:171-182``) — a tz-arithmetic path
        with three branches where one is enough.
        """
        eligible: list[ProcessedEmail] = []
        aged: list[ProcessedEmail] = []
        for email in emails:
            stamp = email.timestamp
            if stamp is None:
                eligible.append(email)
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            (aged if stamp.astimezone(timezone.utc) < cutoff else eligible).append(email)
        return eligible, aged

    def _complete(self, user_id: uuid.UUID, moment: datetime) -> None:
        self._last_poll[user_id] = moment
        self._seeded.add(user_id)

    async def _dispatch_summary(self, summary: str) -> None:
        """Hand the summary to the interaction agent, inside the tenant scope."""
        runtime = _resolve_interaction_runtime()
        try:
            await runtime.handle_agent_message(
                f"Important email watcher notification:\n{summary}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Failed to dispatch important email summary", extra={"error": str(exc)}
            )


_watcher_instance: ImportantEmailWatcher | None = None


def get_important_email_watcher() -> ImportantEmailWatcher:
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = ImportantEmailWatcher()
    return _watcher_instance


__all__ = ["ImportantEmailWatcher", "get_important_email_watcher"]
