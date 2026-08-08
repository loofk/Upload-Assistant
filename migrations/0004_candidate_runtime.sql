ALTER TABLE candidate_items
    DROP CONSTRAINT candidate_items_source_site_id_target_site_id_source_torren_key;

ALTER TABLE candidate_items
    ADD COLUMN discovery_job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    ADD COLUMN recommendation_date date NOT NULL DEFAULT ((now() AT TIME ZONE 'Asia/Shanghai')::date),
    ADD COLUMN rank integer CHECK (rank IS NULL OR rank > 0),
    ADD COLUMN score double precision NOT NULL DEFAULT 0,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE candidate_items
    ADD CONSTRAINT candidate_items_daily_source_target_torrent_key
    UNIQUE (recommendation_date, source_site_id, target_site_id, source_torrent_id);

CREATE INDEX candidate_items_daily_lookup_idx
    ON candidate_items(recommendation_date DESC, source_site_id, target_site_id, rank, score DESC);

CREATE INDEX candidate_items_discovery_job_id_idx ON candidate_items(discovery_job_id);
