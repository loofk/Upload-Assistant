ALTER TABLE notifications
    DROP CONSTRAINT notifications_status_check;

ALTER TABLE notifications
    ADD CONSTRAINT notifications_status_check
    CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'outcome_unknown', 'cancelled'));

CREATE INDEX notifications_outcome_unknown_idx
    ON notifications(updated_at DESC, id)
    WHERE notification_channel_id IS NOT NULL AND status = 'outcome_unknown';
