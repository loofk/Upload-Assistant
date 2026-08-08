ALTER TABLE jobs
    ADD COLUMN replay_of_job_id uuid REFERENCES jobs(id) ON DELETE SET NULL;

CREATE INDEX jobs_replay_of_job_id_idx
    ON jobs(replay_of_job_id)
    WHERE replay_of_job_id IS NOT NULL;
