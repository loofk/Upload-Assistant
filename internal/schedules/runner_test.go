package schedules

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeRuntimeStore struct {
	runs          []Run
	completedJob  string
	failed        bool
	notifications int
}

func (store *fakeRuntimeStore) EnqueueDue(context.Context, time.Time, int) (int, error) {
	return len(store.runs), nil
}
func (store *fakeRuntimeStore) ClaimRun(context.Context, string, time.Time, time.Duration) (Run, error) {
	if len(store.runs) == 0 {
		return Run{}, ErrNoRun
	}
	run := store.runs[0]
	store.runs = store.runs[1:]
	return run, nil
}
func (store *fakeRuntimeStore) CompleteRun(_ context.Context, _, _, jobID string) error {
	store.completedJob = jobID
	return nil
}
func (store *fakeRuntimeStore) FailRun(context.Context, string, string, error, time.Time, time.Duration) error {
	store.failed = true
	return nil
}
func (store *fakeRuntimeStore) PublishTerminalNotifications(context.Context, int) (int, error) {
	store.notifications++
	return 1, nil
}

type fakeJobCreator struct {
	input workflow.CreateJobInput
	err   error
}

func (creator *fakeJobCreator) CreateJob(_ context.Context, input workflow.CreateJobInput) (workflow.Job, error) {
	creator.input = input
	return workflow.Job{ID: "77777777-7777-4777-8777-777777777777"}, creator.err
}

func TestRunnerCreatesIdempotentUnconfirmedDailyJob(t *testing.T) {
	now := time.Date(2026, 8, 8, 1, 0, 0, 0, time.UTC)
	store := &fakeRuntimeStore{runs: []Run{{
		ID: "run", ScheduleID: "66666666-6666-4666-8666-666666666666", ScheduledFor: now,
		Timezone: "Asia/Shanghai", Config: DailyCandidateConfig{Source: "U2", Target: "MTEAM", TargetCount: 10, ScanLimit: 30, Page: 1},
	}}}
	jobs := &fakeJobCreator{}
	runner := NewRunner(store, jobs, "fixture-scheduler", slog.New(slog.NewTextHandler(io.Discard, nil)))
	runner.now = func() time.Time { return now }
	if err := runner.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if jobs.input.Kind != "daily_candidates" || jobs.input.ExecutionMode != workflow.ExecutionAuto || jobs.input.IdempotencyKey == "" || store.completedJob == "" || store.notifications != 1 {
		t.Fatalf("job/store = %#v/%#v", jobs.input, store)
	}
	var input map[string]any
	if err := json.Unmarshal(jobs.input.Input, &input); err != nil || input["schedule_id"] != storeIDForTest() || input["date"] != "2026-08-08" {
		t.Fatalf("scheduled input/error = %#v/%v", input, err)
	}
	if _, exists := input["confirm_upload"]; exists {
		t.Fatalf("scheduler inferred live confirmation: %#v", input)
	}
}

func TestRunnerPersistsRetryOnCreateFailure(t *testing.T) {
	store := &fakeRuntimeStore{runs: []Run{{ID: "run", ScheduleID: "66666666-6666-4666-8666-666666666666", Timezone: "Asia/Shanghai", Config: DailyCandidateConfig{Source: "U2", Target: "MTEAM", TargetCount: 10, ScanLimit: 30, Page: 1}}}}
	runner := NewRunner(store, &fakeJobCreator{err: errors.New("fixture failure")}, "fixture-scheduler", slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := runner.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !store.failed || store.completedJob != "" {
		t.Fatalf("store = %#v", store)
	}
}

func storeIDForTest() string { return "66666666-6666-4666-8666-666666666666" }
