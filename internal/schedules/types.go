package schedules

import (
	"encoding/json"
	"errors"
	"time"
)

var (
	ErrNotFound = errors.New("schedule not found")
	ErrConflict = errors.New("schedule conflict")
	ErrInvalid  = errors.New("invalid schedule")
	ErrNoRun    = errors.New("no schedule run is ready")
)

const KindDailyCandidates = "daily_candidates"

type DailyCandidateConfig struct {
	Source      string `json:"source"`
	Target      string `json:"target"`
	TargetCount int    `json:"target_count"`
	ScanLimit   int    `json:"scan_limit"`
	Page        int    `json:"page"`
}

type Schedule struct {
	ID             string               `json:"id"`
	Name           string               `json:"name"`
	Kind           string               `json:"kind"`
	CronExpression string               `json:"cron_expression"`
	Timezone       string               `json:"timezone"`
	Enabled        bool                 `json:"enabled"`
	Config         DailyCandidateConfig `json:"config"`
	NextRunAt      *time.Time           `json:"next_run_at,omitempty"`
	LastRunAt      *time.Time           `json:"last_run_at,omitempty"`
	CreatedAt      time.Time            `json:"created_at"`
	UpdatedAt      time.Time            `json:"updated_at"`
}

type CreateInput struct {
	Name           string
	CronExpression string
	Timezone       string
	Enabled        bool
	Config         DailyCandidateConfig
	CreatedBy      string
}

type UpdateInput struct {
	CronExpression *string
	Timezone       *string
	Enabled        *bool
	Config         *DailyCandidateConfig
}

type RunStatus string

const (
	RunQueued    RunStatus = "queued"
	RunRunning   RunStatus = "running"
	RunCreated   RunStatus = "created"
	RunFailed    RunStatus = "failed"
	RunCancelled RunStatus = "cancelled"
)

type Run struct {
	ID             string               `json:"id"`
	ScheduleID     string               `json:"schedule_id"`
	ScheduleName   string               `json:"schedule_name"`
	ScheduledFor   time.Time            `json:"scheduled_for"`
	Status         RunStatus            `json:"status"`
	JobID          string               `json:"job_id,omitempty"`
	Attempts       int                  `json:"attempts"`
	NextAttemptAt  time.Time            `json:"next_attempt_at"`
	LeaseExpiresAt *time.Time           `json:"lease_expires_at,omitempty"`
	LastError      string               `json:"last_error,omitempty"`
	CronExpression string               `json:"cron_expression"`
	Timezone       string               `json:"timezone"`
	Config         DailyCandidateConfig `json:"config"`
	CreatedAt      time.Time            `json:"created_at"`
	UpdatedAt      time.Time            `json:"updated_at"`
}

type Notification struct {
	ID            string          `json:"id"`
	ScheduleRunID string          `json:"schedule_run_id,omitempty"`
	JobID         string          `json:"job_id,omitempty"`
	Channel       string          `json:"channel"`
	Status        string          `json:"status"`
	Payload       json.RawMessage `json:"payload"`
	Attempts      int             `json:"attempts"`
	LastError     string          `json:"last_error,omitempty"`
	ScheduledAt   time.Time       `json:"scheduled_at"`
	SentAt        *time.Time      `json:"sent_at,omitempty"`
	CreatedAt     time.Time       `json:"created_at"`
}
