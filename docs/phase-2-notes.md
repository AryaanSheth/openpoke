# Phase 2 — Durable jobs + exactly-once triggers

## Changes

### The queue

- `server/jobs/queue.py:50-68` (new) — the claim, verbatim from plan.md step 2:
  `UPDATE ... WHERE id IN (SELECT id ... ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT :batch)
  RETURNING *`. Mapped back to ORM `Job` objects via `select(Job).from_statement(...)`, so the
  worker never re-fetches what it just claimed. The module docstring names the three ways this
  query is usually written wrong (SELECT-then-UPDATE, LIMIT without a locking subquery, FOR
  UPDATE without SKIP LOCKED) so the next person to "simplify" it knows what breaks.
- `server/jobs/queue.py:113-151` — `enqueue()`. Dedupe is `ON CONFLICT DO NOTHING` against the
  unique index, not a `SELECT` first: two pollers racing on one occurrence must yield one job,
  and check-then-insert cannot guarantee that. Returns `None` on conflict, per the contract.
- `server/jobs/queue.py:71-96` — `fail()` is a single statement, not read-compute-write, so the
  backoff uses the **database** clock and no reaper can interleave. Backoff is
  `base * 2^(attempts-1)` capped, times a random factor in `[0.5, 1.0)` — equal jitter, so a
  batch that failed on one upstream outage does not retry in lockstep and re-create it.
  `attempts >= max_attempts` ⇒ `dead`, never deleted.
- `server/jobs/queue.py:98-111, 211-240` — `reap()`. `status='running' AND claimed_at` older
  than the stale window ⇒ back to `pending`, or straight to `dead` if attempts are already
  exhausted (a handler that kills its worker every time would otherwise be an infinite crash
  loop that `max_attempts` cannot stop, because `fail()` never gets to run).
- `server/jobs/queue.py:243-260` — `dedupe_key_for(user_id, *parts)`. Every key is namespaced by
  tenant. **Deviation from CONTRACT.md, deliberate** — see "New issues" #1.
- `server/jobs/__init__.py` (new) — `KIND_CHAT_TURN` / `KIND_TRIGGER_FIRE` / `KIND_EMAIL_POLL`
  and the status constants.
- `server/jobs/context.py` (new) — `ContextVar` carrying the executing job id, so
  `llm_usage.job_id` can be filled without threading a job id through every agent signature.

### The worker

- `server/jobs/worker.py` (new) — bounded `asyncio.Semaphore` (default 10, env-tunable), and a
  claim batch sized to **free slots**: `min(batch, concurrency - in_flight)`. Claiming more than
  you can run marks rows `running` while they queue, and the reaper then correctly decides those
  rows belong to a dead worker.
- `server/jobs/worker.py:273-297` — `_run_handler` binds the tenant from `jobs.user_id` before
  dispatch. This is the invariant Phase 1 found by firing a real trigger (phase-1-notes Fix 10):
  the poller has no tenant of its own, so the execution agent's first repository write raised
  `LookupError` and every recurring reminder died. Phase 1's 3-line `PHASE 1 STOPGAP` in
  `trigger_scheduler.py` is **deleted**, as it asked.
- `server/jobs/worker.py:193-208` — `_load_timezones` resolves the whole claimed batch's tenant
  timezones in one query. It was one extra session *per job* — an N+1 that, under the test
  harness's `NullPool`, meant a third physical connection per job.
- `server/jobs/worker.py:128-134` — cancellation (`SIGKILL` analogue) skips the drain: awaiting
  handlers that were never told to finish is how a shutdown hangs forever. In-flight rows stay
  `running` and the reaper takes them, which is the path the reaper exists for.
- `server/jobs/worker.py:220-228` — the reaper passes `exclude_worker=self.worker_id`. See
  "Fixes" #5.
- `server/jobs/handlers.py` (new) — `chat_turn` and `trigger_fire`. Handlers signal failure by
  **raising**: both agent runtimes swallow every exception and return `success=False`, which is
  exactly the shape that made the old detached task lose work silently.
- `server/worker.py` (new) — `python -m server.worker`. Runs the job worker, the trigger poller
  and (single-replica only) the importance watcher; installs SIGINT/SIGTERM handlers; closes the
  shared HTTP pool and disposes the engine on exit.

### Call sites

