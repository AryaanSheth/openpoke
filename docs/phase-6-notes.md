# Phase 6 — Deploy shape + CI/CD

## Changes

- `Dockerfile` (new) — single image, two run modes (`python -m server.server` for API,
  `python -m server.worker` for the worker), selected by `command:` — matches
  `docker-compose.yml`'s existing `api`/`worker` split, which already builds both
  services from `context: .` / `dockerfile: Dockerfile`. No edit to `docker-compose.yml`
  was needed; Phase 0 wired the `build:` block correctly already.
- `Dockerfile` — `python:3.14-slim` base (matches the verified local interpreter,
  `.venv` is 3.14.5), non-root `appuser` (uid 10001), dependency layer (`server/requirements.txt`)
  installed before app code is copied so a code-only change doesn't invalidate the pip
  cache, and the image also carries `alembic/` + `alembic.ini` so the same image runs
  the pre-deploy migration job described in `docs/DEPLOY.md` — no second image to build.
- `.dockerignore` (new) — excludes `.env`, `.env.*` (both the symlink name and the
  target name, since Docker's build-context filter matches the symlink's own name, not
  where it points — see Verification below for how this was checked rather than
  assumed), `web/` (this image is backend-only), `server/data/` (legacy local
  file-store state, irrelevant to a stateless container), `.venv/`, `.git/`,
  `node_modules/`, and the usual caches.
