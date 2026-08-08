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
	RegisterArtifact(context.Context, workflow.RegisterArtifactInput) (workflow.Artifact, error)
}

type Execution struct {
	Job     workflow.Job
	Step    workflow.Step
	Attempt workflow.Attempt
	Actor   workflow.Actor
}

type Executor interface {
	Execute(context.Context, Execution) (json.RawMessage, error)
}

type Blocker struct {
	Code         string `json:"code"`
	Message      string `json:"message"`
	SiteCode     string `json:"site_code,omitempty"`
	ObligationID string `json:"obligation_id,omitempty"`
}

type NextAction struct {
	Action      string         `json:"action"`
	Description string         `json:"description,omitempty"`
	Parameters  map[string]any `json:"parameters,omitempty"`
}

type BlockError struct {
	Code        string
	Message     string
	Blockers    []Blocker
	NextActions []NextAction
	ResumeState map[string]any
}

func (e *BlockError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	if len(e.Blockers) > 0 {
		return e.Blockers[0].Message
	}
	return "workflow step is blocked"
}

type Runner struct {
	runtime   Runtime
	executors map[string]Executor
	owner     string
	lease     time.Duration
	poll      time.Duration
	logger    *slog.Logger
}

type Option func(*Runner)

func WithRuleProvider(provider RuleProvider) Option {
	return func(runner *Runner) {
		runner.executors["source_rules"] = ruleGateExecutor{provider: provider, role: "source"}
		runner.executors["target_rules"] = ruleGateExecutor{provider: provider, role: "target"}
	}
}

func New(runtime Runtime, owner string, logger *slog.Logger, options ...Option) *Runner {
	runner := &Runner{
		runtime: runtime, owner: owner, logger: logger,
		lease: 30 * time.Second, poll: time.Second,
		executors: map[string]Executor{
			"source_parse": sourceParseExecutor{},
		},
	}
	for _, option := range options {
		option(runner)
	}
	return runner
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
	output, executeErr := executor.Execute(executionCtx, Execution{Job: job, Step: step, Attempt: attempt, Actor: actor})
	cancel()
	<-heartbeatDone
	if executeErr == nil {
		_, err = r.runtime.CompleteStep(ctx, job.ID, r.owner, attempt.ID, output, actor)
		return err
	}
	var blocked *BlockError
	if errors.As(executeErr, &blocked) {
		blockers := blocked.Blockers
		if len(blockers) == 0 {
			blockers = []Blocker{{Code: blocked.Code, Message: blocked.Message}}
		}
		nextActions := blocked.NextActions
		if nextActions == nil {
			nextActions = []NextAction{}
		}
		resumeState := blocked.ResumeState
		if resumeState == nil {
			resumeState = map[string]any{}
		}
		_, err = r.runtime.BlockStep(
			ctx, job.ID, r.owner, attempt.ID,
			mustJSON(blockers), mustJSON(nextActions), mustJSON(resumeState), actor,
		)
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
		return nil, &BlockError{
			Code: "invalid_job_input", Message: "job input is not valid JSON",
			NextActions: []NextAction{{Action: "replace_job_input"}},
		}
	}
	reference, err := sites.ParseSourceReference(input.SourceURL)
	if err != nil {
		return nil, &BlockError{
			Code: "invalid_source_url", Message: err.Error(), NextActions: []NextAction{{Action: "provide_supported_source_url"}},
			ResumeState: map[string]any{"required": []string{"source_url"}},
		}
	}
	target := stringsUpperTrim(input.Target)
	if target == "" {
		return nil, &BlockError{
			Code: "target_required", Message: "target site is required", NextActions: []NextAction{{Action: "provide_target_site"}},
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
