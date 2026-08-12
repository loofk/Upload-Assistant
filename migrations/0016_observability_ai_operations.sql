-- UA-SPEC-OPS-AI-001: durable operational observability, incidents,
-- evidence-bound diagnostics, API-token lifecycle metadata, and encrypted
-- backup bookkeeping.  Operational logs intentionally remain separate from
-- both audit_events and the append-only job_events chain.

ALTER TABLE step_attempts
    ADD COLUMN trace_id uuid NOT NULL DEFAULT gen_random_uuid();
CREATE INDEX step_attempts_trace_id_idx ON step_attempts(trace_id);

CREATE TABLE operational_logs (
    id bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    level text NOT NULL CHECK (level IN ('debug', 'info', 'warn', 'error')),
    component text NOT NULL,
    message text NOT NULL,
    request_id text,
    trace_id uuid,
    job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    step_key text,
    attempt_id uuid REFERENCES step_attempts(id) ON DELETE SET NULL,
    method text,
    route text,
    status_code integer CHECK (status_code IS NULL OR status_code BETWEEN 100 AND 599),
    duration_ms bigint CHECK (duration_ms IS NULL OR duration_ms >= 0),
    response_bytes bigint CHECK (response_bytes IS NULL OR response_bytes >= 0),
    error_code text,
    actor_type text,
    actor_id text,
    attributes jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX operational_logs_occurred_id_idx ON operational_logs(occurred_at DESC, id DESC);
CREATE INDEX operational_logs_trace_idx ON operational_logs(trace_id, id DESC) WHERE trace_id IS NOT NULL;
CREATE INDEX operational_logs_request_idx ON operational_logs(request_id, id DESC) WHERE request_id IS NOT NULL;
CREATE INDEX operational_logs_job_idx ON operational_logs(job_id, id DESC) WHERE job_id IS NOT NULL;
CREATE INDEX operational_logs_attempt_idx ON operational_logs(attempt_id, id DESC) WHERE attempt_id IS NOT NULL;
CREATE INDEX operational_logs_component_level_idx ON operational_logs(component, level, id DESC);

CREATE TABLE incidents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    kind text NOT NULL,
    fingerprint text NOT NULL UNIQUE,
    title text NOT NULL,
    summary text NOT NULL,
    occurrence_count bigint NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    first_occurred_at timestamptz NOT NULL DEFAULT now(),
    last_occurred_at timestamptz NOT NULL DEFAULT now(),
    job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    trace_id uuid,
    evidence jsonb NOT NULL DEFAULT '{}',
    acknowledged_by uuid REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_at timestamptz,
    resolved_by uuid REFERENCES users(id) ON DELETE SET NULL,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX incidents_status_last_idx ON incidents(status, last_occurred_at DESC, id DESC);
CREATE INDEX incidents_job_idx ON incidents(job_id, last_occurred_at DESC) WHERE job_id IS NOT NULL;

CREATE TABLE llm_providers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    kind text NOT NULL DEFAULT 'openai_compatible' CHECK (kind = 'openai_compatible'),
    base_url text NOT NULL,
    model text NOT NULL,
    data_level text NOT NULL CHECK (data_level IN ('local', 'remote')),
    json_mode boolean NOT NULL DEFAULT true,
    timeout_seconds integer NOT NULL DEFAULT 60 CHECK (timeout_seconds BETWEEN 1 AND 300),
    enabled boolean NOT NULL DEFAULT false,
    outbound_consent boolean NOT NULL DEFAULT false,
    secret_id uuid REFERENCES secrets(id) ON DELETE RESTRICT,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE diagnostics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES llm_providers(id) ON DELETE RESTRICT,
    incident_id uuid REFERENCES incidents(id) ON DELETE SET NULL,
    job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'failed', 'complete', 'cancelled')),
    data_level text NOT NULL CHECK (data_level IN ('local', 'remote')),
    prompt_version text NOT NULL,
    evidence_sha256 text NOT NULL CHECK (length(evidence_sha256) = 64),
    evidence jsonb NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]',
    truncated_fields jsonb NOT NULL DEFAULT '[]',
    omitted_count integer NOT NULL DEFAULT 0 CHECK (omitted_count >= 0),
    result jsonb,
    response_sha256 text,
    input_tokens integer CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens integer CHECK (output_tokens IS NULL OR output_tokens >= 0),
    latency_ms bigint CHECK (latency_ms IS NULL OR latency_ms >= 0),
    error_code text,
    error_message text,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX diagnostics_active_dedupe_idx
    ON diagnostics(provider_id, evidence_sha256, prompt_version)
    WHERE status IN ('queued', 'running');
