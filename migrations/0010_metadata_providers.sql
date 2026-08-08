CREATE TABLE metadata_providers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    adapter text NOT NULL CHECK (adapter IN ('tmdb', 'ptgen')),
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}',
    secret_id uuid REFERENCES secrets(id) ON DELETE RESTRICT,
    health_status text NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('unknown', 'ready', 'failed')),
    last_health_check_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

