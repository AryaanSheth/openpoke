-- Seed N pending jobs owned by the dedicated load-test user
-- (00000000-0000-0000-0000-0000000000aa), in the dedicated `openpoke_loadtest`
-- database (schema created via `alembic upgrade head`, never the live `openpoke`
-- database — a real worker process picked up an earlier draft of this seed
-- because it used kind='chat_turn', see docs/LOADTEST.md "method"). Run as:
--   psql ... -v n=3000000 -f loadtest/db/seed.sql
DELETE FROM jobs;

INSERT INTO jobs (id, user_id, kind, payload, status, attempts, max_attempts, run_at, created_at)
SELECT
    gen_random_uuid(),
    '00000000-0000-0000-0000-0000000000aa'::uuid,
    'loadtest_noop',
    '{}'::jsonb,
    'pending',
    0,
    5,
    now(),
    now()
FROM generate_series(1, :n);
