CREATE TABLE notification_channels (
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

CREATE TABLE media_managers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    adapter text NOT NULL CHECK (adapter IN ('sonarr', 'radarr')),
    enabled boolean NOT NULL DEFAULT true,
    config jsonb NOT NULL DEFAULT '{}',
    secret_id uuid REFERENCES secrets(id) ON DELETE RESTRICT,
    health_status text NOT NULL DEFAULT 'unknown' CHECK (health_status IN ('unknown', 'ready', 'failed')),
    last_health_check_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

DROP INDEX notifications_schedule_run_id_key;

ALTER TABLE notifications
    ADD COLUMN notification_channel_id uuid REFERENCES notification_channels(id) ON DELETE RESTRICT,
    ADD COLUMN payload_sha256 text,
    ADD COLUMN remote_receipt jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN lease_owner text,
    ADD COLUMN lease_expires_at timestamptz,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

CREATE UNIQUE INDEX notifications_in_app_schedule_run_key
    ON notifications(schedule_run_id)
    WHERE schedule_run_id IS NOT NULL AND notification_channel_id IS NULL;

CREATE UNIQUE INDEX notifications_external_schedule_run_channel_key
    ON notifications(schedule_run_id, notification_channel_id)
    WHERE schedule_run_id IS NOT NULL AND notification_channel_id IS NOT NULL;

CREATE INDEX notifications_delivery_claim_idx
    ON notifications(status, scheduled_at, lease_expires_at)
    WHERE notification_channel_id IS NOT NULL;
