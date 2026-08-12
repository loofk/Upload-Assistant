package notifications

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type ProbeResult struct {
	NotificationID string `json:"notification_id"`
	ChannelName    string `json:"channel_name"`
	Status         string `json:"status"`
}

type probeStore interface {
	EnqueueProbe(context.Context, string, workflow.Actor, time.Time) (ProbeResult, error)
	ClaimProbe(context.Context, string, string, time.Time, time.Duration) (Delivery, error)
	Complete(context.Context, string, string, map[string]any) error
	MarkOutcomeUnknown(context.Context, string, string, map[string]any) error
	FailProbe(context.Context, string, string, time.Time) error
}

type Prober struct {
	store      probeStore
	dispatcher *Dispatcher
	owner      string
	now        func() time.Time
}

func NewProber(store probeStore, dispatcher *Dispatcher, owner string) *Prober {
	return &Prober{store: store, dispatcher: dispatcher, owner: strings.TrimSpace(owner), now: time.Now}
}

func (prober *Prober) Probe(ctx context.Context, name string, actor workflow.Actor) (ProbeResult, error) {
	if prober.store == nil || prober.dispatcher == nil || prober.owner == "" {
		return ProbeResult{}, fmt.Errorf("notification probe dependencies are incomplete")
	}
	now := prober.now().UTC()
	result, err := prober.store.EnqueueProbe(ctx, name, actor, now)
	if err != nil {
		return ProbeResult{}, err
	}
	delivery, err := prober.store.ClaimProbe(ctx, result.NotificationID, prober.owner, now, prober.dispatcher.lease)
	if err != nil {
		return result, err
	}
	receipt, deliveryErr := prober.dispatcher.deliver(ctx, delivery)
	if deliveryErr == nil {
		if err := prober.store.Complete(ctx, delivery.ID, prober.owner, receipt); err == nil {
			result.Status = "sent"
			return result, nil
		} else if retryErr := prober.store.Complete(ctx, delivery.ID, prober.owner, receipt); retryErr == nil {
			result.Status = "sent"
			return result, nil
		} else if unknownErr := prober.store.MarkOutcomeUnknown(ctx, delivery.ID, prober.owner, receipt); unknownErr != nil {
			return result, fmt.Errorf("persist test notification receipt: %v; preserve unknown outcome: %w", retryErr, unknownErr)
		}
		result.Status = "outcome_unknown"
		return result, fmt.Errorf("%w: test notification was accepted remotely but its receipt could not be persisted", ErrDeliveryOutcomeUnknown)
	}
	if errors.Is(deliveryErr, ErrDeliveryOutcomeUnknown) {
		if err := prober.store.MarkOutcomeUnknown(ctx, delivery.ID, prober.owner, receipt); err != nil {
			return result, err
		}
		result.Status = "outcome_unknown"
		return result, deliveryErr
	}
	if err := prober.store.FailProbe(ctx, delivery.ID, prober.owner, now); err != nil {
		return result, err
	}
	result.Status = "failed"
	return result, deliveryErr
}

