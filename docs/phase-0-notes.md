# Phase 0 — Foundation

## Changes

- `server/requirements.txt:13-19` — added `sqlalchemy[asyncio]==2.0.51`, `asyncpg==0.31.0`, `alembic==1.18.5`, `pydantic-settings==2.14.2`, `cryptography==49.0.0`, `argon2-cffi==25.1.0`, all pinned to versions verified installing on Python 3.14.5. The seven original pins are untouched.
- `pyproject.toml` (new) — dev deps (`pytest==9.1.1`, `pytest-asyncio==1.4.0`, `ruff==0.15.22`, `mypy==2.3.0`) under `[project.optional-dependencies] dev`, plus `[tool.pytest.ini_options]` with `asyncio_mode = "auto"` and session-scoped loops, ruff config, and mypy config.
- `server/config.py` — rewritten from `pydantic.BaseModel` to `pydantic_settings.BaseSettings`. Settings now resolve at construction, not at import, so tests can override anything.
- `server/config.py:11-25` — deleted the hand-rolled `_load_env_file`; pydantic-settings' `env_file` reads the same repo-root `.env` (which is a symlink to `.env.local` here — verified it loads through the symlink).
- `server/config.py:58` — new `database_url`, env `DATABASE_URL`, default `postgresql+asyncpg://openpoke:openpoke@localhost:5432/openpoke`.
- `server/config.py:60-73` — the five model IDs are now env-overridable (`OPENPOKE_INTERACTION_AGENT_MODEL`, `OPENPOKE_EXECUTION_AGENT_MODEL`, `OPENPOKE_EXECUTION_AGENT_SEARCH_MODEL`, `OPENPOKE_SUMMARIZER_MODEL`, `OPENPOKE_EMAIL_CLASSIFIER_MODEL`). Defaults unchanged at `anthropic/claude-sonnet-4` per the contract — BASELINE.md verified OpenRouter still serves it, and the Haiku routing needs Phase 4 fixtures as evidence.
- `server/config.py:83-85` — CORS default changed from `*` to `http://localhost:3000`. Explicit `OPENPOKE_CORS_ALLOW_ORIGINS=*` still yields `["*"]`, so the escape hatch survives.
- `server/config.py` — every existing setting name and all three properties (`cors_allow_origins`, `resolved_docs_url`, `summarization_enabled`) kept working; the 60+ importers and `Settings = Depends(get_settings)` call sites are unchanged.
- `docker-compose.yml` (new) — `postgres:16` with a `pg_isready` healthcheck and a named volume, plus `api` and `worker`. `docker compose up -d postgres` works standalone today, which is what the tests need.
- `server/db/engine.py` (new) — lazy `get_engine()` / `get_sessionmaker()` / `dispose_engine()`. Nothing connects at import time.
- `server/db/session.py` (new) — `get_session()` FastAPI dependency, commit-on-success / rollback-on-error.
- `server/db/models.py` (new) — all 11 tables per the contract's names: `users`, `gmail_connections`, `conversation_entries`, `working_memory_entries`, `summary_state`, `agents`, `agent_log_entries`, `triggers`, `gmail_seen`, `llm_usage`, `jobs`. Every tenant-scoped table has `user_id` plus a composite index leading with it.
- `server/db/models.py` — `jobs` has the partial index `ix_jobs_pending_run_at ON (status, run_at) WHERE status='pending'` and `UNIQUE(dedupe_key)` in the initial revision, so Phase 2 needs no follow-up migration. A second partial index `ix_jobs_running_claimed_at ON (claimed_at) WHERE status='running'` supports the reaper.
- `server/db/models.py` — `triggers` mirrors the SQLite column set from `services/triggers/store.py:36-51` (`agent_name, payload, start_time, next_trigger, recurrence_rule, timezone, status, last_error, created_at, updated_at`) plus `user_id`, `claimed_at`, `claimed_by`. `id` stays integer because the contract's `trigger_fire` payload passes `trigger_id: int`.
- `alembic.ini:89` — `sqlalchemy.url` deliberately unset and commented out.
- `alembic/env.py` — reads the URL from `server.config.get_settings().database_url`, targets `server.db.models.Base.metadata`, `compare_type` and `compare_server_default` on so `alembic check` catches real drift. Supports `-x db_url=...` for one-offs.
- `alembic/versions/a2c761a21887_initial_schema.py` (new) — the single initial revision.
- `tests/conftest.py` (new) — session-scoped engine (migrated with real Alembic, not `create_all`), function-scoped transaction-rollback `db_session` (no truncation), `httpx.ASGITransport` `client` fixture, and `LLMStub`.
- `tests/test_phase0_smoke.py` (new) — 9 tests proving the harness itself works: per-test settings override, CORS default, rollback isolation, `/api/v1/health` through the ASGI client, LLM stub replay, and that both network seams are blocked.

## Fixes

- `server/config.py:24` (original) — `_load_env_file`'s bare `except Exception: pass` swallowed a malformed `.env` entirely; the server then started and died on the first LLM call with "API key not configured" (BASELINE.md gotcha 1). Gone with the function.
- `server/config.py:35-39` (original) — `_env_int` swallowed a non-numeric `OPENPOKE_PORT` and silently used the fallback. Pydantic now raises a `ValidationError` at startup instead.
- `server/config.py:67` (original) — `enable_docs` was `os.getenv("OPENPOKE_ENABLE_DOCS", "1") != "0"`, so `OPENPOKE_ENABLE_DOCS=false` **enabled** docs. Pydantic's bool parsing now reads `false`/`no`/`off`/`0` correctly.
- `server/config.py:66` (original) — CORS defaulted to `*` (plan Problem 1). Now an allowlist by default.

