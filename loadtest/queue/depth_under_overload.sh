#!/usr/bin/env bash
# Queue depth under sustained overload: arrival rate > service rate.
#
# Each second: insert ARRIVAL new pending jobs, then run one claim-batch of
# SERVICE_BATCH (claim + immediately complete, simulating one worker doing
# useful work rather than being LLM-bound) against the isolated
# `openpoke_loadtest` database. Samples `count(*) WHERE status='pending'`
# every second. Confirms depth grows (arrival > service), nothing is lost
# (pending + running + done == total inserted), and the claim query itself
# doesn't slow down as depth grows (that's the index earning its keep).
#
# Usage: depth_under_overload.sh [seconds] [arrival_per_s] [service_per_s]
set -euo pipefail
SECONDS_N="${1:-20}"
ARRIVAL="${2:-300}"
SERVICE="${3:-100}"
CONTAINER="openpoke-postgres-1"
DB="openpoke_loadtest"
USER_ID="00000000-0000-0000-0000-0000000000aa"

psql_c() { docker exec -i "$CONTAINER" psql -U openpoke -d "$DB" -t -A -c "$1"; }

docker exec -i "$CONTAINER" psql -U openpoke -d "$DB" -c "DELETE FROM jobs;" > /dev/null

echo "t,inserted_total,pending,running,done,claim_ms"
total_inserted=0
for i in $(seq 1 "$SECONDS_N"); do
  psql_c "
    INSERT INTO jobs (id, user_id, kind, payload, status, attempts, max_attempts, run_at, created_at)
    SELECT gen_random_uuid(), '$USER_ID'::uuid, 'loadtest_noop', '{}'::jsonb, 'pending', 0, 5, now(), now()
    FROM generate_series(1, $ARRIVAL);
  " > /dev/null
  total_inserted=$((total_inserted + ARRIVAL))

  t0=$(date +%s.%N)
  claimed=$(psql_c "
    WITH c AS (
      UPDATE jobs SET status='running', claimed_at=now(), claimed_by='depth-service', attempts=attempts+1
      WHERE id IN (
        SELECT id FROM jobs WHERE status='pending' AND run_at <= now()
        ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT $SERVICE
      )
      RETURNING id
    )
    SELECT count(*) FROM c;
  ")
  docker exec -i "$CONTAINER" psql -U openpoke -d "$DB" -c \
    "UPDATE jobs SET status='done', claimed_at=NULL, claimed_by=NULL WHERE status='running' AND claimed_by='depth-service';" > /dev/null
  t1=$(date +%s.%N)
  claim_ms=$(echo "($t1 - $t0) * 1000" | bc)

  pending=$(psql_c "select count(*) from jobs where status='pending';")
  running=$(psql_c "select count(*) from jobs where status='running';")
  done_n=$(psql_c "select count(*) from jobs where status='done';")

  echo "${i},${total_inserted},${pending},${running},${done_n},${claim_ms}"
  sleep 1
done