- `server/services/conversation/chat_handler.py` — `asyncio.create_task(_run_interaction())`
  replaced by an enqueue; returns `202` with `{"job_id": ...}`. The detached-task path is gone.
  The frontend proxy (`web/app/api/chat/route.ts`) forwards the body verbatim without parsing
  and keys off the status code, so the added body is backwards-compatible.
- `server/services/trigger_scheduler.py` — rewritten. `_in_flight: Set[int]` deleted;
  `_CLAIM_DUE_SQL` (`:68`) claims due triggers with the same `SKIP LOCKED` pattern; `poll_once`
  (`:211`) enqueues one `trigger_fire` job per occurrence **and advances the schedule in the same
  transaction**; `execute_trigger_occurrence` (`:278`) is the worker-side execution.
- `server/app.py:67-92` — background loops deleted from the API process; only an engine-dispose
  shutdown hook remains.
- `server/openrouter_client/client.py:43-73` — one lazily-built module-level `httpx.AsyncClient`
  with a keepalive pool, plus `close_http_client()`.
- `server/openrouter_client/client.py:179` — `max_tokens` on every request, default 4096
  (`OPENPOKE_LLM_MAX_TOKENS`).
- `server/openrouter_client/client.py:104-121, 190-199` — `Retry-After` honoured once on 429,
  capped at `llm_retry_after_max_s` (default 30 s). Delta-seconds form only.
- `server/openrouter_client/client.py:123-160, 214` — `_record_usage` writes `llm_usage`
  (`prompt_tokens`, `completion_tokens`, `model`, `user_id`, `job_id`). Wrapped so metering can
  never fail a turn.
- `server/agents/execution_agent/agent.py:45-110` — `window_transcript()`, tail-preserving with
  the original task instruction kept as a head block, an elision marker between, and a hard char
  ceiling applied at entry boundaries so a truncated prompt never contains half an XML tag.
- `server/agents/execution_agent/runtime.py:33-41` — passes `conversation_limit` and
  `history_char_budget`. This is the one-line regression that reopens the fuse if reverted.
- `server/config.py:97-145` — the Phase 2 settings block (worker concurrency/poll/reap/stale,
  backoff base+max, trigger poll interval, watcher flag, `llm_max_tokens`,
  `llm_retry_after_max_s`, execution-agent limit and char budget). **Note: `config.py` is Phase
  0's file.** Additive only, made after Phase 0 finished, in preference to scattering
  `os.getenv()` through five modules.

### Tests (mine, per the brief)

`tests/_jobs_support.py` (shared fixtures), `tests/test_job_durability.py` (8),
`tests/test_exactly_once.py` (7), `tests/test_concurrency_bounds.py` (3),
`tests/test_llm_client.py` (9), `tests/test_prompt_bounds.py` (10).

---

## Fixes

Bugs in existing code, fixed here.

1. **`chat_handler.py:47` — accepted work was not durable.** `asyncio.create_task(...)` then
   `202`. No reference retained (GC-eligible mid-flight), no persistence, no retry, no
   concurrency bound. A deploy dropped every in-flight turn *after* telling the client it was
   accepted. Now a committed row precedes the 202.
2. **`trigger_scheduler.py:34` — `_in_flight: Set[int]` was the only dedupe, and it is
   per-process.** Two replicas fired every reminder twice; the failure got *worse* with
   horizontal scale. Replaced by a row claim plus `UNIQUE(dedupe_key)` — two independent
   mechanisms, either of which suffices. Proven with two live `TriggerScheduler` instances
   (`test_two_schedulers_on_one_database_produce_one_job_per_occurrence`).
3. **`trigger_scheduler.py:58-66` — silent death.** The `try` wrapped the whole `while` loop, so
   one `OperationalError` ended trigger delivery for the process lifetime while `/health` stayed
   green. The `try` is now *inside* the loop (`:186-194`), and the same mistake is avoided in
   `worker.py:117-125`.
4. **`app.py:68-74` — background loops in every API process.** Deleted. Worker is a separate
   entrypoint and container.
5. **A worker's own reaper could reap its own in-flight job.** Found by
   `test_killed_worker_leaves_a_job_the_reaper_recovers`, which observed the handler running
   twice. With a short stale window, the reaper returned a job the same worker was still
   executing, which then re-claimed and re-ran it — a duplicate side effect manufactured
   entirely by the recovery mechanism. `reap(..., exclude_worker=)` removes the self-inflicted
   half: `claimed_by` is `host:pid`, unique to a process lifetime, so a worker alive enough to
   run its reaper still owns its own claims. Pinned by
   `test_a_worker_never_reaps_its_own_in_flight_job`.
