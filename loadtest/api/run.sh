#!/usr/bin/env bash
# Reproduces the k6 /chat/send runs in docs/LOADTEST.md. Starts its own API
# server on :8098 against the isolated `openpoke_loadtest` database (a
# dedicated user/token is minted there) so nothing here touches whatever is
# already running on :8099.
set -euo pipefail
cd "$(dirname "$0")/../.."

export DATABASE_URL="postgresql+asyncpg://openpoke:openpoke@localhost:5432/openpoke_loadtest"

TOKEN_LINE=$(.venv/bin/python -m server.auth create-user --email "loadtest-api-$(date +%s)@example.test" 2>&1 | grep '^token:')
TOKEN=$(echo "$TOKEN_LINE" | awk '{print $2}')
echo "Using token: $TOKEN"

OPENPOKE_PORT=8098 .venv/bin/python -m server.server --port 8098 > /tmp/openpoke_loadtest_api.log 2>&1 &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null || true" EXIT
sleep 2

# Prime the argon2 token cache (300s TTL) so the measured run doesn't pay the
# first-request KDF cost. See server/auth.py.
curl -s -o /dev/null -X POST http://localhost:8098/api/v1/chat/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"warmup"}]}'

VUS_LEVELS=("$@")
if [ "$#" -eq 0 ]; then VUS_LEVELS=(10 14 15); fi
for v in "${VUS_LEVELS[@]}"; do
  echo "=== VUS=$v ==="
  BASE_URL=http://localhost:8098 TOKEN=$TOKEN VUS="$v" DURATION=10s k6 run loadtest/api/chat_send.js
done
