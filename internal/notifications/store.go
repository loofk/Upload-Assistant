package notifications

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
)

type RuntimeProvider interface {
	GetRuntimeNotificationChannel(context.Context, string) (integrations.RuntimeNotificationChannel, error)
}

type Store struct {
	pool     *pgxpool.Pool
	runtimes RuntimeProvider
}

func NewStore(pool *pgxpool.Pool, runtimes RuntimeProvider) *Store {
	return &Store{pool: pool, runtimes: runtimes}
}

func (s *Store) GetRuntimeNotificationChannel(ctx context.Context, name string) (integrations.RuntimeNotificationChannel, error) {
	return s.runtimes.GetRuntimeNotificationChannel(ctx, name)
}

func (s *Store) Claim(ctx context.Context, owner string, now time.Time, lease time.Duration) (Delivery, error) {
	if owner == "" || lease <= 0 {
		return Delivery{}, fmt.Errorf("notification delivery owner and positive lease are required")
	}
	var delivery Delivery
	err := s.pool.QueryRow(ctx, `
		WITH picked AS (
			SELECT notification.id
			FROM notifications notification
			JOIN notification_channels channel ON channel.id = notification.notification_channel_id
			WHERE channel.enabled AND notification.attempts < 8
			  AND ((notification.status IN ('queued', 'failed') AND notification.scheduled_at <= $1)
			       OR (notification.status = 'sending' AND notification.lease_expires_at <= $1))
			ORDER BY notification.scheduled_at, notification.id
			FOR UPDATE OF notification SKIP LOCKED LIMIT 1
		)
		UPDATE notifications notification
		SET status = 'sending', attempts = attempts + 1, lease_owner = $2,
		    lease_expires_at = $1 + make_interval(secs => $3), updated_at = now()
		FROM picked, notification_channels channel
		WHERE notification.id = picked.id AND channel.id = notification.notification_channel_id
		RETURNING notification.id::text, channel.name, notification.payload, notification.attempts`,
		now, owner, lease.Seconds()).Scan(&delivery.ID, &delivery.ChannelName, &delivery.Payload, &delivery.Attempts)
	if errors.Is(err, pgx.ErrNoRows) {
		return Delivery{}, ErrNoDelivery
	}
	if err != nil {
		return Delivery{}, fmt.Errorf("claim notification delivery: %w", err)
	}
	return delivery, nil
}

func (s *Store) Complete(ctx context.Context, id, owner string, receipt map[string]any) error {
	body, err := json.Marshal(receipt)
	if err != nil {
		return fmt.Errorf("serialize notification receipt: %w", err)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin notification completion: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var channelID, channelName string
	err = tx.QueryRow(ctx, `
		UPDATE notifications notification
		SET status = 'sent', sent_at = now(), remote_receipt = $3, last_error = NULL,
		    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
		FROM notification_channels channel
		WHERE notification.id = $1 AND notification.status = 'sending' AND notification.lease_owner = $2
		  AND channel.id = notification.notification_channel_id
		RETURNING channel.id::text, channel.name`, id, owner, body).Scan(&channelID, &channelName)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("notification delivery lease conflict")
	}
	if err != nil {
		return fmt.Errorf("complete notification delivery: %w", err)
	}
	if _, err := tx.Exec(ctx, `UPDATE notification_channels SET health_status = 'ready', last_health_check_at = now(), updated_at = now() WHERE id = $1`, channelID); err != nil {
		return fmt.Errorf("update notification channel health: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ('worker', $1, 'notification.delivered', 'notification', $2, $3)`, owner, id, body); err != nil {
		return fmt.Errorf("audit notification delivery: %w", err)
	}
	_ = channelName
	return tx.Commit(ctx)
}

func (s *Store) Fail(ctx context.Context, id, owner string, now time.Time, retryAfter time.Duration) error {
	if retryAfter < time.Minute {
		retryAfter = time.Minute
	}
	if retryAfter > time.Hour {
		retryAfter = time.Hour
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin notification failure: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var channelID string
	err = tx.QueryRow(ctx, `
		UPDATE notifications notification
		SET status = 'failed', scheduled_at = $3, last_error = 'notification delivery failed',
		    lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
		FROM notification_channels channel
		WHERE notification.id = $1 AND notification.status = 'sending' AND notification.lease_owner = $2
		  AND channel.id = notification.notification_channel_id
		RETURNING channel.id::text`, id, owner, now.Add(retryAfter)).Scan(&channelID)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("notification delivery lease conflict")
	}
	if err != nil {
		return fmt.Errorf("fail notification delivery: %w", err)
	}
	if _, err := tx.Exec(ctx, `UPDATE notification_channels SET health_status = 'failed', last_health_check_at = now(), updated_at = now() WHERE id = $1`, channelID); err != nil {
		return fmt.Errorf("update failed notification channel health: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ('worker', $1, 'notification.delivery_failed', 'notification', $2,
		        jsonb_build_object('error_code', 'delivery_failed', 'retry_at', $3::timestamptz))`, owner, id, now.Add(retryAfter)); err != nil {
		return fmt.Errorf("audit notification failure: %w", err)
	}
	return tx.Commit(ctx)
}
