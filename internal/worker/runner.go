package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type Runtime interface {
	ClaimNextJob(context.Context, string, time.Duration, workflow.Actor) (workflow.Job, error)
	Heartbeat(context.Context, string, string, time.Duration) error
	StartCurrentStep(context.Context, string, string, workflow.Actor) (workflow.Step, workflow.Attempt, error)
	CompleteStep(context.Context, string, string, string, json.RawMessage, workflow.Actor) (workflow.Job, error)
	BlockStep(context.Context, string, string, string, json.RawMessage, json.RawMessage, json.RawMessage, workflow.Actor) (workflow.Job, error)
	FailStep(context.Context, string, string, string, json.RawMessage, json.RawMessage, workflow.Actor) (workflow.Job, error)
}

type Execution struct {
	Job     workflow.Job
	Step    workflow.Step
	Attempt workflow.Attempt
}

type Executor interface {
	Execute(context.Context, Execution) (json.RawMessage, error)
}

type BlockError struct {
	Code        string
	Message     string
	NextActions []string
	ResumeState map[string]any
}

func (e *BlockError) Error() string { return e.Message }

type Runner struct {
	runtime   Runtime
	executors map[string]Executor
	owner     string
	lease     time.Duration
	poll      time.Duration
	logger    *slog.Logger
}

func New(runtime Runtime, owner string, logger *slog.Logger) *Runner {
	return &Runner{
		runtime: runtime, owner: owner, logger: logger,
		lease: 30 * time.Second, poll: time.Second,
		executors: map[string]Executor{
			"source_parse": sourceParseExecutor{},
		},
	}
}

func (r *Runner) Run(ctx context.Context) {
	ticker := time.NewTicker(r.poll)
	defer ticker.Stop()
	for {
		if err := r.RunOnce(ctx); err != nil && !errors.Is(err, workflow.ErrNotFound) && !errors.Is(err, context.Canceled) {
			r.logger.Error("worker iteration failed", "worker_id", r.owner, "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (r *Runner) RunOnce(ctx context.Context) error {
	actor := workflow.Actor{Type: "worker", ID: r.owner}
	job, err := r.runtime.ClaimNextJob(ctx, r.owner, r.lease, actor)
	if err != nil {
		return err
	}
	step, attempt, err := r.runtime.StartCurrentStep(ctx, job.ID, r.owner, actor)
	if err != nil {
		return err
	}
	executor, exists := r.executors[step.Key]
	if !exists {
		blockers := mustJSON([]map[string]string{{
			"code": "executor_not_implemented", "message": fmt.Sprintf("step %s does not have a Go executor yet", step.Key),
		}})
		nextActions := mustJSON([]map[string]string{{
			"action": "wait_for_adapter_migration", "description": "This step will become resumable when its Go adapter is registered.",
		}})
		_, blockErr := r.runtime.BlockStep(ctx, job.ID, r.owner, attempt.ID, blockers, nextActions, mustJSON(map[string]any{
			"step_key": step.Key,
		}), actor)
		return blockErr
	}

	executionCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	heartbeatDone := make(chan struct{})
	go r.heartbeat(executionCtx, cancel, job.ID, heartbeatDone)
	output, executeErr := executor.Execute(executionCtx, Execution{Job: job, Step: step, Attempt: attempt})
	cancel()
	<-heartbeatDone
	if executeErr == nil {
		_, err = r.runtime.CompleteStep(ctx, job.ID, r.owner, attempt.ID, output, actor)
		return err
	}
	var blocked *BlockError
	if errors.As(executeErr, &blocked) {
		blockers := mustJSON([]map[string]string{{"code": blocked.Code, "message": blocked.Message}})
		nextActions := make([]map[string]string, 0, len(blocked.NextActions))
		for _, action := range blocked.NextActions {
			nextActions = append(nextActions, map[string]string{"action": action})
		}
		_, err = r.runtime.BlockStep(ctx, job.ID, r.owner, attempt.ID, blockers, mustJSON(nextActions), mustJSON(blocked.ResumeState), actor)
		return err
	}
	blockers := mustJSON([]map[string]string{{"code": "step_execution_failed", "message": executeErr.Error()}})
	_, err = r.runtime.FailStep(ctx, job.ID, r.owner, attempt.ID, blockers, mustJSON([]map[string]string{{"action": "retry_step"}}), actor)
	return err
}

func (r *Runner) heartbeat(ctx context.Context, cancel context.CancelFunc, jobID string, done chan<- struct{}) {
	defer close(done)
	ticker := time.NewTicker(r.lease / 3)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := r.runtime.Heartbeat(ctx, jobID, r.owner, r.lease); err != nil {
				r.logger.Error("job heartbeat failed", "job_id", jobID, "worker_id", r.owner, "error", err)
				cancel()
				return
			}
		}
	}
}

type sourceParseExecutor struct{}

func (sourceParseExecutor) Execute(_ context.Context, execution Execution) (json.RawMessage, error) {
	var input struct {
		SourceURL string `json:"source_url"`
		Target    string `json:"target"`
	}
	if err := json.Unmarshal(execution.Job.Input, &input); err != nil {
		return nil, &BlockError{Code: "invalid_job_input", Message: "job input is not valid JSON", NextActions: []string{"replace_job_input"}}
	}
	reference, err := sites.ParseSourceReference(input.SourceURL)
	if err != nil {
		return nil, &BlockError{
			Code: "invalid_source_url", Message: err.Error(), NextActions: []string{"provide_supported_source_url"},
			ResumeState: map[string]any{"required": []string{"source_url"}},
		}
	}
	target := stringsUpperTrim(input.Target)
	if target == "" {
		return nil, &BlockError{
			Code: "target_required", Message: "target site is required", NextActions: []string{"provide_target_site"},
			ResumeState: map[string]any{"required": []string{"target"}},
		}
	}
	return mustJSON(map[string]any{"source": reference, "target": target}), nil
}

func stringsUpperTrim(value string) string {
	result := make([]rune, 0, len(value))
	for _, character := range value {
		if character == ' ' || character == '\t' || character == '\r' || character == '\n' {
			continue
		}
		if character >= 'a' && character <= 'z' {
			character -= 'a' - 'A'
		}
		result = append(result, character)
	}
	return string(result)
}

func mustJSON(value any) json.RawMessage {
	body, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return body
}
