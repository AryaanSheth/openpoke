# Phase 5 — Load testing results

Every number below is measured this session, on this hardware, with the commands shown.
Where the plan (`plan.md`, "Do we actually need a real queue?") stated a mechanism without
a magnitude, this is the magnitude. Where a measurement contradicted the plan's stated
mechanism, that's called out explicitly rather than smoothed over.

## Hardware / environment

- MacBook Pro, Apple Silicon (arm64), macOS 26.6, 11 CPUs, 18 GB RAM (host).
- Postgres runs in Docker Desktop (`postgres:16`, actual `16.14`); the Docker VM is
  allocated 11 CPUs / 7.75 GB RAM. **This is a virtualized-disk Postgres, not bare metal** —
  material for the concurrency result below.
- Postgres config is the **unmodified image default**: `shared_buffers=128MB`,
  `fsync=on`, `synchronous_commit=on`, `wal_buffers=4MB`, `max_connections=100`. Nothing
  tuned for this test.
- All DB-layer runs used an **isolated database**, `openpoke_loadtest`, on the same
  Postgres instance/container as the real `openpoke` database, schema created via
  `alembic upgrade head`. See "Method — isolation" below for why this is not optional.
- API-layer runs used a dedicated API server process on `:8098`, pointed at
  `openpoke_loadtest`, separate from whatever is already running on `:8099`.

## Method — isolation (read this before rerunning anything)

The first attempt seeded 3M rows directly into the **live** `openpoke.jobs` table with
`kind='chat_turn'`. The already-running worker process immediately started claiming and
executing them as real jobs — no LLM calls fired (empty-message `chat_turn` jobs
short-circuit in `handlers.py:38-42`), but a bulk recycle `UPDATE` racing against the live
worker's row-level claims produced multi-minute lock waits and had to be killed
(`pg_terminate_backend` on the two stuck load-test sessions; verified after the fact that
zero rows belonging to the load-test user remained in `openpoke` and the two `done` jobs
left in that table belong to real user ids, not the load-test one).

Every script in `loadtest/` after that point targets **`openpoke_loadtest`**, a separate
database on the same instance, and seeds jobs with `kind='loadtest_noop'` — a kind no
handler recognizes — as defense in depth. This is why the scripts hardcode `openpoke_loadtest`
rather than reading `DATABASE_URL`: pointing them at a database with a live worker attached
will repeat the incident.

---

## 1. DB layer — pgbench against the real claim query

Script: `loadtest/db/claim.sql` — the exact `_CLAIM_SQL` from `server/jobs/queue.py:50-62`
(`UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT :batch) RETURNING id`),
byte-for-byte except the `:worker` bind is a fixed literal (irrelevant to correctness —
`SKIP LOCKED` exclusivity comes from the row lock, not from distinct claimant ids).

Method per run (`loadtest/db/run_one.sh`): recycle the 3M-row pool back to `pending`
(`loadtest/db/reset.sql`), snapshot `count(*) WHERE status='pending'` and
`pg_stat_database.xact_commit`, run pgbench for the stated duration, snapshot again.
`jobs/s` and `commits/s` are the **real row-count and commit-count deltas** divided by
actual wall time — not inferred from pgbench's `tps` and an assumed full batch. Idle-baseline
commit noise on this box (other local processes touching Postgres) measured at ~2/s,
negligible against every number below. Reproduce: `loadtest/db/run_sweeps.sh`.

### Concurrency sweep (batch=10 fixed — matches `OPENPOKE_WORKER_CONCURRENCY` default)

| clients | jobs/s | commits/s | jobs/s per client |
|---|---|---|---|
| 1  | 27,900  | 2,790 | 27,900 |
| 2  | 36,800  | 3,680 | 18,400 |
| 4  | 51,800  | 5,180 | 12,950 |
| 8  | 67,700  | 6,770 | 8,460 |
| 16 | 51,500  | 5,160 | 3,220 |

**This does not confirm "scales near-linearly with no contention."** 1→8 clients (8×) buys
2.4× throughput, and 16 clients is *slower* than 8 — a regression, not a plateau. Per-client
efficiency falls monotonically at every step (27.9k → 3.2k jobs/s/client from 1 to 16
clients), which is the opposite of "no contention": if `SKIP LOCKED` truly had zero
contention cost, per-client throughput would stay flat as clients scale.

