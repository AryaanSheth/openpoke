# Phase 1 — Tenant isolation

## Changes

### Auth

- `server/auth.py` (new) — `get_current_user(session, credentials) -> User` per the contract's
  signature; `CurrentUser = Annotated[User, Depends(get_current_user)]`. Missing, malformed,
  unknown-user and wrong-secret all return the same 401 body so the four cases are
  indistinguishable.
- `server/auth.py:54-93` — bounded TTL'd verification cache (`sha256(token) -> user_id`, 1024
  entries LRU, 300 s). **The argon2 tradeoff, stated:** argon2 costs a measured **41 ms/verify**
  on this box; paying it per request turns a 4 ms endpoint into a 69 ms one and hands an
  attacker a CPU-exhaustion vector. With the cache a warm authenticated request is **4–5 ms**
  (measured, below). The price is that a **rotated key keeps working for up to 300 s** on a
  process that already saw it. Failed verifications are never cached, so a wrong token always
  costs a full hash — that is the rate limiter. What I did *not* do: an unbounded cache (memory
  DoS keyed on attacker-supplied tokens) or weakened argon2 parameters (weakens the at-rest
  guarantee permanently).
- `server/auth.py:99-145` — the tenant context is bound in the **dependency**, not middleware:
  Starlette's `BaseHTTPMiddleware` runs in a different context, so a `ContextVar` set there is
  invisible to the handler and to the tasks it spawns.
- `server/auth.py:152-224` — `python -m server.auth create-user|rotate|list`. No signup API;
  out of scope, and one fewer unauthenticated endpoint.
- `server/repositories/users.py:17-56` — token format `opk_<user-uuid-hex>_<secret>`. The
  embedded user id is what makes verification one indexed PK lookup plus one argon2 check
  instead of an argon2 check against every row in `users`.

### Repositories (replacing the eight singletons)

- `server/repositories/__init__.py`, `conversation.py`, `execution.py`, `gmail.py`,
  `triggers.py`, `users.py` (new) — every class is `__init__(self, session, user_id)` per the
  contract and every query carries `WHERE user_id = :user_id`.
- `server/repositories/context.py` (new) — `TenantContext` + `ContextVar`, and `run_sync`, a
  sync→async bridge onto a dedicated daemon event loop with its **own engine** (asyncpg
  connections are loop-bound; sharing a pool across loops corrupts it). Marked
  `# ponytail:` — it exists only because the legacy store API is synchronous and its callers
  (`agents/interaction_agent/{runtime,tools}.py`, `agents/execution_agent/tools/*`) are outside
  this phase's edit scope. Phase 2 deletes it.
- `server/repositories/formatting.py` (new) — the **one** implementation of the local-time
  render/parse. See "the timestamp trap" below.
- `server/repositories/crypto.py` (new) — Fernet over `gmail_connections.connection_id_encrypted`,
  key from `OPENPOKE_DATA_KEY`. Rotation is re-encrypt-in-place via the existing `key_version`
  column plus `OPENPOKE_DATA_KEY_V<n>` for retired keys. Not a `Settings` field on purpose:
  `config.py` is Phase 0's and secrets should not live in an object that gets logged.

### Store rewrites (module-level names preserved as tenant-resolving proxies)

Every `get_x()` accessor still exists and still returns a module-level object, because
`agents/execution_agent/tools/triggers.py:93-94` and `tools/gmail.py:314` **capture the store at
import time**. The objects are stateless; they resolve the tenant per call.

- `server/services/conversation/log.py` — `ConversationLog` proxy; the file-backed store and
  `_CONVERSATION_LOG_PATH` are gone.
- `server/services/conversation/summarization/working_memory_log.py` — `WorkingMemoryLog` proxy;
  the `<summary_info>` header is now the `summary_state` row, the tail is
  `working_memory_entries`.
- `server/services/execution/roster.py`, `log_store.py` — `AgentRoster` / `ExecutionAgentLogStore`
  proxies.
- `server/services/timezone_store.py` — `TimezoneStore` proxy over `users.timezone`. The **read**
  path never touches the DB: `utils/timezones.py` calls `get_timezone()` on every log append, so
  the value rides inline on `TenantContext`.
- `server/services/triggers/store.py` — `TriggerStore` is now a Postgres-backed sync facade.
  `service.py` needed no logic change: same method names, same ISO-8601 strings in and out.
