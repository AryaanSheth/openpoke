#!/usr/bin/env bash
# Unattended before/after demo. Starts everything, proves the bugs on main,
# proves they're fixed on this branch, then cleans up after itself.
#
#   make demo-auto
#
# Use this as a fallback or a dry run. For the live walkthrough prefer the manual
# sequence (`make demo`) — narrating each step is the point, and a script that
# does everything in 40 seconds gives you nothing to talk over.

set -uo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
PORT=8099
OLDPORT=8098
OLDTREE=/tmp/openpoke-main
B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; D=$'\033[2m'; O=$'\033[0m'

step() { echo; echo "${B}=== $* ===${O}"; }
fail() { echo "${R}FAILED: $*${O}"; cleanup; exit 1; }

# Only ever kill what THIS script started. A blanket `pkill -f server.worker`
# also kills the worker behind a live demo app, which is a nasty surprise
# mid-presentation.
OWN_PIDS=""
DEMO_EMAIL=""
cleanup() {
  # Drop the throwaway tenant this run created (CASCADE takes its jobs with it),
  # so repeated demos don't accumulate users and orphaned jobs in the dev DB.
  if [ -n "$DEMO_EMAIL" ]; then
    docker exec openpoke-postgres-1 psql -U openpoke -d openpoke -Atc \
      "DELETE FROM users WHERE email = '$DEMO_EMAIL'" >/dev/null 2>&1
  fi
  pkill -f "uvicorn server.app:app --port $PORT"  2>/dev/null
  pkill -f "uvicorn server.app:app --port $OLDPORT" 2>/dev/null
  for pid in $OWN_PIDS; do kill "$pid" 2>/dev/null; done
  sleep 2
  # Verify, don't assume: a worker that ignored TERM would otherwise leak and
  # keep claiming jobs after the demo ends.
  for pid in $OWN_PIDS; do
    if kill -0 "$pid" 2>/dev/null; then
      disown "$pid" 2>/dev/null || true   # suppress the shell's "Killed: 9" notice
      kill -9 "$pid" 2>/dev/null
    fi
  done
  sleep 1
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_for() {  # wait_for <url> <label>
  for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)
    [ "$code" = "200" ] && { echo "${D}$2 up${O}"; return 0; }
    sleep 1
  done
  return 1
}

# Free the ports this script needs, without touching anything else.
pkill -f "uvicorn server.app:app --port $PORT" 2>/dev/null
pkill -f "uvicorn server.app:app --port $OLDPORT" 2>/dev/null
sleep 1

step "0/5  Postgres + migrations"
make db-up >/dev/null || fail "postgres would not start"
$PY -m alembic upgrade head >/dev/null 2>&1 || fail "migrations failed"
echo "${G}ready${O}"

step "1/5  Gates — tests, lint, types, migration drift"
$PY -m pytest -p no:warnings --tb=no 2>&1 | tail -1
$PY -m ruff check . 2>&1 | tail -1
$PY -m mypy 2>&1 | tail -1
$PY -m alembic check 2>&1 | tail -1

step "2/5  BEFORE — the original app on :$OLDPORT"
test -d "$OLDTREE" || git worktree add "$OLDTREE" main >/dev/null 2>&1
cp -f .env.local "$OLDTREE/.env" 2>/dev/null
( cd "$OLDTREE" && exec "$OLDPWD/$PY" -m uvicorn server.app:app --port $OLDPORT ) \
  >/tmp/demo-old.log 2>&1 &
wait_for "http://127.0.0.1:$OLDPORT/api/v1/health" "original app" \
  || fail "original app did not start (see /tmp/demo-old.log)"
$PY scripts/probe.py before --port $OLDPORT --sqlite "$OLDTREE/server/data/triggers.db"
BEFORE_RC=$?
pkill -f "uvicorn server.app:app --port $OLDPORT" 2>/dev/null
sleep 1

step "3/5  AFTER — API + worker on :$PORT"
$PY -m uvicorn server.app:app --port $PORT >/tmp/demo-api.log 2>&1 &
$PY -m server.worker >/tmp/demo-worker.log 2>&1 &
OWN_PIDS="$OWN_PIDS $!"
wait_for "http://127.0.0.1:$PORT/api/v1/health" "new API" \
  || fail "new API did not start (see /tmp/demo-api.log)"

DEMO_EMAIL="demo-$(date +%s)@example.com"
TOKEN=$($PY -m server.auth create-user --email "$DEMO_EMAIL" 2>/dev/null \
        | awk '/token:/{print $2}')
[ -n "$TOKEN" ] || fail "could not mint a token"
echo "${D}token ${TOKEN:0:20}...${O}"

$PY scripts/probe.py after --port $PORT --token "$TOKEN"
AFTER_RC=$?

step "4/5  Trigger-wipe fix — the two-tenant test"
$PY -m pytest tests/test_phase1_tenancy.py -k "delete_history_leaves" -p no:warnings --tb=no 2>&1 | tail -1

step "5/5  Exactly-once under real concurrency"
$PY -m pytest tests/test_exactly_once.py -p no:warnings --tb=no 2>&1 | tail -1

echo
if [ "${BEFORE_RC:-1}" -eq 0 ] && [ "${AFTER_RC:-1}" -eq 0 ]; then
  echo "${G}${B}DEMO PASSED${O} — four bugs confirmed present on main, all four fixed here."
else
  echo "${R}${B}DEMO INCOMPLETE${O} — before=$BEFORE_RC after=$AFTER_RC (0 = all probes matched)"
fi
echo "${D}logs: /tmp/demo-old.log /tmp/demo-api.log /tmp/demo-worker.log${O}"
echo
