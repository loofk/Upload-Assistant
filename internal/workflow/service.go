package workflow

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

type registeredWorkflow struct {
	definition Definition
	versionID  string
}

type Service struct {
	store       *Store
	defaultKind string
	workflows   map[string]registeredWorkflow
}

func NewService(store *Store, definition Definition, workflowID string) *Service {
	service := &Service{store: store, defaultKind: definition.Name, workflows: make(map[string]registeredWorkflow)}
	_ = service.RegisterDefinition(definition, workflowID)
	return service
}

func (s *Service) RegisterDefinition(definition Definition, workflowID string) error {
	if s == nil || s.store == nil {
		return fmt.Errorf("workflow service store is required")
	}
	if definition.Name == "" || workflowID == "" {
		return fmt.Errorf("workflow name and version id are required")
	}
	if _, _, err := definition.MarshalAndHash(); err != nil {
		return err
	}
	if _, exists := s.workflows[definition.Name]; exists {
		return fmt.Errorf("workflow kind %s is already registered", definition.Name)
	}
	s.workflows[definition.Name] = registeredWorkflow{definition: definition, versionID: workflowID}
	return nil
}

func (s *Service) CreateJob(ctx context.Context, input CreateJobInput) (Job, error) {
	kind := input.Kind
	if kind == "" {
		kind = s.defaultKind
		input.Kind = kind
	}
	registered, exists := s.workflows[kind]
	if !exists {
		return Job{}, fmt.Errorf("%w: %s", ErrUnsupportedKind, kind)
	}
	return s.store.CreateJob(ctx, registered.versionID, registered.definition, input)
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
