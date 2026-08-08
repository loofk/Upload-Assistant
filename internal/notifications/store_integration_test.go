package notifications

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/schedules"
)

type fakeRuntimeProvider struct{}

func (fakeRuntimeProvider) GetRuntimeNotificationChannel(context.Context, string) (integrations.RuntimeNotificationChannel, error) {
	return integrations.RuntimeNotificationChannel{}, nil
}

func TestStoreReconcilesExpiredLeaseBeforeAnyRetryAndPersistsReceipts(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	name := "discord-" + uuid.NewString()[:8]
	var channelID string
	if err := pool.QueryRow(ctx, `INSERT INTO notification_channels(name, adapter, enabled, config) VALUES ($1, 'discord_webhook', true, '{"timeout_seconds":15}') RETURNING id::text`, name).Scan(&channelID); err != nil {
		t.Fatal(err)
	}
	ids := []string{}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM audit_events WHERE resource_type = 'notification' AND resource_id = ANY($1)`, ids)
		_, _ = pool.Exec(context.Background(), `DELETE FROM notifications WHERE id = ANY($1)`, ids)
		_, _ = pool.Exec(context.Background(), `DELETE FROM notification_channels WHERE id = $1`, channelID)
	})
	insert := func() string {
		var id string
		if err := pool.QueryRow(ctx, `INSERT INTO notifications(notification_channel_id, channel, status, payload, payload_sha256) VALUES ($1, $2, 'queued', '{"schedule_name":"fixture"}', repeat('a',64)) RETURNING id::text`, channelID, name).Scan(&id); err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
		return id
	}
	store := NewStore(pool, fakeRuntimeProvider{})
	scheduleStore := schedules.NewStore(pool)
	// Use a bounded future claim instant so database/client clock skew cannot
	// make a freshly inserted default scheduled_at appear not-yet-due.
	now := time.Now().UTC().Add(time.Minute)
	firstID := insert()
	first, err := store.Claim(ctx, "worker-a", now, time.Minute)
	if err != nil || first.ID != firstID || first.Attempts != 1 {
		t.Fatalf("first Claim() = %#v/%v", first, err)
	}
	if _, err := store.Claim(ctx, "worker-b", now, time.Minute); !errors.Is(err, ErrNoDelivery) {
		t.Fatalf("concurrent Claim() error = %v", err)
	}
	if _, err := pool.Exec(ctx, `UPDATE notifications SET lease_expires_at = $2 WHERE id = $1`, firstID, now.Add(-time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Claim(ctx, "worker-b", now, time.Minute); !errors.Is(err, ErrNoDelivery) {
		t.Fatalf("expired Claim() error = %v", err)
	}
	if err := store.Complete(ctx, firstID, "worker-a", map[string]any{"message_id": "stale"}); err == nil {
		t.Fatal("stale lease owner completed delivery")
	}
	var status string
	if err := pool.QueryRow(ctx, `SELECT status FROM notifications WHERE id = $1`, firstID).Scan(&status); err != nil || status != "outcome_unknown" {
		t.Fatalf("expired delivery status = %s/%v", status, err)
	}
	reconciled, err := scheduleStore.ReconcileNotification(ctx, firstID, schedules.NotificationReconciliationInput{
		Decision: "verified_not_delivered", Confirmed: true, EvidenceSHA256: strings.Repeat("b", 64),
		ObservedAt: time.Now().UTC().Format(time.RFC3339), ActorID: "fixture-user",
	}, now)
	if err != nil || reconciled.Status != "queued" {
		t.Fatalf("not-delivered reconciliation = %#v/%v", reconciled, err)
	}
	recovered, err := store.Claim(ctx, "worker-b", now, time.Minute)
	if err != nil || recovered.ID != firstID || recovered.Attempts != 2 {
		t.Fatalf("explicitly retried Claim() = %#v/%v", recovered, err)
	}
	receipt := map[string]any{"message_id": "123", "response_sha256": "fixture"}
	if err := store.Complete(ctx, firstID, "worker-b", receipt); err != nil {
		t.Fatal(err)
	}
	if err := store.Complete(ctx, firstID, "worker-b", receipt); err != nil {
		t.Fatalf("same durable receipt was not idempotent: %v", err)
	}
	var remote json.RawMessage
	if err := pool.QueryRow(ctx, `SELECT status, remote_receipt FROM notifications WHERE id = $1`, firstID).Scan(&status, &remote); err != nil || status != "sent" || !json.Valid(remote) {
		t.Fatalf("completed status/receipt = %s/%s/%v", status, remote, err)
	}

	secondID := insert()
	if _, err := store.Claim(ctx, "worker-c", now, time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := store.Fail(ctx, secondID, "worker-c", now, 2*time.Minute); err != nil {
		t.Fatal(err)
	}
	var lastError string
	var scheduledAt time.Time
	if err := pool.QueryRow(ctx, `SELECT status, last_error, scheduled_at FROM notifications WHERE id = $1`, secondID).Scan(&status, &lastError, &scheduledAt); err != nil || status != "failed" || lastError != "notification delivery failed" || !scheduledAt.After(now) {
		t.Fatalf("failed status = %s/%s/%s/%v", status, lastError, scheduledAt, err)
	}

	thirdID := insert()
	if _, err := store.Claim(ctx, "worker-d", now, time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := store.MarkOutcomeUnknown(ctx, thirdID, "worker-d", map[string]any{"request_sha256": strings.Repeat("c", 64), "message_id": "456"}); err != nil {
		t.Fatal(err)
	}
	if _, err := scheduleStore.ReconcileNotification(ctx, thirdID, schedules.NotificationReconciliationInput{
		Decision: "verified_not_delivered", Confirmed: true, EvidenceSHA256: strings.Repeat("d", 64),
		ObservedAt: time.Now().UTC().Format(time.RFC3339), ActorID: "fixture-user",
	}, now); !errors.Is(err, schedules.ErrInvalid) {
		t.Fatalf("known Discord receipt accepted as not delivered: %v", err)
	}
	reconciled, err = scheduleStore.ReconcileNotification(ctx, thirdID, schedules.NotificationReconciliationInput{
		Decision: "verified_delivered", Confirmed: true, EvidenceSHA256: strings.Repeat("d", 64),
		ObservedAt: time.Now().UTC().Format(time.RFC3339), MessageID: "456", ActorID: "fixture-user",
	}, now)
	if err != nil || reconciled.Status != "sent" || reconciled.SentAt == nil {
		t.Fatalf("delivered reconciliation = %#v/%v", reconciled, err)
	}
}
