CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'auditor' CHECK (role IN ('admin', 'operator', 'auditor')),
    totp_secret_id uuid,
    totp_enabled boolean NOT NULL DEFAULT false,
    disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash bytea NOT NULL UNIQUE,
    csrf_hash bytea NOT NULL,
    expires_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user_id_idx ON sessions(user_id);
CREATE INDEX sessions_expires_at_idx ON sessions(expires_at);

CREATE TABLE api_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name text NOT NULL,
    token_prefix text NOT NULL,
    token_hash bytea NOT NULL UNIQUE,
    scopes text[] NOT NULL DEFAULT '{}',
    expires_at timestamptz,
    last_used_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX api_tokens_user_id_idx ON api_tokens(user_id);

CREATE TABLE secrets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose text NOT NULL,
    ciphertext bytea NOT NULL,
    nonce bytea NOT NULL,
    key_version integer NOT NULL,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    rotated_from uuid REFERENCES secrets(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE users ADD CONSTRAINT users_totp_secret_fk FOREIGN KEY (totp_secret_id) REFERENCES secrets(id) ON DELETE SET NULL;

CREATE TABLE sites (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    adapter text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}',
    live_validation_status text NOT NULL DEFAULT 'unverified' CHECK (live_validation_status IN ('unverified', 'verified', 'failed')),
    active_rule_revision_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site_rule_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    revision integer NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'retired')),
    fingerprint text NOT NULL,
    source_url text,
    captured_at timestamptz,
    markdown_path text NOT NULL,
    markdown_sha256 text NOT NULL,
    parsed_policy jsonb NOT NULL DEFAULT '{}',
    obligations jsonb NOT NULL DEFAULT '[]',
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, revision),
    UNIQUE (site_id, fingerprint)
);
ALTER TABLE sites ADD CONSTRAINT sites_active_rule_revision_fk FOREIGN KEY (active_rule_revision_id) REFERENCES site_rule_revisions(id) ON DELETE RESTRICT;

CREATE TABLE site_rule_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_revision_id uuid NOT NULL REFERENCES site_rule_revisions(id) ON DELETE CASCADE,
    reviewer_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    comment text,
    fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    name text NOT NULL,
    secret_id uuid NOT NULL REFERENCES secrets(id) ON DELETE RESTRICT,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, name)
);

CREATE TABLE downloaders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    adapter text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}',
    secret_id uuid REFERENCES secrets(id) ON DELETE RESTRICT,
    health_status text NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('unknown', 'ready', 'failed')),
    last_health_check_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE downloader_path_mappings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    downloader_id uuid NOT NULL REFERENCES downloaders(id) ON DELETE CASCADE,
    remote_path text NOT NULL,
    local_path text NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    UNIQUE (downloader_id, remote_path)
);

CREATE TABLE image_hosts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    adapter text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    priority integer NOT NULL DEFAULT 100,
    config jsonb NOT NULL DEFAULT '{}',
    secret_id uuid REFERENCES secrets(id) ON DELETE RESTRICT,
    health_status text NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('unknown', 'ready', 'failed')),
    last_health_check_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE screenshot_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    revision integer NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}',
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, revision)
);

CREATE TABLE workflow_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    version integer NOT NULL,
    definition jsonb NOT NULL,
    definition_sha256 text NOT NULL,
    active boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'queued', 'running', 'paused', 'blocked', 'failed', 'complete', 'cancelled')),
    execution_mode text NOT NULL DEFAULT 'auto' CHECK (execution_mode IN ('auto', 'step')),
    workflow_version_id uuid NOT NULL REFERENCES workflow_versions(id) ON DELETE RESTRICT,
    source_site_id uuid REFERENCES sites(id) ON DELETE RESTRICT,
    target_site_id uuid REFERENCES sites(id) ON DELETE RESTRICT,
    current_step_key text,
    stop_after_step text,
    input jsonb NOT NULL DEFAULT '{}',
    config_snapshot jsonb NOT NULL DEFAULT '{}',
    blockers jsonb NOT NULL DEFAULT '[]',
    next_actions jsonb NOT NULL DEFAULT '[]',
    resume_state jsonb NOT NULL DEFAULT '{}',
    summary jsonb NOT NULL DEFAULT '{}',
    idempotency_key text,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX jobs_created_by_idempotency_key_idx ON jobs(created_by, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX jobs_queue_idx ON jobs(status, created_at) WHERE status = 'queued';
CREATE INDEX jobs_lease_idx ON jobs(lease_expires_at) WHERE status = 'running';
CREATE INDEX jobs_finished_at_idx ON jobs(finished_at) WHERE finished_at IS NOT NULL;

CREATE TABLE job_steps (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    step_key text NOT NULL,
    position integer NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'running', 'paused', 'blocked', 'failed', 'complete', 'skipped', 'cancelled')),
    required boolean NOT NULL DEFAULT true,
    gate_kind text,
    input_snapshot jsonb NOT NULL DEFAULT '{}',
    output_summary jsonb NOT NULL DEFAULT '{}',
    blockers jsonb NOT NULL DEFAULT '[]',
    next_actions jsonb NOT NULL DEFAULT '[]',
    resume_state jsonb NOT NULL DEFAULT '{}',
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, step_key),
    UNIQUE (job_id, position)
);