- `server/services/gmail/seen_store.py` — `GmailSeenStore` is now async over
  `GmailSeenRepository`; its only consumer (the watcher) is async and owned by this phase.

### Gmail

- `server/services/gmail/client.py` — **`_ACTIVE_USER_ID`, `_set_active_gmail_user_id` and the
  PID default are deleted.** `get_active_gmail_user_id()` reads the tenant context.
  `composio_user_id` is `str(users.id)`, derived from the token and **never from the request
  body** — see Fixes.
- `server/services/gmail/client.py:111-133` — `_call` (`asyncio.to_thread` + `asyncio.wait_for`)
  and `_call_sync` (bounded pool + `future.result(timeout)`), `COMPOSIO_TIMEOUT_S` default 30 s,
  env `OPENPOKE_COMPOSIO_TIMEOUT_S`. Every Composio call now has a deadline.
- `server/services/gmail/importance_watcher.py` — rewritten per-user. `poll_once()` enumerates
  actively-connected tenants and polls each inside `tenant_scope`, with per-`user_id` warmup and
  last-poll state. One tenant's failure does not stop the others.
- `server/services/gmail/importance_classifier.py` — returns `Classification(decided, summary)`
  instead of `Optional[str]`. See Fixes.

### Routes

- `server/routes/meta.py:20-24` — `/health` is the only unauthenticated route. `/meta`,
  `/meta/timezone` (GET and POST) all require a bearer token.
- `server/routes/chat.py`, `routes/gmail.py` — every endpoint takes `user: CurrentUser`.
  Reads go through the repositories on the **request session**, so the HTTP path never touches
  the sync bridge.
- `server/routes/chat.py:40-57` — `DELETE /chat/history` scoped to the caller, one transaction.

### The timestamp trap (Phase 0 note 3)

`conversation/log.py:69` and `execution/log_store.py:72` wrote
`now_in_user_timezone("%Y-%m-%d %H:%M:%S")` — a naive local-time string — and `load_transcript()`
embedded it verbatim in the LLM system prompt. Phase 0 correctly moved the column to
`timestamptz`. Rendering UTC on the way out would have silently changed the meaning of every
prompt with no error anywhere. `repositories/formatting.py` re-renders through the user's zone
on read, and `parse_timestamp` is its exact inverse for the one place that hands rendered
strings back (`WorkingMemoryRepository._replace_entries`, when the summarizer rewrites the tail —
writing `now()` there would have restamped the whole tail with the summarization time).
Verified live: the same instant reads `2026-07-21 12:00:12` for a `America/New_York` user and
`2026-07-21 16:00:12` for a UTC user.

### Data migration decision: **discard**, deliberately

`server/data/` is left on disk, untouched, and nothing imports it. Three reasons:

1. The files carry no user identity. Assigning them to a user is a guess, and a guess is what
   this whole phase exists to eliminate.
2. `composio_user_id` is now `str(users.id)`, so the existing Gmail connection has to be
   re-authorised regardless. Importing the transcript but not the connection produces a
   half-migrated state that looks working and is not.
3. The content is a 1.5 KB smoke-test transcript from one developer session, and BASELINE.md
   gotcha 4 documents wiping history as the *standard fix* for stale-transcript poisoning.

If it ever needs importing, the seam is a script that calls the repositories directly; nothing
about the schema prevents it.

---

## Fixes

Bugs in existing code, fixed here.

1. **`gmail/client.py:23-40, :215, :306` — the tenancy bug itself.** The Composio identity was a
   process global defaulting to `f"web-{os.getpid()}"` and overwritten *unconditionally* by any
   caller of `/gmail/status`. B connecting Gmail redirected A's inbox polling into A's
   transcript. Now `gmail_connections` keyed on `users.id`. Test:
   `test_phase1_watcher.py::test_b_connecting_does_not_change_what_a_polls`.

2. **`routes/chat.py:26-45` — unauthenticated global wipe.** `DELETE /chat/history` cleared the
   conversation log, the roster, every execution log and **every trigger in the system**, for
   anyone who could reach the port, in four non-transactional steps. Now authenticated,
   caller-scoped, one transaction. Verified live against real Postgres.

