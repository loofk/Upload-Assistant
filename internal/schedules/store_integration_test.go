package schedules

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestStorePersistsScheduleRunAndInAppNotification(t *testing.T) {
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

	store := NewStore(pool)
	name := "candidate-schedule-" + uuid.NewString()
	channelName := "discord-" + uuid.NewString()[:8]
	var channelID string
	if err := pool.QueryRow(ctx, `INSERT INTO notification_channels(name, adapter, enabled, config) VALUES ($1, 'discord_webhook', true, '{"timeout_seconds":15}') RETURNING id::text`, channelName).Scan(&channelID); err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Second)
	schedule, err := store.Create(ctx, CreateInput{
		Name: name, CronExpression: "30 8 * * *", Timezone: "Asia/Shanghai", Enabled: true,
		Config: DailyCandidateConfig{Source: "U2", Target: "MTEAM", TargetCount: 10, ScanLimit: 30, Page: 1, NotificationChannels: []string{channelName}},
	}, now)
	if err != nil {
		t.Fatal(err)
	}
	var jobID string
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), "DELETE FROM schedules WHERE id = $1", schedule.ID)
		_, _ = pool.Exec(context.Background(), "DELETE FROM notification_channels WHERE id = $1", channelID)
		if jobID != "" {
			_, _ = pool.Exec(context.Background(), "DELETE FROM jobs WHERE id = $1", jobID)
		}
	})
	if schedule.NextRunAt == nil || !schedule.NextRunAt.After(now) || schedule.Config.TargetCount != 10 {
		t.Fatalf("schedule = %#v", schedule)
	}
	if _, err := pool.Exec(ctx, "UPDATE schedules SET next_run_at = $2 WHERE id = $1", schedule.ID, now.Add(-time.Minute)); err != nil {
		t.Fatal(err)
	}
	if count, err := store.EnqueueDue(ctx, now, 10); err != nil || count != 1 {
		t.Fatalf("EnqueueDue() = %d/%v", count, err)
	}
	run, err := store.ClaimRun(ctx, "integration-scheduler", now, 2*time.Minute)
	if err != nil || run.ScheduleID != schedule.ID || run.Status != RunRunning || run.Attempts != 1 {
		t.Fatalf("ClaimRun() = %#v/%v", run, err)
	}
	runs, err := store.ListRuns(ctx, schedule.ID, 10)
	if err != nil || len(runs) != 1 || runs[0].ID != run.ID || runs[0].LeaseExpiresAt == nil {
		t.Fatalf("ListRuns() = %#v/%v", runs, err)
	}

	workflowStore := workflow.NewStore(pool)
	definition := workflow.DailyCandidatesDefinition()
	workflowID, err := workflowStore.EnsureDefinition(ctx, definition)
	if err != nil {
		t.Fatal(err)
	}
	service := workflow.NewService(workflowStore, definition, workflowID)
	job, err := service.CreateJob(ctx, workflow.CreateJobInput{
		Kind: "daily_candidates", ExecutionMode: workflow.ExecutionAuto,
		Input: json.RawMessage(`{"source":"U2","target":"MTEAM"}`), IdempotencyKey: "schedule-store-" + uuid.NewString(), Owner: "integration",
		Actor: workflow.Actor{Type: "test", ID: "schedule-store"},
	})
	if err != nil {
		t.Fatal(err)
	}
	jobID = job.ID
	if err := store.CompleteRun(ctx, run.ID, "integration-scheduler", job.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE jobs SET status = 'blocked', summary = '{"ok":false,"target_met":false}' WHERE id = $1`, job.ID); err != nil {
		t.Fatal(err)
	}
	if count, err := store.PublishTerminalNotifications(ctx, 10); err != nil || count != 2 {
		t.Fatalf("PublishTerminalNotifications() = %d/%v", count, err)
	}
	if count, err := store.PublishTerminalNotifications(ctx, 10); err != nil || count != 0 {
		t.Fatalf("idempotent PublishTerminalNotifications() = %d/%v", count, err)
	}
	if _, err := pool.Exec(ctx, `
		UPDATE jobs SET status = 'complete', summary = '{"ok":true,"target_met":true}',
		       updated_at = now() + interval '1 second' WHERE id = $1`, job.ID); err != nil {
		t.Fatal(err)
	}
	if count, err := store.PublishTerminalNotifications(ctx, 10); err != nil || count != 2 {
		t.Fatalf("updated PublishTerminalNotifications() = %d/%v", count, err)
	}
	notifications, err := store.ListNotifications(ctx, 100)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	foundExternal := false
	for _, notification := range notifications {
		if notification.ScheduleRunID == run.ID {
			var payload struct {
				JobStatus string `json:"job_status"`
			}
			_ = json.Unmarshal(notification.Payload, &payload)
			if notification.Channel == "in_app" {
				found = notification.Status == "sent" && notification.JobID == job.ID && notification.Attempts == 2 && payload.JobStatus == "complete"
			} else if notification.Channel == channelName {
				foundExternal = notification.Status == "queued" && notification.NotificationChannelID == channelID && notification.Attempts == 0 && len(notification.PayloadSHA256) == 64 && payload.JobStatus == "complete"
			}
		}
	}
	if !found {
		t.Fatalf("notification for run %s was not published: %#v", run.ID, notifications)
	}
	if !foundExternal {
		t.Fatalf("external notification for run %s was not queued: %#v", run.ID, notifications)
	}
	disabled := false
	updated, err := store.Update(ctx, schedule.ID, UpdateInput{Enabled: &disabled}, now)
	if err != nil || updated.Enabled || updated.NextRunAt != nil {
		t.Fatalf("disabled schedule/error = %#v/%v", updated, err)
	}
}
