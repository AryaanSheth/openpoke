# Deploy

One image (`Dockerfile`, repo root), two run modes — `python -m server.server` (API) and
`python -m server.worker` (worker) — selected by `command:`, not by build. Locally,
`docker-compose.yml` builds both from the same `Dockerfile` already; cloud deploys mirror
that split into two independently-scaled services. See `docs/ARCHITECTURE.md` for why API
and worker scale on different signals.

## Why Docker-first

The app needs a long-running worker process and a real Postgres instance — both are
commodity everywhere: every major cloud (and Fly) runs a container that polls
continuously, and every major cloud offers managed Postgres. Nothing in this
architecture depends on a vendor-specific primitive (no proprietary queue, no
vendor-specific compute model). Committing to one cloud here would buy nothing and cost
portability, so the deploy artifact is a container image and the cloud-specific piece is
just which managed services host it.

## Cloud mapping

| | AWS | GCP | Fly |
|---|---|---|---|
| API | ECS Fargate + ALB | Cloud Run | Fly Machines |
| Worker | ECS Fargate service | **GKE / Compute** — *not* Cloud Run | Fly Machines |
| DB | RDS Postgres | Cloud SQL | Fly Postgres |
| Secrets | Secrets Manager | Secret Manager | Fly Secrets |
| Autoscale | Queue depth → target tracking | Queue depth → custom metric | autoscale on metric |

## The trap worth naming unprompted

**Cloud Run scales to zero and only bills while a request is in flight.** That's exactly
right for the API — no traffic, no cost, no idle server. It is **wrong for the worker**,
which has no request to scale on: its whole job is polling `jobs` continuously
(`claim()`, `FOR UPDATE SKIP LOCKED`) whether or not anyone is making an HTTP request
anywhere. Put the worker on Cloud Run and it either never runs (scaled to zero with
nothing to wake it — there's no incoming request) or has to be kept alive with a
synthetic pinger, which defeats the point of a serverless platform while still paying for
it. This is the single most likely misconfiguration this architecture invites, because
"put everything on the serverless option" is the natural instinct and it's correct for
exactly one of the two services. On GCP the worker belongs on GKE or plain Compute, not
Cloud Run. AWS and Fly don't have this trap in the same shape — ECS Fargate and Fly
Machines both run a long-lived process the same way regardless of which service it is —
but the underlying reason (the worker's workload is "poll forever," not "answer a
request") is the same everywhere, so getting it right on Cloud Run is the sharpest
version of a rule that applies universally.

## Deploy ordering

```
build image → run migrations (pre-deploy job) → rolling deploy API → rolling deploy workers
```

**Migrations run as a pre-deploy job, not on application start.** The natural instinct —
run `alembic upgrade head` in the API's startup hook — races the moment there's more than
one replica: N API processes booting in parallel each try to apply the same migration,
and Alembic gives no guarantee that's safe (it isn't wrapped for concurrent DDL from
multiple independent connections). A pre-deploy job runs the migration exactly once,
before any new-schema-expecting code is live, using the same image (`alembic upgrade
head` from the same container that becomes API/worker — see the `Dockerfile` comment).
Only once that job exits 0 does the rolling deploy of API, then workers, begin.

**Migrations must be backward-compatible for one release.** During a rolling deploy, old
and new code run against the same database simultaneously — some replicas have picked up
the new image, some haven't, for as long as the rollout takes. If the migration that ran
before the rollout makes the schema incompatible with the *old* code, every old replica
that hasn't rolled yet starts failing before the rollout finishes. The same is true in
reverse for rollback: if you roll back the image without rolling back the migration
(normal — nobody reverses a migration to undo a bad deploy), the old code must still work
against the new schema.

The discipline that makes both directions safe is **expand/contract, never
rename-in-place**:

- **Expand**: add the new column/table alongside the old one; both old and new code work
  (old code ignores the new column; new code writes both, or the new one only, depending
  on the change).
- **Migrate**: deploy the code that uses the new shape; verify.
- **Contract**: once every replica is confirmed on the new code and a rollback is no
  longer on the table, a *second* migration removes the old column/table.

A rename is an expand and a contract with no code deployed in between — which is exactly
the case that breaks: it removes the old name in the same step that adds the new one,
so there is no window in which both old and new code can run against the same schema.
Two migrations across two releases, never one migration that does both at once.

## What "rolling deploy" means for each service here

- **API**: standard rolling replace behind a load balancer — old replicas keep serving
  until new ones pass health checks, then old ones drain. Nothing API-specific beyond
  the ordinary pattern, because the API is stateless (`docs/ARCHITECTURE.md`).
- **Workers**: same rolling replacement, but "drain" means letting an in-flight job
  finish or letting the reaper (`claimed_at` older than 5 minutes) reclaim it if the
  worker is killed mid-job — not waiting on an HTTP connection to close. A worker can be
  killed harder than an API replica can, because the queue's claim semantics are the
  actual durability mechanism, not graceful shutdown.

## Secrets

Nothing secret is baked into the image (verified — see `docs/phase-6-notes.md` for how).
Locally, `.env` (a symlink to `.env.local` in this checkout) is read by
`pydantic_settings.BaseSettings` at process start. In the cloud, the equivalent is a
secrets manager injecting the same environment variables at container start — AWS
Secrets Manager, GCP Secret Manager, or Fly Secrets per the table above. Same variable
names, same `Settings` class, different source.
