ALTER TABLE candidate_items
    ADD COLUMN submitted_job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    ADD COLUMN submitted_at timestamptz;

CREATE INDEX candidate_items_submitted_job_id_idx ON candidate_items(submitted_job_id);
