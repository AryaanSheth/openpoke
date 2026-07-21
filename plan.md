# OpenPoke: Local Prototype → Production-Shaped

## Context

OpenPoke works locally for one person. It cannot serve two people, and it silently
loses work on every deploy. Both are structural, not incidental.

The audit found three classes of failure. We fix the two that are **definitional
blockers** — the ones where "add more servers" makes things *worse*, not better —
and deliberately defer the third.

### Problem 1 — There is no tenancy. At all.

Every store is a module-level singleton bound to a hardcoded path, constructed at
*import time*:

| Store | Path | Constructor takes a user? |
|---|---|---|
| `ConversationLog` | `data/conversation/poke_conversation.log` | No (`log.py:55`) |
| `WorkingMemoryLog` | `data/conversation/poke_working_memory.log` | No (`working_memory_log.py:44`) |
| `AgentRoster` | `data/execution_agents/roster.json` | No (`roster.py:14`) |
| `ExecutionAgentLogStore` | `data/execution_agents/<slug>.log` | No (`log_store.py:45`) |
| `TriggerStore` | `data/triggers.db` | No (`store.py:16`) |
| `GmailSeenStore` | `data/gmail_seen.json` | No (`seen_store.py:17`) |
| `TimezoneStore` | `data/timezone.txt` | No (`timezone_store.py:17`) |

The only identifier called `user_id` is a **process-global that defaults to the PID**
(`gmail/client.py:24`, `:215`: `user_id = payload.user_id or f"web-{os.getpid()}"`),
overwritten unconditionally by any caller of `/gmail/status` (`client.py:306`). The
background watcher reads that global (`importance_watcher.py:113`) and writes results
into the one shared conversation log.

**Consequence:** user B connecting Gmail redirects user A's inbox polling into A's
transcript. `DELETE /chat/history` (`routes/chat.py:26-45`) is unauthenticated and
clears the global log, roster, execution logs, *and every trigger in the system*.
There is no auth of any kind, and CORS defaults to `*` (`config.py:66`).

### Problem 2 — Accepted work is not durable, and duplicates across replicas.

`chat_handler.py:47` returns `202 Accepted` and fires the real work into a detached
task:
```python
asyncio.create_task(_run_interaction())
return PlainTextResponse("", status_code=status.HTTP_202_ACCEPTED)
```
No reference is retained (GC-eligible mid-flight), no persistence, no retry, no
concurrency bound. A deploy drops every in-flight turn silently — the client already
got its 202.

The trigger scheduler dedupes with an **in-process set** (`trigger_scheduler.py:34`:
`self._in_flight: Set[int]`) and `fetch_due` (`store.py:104-118`) is a plain `SELECT`
that never marks a row claimed. `app.py:68-74` starts the scheduler in *every*
process. Two workers ⇒ every reminder fires twice, every important-email alert sends
twice. This is the failure that gets *worse* with horizontal scale.

### Problem 3 — LLM resilience & unbounded cost. **We are not fixing this. Here's why.**

It is real. `openrouter_client/client.py:70-86` has no retry, no backoff, no 429 or
`Retry-After` handling, and builds a **new `AsyncClient` per call** (fresh TLS handshake
on every LLM request). The whole repo contains exactly one retry — a 2-attempt, zero-backoff
loop in `summarizer.py:31-67`. Composio calls have **no timeout at all**
(`gmail/client.py:481-494`), so one hung upstream call freezes the entire event loop.

Worst of all, execution-agent prompts grow **without bound**: `ExecutionAgentRuntime`
constructs `ExecutionAgent(agent_name)` with no `conversation_limit`, so it defaults to
`None` (`agent.py:39`) and `build_system_prompt_with_history` (`agent.py:63-96`) loads the
entire per-agent log into the system prompt every call. A trigger agent firing every 5
minutes grows monotonically until it exceeds the context window, after which **every call
fails permanently.**

**The reason we defer it anyway: Problems 1 and 2 are one-way doors. Problem 3 is not.**

Tenancy and durable execution dictate the *shape* of every module — schema, function
signatures, process topology. Retrofitting them means touching the same files a second
time. Problem 3 is entirely localized: retries live inside one 91-line client, prompt
caps are a constructor argument, cost metering is an additive column. **No other code's
shape depends on any of it.** Doing it first buys nothing; doing it later costs nothing.

**It also shrinks once 1 and 2 land:**
- Problem 2's queue gives **job-level retry with backoff for free**. Coarser than
  client-level retry, but a transient 429 stops being permanent data loss — today it is,
  because the 202 was already returned and the detached task only logs (`chat_handler.py:44-45`).
