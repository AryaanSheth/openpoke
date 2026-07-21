#!/usr/bin/env bash
# Run one pgbench claim-query trial against the containerized Postgres and
# print a single CSV line:
#   clients,batch,duration_s,pgbench_tps,jobs_claimed,jobs_per_s,commits_delta,commits_per_s
#
# Usage: run_one.sh <clients> <batch> <duration_s>
#
# Method: reset the load-test pool to 'pending' (loadtest/db/reset.sql),
# snapshot pending-row count and pg_stat_database.xact_commit, run pgbench
# for <duration_s> against loadtest/db/claim.sql, snapshot again. jobs/s and
# commits/s are computed from the real before/after deltas divided by wall
# time actually elapsed (not assumed from pgbench's own duration), so the
# numbers don't depend on pgbench's batch always being full.
set -euo pipefail

CLIENTS="$1"
BATCH="$2"
DURATION="${3:-8}"
CONTAINER="openpoke-postgres-1"
DB="openpoke_loadtest"
USER_ID="00000000-0000-0000-0000-0000000000aa"

psql_c() {
  docker exec -i "$CONTAINER" psql -U openpoke -d "$DB" -t -A -c "$1"
}

# Recycle pool back to pending.
docker exec -i "$CONTAINER" psql -U openpoke -d "$DB" -f /tmp/reset.sql > /dev/null

pending_before=$(psql_c "select count(*) from jobs where status='pending';")
commit_before=$(psql_c "select xact_commit from pg_stat_database where datname='$DB';")
t0=$(date +%s.%N)

JOBS=1
if [ "$CLIENTS" -gt 8 ]; then JOBS=8; fi
if [ "$JOBS" -gt "$CLIENTS" ]; then JOBS="$CLIENTS"; fi

pgbench_out=$(docker exec "$CONTAINER" pgbench -U openpoke -d "$DB" \
  -f /tmp/claim.sql -D batch="$BATCH" -c "$CLIENTS" -j "$JOBS" -T "$DURATION" -n 2>&1)

t1=$(date +%s.%N)
pending_after=$(psql_c "select count(*) from jobs where status='pending';")
commit_after=$(psql_c "select xact_commit from pg_stat_database where datname='$DB';")

elapsed=$(echo "$t1 - $t0" | bc)
tps=$(echo "$pgbench_out" | grep '^tps' | awk '{print $3}')
jobs_claimed=$((pending_before - pending_after))
jobs_per_s=$(echo "scale=1; $jobs_claimed / $elapsed" | bc)
commits_delta=$((commit_after - commit_before))
commits_per_s=$(echo "scale=1; $commits_delta / $elapsed" | bc)

echo "${CLIENTS},${BATCH},${elapsed},${tps},${jobs_claimed},${jobs_per_s},${commits_delta},${commits_per_s}"