- `.github/workflows/ci.yml` (new) — the plan's gate sequence, wired as a strict
  `needs:` chain in the plan's literal order: `lint → typecheck → test → migrations →
  behavioral → load-smoke → gitleaks`. Detail per job below.
- `.github/workflows/ci.yml` — `lint` runs `ruff check .`; `typecheck` runs bare `mypy`
  (no CLI args, so it reads `[tool.mypy] files = [...]` and the per-module
  `ignore_errors` overrides straight from `pyproject.toml` — the CI job cannot silently
  diverge from what's actually configured, since there's nothing to diverge with).
- `.github/workflows/ci.yml` — `test` merges the plan's separate "unit" and
  "integration" stages into one job with a `postgres:16` service container. Reason:
  `tests/conftest.py`'s `_migrated_database` fixture is session-scoped and
  `autouse=True`, so every test in the suite requires a live Postgres connection before
  a single test runs — there is currently no marker or path split that isolates
  DB-free unit tests. Splitting the job today would either run zero tests under a
  misleading "unit" label or fail every "unit" run for lacking a database it never
  needed. Flagged below for Phase 4 to fix at the source (add a marker), not worked
  around in the workflow.
- `.github/workflows/ci.yml` — `migrations` runs against its own fresh `postgres:16`
  service container (genuinely empty, since GitHub service containers are provisioned
  per job): `alembic upgrade head`, then `alembic check` as the drift gate — the modern
  equivalent of `alembic revision --autogenerate --check` (Alembic ≥1.13), same
  comparison, no throwaway revision file.
- `.github/workflows/ci.yml` — `behavioral` and `load-smoke` are real jobs that detect
  their own artifact at runtime (`tests/test_behavioral.py` + non-empty
  `tests/fixtures/`; any `*.js` file with `load` or `k6` in its path outside
  `node_modules`/`.venv`) and run for real if present, or print a `::warning::` naming
  exactly what has to land and skip cleanly (exit 0) if not. Neither job is faked as
  passing, and neither is silently omitted from the workflow file.
- `.github/workflows/ci.yml` — `gitleaks` uses `gitleaks/gitleaks-action@v3` (current
  major as of this session — v2 is being deprecated as GitHub retires Node 20 runners in
  2026), paired with `actions/checkout@v7` and `fetch-depth: 0` so it scans full history,
  not just the diff.
- `docs/ARCHITECTURE.md` (new) — the stateless/stateful seam table, why the `jobs`
  table is the seam between API and worker (shared state, independent scale signals,
  independent deploys), and the sharding/queue-migration ceiling past a single
  Postgres primary, with the move-trigger stated as measured commits/s rather than an
  assumed number.
- `docs/DEPLOY.md` (new) — the AWS/GCP/Fly mapping table, the pre-deploy-job migration
  ordering and why running migrations on app start races N booting replicas, the
  expand/contract discipline for backward-compatible migrations, and the Cloud
  Run-scales-to-zero trap for the worker specifically.
- `docs/MODELS.md` (new) — current env-overridable defaults (all five still
  `anthropic/claude-sonnet-4`), the Haiku/Sonnet per-role routing proposal labeled
  explicitly as a proposal gated on behavioral fixtures (not applied config), the cost
  reasoning carried forward with the plan's own tier-B-on-tier-C caveat intact rather
  than restated as measurement, and the note that Haiku's 200K context ceiling makes
  the Phase 2 prompt cap more urgent, not less.

## Fixes

None. Every file Phase 6 owns (`Dockerfile`, `.dockerignore`, `.github/workflows/*`,
`docs/*`) was new — there was nothing pre-existing to fix in this phase's scope, and the
phase's rules exclude editing any `.py` file under `server/`, so no code fix was in
reach even where one was found (see New issues below).

## New issues

Everything here was discovered while verifying this phase's own deliverables, in a repo
being actively edited by two other agents. None of it is fixed, because none of it is in
Phase 6's file ownership.

1. **`typecheck` would fail right now — a real error, in an in-flight Phase 2 file.
   (medium, not Phase 6's to fix)** Running `mypy` bare (exactly what `ci.yml` does)
   reports `server/jobs/queue.py:238: error: "Result[Any]" has no attribute "rowcount"
   [attr-defined]`, "checked 5 source files" — mypy follows imports out of
   `server/db`/`server/config.py` into `server/jobs`, which isn't in `pyproject.toml`'s
   `ignore_errors` override list. Verified by running the exact CI command locally
   against the current tree. This may already be stale by the time CI runs on a push —
   Phase 2 owns the file and is actively writing it — but it's a real, reproducible
   failure of the gate as configured at the moment I verified it.

2. **`lint` would fail right now — 419 ruff errors, mostly in brand-new test files.
   (low, transient, not Phase 6's to fix)** `ruff check .` reported 419 errors at
   verification time, dominated by `F811` redefinitions in `tests/test_job_durability.py`
   and similar files that appeared mid-session (concurrent Phase 2/4 work). Same caveat
   as above: almost certainly transient, named because it's the literal state of the
   gate I wired up, observed rather than assumed clean.

3. **One behavioral fixture currently fails.** `tests/test_behavioral.py::
   test_important_email_arrival_dispatches_expected_tool_sequence` asserts 3 LLM calls
   (classifier + two interaction-agent rounds) and observed 1, at verification time.
   `tests/test_behavioral.py` and `tests/fixtures/*.json` already exist in this repo —
   built by a concurrent agent while this phase was in progress, ahead of what the task
   brief described as "not yet built." The `behavioral` CI job is written to detect and
   run them for real (see Changes), which it now does; whether this specific failure is
   a genuine app-code regression or simply an in-flight test/implementation pair that
   hasn't converged yet is not something I can determine from outside Phase 1/2/4's
   ownership, and I made no attempt to fix it.

4. **`pip install ".[dev]"` builds an incomplete `openpoke` package — doesn't affect CI
   or the Docker image, but is a live trap. (low)** `[tool.setuptools] packages =
   ["server"]` in `pyproject.toml` lists only the top-level `server` package, not its
   subpackages. Verified: after `pip install ".[dev]"` into a throwaway venv,
   `site-packages/server/` contains only the flat top-level `.py` files —
   `db/`, `routes/`, `services/`, `agents/`, `jobs/`, etc. are all missing. This doesn't
   break the CI jobs in `ci.yml` (they invoke `python -m pytest` / bare `mypy` from the
   repo root, which puts the repo root ahead of site-packages on `sys.path`, so the
   real source tree wins) and doesn't affect the Docker image (it never runs `pip
   install .` at all — it copies `server/` directly). It would break for anyone who
   installs the package and imports it from outside the repo root expecting the
   subpackages to be there. Phase 0 owns `pyproject.toml`.

5. **`load-smoke`'s k6-detection is a path/name heuristic, not a fixed contract.**
   (low) It looks for any `*.js` file with `load` or `k6` in its path outside
   `node_modules`/`.venv`. Good enough to self-arm once Phase 5 lands a script at a
   plausible path, but if it's named or placed unexpectedly the job keeps skipping
   silently rather than erroring — there's no way to distinguish "not built yet" from
   "built somewhere this heuristic doesn't look" from inside the workflow file. Revisit
   once Phase 5's actual script path is known.

6. **`gitleaks` runs last in the chain, per the plan's literal ordering — a deliberate
   tradeoff, named rather than silently picked.** (low) Secret-scanning is cheap and
   independent of the rest of the pipeline; running it first would catch a leaked
   credential in seconds instead of after every other gate has already run. I followed
   the plan's explicit arrow diagram (`... → load smoke → gitleaks`) rather than
   substitute my own ordering — flagging the tradeoff here rather than silently
   optimizing around the given instruction.

7. **Multi-agent contention on the shared local Postgres made full-suite local
   verification unreliable this session.** (informational) Two independent `pytest -q`
   invocations were observed running concurrently against the same
   `localhost:5432` Postgres instance from different agent sessions during this task;
   mine stalled at 0% CPU for over ten minutes (consistent with a lock wait against
   the other session's writes) before I killed it rather than continue blocking on it.
   Not a CI concern — every CI job gets its own fresh, isolated `postgres:16` service
   container — but it means I could not get a clean, current full-suite pass/fail count
   from this machine during the session; items 2 and 3 above are individually
   reproduced, not derived from that stalled run.