- Problem 1 puts `user_id` on every row, which makes per-tenant cost attribution a ~20-line
  `INSERT` rather than a refactor. **Cheap enough that we now do it** — see Phase 2 step 9.
  Metering is what keeps the rest of this deferral honest rather than unfalsifiable.
- The trigger scheduler's silent-death bug (`trigger_scheduler.py:58-66` — the `try` wraps
  the whole loop, so one `OperationalError` kills all triggers for the process lifetime
  while `/health` stays green) is fixed incidentally, because that poller is rewritten in Phase 2.

**On context growth specifically — what is and isn't deferred.**

Get the curve right before defending it: prompt size grows *linearly* with history, so a
user's N turns cost **O(N²)** — quadratic per user, not exponential. Across users it's
linear, so 10k users is a 10k× multiplier on a bill you'd expect. The real hazard is not
aggregate user count; it's **a single long-lived agent**, and it is severe:

> A trigger agent firing every 5 min appends ~1KB/fire × 288 fires/day. By **day 3** its
> system prompt is ~200k tokens — roughly **$0.60/fire ≈ $170/day for one agent** — then it
> crosses the context window and **every call fails permanently.** A fuse, not a curve.

The two paths are in different states and must not be conflated:

| Path | State today | Action |
|---|---|---|
| Interaction agent (`runtime.py:194-199`) | **Already bounded** — summarization compacts at threshold 100 + tail 10. Unbounded `load_transcript()` is only the fallback when summarization is off or errored. | Add a guard so the fallback can't silently unbound; otherwise leave. |
| Execution agents (`agent.py:39,63-96`) | **Genuinely unbounded** — `conversation_limit=None`, no summarization at all. | **Fixed today**, Phase 2 step 8. |

So the unbounded failure **is in scope**. What's deferred is replacing truncation with
real per-agent summarization — a *quality* improvement, not a cost or availability fix.

**Impact we are accepting, stated plainly:**
1. **Execution agents will forget their own older history**, because the Phase 2 step 8 cap
   truncates rather than summarizes. Real risk: an agent that has lost the record of a
   completed action could repeat it (re-send an email). Mitigated by making the window
   **tail-preserving** — keep the original task instruction plus the most recent N entries,
   not a blind head-truncation. Proper summarization (reusing the existing
   `summarization/summarizer.py` machinery per agent) is the deferred piece.
2. **Sequential per-email classification remains** — up to 50 Sonnet calls per 60s poll
   (`importance_watcher.py:200-207`), which can exceed the poll interval. Worse, a
   transient failure is indistinguishable from "not important" (`importance_classifier.py:102-113`)
   while the id is still marked seen (`:210`), so **that email is dropped forever.**
   This is the sharpest edge we are knowingly leaving in.
3. **Blocking Composio I/O** stalls the loop, including `/health` and every other tenant's
   turn. We fix only the two hottest call sites (Phase 2 step 7) and document the rest.

**Two carve-outs we keep**, because they're ~15 lines inside a client Phase 2 already
touches and they prevent retry storms against our own upstream: a shared module-level
`AsyncClient`, and `Retry-After` honoring on 429.

---

## Decisions I made (override any of these)

| Decision | Choice | Why |
|---|---|---|
| Datastore | **Postgres** (SQLAlchemy 2.0 async + asyncpg) | `FOR UPDATE SKIP LOCKED` is the whole exactly-once story. SQLite's single writer is the exact thing under contention. |
| Queue | **A Postgres table**, not Redis/Celery/SQS | Zero new infra, transactional enqueue with the state change. Ceiling is **unmeasured** — see "Do we actually need a real queue?" below. Measured in Phase 5 step 1 before we rely on it. |
| Migrations | **Alembic**, one initial revision | CI gate on drift; the thing that makes "deployable" true rather than claimed. |
| Cloud | **Docker-first, cloud-agnostic**, with an AWS/GCP/Fly mapping doc | Lets you answer whichever cloud the room asks about instead of betting on one. |
| Infra depth | Tests + CI + Docker **actually run**; cloud topology is written | A green pipeline is evidence. A provisioned VPC is a bill. |
| Auth | Bearer token → `users` row, hashed at rest | Enough to make tenancy real and testable. Not an IdP integration. |

### Do we actually need a real queue? — derive demand, don't assert capacity

Any "Postgres can do N jobs/s" figure is hardware-dependent folklore until measured on our
box. So derive the requirement instead; it's the number that decides the design.

At **10,000 users**, stated assumptions:

