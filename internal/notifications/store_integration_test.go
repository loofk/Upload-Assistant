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
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeRuntimeProvider struct{}

func (fakeRuntimeProvider) GetRuntimeNotificationChannel(context.Context, string) (integrations.RuntimeNotificationChannel, error) {
	return integrations.RuntimeNotificationChannel{}, nil
}

type probeRuntimeProvider struct {
	runtime integrations.RuntimeNotificationChannel
}

func (provider probeRuntimeProvider) GetRuntimeNotificationChannel(context.Context, string) (integrations.RuntimeNotificationChannel, error) {
	return provider.runtime, nil
}

func TestSystemEventOutboxOnlyEnqueuesExplicitSubscriptions(t *testing.T) {
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
	suffix := uuid.NewString()[:8]
	subscribedName := "events-" + suffix
	scheduleOnlyName := "schedule-only-" + suffix
	var subscribedID, scheduleOnlyID string
	if err := pool.QueryRow(ctx, `
		INSERT INTO notification_channels(name, adapter, enabled, config)
		VALUES ($1, 'telegram_bot', true, '{"timeout_seconds":15,"event_types":["job.created"]}')
		RETURNING id::text`, subscribedName).Scan(&subscribedID); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `
		INSERT INTO notification_channels(name, adapter, enabled, config)
		VALUES ($1, 'discord_webhook', true, '{"timeout_seconds":15}')
		RETURNING id::text`, scheduleOnlyName).Scan(&scheduleOnlyID); err != nil {
		t.Fatal(err)
	}
	var jobID string
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM notifications WHERE notification_channel_id = ANY($1::uuid[])`, []string{subscribedID, scheduleOnlyID})
		if jobID != "" {
			_, _ = pool.Exec(context.Background(), `DELETE FROM jobs WHERE id = $1`, jobID)
		}
		_, _ = pool.Exec(context.Background(), `DELETE FROM notification_channels WHERE id = ANY($1::uuid[])`, []string{subscribedID, scheduleOnlyID})
	})

	workflowStore := workflow.NewStore(pool)
	definition := workflow.RetorrentDefinition()
	workflowID, err := workflowStore.EnsureDefinition(ctx, definition)
	if err != nil {
		t.Fatal(err)
	}
	job, err := workflowStore.CreateJob(ctx, workflowID, definition, workflow.CreateJobInput{
		Kind: "retorrent", ExecutionMode: workflow.ExecutionStep,
		Input:          json.RawMessage(`{"source_url":"https://example.invalid/details.php?id=1","target":"MTEAM","confirm_upload":false}`),
		IdempotencyKey: "system-event-" + uuid.NewString(), Owner: "integration-test",
		Actor: workflow.Actor{Type: "test", ID: "system-event-outbox"},
	})
	if err != nil {
		t.Fatal(err)
	}
	jobID = job.ID

	var count int
	var eventKey, eventType, payloadJobID, payloadSHA string
	var hashMatches bool
	if err := pool.QueryRow(ctx, `
		SELECT count(*), min(event_key), min(payload->>'event_type'), min(payload->>'job_id'),
		       min(payload_sha256), bool_and(payload_sha256 = encode(digest(convert_to(payload::text, 'UTF8'), 'sha256'), 'hex'))
		FROM notifications WHERE notification_channel_id = $1`, subscribedID).
		Scan(&count, &eventKey, &eventType, &payloadJobID, &payloadSHA, &hashMatches); err != nil {
		t.Fatal(err)
	}
	if count != 1 || !strings.HasPrefix(eventKey, "job-event:") || eventType != "job.created" ||
		payloadJobID != job.ID || len(payloadSHA) != 64 || !hashMatches {
		t.Fatalf("subscribed outbox = count=%d key=%q type=%q job=%q sha=%q valid=%t", count, eventKey, eventType, payloadJobID, payloadSHA, hashMatches)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM notifications WHERE notification_channel_id = $1`, scheduleOnlyID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("schedule-only channel received %d unsolicited system event(s)", count)
	}
}

func TestNotificationProbePersistsSingleAttemptIntent(t *testing.T) {
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
	name := "probe-" + uuid.NewString()[:8]
	var channelID string
	if err := pool.QueryRow(ctx, `
		INSERT INTO notification_channels(name, adapter, enabled, config)
		VALUES ($1, 'discord_webhook', true, '{"timeout_seconds":15,"event_types":[]}')
		RETURNING id::text`, name).Scan(&channelID); err != nil {
		t.Fatal(err)
	}
	var notificationID string
	t.Cleanup(func() {
		if notificationID != "" {
			_, _ = pool.Exec(context.Background(), `DELETE FROM notifications WHERE id = $1`, notificationID)
		}
		_, _ = pool.Exec(context.Background(), `DELETE FROM notification_channels WHERE id = $1`, channelID)
	})
	store := NewStore(pool, probeRuntimeProvider{runtime: integrations.RuntimeNotificationChannel{
		NotificationChannel: integrations.NotificationChannel{Name: name, Adapter: "discord_webhook", Enabled: true},
		ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 15},
		Credentials:         map[string]string{"webhook_url": "https://example.invalid/api/webhooks/fixture"},
	}})
	probe, err := store.EnqueueProbe(ctx, name, workflow.Actor{Type: "test", ID: "probe"}, time.Now().UTC())
	if err != nil {
		t.Fatal(err)
	}
	notificationID = probe.NotificationID
	var status, eventKey string
	var attempts int
	if err := pool.QueryRow(ctx, `SELECT status, attempts, event_key FROM notifications WHERE id = $1`, notificationID).Scan(&status, &attempts, &eventKey); err != nil {
		t.Fatal(err)
	}
	if status != "queued" || attempts != 7 || !strings.HasPrefix(eventKey, "configuration-probe:") {
		t.Fatalf("queued probe = status=%q attempts=%d event=%q", status, attempts, eventKey)
	}
	delivery, err := store.ClaimProbe(ctx, notificationID, "probe-worker", time.Now().UTC(), time.Minute)
	if err != nil || delivery.Attempts != 8 {
		t.Fatalf("ClaimProbe() = %#v, %v", delivery, err)
	}
	if err := store.FailProbe(ctx, notificationID, "probe-worker", time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	var health string
	if err := pool.QueryRow(ctx, `SELECT notification.status, channel.health_status FROM notifications notification JOIN notification_channels channel ON channel.id = notification.notification_channel_id WHERE notification.id = $1`, notificationID).Scan(&status, &health); err != nil {
		t.Fatal(err)
	}
	if status != "failed" || health != "failed" {
		t.Fatalf("terminal probe = status=%q health=%q", status, health)
	}
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
