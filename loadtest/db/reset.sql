-- Recycle the load-test pool back to 'pending' between pgbench runs, instead
-- of re-seeding millions of rows every time. Run against the dedicated
-- `openpoke_loadtest` database (never `openpoke`).
UPDATE jobs
SET status = 'pending', run_at = now(), claimed_at = NULL, claimed_by = NULL, attempts = 0
WHERE user_id = '00000000-0000-0000-0000-0000000000aa'::uuid AND status <> 'pending';
