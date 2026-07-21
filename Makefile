# OpenPoke — demo and development entrypoints.
#
#   make help          list everything
#   make demo          the full before/after, start to finish
#
# Every target here is something that actually ran during development. Nothing is
# aspirational — if a target is in this file it worked at least once.

PY      := .venv/bin/python
PORT    := 8099
OLDPORT := 8098
OLDTREE := /tmp/openpoke-main
PSQL    := docker exec openpoke-postgres-1 psql -U openpoke -d openpoke -Atc

.DEFAULT_GOAL := help
.PHONY: help db-up db-down migrate test test-once gates token api worker \
        old-tree old-api probe-before probe-after demo demo-auto diagram \
        preflight explain cost depth tenancy schema clean-demo nuke-old

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- infrastructure

db-up: ## Start Postgres and wait for healthy
	docker compose up -d postgres
	@until docker exec openpoke-postgres-1 pg_isready -U openpoke -q 2>/dev/null; do \
	  printf '.'; sleep 1; done; echo " postgres ready"

db-down: ## Stop Postgres (keeps the volume)
	docker compose stop postgres

migrate: db-up ## Apply migrations
	$(PY) -m alembic upgrade head

# ---------------------------------------------------------------------- verify

test: migrate ## Full test suite (~20s). Runs against openpoke_test — safe with the app running.
	$(PY) -m pytest

test-once: migrate ## Just the exactly-once suite — the headline demo
	$(PY) -m pytest tests/test_exactly_once.py -v

preflight: ## Validate the integration surface (keys, SDK shape, DB) before demoing
	@$(PY) -m server.preflight

gates: migrate ## Everything CI gates on: tests, lint, types, migration drift
	@echo "=== pytest ==="   && $(PY) -m pytest -p no:warnings --tb=no
	@echo "=== ruff ==="     && $(PY) -m ruff check .
	@echo "=== mypy ==="     && $(PY) -m mypy
	@echo "=== alembic ==="  && $(PY) -m alembic check

# ------------------------------------------------------------------- run it

token: migrate ## Mint a fresh bearer token (prints user_id + token)
	@$(PY) -m server.auth create-user --email demo-$$(date +%s)@example.com 2>/dev/null

api: migrate ## Run the API in the foreground on $(PORT)
	$(PY) -m uvicorn server.app:app --port $(PORT)

worker: migrate ## Run a worker in the foreground (repeat in more shells to scale)
	$(PY) -m server.worker

# --------------------------------------------------------- before/after probes
#
# The new test suite CANNOT run against main: tests/, server/db/, and server/auth.py
# do not exist there, so pytest dies at collection with ModuleNotFoundError. That
# proves nothing about behaviour. These probes hit a running server over HTTP and
# assert on what it actually does, which is the honest before/after.

old-tree: ## Check out main into $(OLDTREE) as a git worktree
	@test -d $(OLDTREE) || git worktree add $(OLDTREE) main
	@cp -f .env.local $(OLDTREE)/.env 2>/dev/null || true
	@echo "main checked out at $(OLDTREE)"

old-api: old-tree ## Run the ORIGINAL app on $(OLDPORT) (file/SQLite storage, no auth)
	cd $(OLDTREE) && $(CURDIR)/$(PY) -m uvicorn server.app:app --port $(OLDPORT)

probe-before: ## Prove the four bugs are PRESENT on main (needs `make old-api` running)
	@$(PY) scripts/probe.py before --port $(OLDPORT) --sqlite $(OLDTREE)/server/data/triggers.db

probe-after: ## Prove the four bugs are GONE (needs `make api` + `make worker` running)
	@test -n "$(TOKEN)" || (echo "usage: make probe-after TOKEN=opk_..."; exit 1)
	@$(PY) scripts/probe.py after --port $(PORT) --token $(TOKEN)
	@echo ""
	@echo "  The trigger-wipe probe skips here: there is no SQLite store to seed."
	@echo "  Its real proof is the two-tenant test, run next:"
	@echo ""
	@$(PY) -m pytest tests/test_phase1_tenancy.py -k "delete_history_leaves" -v 2>&1 | tail -4

