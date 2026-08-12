-- Provider routing and operator-visible connection state. Existing providers
-- retain incident diagnosis access; rule analysis must be explicitly enabled.

ALTER TABLE llm_providers
    ADD COLUMN reasoning_effort text NOT NULL DEFAULT 'default'
        CHECK (reasoning_effort IN ('default', 'low', 'medium', 'high')),
    ADD COLUMN use_cases text[] NOT NULL DEFAULT ARRAY['incident_diagnosis']::text[],
    ADD COLUMN health_status text NOT NULL DEFAULT 'unknown'
        CHECK (health_status IN ('unknown', 'ready', 'failed')),
    ADD COLUMN last_probe_at timestamptz,
    ADD COLUMN last_probe_latency_ms bigint
        CHECK (last_probe_latency_ms IS NULL OR last_probe_latency_ms >= 0),
    ADD COLUMN last_probe_error_code text,
    ADD CONSTRAINT llm_providers_use_cases_valid CHECK (
        use_cases <@ ARRAY['incident_diagnosis', 'rule_analysis']::text[]
    );
