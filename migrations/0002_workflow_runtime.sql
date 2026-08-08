DROP INDEX IF EXISTS jobs_created_by_idempotency_key_idx;

ALTER TABLE jobs
    ADD COLUMN idempotency_owner text NOT NULL DEFAULT 'system',
    ADD COLUMN idempotency_request_hash text;

CREATE UNIQUE INDEX jobs_idempotency_owner_key_idx
    ON jobs(idempotency_owner, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX workflow_versions_single_active_idx
    ON workflow_versions(name)
    WHERE active = true;

CREATE INDEX job_steps_runnable_idx
    ON job_steps(job_id, position)
    WHERE status IN ('ready', 'running', 'paused', 'blocked', 'failed');
