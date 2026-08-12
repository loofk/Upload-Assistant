CREATE TABLE site_rule_source_sets (
    site_id uuid PRIMARY KEY REFERENCES sites(id) ON DELETE CASCADE,
    sources jsonb NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[a-f0-9]{64}$'),
    scope_confirmed boolean NOT NULL DEFAULT false,
    cookie_hosts_confirmed boolean NOT NULL DEFAULT false,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(sources) = 'array')
);

CREATE TABLE site_rule_collection_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    source_set_fingerprint text NOT NULL CHECK (source_set_fingerprint ~ '^[a-f0-9]{64}$'),
    provider_id uuid NOT NULL REFERENCES llm_providers(id) ON DELETE RESTRICT,
    status text NOT NULL CHECK (status IN ('queued','fetching','analyzing','ready','failed')),
    not_before timestamptz NOT NULL DEFAULT now(),
    idempotency_key text NOT NULL,
    created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    trace_id uuid,
    rule_revision_id uuid REFERENCES site_rule_revisions(id) ON DELETE SET NULL,
    error_code text,
    error_detail text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (created_by, idempotency_key),
    CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    CHECK (error_code IS NULL OR length(error_code) <= 128),
    CHECK (error_detail IS NULL OR length(error_detail) <= 2000)
);

CREATE TABLE site_rule_collection_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES site_rule_collection_runs(id) ON DELETE CASCADE,
    source_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    source_url text NOT NULL,
    scope text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','fetching','ready','failed')),
    http_status integer,
    content_type text,
    size_bytes bigint,
    text_sha256 text,
    storage_path text,
    error_code text,
    error_detail text,
    captured_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, source_id),
    UNIQUE (run_id, ordinal),
    CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[a-f0-9]{64}$'),
    CHECK (error_code IS NULL OR length(error_code) <= 128),
    CHECK (error_detail IS NULL OR length(error_detail) <= 2000)
);

CREATE INDEX site_rule_collection_runs_queue_idx
    ON site_rule_collection_runs(not_before, created_at) WHERE status = 'queued';
CREATE INDEX site_rule_collection_runs_site_idx
    ON site_rule_collection_runs(site_id, created_at DESC);
CREATE INDEX site_rule_collection_documents_run_idx
    ON site_rule_collection_documents(run_id, source_id);
