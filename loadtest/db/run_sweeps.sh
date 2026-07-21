#!/usr/bin/env bash
# Reproduces the two pgbench sweeps in docs/LOADTEST.md against the isolated
# `openpoke_loadtest` database (never `openpoke` — see that file's "Method"
# section for why isolation matters: an earlier draft of this seeded real
# `chat_turn` jobs into the live database and the live worker started
# processing them).
#
# Prereqs:
#   docker compose up -d postgres
#   .venv/bin/python -m alembic -x db_url="postgresql+asyncpg://openpoke:openpoke@localhost:5432/openpoke_loadtest" upgrade head
#   docker exec openpoke-postgres-1 psql -U openpoke -d openpoke_loadtest -c \
#     "INSERT INTO users (id, email, api_key_hash, timezone) VALUES ('00000000-0000-0000-0000-0000000000aa', 'loadtest-db@example.test', 'unused', 'UTC') ON CONFLICT DO NOTHING;"
#   cat loadtest/db/seed.sql | docker exec -i openpoke-postgres-1 psql -U openpoke -d openpoke_loadtest -v n=3000000 -f -
set -euo pipefail
cd "$(dirname "$0")/../.."

docker cp loadtest/db/claim.sql openpoke-postgres-1:/tmp/claim.sql
docker cp loadtest/db/reset.sql openpoke-postgres-1:/tmp/reset.sql

echo "=== Concurrency sweep (batch=10 fixed, matches OPENPOKE_WORKER_CONCURRENCY default) ==="
echo "clients,batch,elapsed_s,pgbench_tps,jobs_claimed,jobs_per_s,commits_delta,commits_per_s" | tee loadtest/results_concurrency.csv
for c in 1 2 4 8 16; do
  bash loadtest/db/run_one.sh "$c" 10 8 | tee -a loadtest/results_concurrency.csv
done

echo
echo "=== Batch-size sweep (clients=4 fixed) ==="
echo "clients,batch,elapsed_s,pgbench_tps,jobs_claimed,jobs_per_s,commits_delta,commits_per_s" | tee loadtest/results_batchsize.csv
for b in 1 10 100; do
  bash loadtest/db/run_one.sh 4 "$b" 8 | tee -a loadtest/results_batchsize.csv
done
