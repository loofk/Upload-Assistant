-- Extend evidence-bound diagnostics to one operational-log row and allow
-- provider requests up to ten minutes. This does not override an upstream
-- proxy or provider timeout.

ALTER TABLE llm_providers
    DROP CONSTRAINT llm_providers_timeout_seconds_check;
ALTER TABLE llm_providers
    ADD CONSTRAINT llm_providers_timeout_seconds_check
    CHECK (timeout_seconds BETWEEN 1 AND 600);

ALTER TABLE diagnostics
    ADD COLUMN log_id bigint REFERENCES operational_logs(id) ON DELETE SET NULL;
CREATE INDEX diagnostics_log_idx
    ON diagnostics(log_id, created_at DESC) WHERE log_id IS NOT NULL;