CREATE INDEX diagnostics_created_idx ON diagnostics(created_at DESC, id DESC);
CREATE INDEX diagnostics_incident_idx ON diagnostics(incident_id, created_at DESC) WHERE incident_id IS NOT NULL;

CREATE TABLE diagnostic_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    diagnostic_id uuid NOT NULL REFERENCES diagnostics(id) ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence > 0),
    question text NOT NULL,
    result jsonb,
    response_sha256 text,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'failed', 'complete', 'cancelled')),
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (diagnostic_id, sequence)
);

CREATE TABLE operations_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    log_retention_days integer NOT NULL DEFAULT 30 CHECK (log_retention_days BETWEEN 1 AND 365),
    diagnostic_retention_days integer NOT NULL DEFAULT 90 CHECK (diagnostic_retention_days BETWEEN 1 AND 730),
    filesystem_warning_percent numeric(5,2) NOT NULL DEFAULT 80 CHECK (filesystem_warning_percent BETWEEN 1 AND 99),
    filesystem_critical_percent numeric(5,2) NOT NULL DEFAULT 90 CHECK (filesystem_critical_percent BETWEEN 1 AND 100),
    recovery_hysteresis_percent numeric(5,2) NOT NULL DEFAULT 5 CHECK (recovery_hysteresis_percent BETWEEN 1 AND 25),
    database_budget_bytes bigint NOT NULL DEFAULT 10737418240 CHECK (database_budget_bytes > 0),
    queue_warning_count integer NOT NULL DEFAULT 20 CHECK (queue_warning_count > 0),
    queue_warning_age_seconds integer NOT NULL DEFAULT 900 CHECK (queue_warning_age_seconds > 0),
    notification_cooldown_seconds integer NOT NULL DEFAULT 3600 CHECK (notification_cooldown_seconds >= 60),
    auto_diagnostic_incident_kinds text[] NOT NULL DEFAULT '{}',
    auto_diagnostic_provider_id uuid REFERENCES llm_providers(id) ON DELETE SET NULL,
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO operations_settings(singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE capacity_alert_state (
    fingerprint text PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('normal', 'warning', 'critical')),
    current_value numeric NOT NULL,
    last_notified_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE backup_policy (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled boolean NOT NULL DEFAULT false,
    recipient text,
    schedule text NOT NULL DEFAULT '30 3 * * *',
    retention_count integer NOT NULL DEFAULT 7 CHECK (retention_count BETWEEN 1 AND 100),
    updated_by uuid REFERENCES users(id) ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (recipient IS NULL OR recipient ~ '^age1[0-9a-z]+$')
);
INSERT INTO backup_policy(singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE maintenance_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    read_only boolean NOT NULL DEFAULT false,
    reason text,
    owner text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO maintenance_state(singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE backup_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'deferred', 'failed', 'complete', 'verified')),
    bundle_path text,
    bundle_sha256 text,
    manifest jsonb,
    size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
    app_version text NOT NULL,
    error_code text,
    error_message text,
    requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
    started_at timestamptz,
    finished_at timestamptz,
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX backup_runs_created_idx ON backup_runs(created_at DESC, id DESC);

-- Failures are aggregated transactionally with the durable workflow event.
-- Planned pauses, manual gates, duplicate results, and upload confirmations are
-- deliberately absent from this trigger.
CREATE OR REPLACE FUNCTION aggregate_failed_job_incident()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    incident_fingerprint text;
    incident_title text;
    attempt_trace_id uuid;
BEGIN
    IF NEW.event_type <> 'step.failed' THEN
        RETURN NEW;
    END IF;
    incident_fingerprint := encode(digest(convert_to(
        'step_failed:' || NEW.job_id::text || ':' || COALESCE(NEW.job_step_id::text, '') || ':' ||
        COALESCE(NEW.payload #>> '{blockers,0,code}', 'step_execution_failed'), 'UTF8'), 'sha256'), 'hex');
    incident_title := COALESCE(NEW.payload #>> '{blockers,0,message}', '任务步骤执行失败');
    SELECT trace_id INTO attempt_trace_id FROM step_attempts WHERE id = NEW.attempt_id;
    INSERT INTO incidents(severity, kind, fingerprint, title, summary, job_id, trace_id, evidence)
    VALUES ('critical', 'step_failed', incident_fingerprint, incident_title, incident_title,
            NEW.job_id, attempt_trace_id, jsonb_build_object('job_event_id', NEW.id, 'attempt_id', NEW.attempt_id,
                                            'error_code', COALESCE(NEW.payload #>> '{blockers,0,code}', 'step_execution_failed')))
    ON CONFLICT (fingerprint) DO UPDATE SET
        status = 'open', occurrence_count = incidents.occurrence_count + 1,
        last_occurred_at = NEW.created_at, job_id = NEW.job_id, trace_id = attempt_trace_id,
        evidence = EXCLUDED.evidence, updated_at = now(), resolved_by = NULL, resolved_at = NULL;
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_events_aggregate_failed_incident
AFTER INSERT ON job_events
FOR EACH ROW EXECUTE FUNCTION aggregate_failed_job_incident();

-- Repeated operational blockers become actionable only after a second
-- occurrence inside the bounded window. Human gates and remote-write
-- reconciliation states remain deliberately excluded.
CREATE OR REPLACE FUNCTION aggregate_repeated_blocker_incident()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    blocker_code text;
    blocker_message text;
    occurrence_total bigint;
    incident_fingerprint text;
    attempt_trace_id uuid;
BEGIN
    IF NEW.event_type <> 'step.blocked' THEN
        RETURN NEW;
    END IF;
    blocker_code := COALESCE(NEW.payload #>> '{blockers,0,code}', '');
    blocker_message := COALESCE(NEW.payload #>> '{blockers,0,message}', blocker_code);
    IF blocker_code = '' OR blocker_code ~* '(manual|obligation|accept_rules|confirm_upload|duplicate|wait|pause|reconciliation|outcome_unknown)' THEN
        RETURN NEW;
    END IF;
    SELECT count(*) INTO occurrence_total
      FROM job_events
     WHERE job_id = NEW.job_id AND event_type = 'step.blocked'
       AND payload #>> '{blockers,0,code}' = blocker_code
       AND created_at >= NEW.created_at - interval '30 minutes';
    IF occurrence_total < 2 THEN
        RETURN NEW;
    END IF;
    incident_fingerprint := encode(digest(convert_to(
        'repeated_blocker:' || NEW.job_id::text || ':' || blocker_code, 'UTF8'), 'sha256'), 'hex');
    SELECT trace_id INTO attempt_trace_id FROM step_attempts WHERE id = NEW.attempt_id;
    INSERT INTO incidents(severity,kind,fingerprint,title,summary,job_id,trace_id,evidence)
    VALUES ('warning','repeated_blocker',incident_fingerprint,'任务阻塞重复发生',blocker_message,
            NEW.job_id,attempt_trace_id,jsonb_build_object('job_event_id',NEW.id,'attempt_id',NEW.attempt_id,'error_code',blocker_code))
    ON CONFLICT(fingerprint) DO UPDATE SET status='open',occurrence_count=incidents.occurrence_count+1,
        last_occurred_at=NEW.created_at,trace_id=attempt_trace_id,evidence=EXCLUDED.evidence,updated_at=now(),resolved_by=NULL,resolved_at=NULL;
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_events_aggregate_repeated_blocker_incident
AFTER INSERT ON job_events
FOR EACH ROW EXECUTE FUNCTION aggregate_repeated_blocker_incident();

-- Health probes are already audited by every integration manager. A trigger
-- observes two consecutive failures without coupling those packages to the
-- operations package or initiating any probe itself.
CREATE OR REPLACE FUNCTION aggregate_integration_health_incident()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    previous_status text;
    incident_fingerprint text;
BEGIN
    IF right(NEW.action, 7) <> '.health' OR COALESCE(NEW.payload->>'status','') <> 'failed' THEN
        RETURN NEW;
    END IF;
    SELECT payload->>'status' INTO previous_status
      FROM audit_events
     WHERE id <> NEW.id AND action = NEW.action AND resource_type = NEW.resource_type
       AND resource_id IS NOT DISTINCT FROM NEW.resource_id
     ORDER BY created_at DESC,id DESC LIMIT 1;
    IF previous_status IS DISTINCT FROM 'failed' THEN
        RETURN NEW;
    END IF;
    incident_fingerprint := encode(digest(convert_to(
        'integration_health:' || NEW.resource_type || ':' || COALESCE(NEW.resource_id,''), 'UTF8'), 'sha256'), 'hex');
    INSERT INTO incidents(severity,kind,fingerprint,title,summary,trace_id,evidence)
    VALUES ('warning','integration_health',incident_fingerprint,'集成连续健康探测失败',
            NEW.resource_type || ' 连续两次健康探测失败',NEW.trace_id,
            jsonb_build_object('audit_event_id',NEW.id,'resource_type',NEW.resource_type,'resource_id',NEW.resource_id))
    ON CONFLICT(fingerprint) DO UPDATE SET status='open',occurrence_count=incidents.occurrence_count+1,
        last_occurred_at=NEW.created_at,trace_id=NEW.trace_id,evidence=EXCLUDED.evidence,
        updated_at=now(),resolved_by=NULL,resolved_at=NULL;
    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_events_aggregate_integration_health_incident
AFTER INSERT ON audit_events
FOR EACH ROW EXECUTE FUNCTION aggregate_integration_health_incident();
