CREATE INDEX audit_events_created_id_idx
    ON audit_events(created_at DESC, id DESC);

CREATE INDEX audit_events_resource_created_idx
    ON audit_events(resource_type, resource_id, created_at DESC, id DESC);

CREATE INDEX audit_events_action_created_idx
    ON audit_events(action, created_at DESC, id DESC);
