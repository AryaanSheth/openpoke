// k6 load test for POST /api/v1/chat/send.
//
// The endpoint is now a pure enqueue (server/services/conversation/chat_handler.py):
// validate auth, insert one `jobs` row, return 202 with a job id. No LLM call, no
// agent runtime, on this path. The claim is that p95 should be single-digit ms
// and flat under load.
//
// Auth is argon2 with a 300s in-process token cache (server/auth.py), so the
// *first* request per server process pays the KDF (~40-100ms observed locally).
// Run 2-3 warmup requests against the target server with curl BEFORE starting
// k6 (see loadtest/api/run.sh) so the measured run isn't dominated by that.
//
// Usage:
//   BASE_URL=http://localhost:8098 TOKEN=opk_... k6 run loadtest/api/chat_send.js
//   VUS=50 DURATION=20s BASE_URL=... TOKEN=... k6 run loadtest/api/chat_send.js

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8098';
const TOKEN = __ENV.TOKEN;

if (!TOKEN) {
  throw new Error('Set TOKEN to a bearer token minted with `python -m server.auth create-user`.');
}

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-vus',
      vus: Number(__ENV.VUS || 20),
      duration: __ENV.DURATION || '20s',
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<100'],
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const res = http.post(
    `${BASE_URL}/api/v1/chat/send`,
    JSON.stringify({ messages: [{ role: 'user', content: 'loadtest message' }] }),
    { headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' } }
  );
  check(res, { 'status is 202': (r) => r.status === 202 });
}
