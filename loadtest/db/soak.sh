#!/usr/bin/env bash
# The 30-minute soak the plan asks for and the first load pass didn't run.
#
#   loadtest/db/soak.sh [minutes] [clients] [batch]
#
# Why it exists: every claim is an UPDATE, so every claim leaves a dead tuple.
# A queue table is the textbook autovacuum-bloat case, and throughput that looks
# fine for 60 seconds can collapse over hours. A benchmark that doesn't run long
# enough to bloat proves nothing about production.
#
# Samples every 30s: pending depth, claims/s over the interval, n_dead_tup,
# n_live_tup, last autovacuum, and total table+index size. Writes CSV so the
# trend is plottable, not anecdotal.
set -uo pipefail

MINUTES="${1:-30}"
CLIENTS="${2:-4}"
BATCH="${3:-10}"
CONTAINER="openpoke-postgres-1"
DB="openpoke_soak"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../results_soak.csv"

psql() { docker exec -i "$CONTAINER" psql -U openpoke -d "$DB" -Atc "$1"; }
admin() { docker exec -i "$CONTAINER" psql -U openpoke -d postgres -Atc "$1"; }

echo "=== soak: ${MINUTES}min, ${CLIENTS} clients, batch ${BATCH} ==="

# Isolated database — never the dev or test one.
admin "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1 \
  || admin "CREATE DATABASE $DB" >/dev/null
docker exec -i "$CONTAINER" psql -U openpoke -d "$DB" -q <<'SQL'
CREATE TABLE IF NOT EXISTS jobs (
  id BIGSERIAL PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'pending',
  run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempts INT NOT NULL DEFAULT 0,
  claimed_at TIMESTAMPTZ,
  claimed_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_pending_run_at ON jobs (status, run_at) WHERE status='pending';
TRUNCATE jobs;
INSERT INTO jobs (status) SELECT 'pending' FROM generate_series(1, 200000);
SQL
echo "seeded 200k pending rows"

# A recycler keeps the queue non-empty so claims keep happening for the whole
# run — otherwise the table drains in minutes and the soak measures nothing.
( while true; do
    psql "UPDATE jobs SET status='pending', claimed_at=NULL WHERE status='running'" >/dev/null 2>&1
    sleep 10
  done ) &
RECYCLER=$!

docker exec -i "$CONTAINER" pgbench -U openpoke -d "$DB" -n -c "$CLIENTS" -j 2 \
  -T $((MINUTES * 60)) -D batch="$BATCH" -f - >/tmp/soak_pgbench.log 2>&1 <"$HERE/claim.sql" &
PGBENCH=$!

cleanup() { kill $RECYCLER $PGBENCH 2>/dev/null; }
trap cleanup EXIT INT TERM

echo "elapsed_s,pending,claims_per_s,n_dead_tup,n_live_tup,table_bytes,last_autovacuum" > "$OUT"
START=$(date +%s); PREV_CLAIMED=0
while kill -0 $PGBENCH 2>/dev/null; do
  sleep 30
  NOW=$(date +%s); EL=$((NOW - START))
  read -r PENDING CLAIMED DEAD LIVE BYTES VAC <<<"$(psql "
    SELECT (SELECT count(*) FROM jobs WHERE status='pending'),
           (SELECT count(*) FROM jobs WHERE status='running'),
           s.n_dead_tup, s.n_live_tup,
           pg_total_relation_size('jobs'),
           coalesce(to_char(greatest(s.last_autovacuum, s.last_vacuum),'HH24:MI:SS'),'never')
    FROM pg_stat_user_tables s WHERE s.relname='jobs'" | tr '|' ' ')"
  RATE=$(awk "BEGIN{printf \"%.0f\", ($CLAIMED - $PREV_CLAIMED)/30}")
  PREV_CLAIMED=$CLAIMED
  echo "$EL,$PENDING,$RATE,$DEAD,$LIVE,$BYTES,$VAC" >> "$OUT"
  printf "  t+%-5ss dead_tup=%-10s size=%-8s autovacuum=%s\n" \
    "$EL" "$DEAD" "$(numfmt --to=iec "$BYTES" 2>/dev/null || echo "$BYTES")" "$VAC"
done

cleanup
echo
echo "=== done — $OUT ==="
tail -3 "$OUT"
echo
echo "Read it: if n_dead_tup and table_bytes grow without bound while autovacuum"
echo "never fires, tune autovacuum_vacuum_scale_factor ON THIS TABLE specifically."
