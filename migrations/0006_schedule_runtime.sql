CREATE TABLE schedule_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id uuid NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    scheduled_for timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'created', 'failed', 'cancelled')),
    job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (schedule_id, scheduled_for)
);

CREATE INDEX schedule_runs_claim_idx
    ON schedule_runs(status, next_attempt_at, lease_expires_at, scheduled_for);
CREATE INDEX schedule_runs_job_id_idx ON schedule_runs(job_id);

ALTER TABLE notifications
    ADD COLUMN schedule_run_id uuid REFERENCES schedule_runs(id) ON DELETE CASCADE,
    ADD COLUMN job_id uuid REFERENCES jobs(id) ON DELETE SET NULL;

CREATE UNIQUE INDEX notifications_schedule_run_id_key
    ON notifications(schedule_run_id) WHERE schedule_run_id IS NOT NULL;
CREATE INDEX notifications_job_id_idx ON notifications(job_id);
