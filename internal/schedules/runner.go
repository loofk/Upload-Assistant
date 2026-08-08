package schedules

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type RuntimeStore interface {
	EnqueueDue(context.Context, time.Time, int) (int, error)
	ClaimRun(context.Context, string, time.Time, time.Duration) (Run, error)
	CompleteRun(context.Context, string, string, string) error
	FailRun(context.Context, string, string, error, time.Time, time.Duration) error
	PublishTerminalNotifications(context.Context, int) (int, error)
}

type JobCreator interface {
	CreateJob(context.Context, workflow.CreateJobInput) (workflow.Job, error)
}

type Runner struct {
	store  RuntimeStore
	jobs   JobCreator
	owner  string
	logger *slog.Logger
	poll   time.Duration
	lease  time.Duration
	now    func() time.Time
}

func NewRunner(store RuntimeStore, jobs JobCreator, owner string, logger *slog.Logger) *Runner {
	if logger == nil {
		logger = slog.Default()
	}
	return &Runner{
		store: store, jobs: jobs, owner: strings.TrimSpace(owner), logger: logger,
		poll: 30 * time.Second, lease: 2 * time.Minute, now: time.Now,
	}
}

func (runner *Runner) Run(ctx context.Context) {
	runner.tick(ctx)
	ticker := time.NewTicker(runner.poll)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			runner.tick(ctx)
		}
	}
}

func (runner *Runner) tick(ctx context.Context) {
	if err := runner.RunOnce(ctx); err != nil && !errors.Is(err, context.Canceled) {
		runner.logger.Error("daily candidate scheduler tick failed", "error", err)
	}
}

func (runner *Runner) RunOnce(ctx context.Context) error {
	if runner.store == nil || runner.jobs == nil || runner.owner == "" {
		return fmt.Errorf("daily candidate scheduler dependencies are incomplete")
	}
	now := runner.now().UTC()
	if _, err := runner.store.EnqueueDue(ctx, now, 25); err != nil {
		return err
	}
	for processed := 0; processed < 25; processed++ {
		run, err := runner.store.ClaimRun(ctx, runner.owner, now, runner.lease)
		if errors.Is(err, ErrNoRun) {
			break
		}
		if err != nil {
			return err
		}
		if err := runner.createJob(ctx, run, now); err != nil {
			runner.logger.Warn("scheduled daily candidate job will retry", "schedule_id", run.ScheduleID, "schedule_run_id", run.ID, "attempt", run.Attempts)
			retry := time.Duration(max(1, run.Attempts)) * time.Minute
			if failErr := runner.store.FailRun(ctx, run.ID, runner.owner, err, now, retry); failErr != nil {
				return fmt.Errorf("create scheduled job: %v; record retry: %w", err, failErr)
			}
			continue
		}
	}
	_, err := runner.store.PublishTerminalNotifications(ctx, 50)
	return err
}

func (runner *Runner) createJob(ctx context.Context, run Run, now time.Time) error {
	date, err := RecommendationDate(run.Timezone, now)
	if err != nil {
		return err
	}
	input, _ := json.Marshal(map[string]any{
		"schedule_id": run.ScheduleID, "source": run.Config.Source, "target": run.Config.Target,
		"target_count": run.Config.TargetCount, "scan_limit": run.Config.ScanLimit,
		"page": run.Config.Page, "date": date,
		"notification_channels": run.Config.NotificationChannels,
	})
	job, err := runner.jobs.CreateJob(ctx, workflow.CreateJobInput{
		Kind: "daily_candidates", ExecutionMode: workflow.ExecutionAuto, Input: input,
		IdempotencyKey: "daily-schedule:" + run.ScheduleID + ":" + run.ScheduledFor.UTC().Format(time.RFC3339Nano),
		Owner:          "schedule:" + run.ScheduleID,
		Actor:          workflow.Actor{Type: "scheduler", ID: run.ScheduleID},
	})
	if err != nil {
		return err
	}
	if err := runner.store.CompleteRun(ctx, run.ID, runner.owner, job.ID); err != nil {
		return fmt.Errorf("link scheduled job: %w", err)
	}
	return nil
}
