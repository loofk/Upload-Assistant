-- Bind durable AI work to the exact non-secret provider execution contract
-- that the operator reviewed when the work was queued. Existing in-flight
-- rows intentionally remain NULL and are failed safely after upgrade instead
-- of being sent through a potentially changed endpoint or data boundary.
ALTER TABLE diagnostics
    ADD COLUMN provider_config_sha256 text,
    ADD CONSTRAINT diagnostics_provider_config_sha256_check
        CHECK (provider_config_sha256 IS NULL OR provider_config_sha256 ~ '^[a-f0-9]{64}$');

ALTER TABLE site_rule_collection_runs
    ADD COLUMN provider_config_sha256 text,
    ADD CONSTRAINT site_rule_collection_runs_provider_config_sha256_check
        CHECK (provider_config_sha256 IS NULL OR provider_config_sha256 ~ '^[a-f0-9]{64}$');

DROP INDEX diagnostics_active_dedupe_idx;
CREATE UNIQUE INDEX diagnostics_active_dedupe_idx
    ON diagnostics(provider_id, evidence_sha256, prompt_version, provider_config_sha256)
    WHERE status IN ('queued', 'running') AND provider_config_sha256 IS NOT NULL;

CREATE INDEX diagnostics_running_started_idx
    ON diagnostics(started_at) WHERE status = 'running';

CREATE INDEX diagnostic_messages_running_created_idx
    ON diagnostic_messages(created_at) WHERE status = 'running';