# ------------------------------------------------------------------ demo flow

demo-auto: ## Run the ENTIRE before/after demo unattended (~90s, starts and stops everything)
	@chmod +x scripts/demo_auto.sh && ./scripts/demo_auto.sh

demo: ## Print the live demo runbook (does NOT run anything — see demo-auto)
	@echo ""
	@echo "  LIVE DEMO — run each block in its own terminal"
	@echo ""
	@echo "  0. make db-up"
	@echo "  1. make gates                 # 97 tests, lint, types, migration drift"
	@echo "  2. make test-once             # the headline: 8 claimers, 100 jobs"
	@echo "  3. git diff HEAD -- server/services/trigger_scheduler.py | head -60"
	@echo "                                # the deleted in-process _in_flight set"
	@echo ""
	@echo "  BEFORE:"
	@echo "  4. make old-api               # terminal 2 — original app on $(OLDPORT)"
	@echo "  5. make probe-before          # four bugs confirmed present"
	@echo ""
	@echo "  AFTER:"
	@echo "  6. make api                   # terminal 2"
	@echo "  7. make worker                # terminal 3 (repeat for 3 workers)"
	@echo "  8. make token                 # copy the token"
	@echo "  9. make probe-after TOKEN=opk_...   # same four probes, all fixed"
	@echo ""
	@echo "  10. see docs/INTERVIEW.md for the narration and the diagram"
	@echo ""

diagram: ## Print the architecture diagram to draw by hand
	@sed -n '/^```$$/,/^```$$/p' docs/INTERVIEW.md | sed -n '/browser/,/trigger poller/p'

# -------------------------------------------------------------------- cleanup

clean-demo: ## Stop demo API/worker processes
	-@pkill -f "uvicorn server.app" 2>/dev/null || true
	-@pkill -f "server.worker" 2>/dev/null || true
	@echo "demo processes stopped"

nuke-old: ## Remove the main worktree
	-git worktree remove $(OLDTREE) --force 2>/dev/null || true
	@echo "$(OLDTREE) removed"

# ------------------------------------------------------- presentation helpers

explain: ## Show the claim query using the partial index (seeds rows, then cleans up)
	@$(PSQL) "INSERT INTO jobs (id, user_id, kind, payload, status, run_at) \
	  SELECT gen_random_uuid(), (SELECT id FROM users LIMIT 1), 'chat_turn', '{}'::jsonb, 'pending', now() \
	  FROM generate_series(1,50000)" >/dev/null
	@$(PSQL) "ANALYZE jobs" >/dev/null
	@echo "--- 50k pending rows; this is the real claim predicate ---"
	@$(PSQL) "EXPLAIN (ANALYZE, BUFFERS) SELECT id FROM jobs \
	  WHERE status='pending' AND run_at<=now() ORDER BY run_at \
	  FOR UPDATE SKIP LOCKED LIMIT 10" | head -8
	@$(PSQL) "DELETE FROM jobs" >/dev/null
	@$(PSQL) "VACUUM jobs" >/dev/null
	@echo "--- cleaned up ---"

cost: ## Per-model LLM spend recorded by the metering table
	@$(PSQL) "SELECT model, count(*) AS calls, sum(prompt_tokens) AS tok_in, \
	  sum(completion_tokens) AS tok_out, \
	  '\$$'||round((sum(prompt_tokens)*3.0+sum(completion_tokens)*15.0)/1000000,4) AS cost \
	  FROM llm_usage GROUP BY model" || true
	@echo "(empty means no chat turn has run yet on this database)"

depth: ## Queue depth by status — the autoscaling signal
	@$(PSQL) "SELECT status, count(*) FROM jobs GROUP BY status ORDER BY 1" || true

tenancy: ## Prove every tenant-scoped table carries user_id
	@$(PSQL) "SELECT table_name FROM information_schema.columns \
	  WHERE column_name='user_id' AND table_schema='public' ORDER BY 1"

schema: ## Indexes on the jobs table, including the partial one
	@$(PSQL) "SELECT indexdef FROM pg_indexes WHERE tablename='jobs' ORDER BY indexname"
