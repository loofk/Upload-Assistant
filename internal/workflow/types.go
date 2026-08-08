package workflow

import (
	"encoding/json"
	"errors"
	"time"
)

var (
	ErrNotFound        = errors.New("workflow resource not found")
	ErrConflict        = errors.New("workflow state conflict")
	ErrReplayUnsafe    = errors.New("workflow replay is not allowed")
	ErrReconciliation  = errors.New("workflow reconciliation is required")
	ErrUnsupportedKind = errors.New("workflow kind is not registered")
)

type JobStatus string

const (
	JobDraft     JobStatus = "draft"
	JobQueued    JobStatus = "queued"
	JobRunning   JobStatus = "running"
	JobPaused    JobStatus = "paused"
	JobBlocked   JobStatus = "blocked"
	JobFailed    JobStatus = "failed"
	JobComplete  JobStatus = "complete"
	JobCancelled JobStatus = "cancelled"
)

type StepStatus string

const (
	StepPending   StepStatus = "pending"
	StepReady     StepStatus = "ready"
	StepRunning   StepStatus = "running"
	StepPaused    StepStatus = "paused"
	StepBlocked   StepStatus = "blocked"
	StepFailed    StepStatus = "failed"
	StepComplete  StepStatus = "complete"
	StepSkipped   StepStatus = "skipped"
	StepCancelled StepStatus = "cancelled"
)

type ExecutionMode string

const (
	ExecutionAuto ExecutionMode = "auto"
	ExecutionStep ExecutionMode = "step"
)

type Actor struct {
	Type string
	ID   string
}

type Job struct {
	ID            string          `json:"id"`
	ReplayOfJobID string          `json:"replay_of_job_id,omitempty"`
	Kind          string          `json:"kind"`
	Status        JobStatus       `json:"status"`
	ExecutionMode ExecutionMode   `json:"execution_mode"`
	CurrentStep   string          `json:"current_step,omitempty"`
	Input         json.RawMessage `json:"input"`
	Blockers      json.RawMessage `json:"blockers"`
	NextActions   json.RawMessage `json:"next_actions"`
	ResumeState   json.RawMessage `json:"resume_state"`
	Summary       json.RawMessage `json:"summary"`
	CreatedAt     time.Time       `json:"created_at"`
	UpdatedAt     time.Time       `json:"updated_at"`
	StartedAt     *time.Time      `json:"started_at,omitempty"`
	FinishedAt    *time.Time      `json:"finished_at,omitempty"`
}

type ListJobsFilter struct {
	Status          JobStatus
	Kind            string
	Limit           int
	BeforeCreatedAt *time.Time
	BeforeID        string
}

type JobPage struct {
	Jobs    []Job
	HasMore bool
}

type Step struct {
	ID            string          `json:"id"`
	JobID         string          `json:"job_id"`
	Key           string          `json:"key"`
	Position      int             `json:"position"`
	Status        StepStatus      `json:"status"`
	Required      bool            `json:"required"`
	GateKind      string          `json:"gate_kind,omitempty"`
	InputSnapshot json.RawMessage `json:"input_snapshot"`
	OutputSummary json.RawMessage `json:"output_summary"`
	Blockers      json.RawMessage `json:"blockers"`
	NextActions   json.RawMessage `json:"next_actions"`
	ResumeState   json.RawMessage `json:"resume_state"`
	StartedAt     *time.Time      `json:"started_at,omitempty"`
	FinishedAt    *time.Time      `json:"finished_at,omitempty"`
}

type Attempt struct {
	ID             string          `json:"id"`
	JobID          string          `json:"job_id,omitempty"`
	StepID         string          `json:"step_id"`
	StepKey        string          `json:"step_key,omitempty"`
	StepPosition   int             `json:"step_position,omitempty"`
	Number         int             `json:"number"`
	Status         StepStatus      `json:"status"`
	Adapter        string          `json:"adapter,omitempty"`
	AdapterVersion string          `json:"adapter_version,omitempty"`
	InputSnapshot  json.RawMessage `json:"input_snapshot,omitempty"`
	OutputSummary  json.RawMessage `json:"output_summary,omitempty"`
	ErrorCode      string          `json:"error_code,omitempty"`
	ErrorDetails   json.RawMessage `json:"error_details,omitempty"`
	StartedAt      time.Time       `json:"started_at"`
	FinishedAt     *time.Time      `json:"finished_at,omitempty"`
}

type ListAttemptsFilter struct {
	Limit         int
	AfterPosition int
	AfterNumber   int
}

type AttemptPage struct {
	Attempts []Attempt
	HasMore  bool
}

type Event struct {
	ID           string          `json:"id"`
	JobID        string          `json:"job_id"`
	StepID       string          `json:"step_id,omitempty"`
	AttemptID    string          `json:"attempt_id,omitempty"`
	Sequence     int64           `json:"sequence"`
	Type         string          `json:"type"`
	ActorType    string          `json:"actor_type"`
	ActorID      string          `json:"actor_id,omitempty"`
	Payload      json.RawMessage `json:"payload"`
	PreviousHash string          `json:"previous_hash,omitempty"`
	Hash         string          `json:"hash"`
	CreatedAt    time.Time       `json:"created_at"`
}

type Artifact struct {
	ID             string          `json:"id"`
	JobID          string          `json:"job_id"`
	StepID         string          `json:"step_id,omitempty"`
	AttemptID      string          `json:"attempt_id,omitempty"`
	Kind           string          `json:"kind"`
	StorageBackend string          `json:"storage_backend"`
	StoragePath    string          `json:"storage_path"`
	Filename       string          `json:"filename"`
	MIMEType       string          `json:"mime_type,omitempty"`
	SizeBytes      int64           `json:"size_bytes"`
	SHA256         string          `json:"sha256"`
	Metadata       json.RawMessage `json:"metadata"`
	ExpiresAt      time.Time       `json:"expires_at"`
	CreatedAt      time.Time       `json:"created_at"`
}

type RegisterArtifactInput struct {
	JobID       string
	StepID      string
	AttemptID   string
	Kind        string
	StoragePath string
	Filename    string
	MIMEType    string
	SizeBytes   int64
	SHA256      string
	Metadata    json.RawMessage
	Retention   time.Duration
	Actor       Actor
}

type CreateJobInput struct {
	Kind           string
	ExecutionMode  ExecutionMode
	StopAfterStep  string
	Input          json.RawMessage
	IdempotencyKey string
	Owner          string
	Actor          Actor
}

type ReplayJobInput struct {
	ExecutionMode  ExecutionMode
	StopAfterStep  string
	IdempotencyKey string
	Owner          string
	Actor          Actor
}

type BlockStepInput struct {
	JobID       string
	AttemptID   string
	Blockers    json.RawMessage
	NextActions json.RawMessage
	ResumeState json.RawMessage
	Actor       Actor
}