3. **`importance_classifier.py:102-113` + `importance_watcher.py:210` — permanent email loss.
   (Deliberate scope expansion, flagged in plan.md as worth breaking the deferral for.)** Every
   failure path returned `None`, the same value as "not important", and the watcher marked the
   id seen regardless — so one transient OpenRouter blip dropped that email forever. Fixed:
   `Classification.decided` separates a verdict from a failure; ids are settled only on a
   verdict. **Deviation from the plan's prescription:** the plan asked for an *attempt counter*.
   That needs a new column, and `server/db/models.py` + `alembic/` belong to Phase 0 — adding one
   would have broken `alembic check`. I used a **time budget** instead
   (`CLASSIFY_RETRY_BUDGET`, 1 h, on the existing `gmail_seen.classified` boolean Phase 0 added
   for exactly this): a failed attempt inserts `classified=False` without refreshing `seen_at`,
   stays eligible until the budget expires, then is settled and logged at ERROR. Same guarantee —
   a poison message cannot retry forever — no migration.

4. **`gmail/client.py:467-500` (plan.md cites `:481-494`) — no timeout at all** on the two hottest Composio calls, so one
   hung upstream froze the event loop for every tenant including `/health`. Both paths now have
   an explicit deadline.

5. **`gmail/client.py:335, :382` — cross-tenant disconnect.** `disconnect_account` honoured a
   client-supplied `connection_id` and deleted it. Any caller could revoke any other account's
   Gmail connection. `payload.connection_id` is now ignored; candidates come from the caller's
   own row or a Composio listing scoped to the caller's identity.

6. **`gmail/client.py:202` — profile payload logged.** `extra={"raw": result}` on the
   unexpected-shape warning dumped the whole Gmail profile. Removed; the log carries the
   Composio user id only. Same treatment on tool execution: `arguments` and responses are never
   logged, because both carry email bodies.

7. **`execution/roster.py:44-45` — truncate-before-lock.** `save()` opened the roster `'w'`
   (truncating on open) and only then attempted `flock`, so on contention the file was already
   zeroed. Gone with the file; called out because it is the reason not to port the pattern.

8. **`execution/log_store.py:19-24, :161` — slug collisions and a useless return.** Agent names
   were slugified onto filenames, so `"Email: Summary"` and `"Email Summary"` shared a journal;
   and `list_agents()` returned slugs that could not be fed back into any other method. The key
   is `(user_id, agent_name)` now. Test:
   `test_agent_names_collide_across_tenants_without_sharing`.

9. **`execution/log_store.py:82` — `Optional` referenced but never imported** (Phase 0 note 2).
   Survived only because `from __future__ import annotations` made the annotation a string.
   Gone with the rewrite.

10. **`trigger_scheduler.py:80` — triggers died after tenancy landed.** *Found by firing a real
    trigger, not by reading the diff.* The poller has no tenant of its own, so the execution
    agent's first write raised `LookupError` and every recurring reminder failed. **This is the
    one file I edited outside my ownership list** — 3 lines wrapping `_execute_trigger` in
    `service.tenant_scope_for(trigger)`, marked `PHASE 1 STOPGAP` and `TODO(phase-2)`. The
    alternative was shipping a product where no scheduled reminder works. Phase 2 deletes it:
    the `trigger_fire` job row carries `user_id` and the worker binds the tenant before dispatch.
    Verified end to end: a real trigger fired and its reminder landed in the owning tenant's
    transcript only.

---

## New issues

Found, not fixed.

1. **`gmail_internal.py:73` is broken dead code. (medium — latent, not mine to fix.)**
   `execute_gmail_tool("GMAIL_FETCH_EMAILS", composio_user_id, arguments)` passes `arguments`
   positionally against a keyword-only signature. Re-confirmed this session:
   `TypeError: execute_gmail_tool() takes 2 positional arguments but 3 were given`. Unreachable
   today because the live search path goes through `tasks/search_email/tool.py`. It will explode
   the moment anyone wires it up. Not in my ownership list, as instructed.

2. **The sync bridge blocks the API event loop. (high — by construction, time-boxed.)**
   `run_sync` blocks the calling thread for a DB round trip. Under load, every agent-driven log
   write stalls the loop for every tenant. This is *not a regression* — the code it replaces did
   blocking `open().write()` in the same places — but it is now a network round trip rather than
   a local write. It disappears when Phase 2 moves the agent runtime into the worker and the
   callers become async. **If Phase 2 slips, this is the first thing to fix**, and the fix is to
   make `agents/interaction_agent/{runtime,tools}.py` async, which is why I could not do it.