func (s *Store) EnqueueProbe(ctx context.Context, name string, actor workflow.Actor, now time.Time) (ProbeResult, error) {
	runtime, err := s.GetRuntimeNotificationChannel(ctx, strings.TrimSpace(name))
	if err != nil {
		return ProbeResult{}, err
	}
	payload, err := json.Marshal(map[string]any{
		"event_type": "configuration.test", "title": "Upload Assistant 测试通知",
		"message":     "通知渠道连接测试成功。此消息仅由配置页的明确测试操作触发。",
		"occurred_at": now.UTC().Format(time.RFC3339),
	})
	if err != nil {
		return ProbeResult{}, fmt.Errorf("encode notification probe payload: %w", err)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return ProbeResult{}, fmt.Errorf("begin notification probe: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var channelID string
	if err := tx.QueryRow(ctx, `SELECT id::text FROM notification_channels WHERE name = $1 AND enabled FOR UPDATE`, runtime.Name).Scan(&channelID); errors.Is(err, pgx.ErrNoRows) {
		return ProbeResult{}, fmt.Errorf("notification channel is disabled or missing")
	} else if err != nil {
		return ProbeResult{}, fmt.Errorf("lock notification channel for probe: %w", err)
	}
	var result ProbeResult
	result.ChannelName = runtime.Name
	eventKey := "configuration-probe:" + uuid.NewString()
	if err := tx.QueryRow(ctx, `
		INSERT INTO notifications(notification_channel_id, channel, status, payload, payload_sha256, attempts, scheduled_at, event_key)
		VALUES ($1, $2, 'queued', $3, $4, 7, $5, $6)
		RETURNING id::text`, channelID, runtime.Name, payload, sha256Hex(payload), now, eventKey).Scan(&result.NotificationID); err != nil {
		return ProbeResult{}, fmt.Errorf("enqueue notification probe: %w", err)
	}
	result.Status = "queued"
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, NULLIF($2, ''), 'notification.probe_queued', 'notification', $3,
		        jsonb_build_object('channel_name', $4::text, 'payload_sha256', $5::text, 'automatic_retry', false))`,
		actor.Type, actor.ID, result.NotificationID, runtime.Name, sha256Hex(payload)); err != nil {
		return ProbeResult{}, fmt.Errorf("audit notification probe queue: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return ProbeResult{}, fmt.Errorf("commit notification probe: %w", err)
	}
	return result, nil
}

func (s *Store) ClaimProbe(ctx context.Context, id, owner string, now time.Time, lease time.Duration) (Delivery, error) {
	if strings.TrimSpace(id) == "" || strings.TrimSpace(owner) == "" || lease <= 0 {
		return Delivery{}, fmt.Errorf("notification probe id, owner and positive lease are required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Delivery{}, fmt.Errorf("begin notification probe claim: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var delivery Delivery
	err = tx.QueryRow(ctx, `
		UPDATE notifications notification
		SET status = 'sending', attempts = attempts + 1, lease_owner = $2,
		    lease_expires_at = $3::timestamptz + make_interval(secs => $4), updated_at = now()
		FROM notification_channels channel
		WHERE notification.id = $1 AND notification.status = 'queued' AND notification.attempts = 7
		  AND channel.id = notification.notification_channel_id AND channel.enabled
		RETURNING notification.id::text, channel.name, notification.payload, notification.attempts`,
		id, owner, now, lease.Seconds()).Scan(&delivery.ID, &delivery.ChannelName, &delivery.Payload, &delivery.Attempts)
	if errors.Is(err, pgx.ErrNoRows) {
		return Delivery{}, fmt.Errorf("notification probe is no longer claimable")
	}
	if err != nil {
		return Delivery{}, fmt.Errorf("claim notification probe: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ('worker', $1, 'notification.probe_delivery_started', 'notification', $2,
		        jsonb_build_object('attempt', $3::integer, 'lease_seconds', $4::double precision, 'automatic_retry', false))`,
		owner, delivery.ID, delivery.Attempts, lease.Seconds()); err != nil {
		return Delivery{}, fmt.Errorf("audit notification probe intent: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return Delivery{}, fmt.Errorf("commit notification probe claim: %w", err)
	}
	return delivery, nil
}

func (s *Store) FailProbe(ctx context.Context, id, owner string, now time.Time) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin notification probe failure: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var channelID string
	err = tx.QueryRow(ctx, `
		UPDATE notifications notification
		SET status = 'failed', last_error = 'notification test delivery failed', scheduled_at = $3,
		    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
		FROM notification_channels channel
		WHERE notification.id = $1 AND notification.status = 'sending' AND notification.lease_owner = $2
		  AND channel.id = notification.notification_channel_id
		RETURNING channel.id::text`, id, owner, now).Scan(&channelID)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("notification probe lease conflict")
	}
	if err != nil {
		return fmt.Errorf("fail notification probe: %w", err)
	}
	if _, err := tx.Exec(ctx, `UPDATE notification_channels SET health_status = 'failed', last_health_check_at = now(), updated_at = now() WHERE id = $1`, channelID); err != nil {
		return fmt.Errorf("update failed notification probe health: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ('worker', $1, 'notification.probe_failed', 'notification', $2,
		        jsonb_build_object('error_code', 'delivery_failed', 'automatic_retry', false))`, owner, id); err != nil {
		return fmt.Errorf("audit notification probe failure: %w", err)
	}
	return tx.Commit(ctx)
}
