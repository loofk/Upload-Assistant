-- Provider protocol negotiation and safe, operator-visible probe evidence.
-- Existing providers keep Chat Completions behavior; a root base URL is
-- normalized to the standard /v1 API prefix by the application.

ALTER TABLE llm_providers
    ADD COLUMN api_mode text NOT NULL DEFAULT 'chat_completions'
        CHECK (api_mode IN ('chat_completions', 'responses')),
    ADD COLUMN capabilities jsonb NOT NULL DEFAULT '{"catalog_source":"unknown","models":[]}'::jsonb,
    ADD COLUMN last_probe_evidence jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE llm_providers DROP CONSTRAINT llm_providers_health_status_check;
ALTER TABLE llm_providers
    ADD CONSTRAINT llm_providers_health_status_check
    CHECK (health_status IN ('unknown', 'catalog_ready', 'ready', 'failed'));

ALTER TABLE llm_providers DROP CONSTRAINT llm_providers_reasoning_effort_check;
ALTER TABLE llm_providers
    ADD CONSTRAINT llm_providers_reasoning_effort_check
    CHECK (reasoning_effort IN ('default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'));