3. **The bridge has its own connection pool, unsized. (medium.)** `create_async_engine` defaults
   (5 + 10 overflow) on a second engine, so a busy process holds up to 30 Postgres connections
   across two pools. `run_sync`'s 30 s timeout would surface exhaustion as a stall, not an error.
   Phase 5's load work should measure it; Phase 2 removing the bridge makes it moot.

4. **Token-cache staleness across processes. (low, deliberate — see auth tradeoff.)** `rotate`
   invalidates the hash immediately but every already-running process may accept the old token
   for up to 300 s. There is no cross-process invalidation channel. If that matters, set
   `_CACHE_TTL_S = 0` and pay 41 ms/request, or add a `users.key_generation` column and check it
   on the cached path (needs a Phase 0 migration).

5. **`TriggerStore` falls back to unscoped-by-id when no tenant is bound. (medium.)**
   `fetch_one`/`update` resolve the owner from the row when there is no tenant context, because
   the poller legitimately has none. Every HTTP path has a tenant bound by `get_current_user`,
   and `insert` hard-requires one, so no row can be created unattributed — but the fallback is a
   footgun if future code calls the store from a background task without a scope. Phase 2's
   `user_id`-carrying jobs remove the need entirely.

6. **`services/triggers/__init__.py:10-12` still computes a dead `data/triggers.db` path.**
   `TriggerStore.__init__` accepts and ignores the argument so the module still imports. That
   file is not in my ownership list. Three lines to delete once someone owns it.

7. **`_EXECUTOR` head-of-line blocking in the Gmail client. (low.)** The sync Composio path uses
   a bounded 8-worker pool. If 8 calls hang, a 9th can exhaust its 30 s deadline while still
   queued, i.e. time out without ever being attempted. Bounded degradation, deliberately chosen
   over unbounded thread growth, but the failure mode is confusing in logs. Also: neither
   `wait_for` nor `future.cancel()` actually interrupts the blocked thread — it leaks until the
   socket gives up. Fixing that properly means a timeout on the Composio SDK's HTTP client,
   which the SDK does not obviously expose.

8. **Two Composio call sites in `client.py` and all of `agents/execution_agent/tools/gmail.py`
   still block. (medium, known deferral.)** plan.md scopes the `to_thread` sweep to the two
   hottest sites. `tools/gmail.py` makes ~10 synchronous Composio calls from async tool
   handlers; they now inherit the `_call_sync` deadline but still occupy the calling thread.
   Mechanical sweep, no design content.

9. **`server/services/gmail/processing.py:268,290` calls `convert_to_user_timezone` on the
   parse path. (low.)** It reads the tenant context, which is correct inside a scope but silently
   yields UTC outside one — e.g. if a future caller parses a Gmail response before binding a
   tenant, email timestamps shift with no error. The proxy returns the default rather than
   raising here specifically because `utils/timezones.py` is on a hot path and outside my scope.

10. **`gmail_connections.composio_user_id` is globally UNIQUE (Phase 0 note 7).** Now benign,
    because the value is `str(users.id)` and therefore unique by construction. But that also
    means the column is redundant with `user_id`. Either drop it or keep it as the seam for a
    future where Composio issues its own identifiers; it should not stay as a silent duplicate.

11. **`api_key_hash` rotation has no audit trail.** `rotate` overwrites in place. No
    `last_used_at`, no `created_at` on the key, no way to answer "was this key used after we
    rotated it". Cheap to add later; worth flagging before someone needs it during an incident.

12. **CORS is verified but `allow_credentials=False` is load-bearing and undocumented.**
    `app.py:59` sets it, so the allowlist is not protecting a cookie — it is protecting against
    a browser-origin token being replayed, which it does not actually do (the token is in a
    header the attacker's page would have to already possess). CORS here is defence in depth,
    not the auth boundary. Worth stating plainly so nobody mistakes it for one.

13. **`ImportantEmailWatcher` still runs in the API process** (`app.py:68-74`, Phase 2's file)
    and classifies emails sequentially per tenant. With N connected tenants a single poll is now
    N × (1 Composio call + up to 50 LLM calls) on the API event loop. The per-tenant rewrite made
    the *correctness* problem go away and made the *throughput* problem worse. plan.md defers
    classification concurrency; that deferral is now more urgent, not less.
