-- pgbench script: the real claim query, verbatim from server/jobs/queue.py
-- (_CLAIM_SQL), minus the SQLAlchemy `:worker` bind (hardcoded here — SKIP
-- LOCKED exclusivity comes from the row lock, not from a distinct
-- claimed_by, so this doesn't change what's being measured).
--
-- :batch is supplied on the command line via `pgbench -D batch=N`.
--
-- One statement == one implicit (autocommit) transaction, so pgbench's
-- reported tps IS commits/sec for this script, with no extra bookkeeping.
UPDATE jobs SET status='running', claimed_at=now(), claimed_by='pgbench-worker', attempts=attempts+1
WHERE id IN (
    SELECT id FROM jobs
    WHERE status='pending' AND run_at <= now()
    ORDER BY run_at
    FOR UPDATE SKIP LOCKED
    LIMIT :batch
)
RETURNING id;