CREATE TABLE step_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_step_id uuid NOT NULL REFERENCES job_steps(id) ON DELETE CASCADE,
    attempt integer NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'paused', 'blocked', 'failed', 'complete', 'cancelled')),
    adapter text,
    adapter_version text,
    input_snapshot jsonb NOT NULL DEFAULT '{}',
    output_summary jsonb NOT NULL DEFAULT '{}',
    error_code text,
    error_message text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (job_step_id, attempt)
);

CREATE TABLE job_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    job_step_id uuid REFERENCES job_steps(id) ON DELETE CASCADE,
    attempt_id uuid REFERENCES step_attempts(id) ON DELETE CASCADE,
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    actor_type text NOT NULL,
    actor_id text,
    payload jsonb NOT NULL DEFAULT '{}',
    previous_hash text,
    event_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence),
    UNIQUE (job_id, event_hash)
);
CREATE INDEX job_events_job_created_idx ON job_events(job_id, created_at);

CREATE TABLE artifacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    job_step_id uuid REFERENCES job_steps(id) ON DELETE CASCADE,
    attempt_id uuid REFERENCES step_attempts(id) ON DELETE CASCADE,
    kind text NOT NULL,
    storage_backend text NOT NULL DEFAULT 'local',
    storage_path text NOT NULL,
    filename text NOT NULL,
    mime_type text,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    sha256 text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artifacts_job_id_idx ON artifacts(job_id);
CREATE INDEX artifacts_expires_at_idx ON artifacts(expires_at);

CREATE TABLE approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    job_step_id uuid REFERENCES job_steps(id) ON DELETE CASCADE,
    kind text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'consumed')),
    binding_hash text NOT NULL,
    rule_fingerprints jsonb NOT NULL DEFAULT '{}',
    token_hash bytea,
    expires_at timestamptz,
    decided_by uuid REFERENCES users(id) ON DELETE SET NULL,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX approvals_job_id_idx ON approvals(job_id);

CREATE TABLE schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    kind text NOT NULL,
    cron_expression text NOT NULL,
    timezone text NOT NULL DEFAULT 'Asia/Shanghai',
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}',
    next_run_at timestamptz,
    last_run_at timestamptz,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE candidate_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id uuid REFERENCES schedules(id) ON DELETE SET NULL,
    source_site_id uuid NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    target_site_id uuid NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    source_torrent_id text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate', 'blocked', 'submitted', 'expired')),
    discovered_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    UNIQUE (source_site_id, target_site_id, source_torrent_id)
);
CREATE INDEX candidate_items_expires_at_idx ON candidate_items(expires_at);

CREATE TABLE notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'cancelled')),
    payload jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    scheduled_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_type text NOT NULL,
    actor_id text,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id text,
    trace_id uuid,
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_created_at_idx ON audit_events(created_at);

CREATE TABLE legacy_imports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind text NOT NULL,
    source_path text,
    source_sha256 text,
    status text NOT NULL CHECK (status IN ('running', 'blocked', 'failed', 'complete')),
    report jsonb NOT NULL DEFAULT '{}',
    imported_by uuid REFERENCES users(id) ON DELETE SET NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE INDEX legacy_imports_expires_at_idx ON legacy_imports(expires_at);
