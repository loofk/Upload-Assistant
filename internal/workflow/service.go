package workflow

import (
	"context"
	"encoding/json"
	"time"
)

type Service struct {
	store      *Store
	definition Definition
	workflowID string
}

func NewService(store *Store, definition Definition, workflowID string) *Service {
	return &Service{store: store, definition: definition, workflowID: workflowID}
}

func (s *Service) CreateJob(ctx context.Context, input CreateJobInput) (Job, error) {
	return s.store.CreateJob(ctx, s.workflowID, s.definition, input)
}

func (s *Service) GetJob(ctx context.Context, id string) (Job, error) {
	return s.store.GetJob(ctx, id)
}

func (s *Service) ListJobs(ctx context.Context, filter ListJobsFilter) (JobPage, error) {
	return s.store.ListJobs(ctx, filter)
}

func (s *Service) ListSteps(ctx context.Context, id string) ([]Step, error) {
	return s.store.ListSteps(ctx, id)
}

func (s *Service) ListEvents(ctx context.Context, id string, after int64, limit int) ([]Event, error) {
	return s.store.ListEvents(ctx, id, after, limit)
}

func (s *Service) RegisterArtifact(ctx context.Context, input RegisterArtifactInput) (Artifact, error) {
	return s.store.RegisterArtifact(ctx, input)
}

func (s *Service) ListArtifacts(ctx context.Context, id string) ([]Artifact, error) {
	return s.store.ListArtifacts(ctx, id)
}

func (s *Service) GetArtifact(ctx context.Context, jobID, artifactID string) (Artifact, error) {
	return s.store.GetArtifact(ctx, jobID, artifactID)
}

func (s *Service) PauseJob(ctx context.Context, id string, actor Actor) (Job, error) {
	return s.store.PauseJob(ctx, id, actor)
}

func (s *Service) ResumeJob(ctx context.Context, id string, resumeState json.RawMessage, actor Actor) (Job, error) {
	return s.store.ResumeJob(ctx, id, resumeState, actor)
}

func (s *Service) CancelJob(ctx context.Context, id string, actor Actor) (Job, error) {
	return s.store.CancelJob(ctx, id, actor)
}

func (s *Service) ClaimNextJob(ctx context.Context, owner string, lease time.Duration, actor Actor) (Job, error) {
	return s.store.ClaimNextJob(ctx, owner, lease, actor)
}

func (s *Service) Heartbeat(ctx context.Context, jobID, owner string, lease time.Duration) error {
	return s.store.Heartbeat(ctx, jobID, owner, lease)
}

func (s *Service) StartCurrentStep(ctx context.Context, jobID, owner string, actor Actor) (Step, Attempt, error) {
	return s.store.StartCurrentStep(ctx, jobID, owner, actor)
}

func (s *Service) CompleteStep(ctx context.Context, jobID, owner, attemptID string, output json.RawMessage, actor Actor) (Job, error) {
	return s.store.CompleteStep(ctx, jobID, owner, attemptID, output, actor)
}

func (s *Service) BlockStep(
	ctx context.Context,
	jobID, owner, attemptID string,
	blockers, nextActions, resumeState json.RawMessage,
	actor Actor,
) (Job, error) {
	return s.store.BlockStep(ctx, jobID, owner, attemptID, blockers, nextActions, resumeState, actor)
}

func (s *Service) FailStep(
	ctx context.Context,
	jobID, owner, attemptID string,
	blockers, nextActions json.RawMessage,
	actor Actor,
) (Job, error) {
	return s.store.FailStep(ctx, jobID, owner, attemptID, blockers, nextActions, actor)
}