6. **`openrouter_client/client.py:70` — a new `AsyncClient` (and TLS handshake) per LLM call**,
   on a path the agent loops hit up to 8 times per turn.
7. **`openrouter_client/client.py` never set `max_tokens`**, so OpenRouter reserved the model's
   full 64k output ceiling and a small credit balance 402'd immediately with *"requested up to
   64000 tokens, but can only afford N"* (BASELINE.md gotcha 1).
8. **`openrouter_client/client.py:82` discarded `usage`** on every response.
9. **`agent.py:39` + `runtime.py:33` — genuinely unbounded execution-agent prompts.**
   `conversation_limit=None` loaded the entire per-agent log into the system prompt every call.
   A trigger agent firing every 5 min crosses the context window in days, after which *every*
   call fails permanently. `test_a_trigger_agent_firing_every_five_minutes_for_three_days_stays_bounded`
   runs 864 fires: an ~818 KB transcript renders to ≤ 24 000 chars.
10. **A malformed `recurrence_rule` would have wedged the new poller** into re-claiming the same
    row every tick forever. Found while writing a test with a DTSTART-less RRULE — dateutil
    parses it, then cannot compare it to an aware `now`. `_advance` (`:113`) parks the trigger as
    `paused` with the reason in `last_error`.

---

## New issues

Found, not fixed.

1. **The trigger `dedupe_key` deviates from CONTRACT.md. (low, deliberate — but it is a contract
   change, so you must decide.)** The contract fixes `trigger:{trigger_id}:{occurrence_iso}`; my
   brief said to namespace *every* key with `user_id`. I applied the rule uniformly:
   `{user_id}:trigger:{trigger_id}:{occurrence_iso}`. The contract's key is not itself unsafe
   (trigger ids are a global sequence), but the rule is what survives the next kind being added.
   One line: `queue.py:dedupe_key_for`. If a Phase 4 test asserts the literal contract string, it
   is that line that changes, nothing else.
2. **`email_poll` is not implemented as a job kind. (medium.)** `ImportantEmailWatcher` keeps
   `_seeded` and `_last_poll` **per tenant in process memory**
   (`services/gmail/importance_watcher.py:75-76`). Turning a poll into a job would let two
   workers each perform their own warmup and re-classify the same inbox — worse than today.
   Instead the watcher runs as a loop inside the worker process, and
   **exactly one replica may enable it** (`OPENPOKE_WORKER_RUN_EMAIL_WATCHER=0` on the rest).
   That is a real horizontal-scaling limit I am introducing knowingly: the *job worker* and the
   *trigger poller* scale to N, the watcher does not. Fixing it means moving that state to
   Postgres, in a Phase 1 file.
3. **The stale window is the one remaining way to run a handler twice. (medium — inherent to a
   lease queue, named rather than solved.)** If `job_stale_after_s` is shorter than the slowest
   handler, another worker reaps a live job and re-runs it. Default is 300 s against an LLM turn
   that can be 8 tool iterations × a 60 s per-call timeout — **not a comfortable margin**. The
   real fix is a lease heartbeat (the handler touches `claimed_at` while it runs), roughly 15
   lines, deliberately not added under time pressure. Interim mitigation: raise
   `OPENPOKE_JOB_STALE_AFTER_S` above your p99 handler runtime.
4. **Trigger retry can produce a duplicate side effect. (medium, deliberate tradeoff.)**
   `execute_trigger_occurrence` raises on failure so the job retries, which recovers a transient
   429 — but an agent that failed *after* sending an email will send it again. Mitigated three
   ways: `TRIGGER_JOB_MAX_ATTEMPTS = 3` rather than the default 5; the tail-preserving agent
   history means a retried agent can see its own prior action; and the schedule advances at
   enqueue time so a permanent failure costs one occurrence, not the trigger. Real idempotency
   needs the agent tools themselves to be idempotent, which is out of scope.
