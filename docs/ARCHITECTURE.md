# Architecture — stateless/stateful seams and how each component scales

This describes the deploy topology the rewrite produces, not the code inside each
component. See `plan.md` for why the rewrite happened at all (no tenancy, no durable
work) and `docs/DEPLOY.md` for how this topology maps onto a cloud.

## The seam table

| Component | State | Scales by |
|---|---|---|
| API (FastAPI) | **Stateless** | RPS / CPU. Any replica count. |
| Worker | **Stateless** (state is in the claim) | **Queue depth** — the correct autoscale signal |
| Trigger poller | Stateless, safe at N>1 after Phase 2 | Fixed small count |
| Postgres | **Stateful** — the only one | Vertical, then read replicas, then shard by `user_id` |

Every row above other than Postgres holds no durable state in the process itself. Kill
any API replica, any worker, or the trigger poller mid-request and nothing is lost that
wasn't already lost by definition — the API's in-flight HTTP request retries at the
load balancer, and the worker's in-flight job is recovered by the reaper
(`claimed_at < now() - interval '5 min'` reverts a job to `pending`, per `plan.md`
Phase 2 step 3). That recoverability is what "stateless" means here — not that the
process does nothing, but that nothing it's doing is only known to that process.

## Why the queue is the seam

API and worker share exactly one thing: the `jobs` table. The API's entire
responsibility for a chat turn, a trigger, or an email poll is to write one row
(`enqueue()`) and return. The worker's entire responsibility is to read due rows
(`claim()`, `FOR UPDATE SKIP LOCKED`) and execute them. Neither process holds a
reference to the other, calls the other, or needs the other to be a particular size.

That's what makes them independently scalable and independently deployable:

- **Independent signals.** API load is request rate; worker load is queue depth. A
  traffic spike that doubles chat requests does not by itself mean you need more
  workers — it means more rows get written. Workers scale on how fast rows are
  arriving relative to how fast they're being drained, which is queue depth, not RPS.
- **Independent deploys.** Roll the API without touching a single worker process, and
  vice versa. A worker mid-job during an API deploy is unaffected — it isn't holding
  an HTTP connection to the API at all.
- **Independent failure domains.** The API returning 500s doesn't stop workers from
  draining the backlog they already have. Workers falling behind doesn't make the API
  slow — `POST /chat/send` is a single `INSERT`, not a wait on the work itself.

This is the direct payoff of Phase 2: before it, `chat_handler.py` ran the real work
in a detached `asyncio.create_task` inside the API process itself (`plan.md` Problem
2). That collapsed the API and the worker into one failure domain — an API deploy
silently dropped in-flight turns, and the client had already gotten its `202`. Putting
the job in a table is what lets the two processes stop needing to be one thing at one
scale.

The trigger poller is a third stateless process. Before Phase 2 it kept in-process
dedupe state (`self._in_flight: Set[int]`), which is *why* it was unsafe at N>1 — two
poller processes each had their own idea of what was in flight, so a due trigger fired
once per process. After Phase 2 it claims due triggers with the same `SKIP LOCKED`
pattern the job queue uses and enqueues with a deterministic `dedupe_key` — the
in-process set is gone, so N>1 pollers no longer means N duplicate fires. It stays a
"fixed small count" rather than something you autoscale, because polling throughput
isn't the bottleneck (see the demand math in `plan.md`) — redundancy for availability
is the only reason to run more than one.

## Postgres — the one stateful component, and its ceiling

Everything above scales by adding more of the same kind of process. Postgres does not
— there is one primary, and the path past it is a sequence of different techniques,
each with a different cost:

1. **Vertical** — bigger instance. Free in the sense that it requires no application
   change; expensive in the sense that it has a ceiling (the largest instance your
   cloud offers) and a single point of failure the whole time.
2. **Read replicas** — offloads reads (most of what an API does) from the primary,
   which still takes every write. Buys headroom without changing the schema; does not
   help write throughput, which is what the job queue actually stresses.
3. **Shard by `user_id`** — the tenant key is already on every table (Phase 1), which
   is what makes this the natural shard key rather than a retrofit. This is the real
   ceiling-breaker for write throughput, and the most expensive step: it changes how
   every query is routed, not just where it runs.

**Say the ceiling out loud, because it's easy to leave implicit:** one Postgres
primary is the architecture as shipped. The seam past it is tenant-hash sharding or
Citus. The `jobs` table specifically is the first thing to move to a dedicated queue
(SQS, Redis Streams) if it becomes the bottleneck — not because a Postgres-backed
queue is fragile, but because it shares I/O with every other table on the same
primary, and a queue under heavy churn (every claim is an `UPDATE`, i.e. a dead tuple)
competes with everything else for vacuum and WAL bandwidth.

**The trigger to move is measured commits/s, not a guess.** `plan.md`'s own position
on this: "Any 'Postgres can do N jobs/s' figure is hardware-dependent folklore until
measured on our box." Phase 5 step 1 (`pgbench` against the claim query, swept across
concurrency and batch size) is what produces that number. Move the queue off Postgres
when that measurement says the `jobs` table's commit rate is competing with the rest
of the schema for headroom — not before, and not on a hunch that a "real" queue must
be needed at some unspecified scale.

## What this buys, concretely

- Deploying a worker fix does not require an API deploy, or the reverse.
- Autoscaling policy is two independent, correctly-chosen signals (RPS for API, queue
  depth for worker) instead of one signal (RPS) misapplied to both — which is exactly
  the mistake `docs/DEPLOY.md` calls out for the worker on Cloud Run.
- The only component that needs a capacity plan beyond "add more replicas" is
  Postgres, and its escalation path is explicit rather than "we'll figure it out."