| Source | Assumption | Jobs/s (avg) |
|---|---|---|
| Chat turns | 50 msgs/user/day | ~6 |
| Trigger fires | 5 recurring triggers/user, hourly | ~14 |
| Email polls | 1 poll/user/60s | **~167** |
| Email classification | ~2 important emails/user/day | ~0.2 |

**Order 200/s average, ~500-1000/s at diurnal peak.** So the question isn't whether Postgres
can do 5k/s — it's whether it can do ~1k/s, which is a far less heroic claim.

**What actually governs it** (mechanism, not magnitude): throughput is bound by **commits/sec**,
not rows. Each job costs ~3 row-writes (insert → claim → complete) and a WAL fsync per commit,
so the dominant lever is **batch size** — claiming N jobs in one `UPDATE ... RETURNING` is one
commit for N jobs. Second-order: every `UPDATE` leaves a dead tuple, and a queue table is the
textbook autovacuum-bloat case. **Deliberately no numbers here** — the mechanism is what you
reason from; the magnitude comes from Phase 5 step 1 on our hardware.

**The two things this reframing exposes — both more important than the queue ceiling:**

1. **167 Gmail polls/s is a Composio rate-limit problem long before it is a Postgres
   problem.** Per-user polling doesn't survive 10k users regardless of our queue. The fix is
   push (Gmail watch + Pub/Sub) or heavily staggered batch polling — a design change we
   should name now even though it's out of scope today.
2. **Worker concurrency, not queue throughput, is the real ceiling.** At ~10s of LLM latency
   per job and 200 jobs/s, Little's Law gives **~2,000 jobs in flight**. Handing out rows is
   trivial; having 2,000 concurrent LLM calls in budget and under OpenRouter's rate limit is
   the actual constraint. The queue is nowhere near being the bottleneck.

**Ceiling to name out loud:** one Postgres primary. The seam past it is tenant-hash sharding
or Citus; the queue table is the first thing to move to SQS/Redis Streams — and the trigger
to move is measured commits/s, not a guess. Say this before you're asked.

---

## Phase 0 — Foundation *(do first, blocks everything; 1 agent)*

Nothing else can start until the DB layer and test harness exist.

**Files:** new `server/db/` (`engine.py`, `models.py`, `session.py`), `alembic/`,
`docker-compose.yml`, `server/requirements.txt`, `pyproject.toml`, `tests/conftest.py`