## New issues

Found while reading the existing code. None fixed — every one lands in a file another phase owns.

1. **`gmail_internal.py:73` is broken dead code. (medium — latent)** `execute_gmail_tool("GMAIL_FETCH_EMAILS", composio_user_id, arguments)` passes `arguments` positionally, but `services/gmail/client.py:473-479` declares it keyword-only. Verified: `TypeError: execute_gmail_tool() takes 2 positional arguments but 3 were given`. It has never fired because the live search path goes through `tool.py:_perform_search`, not this function — `gmail_fetch_emails` is referenced only as a *string* (`schemas.py:11`) and in prompt text (`system_prompt.py:20`). So it is dead code that will explode the moment anyone wires it up. Either delete it or fix the call; Phase 1/2 own the file.

2. **`services/execution/log_store.py:82` references an unimported `Optional`. (low)** The module imports `Dict, Iterator, List, Tuple` only. It survives purely because `from __future__ import annotations` makes the annotation a string — any `typing.get_type_hints()`, pydantic `validate_call`, or a future dataclass conversion raises `NameError`. Phase 1 rewrites this file anyway.

3. **Log timestamps are local-time strings with no offset, and they go straight into LLM prompts. (medium — will bite Phase 1)** `conversation/log.py:69` and `execution/log_store.py:74` write `now_in_user_timezone("%Y-%m-%d %H:%M:%S")` and `load_transcript()` embeds that exact string in the system prompt. I stored `ts` as `timestamptz`, which is correct, but Phase 1 must re-render it through the user's timezone at prompt-build time or every prompt silently changes shape. Phase 4's behavioral fixtures will catch this — after the fact.

4. **`triggers` timestamps changed type; the API contract did not. (medium — will bite Phase 1)** SQLite stored ISO-8601 `...Z` strings (`triggers/utils.py:to_storage_timestamp`), and `TriggerRecord` (`triggers/models.py:16-23`) declares `start_time`/`next_trigger`/`created_at`/`updated_at` as `str`. The Postgres columns are `timestamptz`. Phase 1 must serialize back through `to_storage_timestamp` or the trigger API response shape changes for the frontend.

5. **No data-migration path for existing local state. (medium)** Phase 0 created the tables but changed no store, so `server/data/{conversation/*.log, execution_agents/*, triggers.db, gmail_seen.json, timezone.txt}` are still the live source of truth. Nothing backfills them into Postgres. Phase 1 either writes a one-shot importer or the developer's local history is lost at cutover. Worth deciding explicitly rather than discovering.

6. **`jobs.dedupe_key` is globally unique, not per-tenant. (medium — Phase 2 must know)** Safe for the contract's `trigger:{trigger_id}:{occurrence_iso}` because trigger ids are global. But a future key built from tenant-scoped data — `email_poll:{date}`, say — would collide *across tenants* and silently drop one user's job. Phase 2 must namespace every dedupe key with `user_id`, or we swap to `UNIQUE(user_id, dedupe_key)`.

7. **`gmail_connections.composio_user_id` is UNIQUE, which assumes one Composio identity per tenant forever. (low)** Needed so the importance watcher can resolve Composio-id → tenant. If a user disconnects and reconnects and Composio issues a different id, you get a second row for the same tenant with no "current" flag. Phase 1 should decide upsert-vs-insert and possibly add a partial unique on `(user_id) WHERE status='ACTIVE'`.

8. **CORS default change is a breaking change for some local setups. (low, deliberate)** Anything not on `http://localhost:3000` now fails CORS — including the `:3001` case BASELINE.md gotcha 2 describes, where Next.js auto-increments the port. Fix is `OPENPOKE_CORS_ALLOW_ORIGINS=http://localhost:3001`. Called out because it will look like a regression.

9. **16 mypy errors in the pre-existing tree, suppressed rather than fixed. (low, scope)** `pyproject.toml` `[[tool.mypy.overrides]] ignore_errors = true` covers `server.services.*`, `server.agents.*`, `server.routes.*` etc. so the gate is meaningful for new code. Items 1 and 2 above came out of that run — the rest are `str | None` sloppiness. Each owner should delete their module from that list.

10. **`docker compose up api` / `worker` do not work yet. (expected)** There is no `Dockerfile` (Phase 6) and no `server/worker.py` (Phase 2). Compose is written so `postgres` is independently usable, which is all Phase 0 needs.

11. **`gmail_seen.classified` exists but nothing sets it. (informational)** I added the boolean beyond the spec so Phase 1 can implement "mark seen only on successful classification" — the permanent-email-loss bug the plan flags at `importance_classifier.py:102-113` / `importance_watcher.py:210` — without needing a second migration. It defaults to `false` and is currently dead weight if Phase 1 declines to use it.

12. **Background loops still start in the API process.** `app.py:68-74` is unchanged and Phase 2 owns it; noting only that the test harness sidesteps it by accident, not by design — `httpx.ASGITransport` does not run lifespan events, so `pytest` never starts the scheduler or the watcher. If Phase 4 ever switches to a lifespan-running client, the watcher will start inside the test suite and try to poll Gmail.
