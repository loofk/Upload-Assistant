-- pgcrypto is a trusted PostgreSQL extension and supplies digest(), used below
-- to bind each durable notification payload to its SHA-256 evidence.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE notifications
    ADD COLUMN event_key text;

CREATE UNIQUE INDEX notifications_channel_event_key
    ON notifications(notification_channel_id, event_key)
    WHERE notification_channel_id IS NOT NULL AND event_key IS NOT NULL;

CREATE OR REPLACE FUNCTION enqueue_job_event_notifications()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    notification_payload jsonb;
    event_title text;
    event_message text;
BEGIN
    IF NEW.event_type NOT IN (
        'job.created',
        'job.paused',
        'job.resumed',
        'job.cancelled',
        'job.completed',
        'step.blocked',
        'step.failed',
        'step.deferred',
        'target_package.revision_requested',
        'job.reconciliation_acknowledged'
    ) THEN
        RETURN NEW;
    END IF;

    event_title := CASE NEW.event_type
        WHEN 'job.created' THEN '新任务已创建'
        WHEN 'job.paused' THEN '任务已暂停'
        WHEN 'job.resumed' THEN '任务已继续'
        WHEN 'job.cancelled' THEN '任务已取消'
        WHEN 'job.completed' THEN '任务已完成'
        WHEN 'step.blocked' THEN '任务需要人工处理'
        WHEN 'step.failed' THEN '任务执行失败'
        WHEN 'step.deferred' THEN '任务因访问频率延后'
        WHEN 'target_package.revision_requested' THEN '发布内容已申请重新生成'
        WHEN 'job.reconciliation_acknowledged' THEN '远程结果已人工核对'
        ELSE '任务状态已变化'
    END;
    event_message := CASE NEW.event_type
        WHEN 'step.blocked' THEN COALESCE(NEW.payload #>> '{blockers,0,message}', '请打开任务查看最终问题和解决方案。')
        WHEN 'step.failed' THEN COALESCE(NEW.payload #>> '{blockers,0,message}', '请打开任务查看失败原因。')
        WHEN 'step.deferred' THEN COALESCE(NEW.payload ->> 'reason', '站点访问频率门禁要求稍后继续。')
        WHEN 'target_package.revision_requested' THEN '旧版本证据仍保留，新版本将从发布内容生成环节继续。'
        ELSE ''
    END;

    SELECT jsonb_build_object(
        'event_type', NEW.event_type,
        'title', event_title,
        'message', event_message,
        'job_id', job.id::text,
        'job_status', job.status,
        'current_step', COALESCE(job.current_step_key, ''),
        'occurred_at', to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
    )
    INTO notification_payload
    FROM jobs job
    WHERE job.id = NEW.job_id;

    INSERT INTO notifications(
        job_id, notification_channel_id, channel, status, payload,
        payload_sha256, attempts, scheduled_at, event_key
    )
    SELECT
        NEW.job_id, channel.id, channel.name, 'queued', notification_payload,
        encode(digest(convert_to(notification_payload::text, 'UTF8'), 'sha256'), 'hex'),
        0, now(), 'job-event:' || NEW.id::text
    FROM notification_channels channel
    WHERE channel.enabled
      -- System-event delivery is opt-in. Existing schedule-only channels that
      -- predate event_types must not start receiving notifications after an upgrade.
      AND jsonb_typeof(channel.config -> 'event_types') = 'array'
      AND (channel.config -> 'event_types') ? NEW.event_type
    ON CONFLICT (notification_channel_id, event_key)
        WHERE notification_channel_id IS NOT NULL AND event_key IS NOT NULL
        DO NOTHING;

    RETURN NEW;
END;
$$;

CREATE TRIGGER job_events_enqueue_notifications
AFTER INSERT ON job_events
FOR EACH ROW
EXECUTE FUNCTION enqueue_job_event_notifications();
