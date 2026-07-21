"""Background worker entrypoint: ``python -m server.worker``.

Separate process, separate container. Three loops run here and none of them run
in the API process any more:

* the **job worker** — claims and executes ``jobs`` rows (N replicas safe)
* the **trigger poller** — turns due triggers into jobs (N replicas safe)
* the **importance watcher** — polls Gmail (**one replica only**, see below)

The watcher keeps ``_seeded`` / ``_last_poll`` per tenant in process memory
(``services/gmail/importance_watcher.py``), so two replicas would each perform
their own warmup and re-classify the same inbox. Set
``OPENPOKE_WORKER_RUN_EMAIL_WATCHER=0`` on every replica but one. Moving that
state into Postgres — which is what makes ``email_poll`` a real job kind — needs
the watcher rewritten, and it is not this phase's file.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from .config import get_settings
from .db.engine import dispose_engine
from .jobs.worker import new_worker
from .logging_config import configure_logging, logger


async def _amain() -> int:
    configure_logging()
    settings = get_settings()

    worker = new_worker()
    from .services.trigger_scheduler import TriggerScheduler

    scheduler = TriggerScheduler(worker_id=worker.worker_id)

    watcher = None
    if settings.worker_run_email_watcher:
        from .services.gmail import get_important_email_watcher

        watcher = get_important_email_watcher()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)

    await scheduler.start()
    if watcher is not None:
        await watcher.start()

    try:
        await worker.run()
    finally:
        await scheduler.stop()
        if watcher is not None:
            await watcher.stop()
        from .openrouter_client.client import close_http_client

        await close_http_client()
        await dispose_engine()
        logger.info("worker shutdown complete")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover - CLI invocation guard
    raise SystemExit(main())


__all__ = ["main"]
