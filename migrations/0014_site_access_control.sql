CREATE TABLE site_access_policies (
    site_id uuid PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
    enabled boolean NOT NULL DEFAULT false,
    general_min_interval_seconds integer NOT NULL CHECK (general_min_interval_seconds BETWEEN 1 AND 86400),
    general_max_requests_per_hour integer NOT NULL CHECK (general_max_requests_per_hour BETWEEN 1 AND 3600),
    search_min_interval_seconds integer NOT NULL CHECK (search_min_interval_seconds BETWEEN 1 AND 86400),
    search_max_requests_per_hour integer NOT NULL CHECK (search_max_requests_per_hour BETWEEN 1 AND 3600),
    max_concurrency integer NOT NULL CHECK (max_concurrency BETWEEN 1 AND 4),
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site_access_leases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    operation text NOT NULL,
    request_class text NOT NULL CHECK (request_class IN ('general', 'search')),
    job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    attempt_id uuid REFERENCES step_attempts(id) ON DELETE SET NULL,
    owner text NOT NULL,
    policy_fingerprint text NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    completed_at timestamptz,
    outcome text CHECK (outcome IS NULL OR outcome IN ('completed', 'failed', 'cooldown')),
    status_code integer,
    response_sha256 text
);

CREATE INDEX site_access_leases_active_idx
    ON site_access_leases(site_id, expires_at)
    WHERE completed_at IS NULL;

CREATE INDEX site_access_leases_quota_idx
    ON site_access_leases(site_id, request_class, acquired_at DESC);

CREATE TABLE site_access_cooldowns (
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    request_class text NOT NULL CHECK (request_class IN ('general', 'search')),
    until_at timestamptz NOT NULL,
    reason text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, request_class)
);

ALTER TABLE jobs
    ADD COLUMN not_before timestamptz;

CREATE INDEX jobs_claim_not_before_idx
    ON jobs(status, not_before, created_at)
    WHERE status = 'queued';