1. Add deps: `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `alembic`, `pydantic-settings`,
   `cryptography`, `argon2-cffi`; dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
   **Pin them** — current requirements are all unbounded `>=`.
2. `docker-compose.yml`: `postgres:16` + api + worker. This is also the test DB.
3. Schema (one Alembic revision). Every tenant-scoped table carries `user_id` with a
   composite index:
   - `users` (id, email, `api_key_hash`, timezone, created_at)
   - `gmail_connections` (user_id, `composio_user_id`, `connection_id_encrypted`, status)
   - `conversation_entries` (user_id, seq, tag, payload, ts) — replaces the `.log` files
   - `working_memory_entries`, `summary_state`
   - `agents` (user_id, name) — replaces `roster.json`; kills the `_slugify` filename collision
   - `agent_log_entries` (user_id, agent_name, …) — replaces `<slug>.log`
   - `triggers` (+ `user_id`, `claimed_at`, `claimed_by`) — migrated from the SQLite table
   - `gmail_seen` (user_id, message_id) — unique constraint replaces the JSON deque
   - `llm_usage` (user_id, job_id, model, prompt_tokens, completion_tokens, ts) — Phase 2 step 9
   - `jobs` — see Phase 2
3b. **⚠️ Check the model ID before anything else.** All five model settings
   (`config.py:54-58`) are hardcoded to `anthropic/claude-sonnet-4`. **Sonnet 4
   (`claude-sonnet-4-20250514`) retired 2026-06-15** — roughly five weeks before this plan was
   written. Retired models 404 on the first-party API; this repo calls through OpenRouter, so
   whether it still resolves depends on OpenRouter's catalog — **verify with one live call before
   assuming the app runs at all.** Replacement is `claude-sonnet-5` (or `claude-opus-4-8` for the
   interaction agent if quality matters more than cost). Sonnet-tier pricing is $3/M input,
   $15/M output — the basis for the $170/day figure above. Make the IDs env-overridable in the
   same change (step 4).

   **Route by role, don't swap globally.** The config already has five separate model settings —
   use them. Haiku 4.5 is **$1/$5 per MTok vs Sonnet's $3/$15 (3× cheaper both sides)**, but
   **200K context vs 1M**, which is the deciding constraint on the history-carrying agents.

   | Setting (`config.py:54-58`) | Model | Why |
   |---|---|---|
   | `email_classifier_model` | **Haiku** | Binary classify + short summary against a fixed tool schema (`importance_classifier.py:15-45`). Textbook Haiku task, and **by far the highest call volume** — this one line is most of the savings. |
   | `summarizer_model` | **Haiku** | Compress a transcript. Single call, no tool loop, quality bar is low. |
   | `execution_agent_search_model` | **Haiku, verify** | Mostly filtering/extraction over email results. Validate with fixtures before committing. |
   | `execution_agent_model` | **Sonnet** | Multi-step tool loop with side effects (sends real email). 200K context is also tight against per-agent history. |
   | `interaction_agent_model` | **Sonnet** | Orchestrator: 8-iteration tool loop, decides delegation, writes user-facing text. The one place quality is the product. |

   **Order of operations:** the Phase 4 behavioral fixtures are exactly the harness for validating
   a downgrade — build them first, then downgrade with evidence instead of vibes. And note the
   200K ceiling makes the Phase 2 step 8 prompt cap *more* urgent, not less: on Haiku the
   context fuse blows 5× sooner.
4. **Fix `config.py` first.** It's `pydantic.BaseModel` with `Field(default=os.getenv(...))`
   evaluated at *import time* and `@lru_cache` on `get_settings()`. Settings are frozen
   at first import and cannot be overridden per-test — this is the single biggest
   testability blocker. Convert to `pydantic_settings.BaseSettings`, drop the hand-rolled
   `_load_env_file` (note its `except Exception: pass` at `config.py:24` swallows malformed
   `.env` silently). Add env overrides for the 5 model names, which are currently
   hardcoded with no override (`config.py:54-58`).
5. `tests/conftest.py`: session-scoped engine, **function-scoped transaction rollback**
   fixture, `httpx.ASGITransport` client, and an `LLMStub` that records calls. No test
   ever touches OpenRouter or Composio.

**Done when:** `docker compose up` gives a migrated DB and `pytest` collects and passes zero tests.

---

## Phase 1 — Tenant isolation *(1 agent, after Phase 0)*

**Files:** new `server/auth.py`, `server/repositories/*.py`; rewrite
`services/conversation/log.py`, `summarization/working_memory_log.py`,
`execution/roster.py`, `execution/log_store.py`, `gmail/seen_store.py`,
`timezone_store.py`, `triggers/store.py`, `gmail/client.py`; `routes/*.py`

1. `auth.py`: `get_current_user` FastAPI dependency. `Authorization: Bearer <token>` →
   argon2-verify against `users.api_key_hash` → `User`. Missing/bad ⇒ 401. Apply to
   every route except `/health`.
2. **Delete the singletons.** Replace each `get_x()` module global with a repository
   class taking `(session, user_id)`, constructed per request via `Depends`. This is the
   mechanical bulk of the phase — same pattern, ~8 stores. Representative:
   `services/conversation/log.py:214`, `execution/roster.py:87`, `triggers/__init__.py:12`.
3. Every query gets `WHERE user_id = :user_id`. Cross-tenant access returns **404, not
   403** — don't leak existence.
4. Kill `_ACTIVE_USER_ID` (`gmail/client.py:23-40`). The Composio user id comes from the
   authenticated user's `gmail_connections` row. Remove the PID default at `client.py:215`.
5. Scope `DELETE /chat/history` (`routes/chat.py:26-45`) to the caller, in one transaction.
6. Lock CORS to an allowlist; docs off unless explicitly enabled.

**Watch for:** `roster.py:44-58` opens with `'w'` (truncating) *before* taking `flock` —
on contention the file is already zeroed. Don't port that pattern; it disappears with the DB.

**Done when:** Phase 4's isolation tests pass.

---

## Phase 2 — Durable jobs + exactly-once triggers *(1 agent, after Phase 0)*

Can run in parallel with Phase 1 — coordinate only on the `jobs` table shape.

**Files:** new `server/jobs/` (`models.py`, `queue.py`, `worker.py`),
`server/worker.py` entrypoint; rewrite `services/trigger_scheduler.py`,
`services/conversation/chat_handler.py`; `app.py`

1. `jobs` table: `id, user_id, kind, payload, status, attempts, max_attempts, run_at,
   claimed_at, claimed_by, dedupe_key, last_error`. Partial index on
   `(status, run_at) WHERE status='pending'`. Unique on `dedupe_key`.
2. **Claim** — this is the core of the fix:
   ```sql
   UPDATE jobs SET status='running', claimed_at=now(), claimed_by=:worker, attempts=attempts+1
   WHERE id IN (
     SELECT id FROM jobs WHERE status='pending' AND run_at<=now()
     ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT :batch
   ) RETURNING *;
   ```
   `SKIP LOCKED` makes N workers safe with zero coordination and no leader election.
3. Worker loop: bounded `asyncio.Semaphore` (default 10, env-tunable), exponential
   backoff with jitter on retry, `attempts >= max_attempts` ⇒ `status='dead'`.
   **Reaper:** `status='running' AND claimed_at < now() - interval '5 min'` ⇒ back to
   pending. That's what recovers a killed worker.
4. `chat_handler.py:47` — replace `asyncio.create_task` with an enqueue; return 202 **with
   a job id** so the client can poll. Delete the detached-task path.
5. `trigger_scheduler.py` — delete `_in_flight` (`:34`). The poller claims due triggers with
   the same `SKIP LOCKED` pattern and enqueues a job with
   `dedupe_key = f"trigger:{id}:{occurrence_iso}"`. Idempotent by construction.
6. `app.py:68-74` — background loops must **not** start in the API process. Worker is a
   separate entrypoint (`python -m server.worker`), separate container.
7. While here (the Problem 3 carve-outs, ~15 lines): shared module-level
   `httpx.AsyncClient` in `openrouter_client/client.py` (`:70` builds a new pool per call —
   TLS handshake on every LLM request), and `Retry-After` honoring on 429. Wrap the two
   hottest Composio calls in `asyncio.to_thread` and give them a timeout — today they have
   none (`gmail/client.py:481-494`), so a hung call freezes the loop.
8. **Bleed-stop on unbounded prompts:** pass a `conversation_limit` to `ExecutionAgent` at
   `execution_agent/runtime.py:33`. It currently defaults to `None` (`agent.py:39`), which
   is what makes recurring-agent prompts grow until every call fails permanently.
   Make the window **tail-preserving** — original task instruction + most recent N entries —
   so a truncated agent doesn't forget a completed action and repeat it. Mark it
   `# ponytail: truncation caps prompt growth; summarize agent history if recall suffers`.
9. **Cost metering (pulled into scope).** OpenRouter already returns `usage` on every
   response and we throw it away (`client.py:82`). Record `prompt_tokens`,
   `completion_tokens`, `model`, `user_id`, `job_id` to an `llm_usage` table. ~20 lines
   once Phase 1 supplies `user_id`. This is what makes the whole cost argument evidence
   instead of assertion — and it's the regression signal for prompt bloat: a query for
   *"p95 prompt_tokens by agent over time"* catches a creeping prompt before it becomes a
   $170/day fuse. Without it, the deferral in Problem 3 is unfalsifiable.

**Done when:** Phase 4's durability and exactly-once tests pass.

---

## Phase 3 — Security *(folds into 1 & 2; call out explicitly)*

Concrete, not abstract — this is what you'll be asked to defend.

- **Gmail tokens.** Composio holds the OAuth refresh tokens; we hold the
  `connection_id` that redeems them, so it is a bearer credential and gets encrypted at
  rest. Fernet (`cryptography`) over the column, data key from env locally / KMS in
  cloud. Store a `key_version` column so rotation is re-encrypt-in-place, not a migration.
- **LLM API key.** One platform-owned key, never per-user, never in the DB, never logged.
  The tenant risk isn't theft, it's *spend* — so per-user token accounting from Phase 2
  step 7 plus a per-tenant rate limit is the actual control.
- **Secrets.** `.env` local only. Cloud: AWS Secrets Manager / GCP Secret Manager
  injected as env at container start. Nothing secret in the image, nothing in git.
  Add a `gitleaks` CI step.
- **Auth.** Argon2-hashed API keys; constant-time compare; 401 vs 404 discipline from
  Phase 1 step 3.
- **Transport/edge.** CORS allowlist (currently `*`), docs disabled in prod, request-size
  cap, per-tenant rate limit.
- **Logging.** Scrub email bodies and tokens. Log `user_id`, never credentials.

---

## Phase 4 — Test suite *(1 agent; write tests FIRST so they fail)*

The point is regression-catching, not coverage. **Every test below must fail on `main`
and pass after.** Verify that, and capture the output — it's the demo. `

`tests/`
- `test_tenancy.py`
  - A's `/chat/history` never contains B's messages
  - `DELETE /chat/history` as A leaves B's conversation **and B's triggers** intact ← the current global-wipe bug
  - Unauthenticated ⇒ 401; A fetching B's trigger by id ⇒ 404
  - B connecting Gmail does not change which mailbox A's watcher polls ← the `_ACTIVE_USER_ID` bug
- `test_job_durability.py`
  - Enqueue → kill worker mid-job → new worker reaps and completes it
  - `/chat/send` returns 202 **and** a row exists in `jobs` (today: nothing persists)
  - Handler raising twice then succeeding ⇒ `attempts == 3`, exactly one side effect
  - Exhausted retries ⇒ `status='dead'`, not silently gone
- `test_exactly_once.py` ← **the headline test**
  - 8 concurrent claimers, 100 due jobs ⇒ each claimed exactly once, union == 100
  - Two `TriggerScheduler` instances on one DB ⇒ a due trigger produces **one** job
    (asserts the `_in_flight` bug is dead)
  - `dedupe_key` collision ⇒ second enqueue is a no-op
- `test_concurrency_bounds.py` — 200 jobs, semaphore of 10 ⇒ observed max in-flight ≤ 10
- `test_llm_client.py` — 429 + `Retry-After` ⇒ retried not raised; 401 ⇒ fails fast, no
  retry; client reused across calls
- `test_behavioral.py` — recorded LLM fixtures; assert the **tool-call sequence** for
  "remind me tomorrow at 9" and an important-email arrival. This is the regression net
  for prompt/model changes.
- `test_prompt_bounds.py` — append 500 entries to one agent's history, assert the built
  system prompt stays under a fixed char budget. Guards the Phase 2 step 8 bleed-stop, and
  fails loudly if someone removes the limit. Cheap insurance on the one deferred problem
  that can hard-fail in production.

Isolation tests must exercise **real concurrency** — separate sessions/tasks against the
same Postgres, not mocks. A mocked exactly-once test proves nothing.

---

## Phase 5 — Load testing *(1 agent; three layers, separately)*

Testing only the API conflates our code with LLM latency. Test each layer alone.

1. **DB layer** — `pgbench` with a custom script running the claim query. This is where the
   asserted queue ceiling gets replaced with a measured one. Three sweeps:
   - **Concurrency:** 1/2/4/8/16 workers → claimed-jobs/s. Shows `SKIP LOCKED` scaling
     near-linearly with no lock contention.
   - **Batch size:** claim `LIMIT` of 1/10/100 → jobs per *commit*. This is the dominant
     lever; report commits/s alongside jobs/s, since commits are what WAL fsync bounds.
   - **`EXPLAIN (ANALYZE, BUFFERS)`** on the claim and on `fetch_due`, before/after the
     partial index — index scan vs seq scan is the money slide.
   - **Soak (the one a 60s benchmark misses):** run sustained load for 30+ min while
     watching `pg_stat_user_tables.n_dead_tup` and table/index size. Every claim is an
     `UPDATE`, so the queue table is the textbook autovacuum-bloat case — throughput that
     looks fine for a minute can collapse over hours. If dead tuples grow unbounded, tune
     `autovacuum_vacuum_scale_factor` **on that table specifically**, and report it. A
     benchmark that doesn't run long enough to bloat proves nothing about production.
2. **Service layer** — `pytest-benchmark` (or a plain asyncio harness) driving the job
   dispatcher and agent runtime with the `LLMStub`. **No network.** Isolates our
   throughput ceiling from OpenRouter's latency. Report jobs/s and p99.
3. **API layer** — k6 against `/chat/send`. It's now a pure enqueue, so p95 should be
   single-digit ms and flat under load. Contrast with `main`, where the endpoint returns
   fast but the work behind it is unbounded and lossy. Thresholds:
   `p95 < 100ms`, `error rate < 1%`.

Also run a **queue-depth-under-sustained-arrival** test: arrival rate > service rate and
confirm depth grows linearly, latency degrades gracefully, and nothing is dropped. That's
the number that tells you when to add workers, and it's the autoscaling signal.

---

## Phase 6 — Deploy shape + CI/CD *(1 agent)*

**Files:** `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`, `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`

### Stateless vs stateful — the seams

| Component | State | Scales by |
|---|---|---|
| API (FastAPI) | **Stateless** | RPS / CPU. Any replica count. |
| Worker | **Stateless** (state is in the claim) | **Queue depth** — the correct autoscale signal |
| Trigger poller | Stateless, safe at N>1 after Phase 2 | Fixed small count |
| Postgres | **Stateful** — the only one | Vertical, then read replicas, then shard by `user_id` |

**The queue is the seam.** API and worker share only the `jobs` table, so they scale on
independent signals and deploy independently. That's the split that already exists in the
design and the reason Phase 2 was worth doing.

### CI — gates that catch *behavior*, not just syntax

```
lint (ruff) → typecheck (mypy) → unit
  → integration  [postgres:16 service container]
  → migrations   [alembic upgrade head on empty DB; then autogenerate --check ⇒ fail on drift]
  → behavioral   [recorded-fixture tool-call sequences]
  → load smoke   [k6, p95 threshold ⇒ fail the build]
  → gitleaks
```

The three that catch what lint never will: **migration drift** (model changed, migration
didn't), **behavioral fixtures** (a prompt edit silently changed which tool the agent
calls), and **the k6 threshold** (a regression that's correct but 10× slower).

Deploy: build image → run migrations as a **pre-deploy job, not on app start** (N replicas
booting would race) → rolling deploy API → rolling deploy workers. Migrations must be
backward-compatible for one release so rollback works: expand/contract, never
rename-in-place.

### Cloud mapping (`docs/DEPLOY.md`)

| | AWS | GCP | Fly |
|---|---|---|---|
| API | ECS Fargate + ALB | Cloud Run | Fly Machines |
| Worker | ECS Fargate service | **GKE / Compute** — *not* Cloud Run | Fly Machines |
| DB | RDS Postgres | Cloud SQL | Fly Postgres |
| Secrets | Secrets Manager | Secret Manager | Fly Secrets |
| Autoscale | Queue depth → target tracking | Queue depth → custom metric | autoscale on metric |

**Why Docker-first:** the app needs a long-running worker and a real Postgres — both
commodity everywhere. Nothing here is cloud-specific, so committing to a vendor buys
nothing and costs portability. Say that, then show the table.

**The one trap worth volunteering:** Cloud Run scales to zero and only bills during a
request — perfect for the API, *wrong* for the worker, which must poll continuously.
Putting the worker on Cloud Run is the mistake this architecture invites, and naming it
unprompted is the strongest signal in the whole deck.

---

## Verification / the demo

Run in this order:

1. **Before.** On `main`: `pytest tests/ -v` ⇒ tenancy, durability, and exactly-once tests
   fail. Show `test_exactly_once` producing 2 jobs for 1 trigger and
   `test_tenancy` showing A's delete destroying B's triggers.
2. **After.** On the branch: same command, all green.
3. **Live durability.** `docker compose up`, enqueue work, `docker kill` the worker
   mid-job, restart ⇒ job completes. On `main` the equivalent work vanishes.
4. **Live exactly-once.** Scale to 3 workers (`docker compose up --scale worker=3`), fire a
   due trigger, show one execution. On `main` with 3 API processes: three.
5. **Numbers.** pgbench claim throughput at 1/2/4/8 workers; k6 p95 on `/chat/send`;
   queue depth under sustained overload.
6. **CI.** Green pipeline. Then push a deliberate model-drift commit and a prompt change
   ⇒ show the migration-drift and behavioral jobs going red.

Step 6 is the one that proves the pipeline catches behavioral regressions rather than
asserting it.

---

## Agent assignment

| Phase | Depends on | Parallel? |
|---|---|---|
| 0 — Foundation | — | **Blocks all** |
| 1 — Tenancy | 0 | ∥ with 2 |
| 2 — Jobs | 0 | ∥ with 1 |
| 4 — Tests | 0 | Start early; must fail first |
| 5 — Load | 1, 2 | — |
| 6 — CI/Deploy | 1, 2, 4 | ∥ with 5 |

Phase 3 (security) is not separate — it lands inside 1 and 2, then gets written up.

## Out of scope today — the precise residue of Problem 3

The deferral narrowed as we costed it. We now **bound** context growth (Phase 2 step 8) and
**measure** it (step 9). What we are explicitly *not* doing is **making the LLM layer smart**:

| Not fixing | Impact accepted | Why it waits |
|---|---|---|
| Per-agent history **summarization** | Agents forget older history; tail-preserving truncation limits, doesn't eliminate, the recall loss | Bounded and measured is enough to be safe. Smart is a quality upgrade. |
| Email classification **concurrency** | Up to 50 sequential Sonnet calls can exceed the 60s poll interval; the loop drifts | Needs the watcher restructured, which needs tenancy (Phase 1) landed first |
| **Client-level** LLM retry beyond `Retry-After` | Coarser recovery granularity — a whole job replays rather than one call | Phase 2's job-level retry already removes the data-loss case |
| Blocking Composio I/O beyond the 2 hottest sites | Remaining sync calls still stall the loop | Mechanical `to_thread` sweep, no design content |
| Frontend auth UI · real cloud provisioning | — | Not code-shape decisions |

**One item I want to flag rather than silently defer:** the classifier swallows every error
and returns `None` (`importance_classifier.py:102-113`), and the watcher marks the id seen
regardless (`importance_watcher.py:210`). A transient OpenRouter blip is therefore
indistinguishable from "not important," and **that email is dropped permanently.** That is
data loss, not a performance ceiling — the one category worth breaking the deferral for.
Fix is ~20 lines: mark seen only on successful classification, plus an attempt counter so a
poison email can't retry forever. **Recommend pulling this in; flagging because it expands
the agreed scope.**

If asked "what's next," it's this table in order — and the reason it's next rather than now
is that nothing else has to change shape for any of it to land.

---

## Appendix — Provenance of every number in this plan

Rule: **if you can't say where a number came from, don't say the number.** Four tiers.
Anything in tier C is an assumption you should be ready to have challenged; anything that was
tier D has been deleted rather than defended.

### A — Read directly from the code (cite the line, it's checkable)

| Value | Source |
|---|---|
| 7,148 lines Python / 64 files | `wc -l` over `server/**/*.py` |
| Summary threshold 100, tail 10 | `config.py:71-72` |
| `MAX_TOOL_ITERATIONS = 8` | `interaction_agent/runtime.py:28` |
| Batch timeout 90s | `batch_manager.py:40` |
| OpenRouter timeout 60s, no retry | `openrouter_client/client.py:76` |
| Trigger poll 10s | `trigger_scheduler.py:29` |
| Watcher poll 60s / lookback 10min / max 50 / seen 300 | `importance_watcher.py:28-31` |
| `conversation_limit` defaults to `None` | `execution_agent/agent.py:39` |
| Gmail user id defaults to PID | `gmail/client.py:215` |
| SQLite busy timeout 30s | `triggers/store.py:32` |
| Frontend poll 1.5s | `web/app/page.tsx` |

### B — Arithmetic from tier A (sound if the inputs are)

| Value | Derivation |
|---|---|
| 480s max execution | `MAX_TOOL_ITERATIONS` 8 × 60s LLM timeout — both tier A |
| 288 trigger fires/day | 24×60 ÷ 5-minute interval |
| ~2,000 jobs in flight | Little's Law: 200 jobs/s × 10s latency — **latency is tier C** |

### C — My assumptions. Not measured. Challenge these first.

| Assumption | Used for | How to replace it |
|---|---|---|
| 50 msgs/user/day | Demand math | Instrument real traffic |
| 5 recurring triggers/user | Demand math | Query the triggers table |
| 2 important emails/user/day | Demand math | Count watcher dispatches |
| ~1KB history appended per agent fire | The $170/day fuse | Measure one agent log over a day |
| ~10s LLM latency per job | Little's Law → worker count | Phase 2 step 9's `llm_usage` gives this directly |
| ~4 chars/token | 864KB → ~216k tokens | `count_tokens` on a real transcript |
| 50 emails classified/user/day, ~1k tokens each | Haiku-vs-Sonnet savings on the classifier | Count watcher classify calls + `llm_usage` tokens |

**Classifier model-swap estimate** (tier B on the two tier-C rows above, at 10k users):
500k calls/day × ~1.1k tokens ⇒ **~$2.2k/day on Sonnet vs ~$0.7k/day on Haiku — roughly $45k/month
saved from one config line.** Same caveat as the $170/day figure: order-of-magnitude, not a
forecast. Phase 2 step 9's `llm_usage` table replaces both with measurements within a day of
launch, which is the actual point of shipping it.

**The $170/day figure is tier B resting on two tier-C inputs** (1KB/fire, 4 chars/token) plus a
tier-D-turned-verified price. Treat it as an order-of-magnitude argument — "this becomes a
four-figure monthly line item and then hard-fails" — not a forecast. The conclusion (cap the
prompt) holds across a wide range of those inputs, which is why it's worth acting on anyway.

### D — Verified externally (checked against Anthropic's model/pricing reference)

| Claim | Status |
|---|---|
| Sonnet-tier $3/M in, $15/M out | ✅ Confirmed |
| `anthropic/claude-sonnet-4` is **retired** (2026-06-15) | ✅ Confirmed — see Phase 0 step 3b |
| OpenRouter still serves that ID | ❓ **Unverified** — needs one live call |

### Deleted rather than defended

- ~~"Postgres handles 1-5k jobs/s"~~ — an unmeasured prior. Replaced with derived demand
  (~200/s) plus a commitment to measure (Phase 5 step 1).
- ~~"batching is the difference between ~1k/s and ~50k/s"~~ — same problem, same fix. The
  *mechanism* (commits, not rows) is retained because it's what you reason from; the magnitudes
  are gone because they were invented.