**What this is not:** row-lock contention. `SKIP LOCKED` guarantees workers never block on
the same row, and nothing in `pg_stat_activity` during these runs showed `Lock` wait events
between pgbench sessions (checked during the concurrency runs — unlike the isolation
incident above, where genuine `Lock:transactionid` waits *did* appear against the live
worker). What's plausible instead, **labeled as inference, not measured directly**: each
pgbench client is a full Postgres backend process, and `synchronous_commit=on` means every
one of these single-statement transactions pays a real WAL fsync. 16 concurrent backends
plus pgbench's own client process compete for the 11 vCPUs Docker Desktop's VM allocates,
and fsync latency through a virtualized disk is a plausible second factor. **Not verified
this session** — the next step to close it is the same sweep against bare-metal Postgres
(no Docker) to separate "Postgres's own ceiling" from "this VM's disk/CPU ceiling."

### Batch-size sweep (4 clients fixed)

| batch (`LIMIT`) | jobs/s | commits/s |
|---|---|---|
| 1   | 9,350   | 9,350 |
| 10  | 64,700  | 6,470 |
| 100 | 163,300 | 1,630 |

This is the plan's mechanism, and it **is confirmed, with a caveat**. Commits/s falls as
batch size grows (9,350 → 1,630, a 5.7× drop) while jobs/s rises 17.5× — throughput is
clearly not row-bound, and batching is clearly the dominant lever, exactly as argued. The
caveat: the plan's mental model ("commits/sec is the fixed hardware ceiling, so batch size
is free") is not literally true — **commits/sec itself drops 5.7× as batch grows**, because
a bigger claim does more locking and index-walking work per transaction, so it is not a
constant you multiply against. Net effect: a 100× larger batch buys 17.5× the throughput,
not 100×, and not "however large you want for free." The direction of the plan's claim is
right; the magnitude has real, measured diminishing returns.

### `EXPLAIN (ANALYZE, BUFFERS)` on the claim query

```
Update on jobs (actual time=1.222..1.429 rows=10 loops=1)
  ->  Nested Loop (actual time=1.051..1.169 rows=10 loops=1)
        ->  HashAggregate ... Subquery Scan ... Limit
              ->  LockRows (actual time=1.014..1.018 rows=10 loops=1)
                    ->  Index Scan using ix_jobs_pending_run_at on jobs jobs_1
                          Index Cond: ((status = 'pending') AND (run_at <= now()))
        ->  Index Scan using jobs_pkey on jobs
Execution Time: 1.518 ms
```

**Confirmed: `Index Scan using ix_jobs_pending_run_at`, not a sequential scan.** The partial
index `(status, run_at) WHERE status='pending'` is doing its job — the planner never
considers a seq scan even at 3M rows in the table. Command:
`docker exec openpoke-postgres-1 psql -U openpoke -d openpoke_loadtest -c "EXPLAIN (ANALYZE, BUFFERS) <claim query>"`.

---

## 2. API layer — k6 against `/chat/send`

Script: `loadtest/api/chat_send.js`. `/chat/send` is a pure enqueue (`chat_handler.py`):
auth, one `INSERT` into `jobs`, return 202. Thresholds per the brief: `p95 < 100ms`,
error rate `< 1%`. Auth is argon2 with a 300s in-process token cache
(`server/auth.py`); one warmup request was sent by curl to the running server process
before every k6 run so the measured window doesn't include the ~130-155ms first-request KDF
cost. Reproduce: `loadtest/api/run.sh`.

| VUs | p95 | throughput | errors |
|---|---|---|---|
| 10 | 15ms | 873 req/s | 0% |
| 12 | 17ms | 918 req/s | 0% |
| 13 | 19ms | 888 req/s | 0% |
| 14 | 20ms | 899 req/s | 0% |
| **15** | **30,120ms** | — | **6.7–100%** |
| 20–30 | ~30,000ms | — | 50–100% |

**Both thresholds pass, and hold flat, from 1 up through 14 concurrent requests** — p95
15–20ms the whole range, which is the "single-digit-ms, flat" story the plan predicted,
modulo the ms being low-double-digit rather than single-digit (likely the argon2 cache
lookup plus one DB round trip on this VM, not investigated further).

**At exactly 15 concurrent requests it does not degrade — it collapses.** Root cause,
confirmed from the server's own traceback
(`sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection
timed out, timeout 30.00`): `server/db/engine.py:22` calls `create_async_engine()` with no
`pool_size`/`max_overflow`, so it gets SQLAlchemy's defaults — 5 base + 10 overflow = 15
connections, with a 30s acquire timeout. This was reproduced twice (fresh server restart,
clean pool) at VU=14 (fine) vs VU=15 (collapse), and is deterministic on the pool-size
boundary, not a fluke. Past 15 in-flight requests, waiters queue for a connection that
doesn't free in time and **every** request in the batch — not just the excess — times out
at ~30s, because k6's closed-loop VUs all arrive together and the whole cohort piles onto
the same wait.

