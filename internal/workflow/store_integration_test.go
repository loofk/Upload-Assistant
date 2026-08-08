package workflow

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
)

func TestStoreLifecycleAndAuditChain(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatalf("database.Open() error = %v", err)
	}
	t.Cleanup(pool.Close)
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatalf("database.Migrate() error = %v", err)
	}

	store := NewStore(pool)
	definition := RetorrentDefinition()
	workflowID, err := store.EnsureDefinition(ctx, definition)
	if err != nil {
		t.Fatalf("EnsureDefinition() error = %v", err)
	}
	idempotencyKey := "integration-" + uuid.NewString()
	input := CreateJobInput{
		Kind:           "retorrent",
		ExecutionMode:  ExecutionStep,
		Input:          json.RawMessage(`{"source_url":"https://example.invalid/details.php?id=1","target":"MTEAM"}`),
		IdempotencyKey: idempotencyKey,
		Owner:          "integration-test",
		Actor:          Actor{Type: "test", ID: "store-lifecycle"},
	}
	job, err := store.CreateJob(ctx, workflowID, definition, input)
	if err != nil {
		t.Fatalf("CreateJob() error = %v", err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), "DELETE FROM jobs WHERE id = $1", job.ID)
	})
	if job.Status != JobQueued || job.CurrentStep != "source_parse" {
		t.Fatalf("created job status/current = %s/%s", job.Status, job.CurrentStep)
	}

	replayed, err := store.CreateJob(ctx, workflowID, definition, input)
	if err != nil {
		t.Fatalf("idempotent CreateJob() error = %v", err)
	}
	if replayed.ID != job.ID {
		t.Fatalf("idempotent job ID = %s, want %s", replayed.ID, job.ID)
	}
	conflicting := input
	conflicting.Input = json.RawMessage(`{"source_url":"https://example.invalid/details.php?id=2","target":"MTEAM"}`)
	if _, err := store.CreateJob(ctx, workflowID, definition, conflicting); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflicting idempotency error = %v, want ErrConflict", err)
	}

	steps, err := store.ListSteps(ctx, job.ID)
	if err != nil {
		t.Fatalf("ListSteps() error = %v", err)
	}
	if len(steps) != len(definition.Steps) || steps[0].Status != StepReady {
		t.Fatalf("unexpected steps: count=%d first=%s", len(steps), steps[0].Status)
	}

	job, err = store.PauseJob(ctx, job.ID, Actor{Type: "test", ID: "store-lifecycle"})
	if err != nil || job.Status != JobPaused {
		t.Fatalf("PauseJob() job/error = %s/%v", job.Status, err)
	}
	job, err = store.ResumeJob(ctx, job.ID, json.RawMessage(`{"reason":"integration-test"}`), Actor{Type: "test", ID: "store-lifecycle"})
	if err != nil || job.Status != JobQueued {
		t.Fatalf("ResumeJob() job/error = %s/%v", job.Status, err)
	}
	claimed, err := store.ClaimNextJob(ctx, "integration-worker", time.Minute, Actor{Type: "worker", ID: "integration-worker"})
	if err != nil {
		t.Fatalf("ClaimNextJob() error = %v", err)
	}
	if claimed.ID != job.ID || claimed.Status != JobRunning {
		t.Fatalf("claimed job = %s/%s, want %s/running", claimed.ID, claimed.Status, job.ID)
	}
	if err := store.Heartbeat(ctx, job.ID, "integration-worker", time.Minute); err != nil {
		t.Fatalf("Heartbeat() error = %v", err)
	}
	step, attempt, err := store.StartCurrentStep(ctx, job.ID, "integration-worker", Actor{Type: "worker", ID: "integration-worker"})
	if err != nil {
		t.Fatalf("StartCurrentStep() error = %v", err)
	}
	if step.Key != "source_parse" || attempt.Number != 1 {
		t.Fatalf("started step/attempt = %s/%d", step.Key, attempt.Number)
	}
	var firstSnapshot struct {
		JobInput      map[string]any             `json:"job_input"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(step.InputSnapshot, &firstSnapshot); err != nil {
		t.Fatalf("decode first input snapshot: %v", err)
	}
	if firstSnapshot.JobInput["target"] != "MTEAM" || len(firstSnapshot.PreviousSteps) != 0 {
		t.Fatalf("first input snapshot = %#v", firstSnapshot)
	}
	artifact, err := store.RegisterArtifact(ctx, RegisterArtifactInput{
		JobID: job.ID, StepID: step.ID, AttemptID: attempt.ID, Kind: "source_reference",
		StoragePath: "jobs/source-reference.json", Filename: "source-reference.json",
		MIMEType: "application/json", SizeBytes: 42, SHA256: "6f5bd96f4f9f80f6765028e0162833fd2829a5775d25f64688befe7bc8c67f74",
		Metadata: json.RawMessage(`{"test":true}`), Retention: 30 * 24 * time.Hour,
		Actor: Actor{Type: "worker", ID: "integration-worker"},
	})
	if err != nil || artifact.ID == "" {
		t.Fatalf("RegisterArtifact() artifact/error = %s/%v", artifact.ID, err)
	}
	artifacts, err := store.ListArtifacts(ctx, job.ID)
	if err != nil || len(artifacts) != 1 {
		t.Fatalf("ListArtifacts() count/error = %d/%v", len(artifacts), err)
	}
	job, err = store.CompleteStep(
		ctx, job.ID, "integration-worker", attempt.ID,
		json.RawMessage(`{"tracker":"U2","torrent_id":"60635"}`),
		Actor{Type: "worker", ID: "integration-worker"},
	)
	if err != nil || job.Status != JobPaused || job.CurrentStep != "source_inspect" {
		t.Fatalf("CompleteStep() job/error = %s/%s/%v", job.Status, job.CurrentStep, err)
	}
	job, err = store.ResumeJob(ctx, job.ID, json.RawMessage(`{}`), Actor{Type: "test", ID: "store-lifecycle"})
	if err != nil || job.Status != JobQueued {
		t.Fatalf("second ResumeJob() job/error = %s/%v", job.Status, err)
	}
	claimed, err = store.ClaimNextJob(ctx, "integration-worker", time.Minute, Actor{Type: "worker", ID: "integration-worker"})
	if err != nil || claimed.ID != job.ID {
		t.Fatalf("second ClaimNextJob() job/error = %s/%v", claimed.ID, err)
	}
	step, attempt, err = store.StartCurrentStep(ctx, job.ID, "integration-worker", Actor{Type: "worker", ID: "integration-worker"})
	if err != nil || step.Key != "source_inspect" {
		t.Fatalf("second StartCurrentStep() step/error = %s/%v", step.Key, err)
	}
	var secondSnapshot struct {
		ResumeState   map[string]any             `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(step.InputSnapshot, &secondSnapshot); err != nil {
		t.Fatalf("decode second input snapshot: %v", err)
	}
	if _, exists := secondSnapshot.PreviousSteps["source_parse"]; !exists || secondSnapshot.ResumeState["reason"] != "integration-test" {
		t.Fatalf("second input snapshot = %#v", secondSnapshot)
	}
	job, err = store.BlockStep(
		ctx, job.ID, "integration-worker", attempt.ID,
		json.RawMessage(`[{"code":"credential_required"}]`),
		json.RawMessage(`[{"action":"configure_site"}]`),
		json.RawMessage(`{"site":"U2"}`),
		Actor{Type: "worker", ID: "integration-worker"},
	)
	if err != nil || job.Status != JobBlocked {
		t.Fatalf("BlockStep() job/error = %s/%v", job.Status, err)
	}
	job, err = store.CancelJob(ctx, job.ID, Actor{Type: "test", ID: "store-lifecycle"})
	if err != nil || job.Status != JobCancelled {
		t.Fatalf("CancelJob() job/error = %s/%v", job.Status, err)
	}

	events, err := store.ListEvents(ctx, job.ID, 0, 100)
	if err != nil {
		t.Fatalf("ListEvents() error = %v", err)
	}
	if len(events) < 10 {
		t.Fatalf("event count = %d, want at least 10", len(events))
	}
	if err := VerifyEventChain(events); err != nil {
		t.Fatalf("VerifyEventChain() error = %v", err)
	}
}