5. **Phase 1's sync bridge survives. (high — inherited, and it moved rather than shrank.)**
   `repositories/context.run_sync` blocks the calling thread on a DB round trip. It is now the
   *worker's* loop it blocks, not the API's — which is a real improvement, since the API process
   no longer runs agent code at all — but with `worker_concurrency=10` a blocked loop stalls the
   other nine jobs. Deleting it means making `agents/interaction_agent/{runtime,tools}.py` async,
   which is outside my ownership list and is not a half-convertible change. Phase 1 flagged this
   as the first thing to fix if it survived; it survived.
6. **Nothing exposes job status to the client. (medium.)** `POST /chat/send` now returns a
   `job_id`, and there is no `GET /jobs/{id}` to poll it with — `routes/*` is Phase 1's. The
   frontend still infers completion from `/chat/history`, exactly as before, so this is not a
   regression; it is an unfinished half of the 202 contract. ~15 lines in a route file.
7. **The shared `httpx.AsyncClient` assumes one event loop per process. (low.)** True for the
   API and the worker today; Phase 1's bridge runs a second loop but makes no LLM calls. If that
   ever changes, the pool corrupts the same way asyncpg's does. Marked `# ponytail:` at
   `client.py:43`.
8. **`max_tokens=4096` is a judgement call, not a measurement. (low.)** It cuts the reservation
   16× from the 64k ceiling. If a legitimate response needs more it is truncated silently — the
   `finish_reason` would say `length` and nothing checks it. `llm_usage.completion_tokens`
   clustering at 4096 is the signal; that query is now possible, which is the point of step 9.
9. **Truncation, not summarisation, for agent history. (accepted, per plan.md.)** An execution
   agent forgets history beyond 20 requests / 24 000 chars. The head (original task) and tail
   (recent actions) are kept, so the repeat-a-completed-action risk is bounded to old actions.
   The deferred piece is per-agent summarisation reusing `summarization/summarizer.py`.
10. **Watcher throughput got worse under per-tenant correctness. (medium — Phase 1's #13,
    re-flagged.)** One poll is now N tenants × (1 Composio call + up to 50 sequential LLM calls),
    and it is on the worker's loop. plan.md defers classification concurrency; that deferral is
    more urgent now, not less. Not fixed: the watcher is a Phase 1 file.
11. **`tests/_jobs_support.py` requires exclusive use of the test database. (informational, but
    it will waste someone's afternoon.)** `claim()` has no `WHERE user_id` — by design, a worker
    services every tenant — so a second copy of the suite running against the same Postgres
    steals jobs and produces baffling count mismatches. This cost me a wrong diagnosis when three
    agents' pytest processes overlapped. `_no_worker_outlives_its_test` catches the in-process
    version of the same problem; the cross-process version needs a separate database.
12. **`server/services/__init__.py` still exports `get_trigger_scheduler`** and the API process
    still imports it transitively. Harmless (nothing calls `.start()` there any more) but the
    import graph no longer reflects the process topology.

---

## `TODO(phase-1-integration)` seams — the complete list

Four, all lazy imports so the module still imports if the Phase 1 internal moves.

| Location | Depends on | What to do |
|---|---|---|
| `server/jobs/worker.py:280` | `repositories.context.{TenantContext, tenant_scope}` | Nothing, if those names stay. This is the tenant binding from `jobs.user_id` — the invariant Phase 1's Fix 10 was a stopgap for. |
| `server/jobs/handlers.py:31` | `agents.interaction_agent.runtime` reaching Postgres through `run_sync` | Delete the comment when the bridge dies (New issues #5). No code change. |
| `server/openrouter_client/client.py:134` | `repositories.context.get_tenant()` | If tenancy stops being a `ContextVar`, `_record_usage` needs the user id threaded in from the caller instead. |
| `server/services/conversation/chat_handler.py:45` | `repositories.context.require_tenant()` | If `routes/chat.py` starts passing `user` explicitly, take it as a parameter and drop the ContextVar read. `routes/chat.py` needs **no change** as things stand. |

Deleted on Phase 1's instruction, for the record:
- the `PHASE 1 STOPGAP` block in `trigger_scheduler.py` (`_execute_trigger`'s `tenant_scope_for`
  wrapper) — replaced by tenant binding from the job row.
- `services/triggers/service.py:tenant_scope_for` is now **unused by the scheduler**. It is a
  Phase 1 file, so I left it in place rather than deleting a public method out from under
  whatever else might call it.