This is a real, previously-unmeasured ceiling: **the API layer's actual concurrency limit is
~15 in-flight requests, set by an unconfigured connection-pool default, not by CPU or the
claim query.** It is one line to fix (`pool_size=`/`max_overflow=` on the engine) but was not
fixed here — Phase 5's job is to measure, and this is exactly the kind of number the plan
said measurement would surface. Flagged in `docs/phase-5-notes.md` "New issues."

---

## 3. Queue depth under sustained overload

Script: `loadtest/queue/depth_under_overload.sh`. Arrival 300 jobs/s, service (claim +
complete, real claim query) capped at 100/s — arrival deliberately exceeds service. Sampled
every second for 15s against `openpoke_loadtest`.

| t (s) | pending | done | claim+complete latency |
|---|---|---|---|
| 1  | 200  | 100  | 550ms (cold) |
| 5  | 1,000 | 500  | 162ms |
| 10 | 2,000 | 1,000 | 197ms |
| 15 | 3,000 | 1,500 | 140ms |

- **Depth grows linearly**, at exactly arrival − service = 200/s (200, 400, ... 3,000 —
  matches to the row).
- **Nothing dropped**: `inserted_total == pending + running + done` at every sample,
  every second, over the full run.
- **Claim latency stays flat** (~140–220ms after the first cold sample) even as pending
  depth grows 15×, from 200 to 3,000 rows. The partial index means claim cost is a
  function of batch size, not queue depth — this is the index result above paying off under
  load, not just in isolation.

This is the autoscaling signal named in the plan: pending-count growth under
arrival-exceeds-service is the number to alarm on, and it's linear and legible, not a cliff.

---

## 4. Service layer — not run

Cut per the brief's explicit priority order ("cut this first if time is short" — it was).
`pytest-benchmark` or a plain asyncio harness driving the dispatcher with `LLMStub` remains
unrun. Lowest-value of the four layers here because it isolates our own dispatch throughput
from OpenRouter latency, which nothing above needed — the API and DB layers already show our
code is not the bottleneck at any tested scale.

## Soak test — explicitly not run

The plan asks for 30+ minutes watching `pg_stat_user_tables.n_dead_tup` and table/index size
while under sustained load. **Not attempted** — it does not fit this session's time budget,
and a 5-10 minute run mislabeled as a soak would be worse than admitting the gap: the failure
mode it's designed to catch (autovacuum falling behind on a table where every claim is an
`UPDATE`, so every claimed row leaves a dead tuple) does not show up on a short run by
construction — throughput that's fine for a minute is exactly what a bloat problem looks like
until it isn't.

**What to run to close this:**
```sql
-- before, during (every few minutes), and after a sustained run:
SELECT n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables WHERE relname = 'jobs';
SELECT pg_size_pretty(pg_total_relation_size('jobs'));
```
Run `loadtest/db/run_one.sh <clients> <batch> <seconds>` with a large `<seconds>` (or a
small wrapper loop) for 30+ minutes at, e.g., 8 clients / batch 10 — the configuration that
measured best above — and watch whether `n_dead_tup` stabilizes (autovacuum keeping up) or
grows unbounded (it isn't). If it grows unbounded, the fix named in the plan is tuning
`autovacuum_vacuum_scale_factor` on the `jobs` table specifically, not a config-wide change.

---

## Assumptions replaced by measurements

| plan.md assumption (deleted/deferred) | Replaced by |
|---|---|
| "Postgres handles 1-5k jobs/s" (deleted, tier D) | Measured 27.9k-67.7k jobs/s claim throughput depending on concurrency/batch, on this hardware — an order of magnitude above the derived ~200/s average demand and the ~500-1000/s diurnal peak from plan.md's demand table. The queue is not the bottleneck at any tested point. |
| "batching is the difference between ~1k/s and ~50k/s" (deleted, tier D) | Measured 9.4k/s (batch=1) → 163k/s (batch=100), 17.5× for a 100× batch increase — batching is real and large, not the ~50× implied by the deleted number. |
| "`SKIP LOCKED` scales near-linearly with no lock contention" (plan.md Phase 5 step 1) | **Not confirmed.** Measured 2.4× throughput for 8× clients, then a regression at 16. No row-lock contention observed; the ceiling looks like WAL-fsync/CPU contention on this Docker VM, unconfirmed pending a bare-metal rerun. |
| "/chat/send p95 should be single-digit ms and flat under load" (plan.md Phase 5 step 3) | p95 15-20ms and flat for 1-14 concurrent requests (close, not single-digit). **Not flat past 15** — collapses to ~30s due to an unconfigured DB connection pool, not the endpoint's own logic. |
