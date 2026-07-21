# Phase 5 — Load testing

## Changes

- `loadtest/db/claim.sql` (new) — pgbench script running the real claim query, verbatim
  from `server/jobs/queue.py:50-62` (`_CLAIM_SQL`), parameterized by `:batch`.
- `loadtest/db/seed.sql`, `loadtest/db/reset.sql` (new) — seed a 3M-row pending pool owned
  by a dedicated load-test user, and recycle it back to `pending` between runs instead of
  re-inserting millions of rows per trial.
- `loadtest/db/run_one.sh` (new) — one pgbench trial: recycle, snapshot
  `count(pending)`/`pg_stat_database.xact_commit`, run pgbench for a fixed duration,
  snapshot again, print `jobs/s` and `commits/s` computed from the real before/after
  deltas (not inferred from pgbench's own `tps` and an assumed full batch).
- `loadtest/db/run_sweeps.sh` (new) — reproduces the concurrency (1/2/4/8/16 clients,
  batch=10) and batch-size (1/10/100, 4 clients) sweeps end-to-end.
- `loadtest/api/chat_send.js` (new) — k6 script against `/chat/send`, thresholds
  `p95<100ms` / error rate `<1%` per the brief.
- `loadtest/api/run.sh` (new) — starts an isolated API server on `:8098` against the
  isolated database, mints a token, warms the argon2 cache, runs k6 at given VU levels.
- `loadtest/queue/depth_under_overload.sh` (new) — arrival(300/s) > service(100/s) queue
  depth sampler against the real claim+complete queries.
- `docs/LOADTEST.md` (new) — the results, tables, hardware/method, and which plan.md
  assumptions each measurement replaces.
- A dedicated Postgres database, `openpoke_loadtest`, created on the same running
  container/instance via `alembic upgrade head` (not a code change, but load-bearing for
  reproducing anything above — see "New issues" #1).

## Fixes

None. This phase measures; it does not change application code. The one candidate fix
found (`server/db/engine.py` connection pool sizing) is filed below, not applied — out of
scope for a measurement phase and the brief didn't ask for it.

## New issues

Found, not fixed.

1. **(high, process not code) An early seeding mistake put real jobs on the live worker.**
   The first seed script used `kind='chat_turn'` against the live `openpoke` database. The
   already-running worker process (started before this session, per the brief) picked up
   the seeded rows as real jobs and began executing them. No LLM calls fired — empty-message
   `chat_turn` jobs short-circuit in `handlers.py:38-42` and return immediately — but a bulk
   recycle `UPDATE` racing the live worker's individual row claims produced several minutes
   of `Lock:transactionid` waits, requiring `pg_terminate_backend` on the two stuck load-test
   sessions to clear. Verified afterward that zero rows belonging to the load-test user
   remained in `openpoke.jobs` and that the two `done` jobs left in that table belong to real
   (non-load-test) user ids. **Everything after this point runs against an isolated
   `openpoke_loadtest` database** with `kind='loadtest_noop'` as defense in depth. Anyone
   rerunning `loadtest/` scripts against a database with a live worker attached will repeat
   this — the scripts hardcode the isolated database name for exactly that reason.
2. **(medium) `server/db/engine.py:22` has no `pool_size`/`max_overflow`, so the API's DB
   connection pool defaults to 5+10=15.** Measured directly: k6 against `/chat/send` at 14
   concurrent requests is flat (p95 20ms, 0% errors); at 15 it collapses to ~30s and mass
   failure — the SQLAlchemy pool-acquire timeout. This is a real production ceiling nobody
   had measured before this session: **the API's actual concurrency limit is ~15 in-flight
   requests**, and it fails as a cliff (everyone times out together), not a graceful
   slowdown, because k6's closed-loop VUs arrive as a cohort and pile onto the same 30s
   wait. Fix is one line (explicit `pool_size=`/`max_overflow=`, sized to expected concurrent
   request volume) plus deciding what a slow-path response should look like once the pool
   *is* exhausted (a fast 503 beats a 30s hang either way — the current default timeout
   guarantees the worst of both).
3. **(medium) `SKIP LOCKED` claim throughput does not scale near-linearly with client
   concurrency on this hardware.** plan.md Phase 5 step 1 predicted near-linear scaling with
   no lock contention. Measured: 1→8 pgbench clients buys 2.4× throughput (not ~8×), and 16
   clients is *slower* than 8 (regression, not plateau). No row-lock contention was observed
   between pgbench sessions during these runs (contrast with issue #1's genuine
   `Lock:transactionid` waits against the live worker) — the likely cause, **not confirmed
   this session**, is WAL-fsync and/or CPU contention specific to running Postgres inside
   Docker Desktop's virtualized VM (11 shared vCPUs, `synchronous_commit=on`, virtualized
   disk). The fix, if this matters for capacity planning, is rerunning
   `loadtest/db/run_sweeps.sh` against bare-metal Postgres to separate "Postgres's own
   ceiling" from "this VM's ceiling" — not attempted here, out of scope for this session's
   time budget.
4. **(low) The batch-size mechanism holds directionally but not at face value.**
   plan.md's mental model treats commits/sec as a fixed ceiling that batching multiplies for
   free. Measured: commits/s itself falls 5.7× from batch=1 to batch=100 (9,350 → 1,630),
   because a larger claim does more per-transaction locking/index work. Net jobs/s gain from
   a 100× larger batch is 17.5×, not 100× and not free. Batching is still clearly the right
   lever (17.5× is large), just with real, measured diminishing returns rather than the
   unbounded-multiplier framing.
5. **(informational) Service-layer benchmark (`pytest-benchmark`/asyncio harness against the
   dispatcher with `LLMStub`) was not run.** Cut per the brief's explicit priority order —
   lowest-value of the four layers, since the API and DB layers already show our own code
   isn't the bottleneck at any tested scale. Isolating dispatch throughput from OpenRouter
   latency remains unmeasured.
6. **(informational) The 30+ minute autovacuum-bloat soak was not run**, per the brief's
   explicit instruction. See `docs/LOADTEST.md` "Soak test — explicitly not run" for the
   exact commands and what to watch (`pg_stat_user_tables.n_dead_tup`, table/index size) to
   close this gap. Every claim is an `UPDATE`, so this is the textbook case where a
   short run proves nothing about production.
