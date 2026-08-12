-- Make streaming the normal transport for long-running compatible model calls,
-- while retaining an explicit per-provider opt-out for providers without SSE.
ALTER TABLE llm_providers
    ADD COLUMN streaming_enabled boolean NOT NULL DEFAULT true;

-- Keep the high-value diagnostic fields in bounded scalar columns so the log
-- console can list and search them without loading or scanning large JSON
-- request/response evidence. Full recursively-redacted attributes remain lazy.
ALTER TABLE operational_logs
    ADD COLUMN action text,
    ADD COLUMN error_detail text;

UPDATE operational_logs
SET action = NULLIF(left(COALESCE(attributes->>'action', attributes->>'operation', ''), 255), ''),
    error_detail = NULLIF(left(COALESCE(attributes->>'error_detail', ''), 4000), '')
WHERE attributes ?| ARRAY['action', 'operation', 'error_detail'];

ALTER TABLE operational_logs
    ADD CONSTRAINT operational_logs_action_length_check
        CHECK (action IS NULL OR length(action) <= 255),
    ADD CONSTRAINT operational_logs_error_detail_length_check
        CHECK (error_detail IS NULL OR length(error_detail) <= 4000),
    ADD COLUMN search_text text GENERATED ALWAYS AS (
        lower(
            component || ' ' || message || ' ' ||
            COALESCE(error_code, '') || ' ' || COALESCE(action, '') || ' ' ||
            COALESCE(error_detail, '') || ' ' || COALESCE(method, '') || ' ' ||
            COALESCE(route, '') || ' ' || COALESCE(request_id, '')
        )
    ) STORED;

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX operational_logs_search_trgm_idx
    ON operational_logs USING gin (search_text gin_trgm_ops);
CREATE INDEX operational_logs_level_id_idx
    ON operational_logs(level, id DESC);
CREATE INDEX operational_logs_error_code_id_idx
    ON operational_logs(error_code, id DESC) WHERE error_code IS NOT NULL;
CREATE INDEX operational_logs_status_code_id_idx
    ON operational_logs(status_code, id DESC) WHERE status_code IS NOT NULL;
