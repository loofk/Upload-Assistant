package workflow

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
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
		Input:          json.RawMessage(`{"source_url":"https://example.invalid/details.php?id=1","target":"MTEAM","accept_rules":{"U2":{"accepted":true,"fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","obligations":{"manual":{"confirmed":true,"evidence":"old"}}}},"confirm_upload":true}`),
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
	if _, err := store.ReplayJob(ctx, job.ID, workflowID, definition, ReplayJobInput{
		IdempotencyKey: "unsafe-active-" + uuid.NewString(), Owner: "integration-test",
	}); !errors.Is(err, ErrReplayUnsafe) {
		t.Fatalf("active ReplayJob() error = %v, want ErrReplayUnsafe", err)
	}
	page, err := store.ListJobs(ctx, ListJobsFilter{Status: JobQueued, Kind: "retorrent", Limit: 100})
	if err != nil {
		t.Fatalf("ListJobs() error = %v", err)
	}
	foundJob := false
	for _, listed := range page.Jobs {
		if listed.ID == job.ID {
			foundJob = true
			break
		}
	}
	if !foundJob {
		t.Fatalf("ListJobs() did not include newly created job %s", job.ID)
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
	loadedArtifact, err := store.GetArtifact(ctx, job.ID, artifact.ID)
	if err != nil || loadedArtifact.ID != artifact.ID || loadedArtifact.SHA256 != artifact.SHA256 {
		t.Fatalf("GetArtifact() artifact/error = %#v/%v", loadedArtifact, err)
	}
	if _, err := store.GetArtifact(ctx, "00000000-0000-4000-8000-000000000000", artifact.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross-job GetArtifact() error = %v, want ErrNotFound", err)
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
		json.RawMessage(`[{"code":"target_upload_outcome_unknown"}]`),
		json.RawMessage(`[{"action":"reconcile_target_upload"}]`),
		json.RawMessage(`{"target_upload":{"outcome":"unreconciled"}}`),
		Actor{Type: "worker", ID: "integration-worker"},
	)
	if err != nil || job.Status != JobBlocked {
		t.Fatalf("BlockStep() job/error = %s/%v", job.Status, err)
	}
	attemptPage, err := store.ListAttempts(ctx, job.ID, ListAttemptsFilter{Limit: 100})
	if err != nil || len(attemptPage.Attempts) != 2 || attemptPage.Attempts[1].ErrorCode != "target_upload_outcome_unknown" {
		t.Fatalf("blocked attempts/error = %#v/%v", attemptPage, err)
	}
	if _, err := store.ResumeJob(ctx, job.ID, json.RawMessage(`{}`), Actor{Type: "test", ID: "store-lifecycle"}); !errors.Is(err, ErrReconciliation) {
		t.Fatalf("unsafe ResumeJob() error = %v, want ErrReconciliation", err)
	}
	reconciliation := fmt.Sprintf(`{"confirm_upload":true,"reconciliation":{"blocker_code":"target_upload_outcome_unknown","attempt_id":%q,"decision":"verified_not_applied","confirmed":true,"evidence_sha256":"%s","observed_at":"2026-08-08T00:00:00Z"}}`, attempt.ID, strings.Repeat("a", 64))
	job, err = store.ResumeJob(ctx, job.ID, json.RawMessage(reconciliation), Actor{Type: "test", ID: "store-lifecycle"})
	if err != nil || job.Status != JobQueued {
		t.Fatalf("reconciled ResumeJob() job/error = %s/%v", job.Status, err)
	}
	job, err = store.CancelJob(ctx, job.ID, Actor{Type: "test", ID: "store-lifecycle"})
	if err != nil || job.Status != JobCancelled {
		t.Fatalf("CancelJob() job/error = %s/%v", job.Status, err)
	}
	replayKey := "safe-replay-" + uuid.NewString()
	replay, err := store.ReplayJob(ctx, job.ID, workflowID, definition, ReplayJobInput{
		ExecutionMode: ExecutionStep, IdempotencyKey: replayKey, Owner: "integration-test",
		Actor: Actor{Type: "test", ID: "store-lifecycle"},
	})
	if err != nil || replay.Status != JobQueued || replay.ReplayOfJobID != job.ID || replay.ExecutionMode != ExecutionStep {
		t.Fatalf("ReplayJob() replay/error = %#v/%v", replay, err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM jobs WHERE id = $1", replay.ID) })
	var replayInput map[string]any
	if err := json.Unmarshal(replay.Input, &replayInput); err != nil {
		t.Fatal(err)
	}
	if _, inherited := replayInput["accept_rules"]; inherited || replayInput["confirm_upload"] != false {
		t.Fatalf("replay inherited authorization input = %#v", replayInput)
	}
	idempotentReplay, err := store.ReplayJob(ctx, job.ID, workflowID, definition, ReplayJobInput{
		ExecutionMode: ExecutionStep, IdempotencyKey: replayKey, Owner: "integration-test",
		Actor: Actor{Type: "test", ID: "store-lifecycle"},
	})
	if err != nil || idempotentReplay.ID != replay.ID {
		t.Fatalf("idempotent ReplayJob() replay/error = %#v/%v", idempotentReplay, err)
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
	replayEvents := 0
	reconciliationEvents := 0
	for _, event := range events {
		if event.Type == "job.replayed" {
			replayEvents++
		}
		if event.Type == "job.reconciliation_acknowledged" {
			reconciliationEvents++
			if bytes.Contains(event.Payload, []byte(`"confirmed"`)) || !bytes.Contains(event.Payload, []byte(`"evidence_sha256"`)) {
				t.Fatalf("unsafe reconciliation event payload = %s", event.Payload)
			}
		}
	}
	if replayEvents != 1 || reconciliationEvents != 1 {
		t.Fatalf("job replay/reconciliation event counts = %d/%d, want 1/1", replayEvents, reconciliationEvents)
	}
	childEvents, err := store.ListEvents(ctx, replay.ID, 0, 100)
	if err != nil || len(childEvents) != 1 || VerifyEventChain(childEvents) != nil {
		t.Fatalf("replay child events/error = %#v/%v", childEvents, err)
	}
	var childPayload struct {
		ReplayOfJobID string   `json:"replay_of_job_id"`
		SafetyResets  []string `json:"safety_resets"`
	}
	if err := json.Unmarshal(childEvents[0].Payload, &childPayload); err != nil ||
		childPayload.ReplayOfJobID != job.ID || len(childPayload.SafetyResets) != 3 {
		t.Fatalf("replay child payload/error = %#v/%v", childPayload, err)
	}
}

func TestReplayRejectsCompletedTargetUpload(t *testing.T) {
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
	definition := RetorrentDefinition()
	workflowID, err := store.EnsureDefinition(ctx, definition)
	if err != nil {
		t.Fatal(err)
	}
	job, err := store.CreateJob(ctx, workflowID, definition, CreateJobInput{
		Kind: "retorrent", ExecutionMode: ExecutionStep,
		Input:          json.RawMessage(`{"source_url":"https://u2.dmhy.org/details.php?id=2","target":"MTEAM"}`),
		IdempotencyKey: "completed-upload-replay-" + uuid.NewString(), Owner: "integration-test",
		Actor: Actor{Type: "test", ID: "completed-upload-replay"},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM jobs WHERE id = $1", job.ID) })
	if _, err := pool.Exec(ctx, "UPDATE job_steps SET status = 'complete' WHERE job_id = $1 AND step_key = 'target_upload'", job.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `UPDATE jobs SET status = 'failed', blockers = '[{"code":"target_torrent_download_failed"}]' WHERE id = $1`, job.ID); err != nil {
		t.Fatal(err)
	}
	_, err = store.ReplayJob(ctx, job.ID, workflowID, definition, ReplayJobInput{
		ExecutionMode: ExecutionStep, IdempotencyKey: "reject-completed-upload-" + uuid.NewString(),
		Owner: "integration-test", Actor: Actor{Type: "test", ID: "completed-upload-replay"},
	})
	if !errors.Is(err, ErrReplayUnsafe) || !strings.Contains(err.Error(), "completed target upload") {
		t.Fatalf("completed target upload replay error = %v", err)
	}
}

func TestStorePausesRunningAttemptAndRecoversExpiredLease(t *testing.T) {
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
	definition := RetorrentDefinition()
	workflowID, err := store.EnsureDefinition(ctx, definition)
	if err != nil {
		t.Fatal(err)
	}
	job, err := store.CreateJob(ctx, workflowID, definition, CreateJobInput{
		Kind: "retorrent", ExecutionMode: ExecutionAuto,
		Input:          json.RawMessage(`{"source_url":"https://u2.dmhy.org/details.php?id=1","target":"MTEAM"}`),
		IdempotencyKey: "pause-recovery-" + uuid.NewString(), Owner: "integration-test",
		Actor: Actor{Type: "test", ID: "pause-recovery"},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM jobs WHERE id = $1", job.ID) })

	actor := Actor{Type: "worker", ID: "worker-one"}
	if _, err := store.ClaimNextJob(ctx, actor.ID, time.Minute, actor); err != nil {
		t.Fatal(err)
	}
	_, firstAttempt, err := store.StartCurrentStep(ctx, job.ID, actor.ID, actor)
	if err != nil {
		t.Fatal(err)
	}
	job, err = store.PauseJob(ctx, job.ID, Actor{Type: "test", ID: "pause-recovery"})
	if err != nil || job.Status != JobPaused {
		t.Fatalf("PauseJob() status/error = %s/%v", job.Status, err)
	}
	var stepStatus, attemptStatus StepStatus
	if err := pool.QueryRow(ctx, `
		SELECT js.status, sa.status FROM job_steps js JOIN step_attempts sa ON sa.job_step_id = js.id
		WHERE sa.id = $1`, firstAttempt.ID).Scan(&stepStatus, &attemptStatus); err != nil {
		t.Fatal(err)
	}
	if stepStatus != StepPaused || attemptStatus != StepPaused {
		t.Fatalf("paused step/attempt = %s/%s", stepStatus, attemptStatus)
	}

	if _, err := store.ResumeJob(ctx, job.ID, json.RawMessage(`{}`), Actor{Type: "test", ID: "pause-recovery"}); err != nil {
		t.Fatal(err)
	}
	actor = Actor{Type: "worker", ID: "worker-two"}
	if _, err := store.ClaimNextJob(ctx, actor.ID, time.Minute, actor); err != nil {
		t.Fatal(err)
	}
	_, secondAttempt, err := store.StartCurrentStep(ctx, job.ID, actor.ID, actor)
	if err != nil || secondAttempt.Number != 2 {
		t.Fatalf("second attempt/error = %d/%v", secondAttempt.Number, err)
	}
	if _, err := pool.Exec(ctx, "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = $1", job.ID); err != nil {
		t.Fatal(err)
	}
	recoveryActor := Actor{Type: "worker", ID: "worker-three"}
	recovered, err := store.ClaimNextJob(ctx, recoveryActor.ID, time.Minute, recoveryActor)
	if err != nil || recovered.ID != job.ID {
		t.Fatalf("recovered job/error = %s/%v", recovered.ID, err)
	}
	var errorCode string
	if err := pool.QueryRow(ctx, "SELECT status, COALESCE(error_code, '') FROM step_attempts WHERE id = $1", secondAttempt.ID).Scan(&attemptStatus, &errorCode); err != nil {
		t.Fatal(err)
	}
	if attemptStatus != StepFailed || errorCode != "worker_lease_expired" {
		t.Fatalf("expired attempt status/code = %s/%s", attemptStatus, errorCode)
	}
	_, thirdAttempt, err := store.StartCurrentStep(ctx, job.ID, recoveryActor.ID, recoveryActor)
	if err != nil || thirdAttempt.Number != 3 {
		t.Fatalf("third attempt/error = %d/%v", thirdAttempt.Number, err)
	}
	if _, err := store.CancelJob(ctx, job.ID, Actor{Type: "test", ID: "pause-recovery"}); err != nil {
		t.Fatal(err)
	}
	firstPage, err := store.ListAttempts(ctx, job.ID, ListAttemptsFilter{Limit: 2})
	if err != nil || len(firstPage.Attempts) != 2 || !firstPage.HasMore {
		t.Fatalf("first attempt page/error = %#v/%v", firstPage, err)
	}
	if firstPage.Attempts[0].Number != 1 || firstPage.Attempts[0].Status != StepPaused ||
		firstPage.Attempts[1].Number != 2 || firstPage.Attempts[1].Status != StepFailed ||
		firstPage.Attempts[1].ErrorCode != "worker_lease_expired" {
		t.Fatalf("first attempt page values = %#v", firstPage.Attempts)
	}
	secondPage, err := store.ListAttempts(ctx, job.ID, ListAttemptsFilter{
		Limit: 2, AfterPosition: firstPage.Attempts[1].StepPosition, AfterNumber: firstPage.Attempts[1].Number,
	})
	if err != nil || len(secondPage.Attempts) != 1 || secondPage.HasMore ||
		secondPage.Attempts[0].Number != 3 || secondPage.Attempts[0].Status != StepCancelled {
		t.Fatalf("second attempt page/error = %#v/%v", secondPage, err)
	}
	events, err := store.ListEvents(ctx, job.ID, 0, 200)
	if err != nil || VerifyEventChain(events) != nil {
		t.Fatalf("event chain/error = %d/%v", len(events), err)
	}
	seenPaused, seenRecovered := false, false
	for _, event := range events {
		seenPaused = seenPaused || event.Type == "step.paused"
		seenRecovered = seenRecovered || event.Type == "step.lease_recovered"
	}
	if !seenPaused || !seenRecovered {
		t.Fatalf("pause/recovery events = %t/%t", seenPaused, seenRecovered)
	}
}

func TestSafeReplayInputAndReconciliationBlockers(t *testing.T) {
	input, err := safeReplayInput("retorrent", json.RawMessage(`{"source_url":"https://u2.dmhy.org/details.php?id=1","accept_rules":{"U2":{"accepted":true}},"confirm_upload":true,"downloader":{"name":"box"}}`))
	if err != nil || string(input) != `{"confirm_upload":false,"downloader":{"name":"box"},"source_url":"https://u2.dmhy.org/details.php?id=1"}` {
		t.Fatalf("safeReplayInput() input/error = %s/%v", input, err)
	}
	for _, code := range []string{"target_upload_outcome_unknown", "downloader_partial_add_requires_reconciliation"} {
		blocker, err := unsafeReplayBlocker(json.RawMessage(`[{"code":"` + code + `"}]`))
		if err != nil || blocker != code {
			t.Fatalf("unsafeReplayBlocker(%s) = %q/%v", code, blocker, err)
		}
	}
}

func TestResumeReconciliationRequiresAttemptBoundEvidence(t *testing.T) {
	now := time.Date(2026, 8, 8, 12, 0, 0, 0, time.UTC)
	attemptID := "55555555-5555-4555-8555-555555555555"
	blockers := json.RawMessage(`[{"code":"target_upload_outcome_unknown"}]`)
	templated, err := addReconciliationTemplate(
		blockers, json.RawMessage(`[{"action":"reconcile_target_upload"}]`),
		json.RawMessage(`{"target_upload":{"submitted_torrent_sha256":"`+strings.Repeat("c", 64)+`"}}`), attemptID,
	)
	if err != nil || !bytes.Contains(templated, []byte(`"required_submitted_torrent_sha256":"`+strings.Repeat("c", 64)+`"`)) {
		t.Fatalf("target reconciliation template/error = %s/%v", templated, err)
	}
	current := json.RawMessage(`{"reconciliation":{"blocker_code":"target_upload_outcome_unknown","attempt_id":"` + attemptID + `","decision":"unreconciled","confirmed":false}}`)
	if _, err := validateResumeReconciliation(blockers, current, json.RawMessage(`{}`), attemptID, now); !errors.Is(err, ErrReconciliation) {
		t.Fatalf("missing reconciliation error = %v", err)
	}
	valid := json.RawMessage(`{"reconciliation":{"blocker_code":"target_upload_outcome_unknown","attempt_id":"` + attemptID + `","decision":"verified_not_applied","confirmed":true,"evidence_sha256":"` + strings.Repeat("a", 64) + `","observed_at":"2026-08-08T11:59:00Z"}}`)
	audit, err := validateResumeReconciliation(blockers, current, valid, attemptID, now)
	if err != nil || audit["decision"] != "verified_not_applied" || len(audit["reconciliation_sha256"].(string)) != 64 {
		t.Fatalf("valid reconciliation audit/error = %#v/%v", audit, err)
	}
	submittedSHA := strings.Repeat("c", 64)
	uploadedCurrent := json.RawMessage(`{"reconciliation":{"blocker_code":"target_upload_outcome_unknown","attempt_id":"` + attemptID + `","required_submitted_torrent_sha256":"` + submittedSHA + `"}}`)
	uploaded := json.RawMessage(`{"reconciliation":{"blocker_code":"target_upload_outcome_unknown","attempt_id":"` + attemptID + `","decision":"verified_uploaded","confirmed":true,"evidence_sha256":"` + strings.Repeat("d", 64) + `","observed_at":"2026-08-08T11:59:00Z","observed_torrent_id":"98765","submitted_torrent_sha256":"` + submittedSHA + `"}}`)
	uploadedAudit, err := validateResumeReconciliation(blockers, uploadedCurrent, uploaded, attemptID, now)
	if err != nil || uploadedAudit["decision"] != "verified_uploaded" || uploadedAudit["observed_torrent_id"] != "98765" {
		t.Fatalf("verified uploaded audit/error = %#v/%v", uploadedAudit, err)
	}
	if !activeVerifiedUploadRecovery(uploaded) {
		t.Fatal("verified_uploaded resume state must remain replay-protected")
	}
	if _, err := validateResumeReconciliation(
		json.RawMessage(`[{"code":"target_reconciliation_candidate_not_found"}]`), uploaded, json.RawMessage(`{}`), attemptID, now,
	); err != nil {
		t.Fatalf("unchanged active recovery resume error = %v", err)
	}
	downgraded := bytes.Replace(uploaded, []byte(`"verified_uploaded"`), []byte(`"verified_not_applied"`), 1)
	if _, err := validateResumeReconciliation(
		json.RawMessage(`[{"code":"target_reconciliation_candidate_not_found"}]`), uploaded,
		downgraded, attemptID, now,
	); !errors.Is(err, ErrReconciliation) {
		t.Fatalf("downgraded active recovery error = %v", err)
	}
	wrongUpload := bytes.Replace(uploaded, []byte(submittedSHA), []byte(strings.Repeat("e", 64)), 1)
	if _, err := validateResumeReconciliation(blockers, uploadedCurrent, wrongUpload, attemptID, now); !errors.Is(err, ErrReconciliation) {
		t.Fatalf("wrong submitted torrent reconciliation error = %v", err)
	}
	downloaderBlockers := json.RawMessage(`[{"code":"downloader_partial_add_requires_reconciliation"}]`)
	downloaderCurrent := json.RawMessage(`{"reconciliation":{"blocker_code":"downloader_partial_add_requires_reconciliation","attempt_id":"` + attemptID + `","required_observed_hash":"0123456789abcdef0123456789abcdef01234567"}}`)
	downloaderValid := json.RawMessage(`{"reconciliation":{"blocker_code":"downloader_partial_add_requires_reconciliation","attempt_id":"` + attemptID + `","decision":"verified_remote_state","confirmed":true,"evidence_sha256":"` + strings.Repeat("b", 64) + `","observed_at":"2026-08-08T11:59:00Z","observed_hash":"0123456789abcdef0123456789abcdef01234567"}}`)
	if _, err := validateResumeReconciliation(downloaderBlockers, downloaderCurrent, downloaderValid, attemptID, now); err != nil {
		t.Fatalf("downloader reconciliation error = %v", err)
	}
}
