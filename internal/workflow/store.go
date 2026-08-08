package workflow

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Store struct {
	pool *pgxpool.Pool
}

func NewStore(pool *pgxpool.Pool) *Store {
	return &Store{pool: pool}
}

func (s *Store) EnsureDefinition(ctx context.Context, definition Definition) (string, error) {
	body, checksum, err := definition.MarshalAndHash()
	if err != nil {
		return "", err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return "", fmt.Errorf("begin workflow definition transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var id, existingChecksum string
	err = tx.QueryRow(ctx, `
		SELECT id::text, definition_sha256
		FROM workflow_versions
		WHERE name = $1 AND version = $2
		FOR UPDATE`, definition.Name, definition.Version).Scan(&id, &existingChecksum)
	if err == nil {
		if existingChecksum != checksum {
			return "", fmt.Errorf("workflow %s version %d checksum changed", definition.Name, definition.Version)
		}
		if _, err := tx.Exec(ctx, "UPDATE workflow_versions SET active = (id::text = $1) WHERE name = $2", id, definition.Name); err != nil {
			return "", fmt.Errorf("activate workflow definition: %w", err)
		}
	} else if errors.Is(err, pgx.ErrNoRows) {
		if _, err := tx.Exec(ctx, "UPDATE workflow_versions SET active = false WHERE name = $1", definition.Name); err != nil {
			return "", fmt.Errorf("deactivate previous workflow definition: %w", err)
		}
		err = tx.QueryRow(ctx, `
			INSERT INTO workflow_versions(name, version, definition, definition_sha256, active)
			VALUES ($1, $2, $3, $4, true)
			RETURNING id::text`, definition.Name, definition.Version, body, checksum).Scan(&id)
		if err != nil {
			return "", fmt.Errorf("insert workflow definition: %w", err)
		}
	} else {
		return "", fmt.Errorf("query workflow definition: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return "", fmt.Errorf("commit workflow definition: %w", err)
	}
	return id, nil
}

func (s *Store) CreateJob(ctx context.Context, workflowVersionID string, definition Definition, input CreateJobInput) (Job, error) {
	if input.Kind == "" {
		input.Kind = definition.Name
	}
	if input.ExecutionMode == "" {
		input.ExecutionMode = ExecutionAuto
	}
	if input.ExecutionMode != ExecutionAuto && input.ExecutionMode != ExecutionStep {
		return Job{}, fmt.Errorf("invalid execution mode %q", input.ExecutionMode)
	}
	if len(input.Input) == 0 {
		input.Input = json.RawMessage(`{}`)
	}
	if !json.Valid(input.Input) {
		return Job{}, fmt.Errorf("job input must be valid JSON")
	}
	canonicalInput, err := canonicalJSON(input.Input)
	if err != nil {
		return Job{}, fmt.Errorf("canonicalize job input: %w", err)
	}
	input.Input = canonicalInput
	if input.Owner == "" {
		input.Owner = "system"
	}
	if input.Actor.Type == "" {
		input.Actor.Type = "system"
	}

	requestHash := sha256Hex([]byte(strings.Join([]string{
		input.Kind, string(input.ExecutionMode), input.StopAfterStep, string(input.Input),
	}, "\x00")))
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Job{}, fmt.Errorf("begin create job transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var id string
	err = tx.QueryRow(ctx, `
		INSERT INTO jobs(
			kind, status, execution_mode, workflow_version_id, current_step_key,
			stop_after_step, input, idempotency_key, idempotency_owner, idempotency_request_hash
		)
		VALUES ($1, 'queued', $2, $3, $4, NULLIF($5, ''), $6, NULLIF($7, ''), $8, $9)
		ON CONFLICT (idempotency_owner, idempotency_key) WHERE idempotency_key IS NOT NULL
		DO UPDATE SET updated_at = jobs.updated_at
		WHERE jobs.idempotency_request_hash = EXCLUDED.idempotency_request_hash
		RETURNING id::text`,
		input.Kind, input.ExecutionMode, workflowVersionID, definition.Steps[0].Key,
		input.StopAfterStep, input.Input, input.IdempotencyKey, input.Owner, requestHash,
	).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, fmt.Errorf("%w: idempotency key was already used with a different request", ErrConflict)
	}
	if err != nil {
		return Job{}, fmt.Errorf("insert job: %w", err)
	}

	var existingSteps int
	if err := tx.QueryRow(ctx, "SELECT count(*) FROM job_steps WHERE job_id = $1", id).Scan(&existingSteps); err != nil {
		return Job{}, fmt.Errorf("count job steps: %w", err)
	}
	if existingSteps == 0 {
		for position, step := range definition.Steps {
			status := StepPending
			if position == 0 {
				status = StepReady
			}
			if _, err := tx.Exec(ctx, `
				INSERT INTO job_steps(job_id, step_key, position, status, required, gate_kind)
				VALUES ($1, $2, $3, $4, $5, NULLIF($6, ''))`,
				id, step.Key, position+1, status, step.Required, step.GateKind,
			); err != nil {
				return Job{}, fmt.Errorf("insert job step %s: %w", step.Key, err)
			}
		}
		if _, err := appendEvent(ctx, tx, id, "", "", "job.created", input.Actor, map[string]any{
			"kind": input.Kind, "execution_mode": input.ExecutionMode, "workflow_version_id": workflowVersionID,
		}); err != nil {
			return Job{}, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return Job{}, fmt.Errorf("commit create job transaction: %w", err)
	}
	return s.GetJob(ctx, id)
}

func (s *Store) GetJob(ctx context.Context, id string) (Job, error) {
	return scanJob(s.pool.QueryRow(ctx, jobSelect+" WHERE id = $1", id))
}

func (s *Store) ListJobs(ctx context.Context, filter ListJobsFilter) (JobPage, error) {
	if filter.Limit <= 0 || filter.Limit > 100 {
		filter.Limit = 25
	}
	query := jobSelect + " WHERE true"
	arguments := make([]any, 0, 5)
	if filter.Status != "" {
		arguments = append(arguments, filter.Status)
		query += fmt.Sprintf(" AND status = $%d", len(arguments))
	}
	if filter.Kind != "" {
		arguments = append(arguments, filter.Kind)
		query += fmt.Sprintf(" AND kind = $%d", len(arguments))
	}
	if filter.BeforeCreatedAt != nil || filter.BeforeID != "" {
		if filter.BeforeCreatedAt == nil || filter.BeforeID == "" {
			return JobPage{}, errors.New("job list cursor requires both created_at and id")
		}
		arguments = append(arguments, *filter.BeforeCreatedAt, filter.BeforeID)
		query += fmt.Sprintf(" AND (created_at, id) < ($%d, $%d::uuid)", len(arguments)-1, len(arguments))
	}
	arguments = append(arguments, filter.Limit+1)
	query += fmt.Sprintf(" ORDER BY created_at DESC, id DESC LIMIT $%d", len(arguments))
	rows, err := s.pool.Query(ctx, query, arguments...)
	if err != nil {
		return JobPage{}, fmt.Errorf("list jobs: %w", err)
	}
	defer rows.Close()
	jobs := make([]Job, 0, filter.Limit+1)
	for rows.Next() {
		job, err := scanJob(rows)
		if err != nil {
			return JobPage{}, err
		}
		jobs = append(jobs, job)
	}
	if err := rows.Err(); err != nil {
		return JobPage{}, fmt.Errorf("iterate jobs: %w", err)
	}
	hasMore := len(jobs) > filter.Limit
	if hasMore {
		jobs = jobs[:filter.Limit]
	}
	return JobPage{Jobs: jobs, HasMore: hasMore}, nil
}

func (s *Store) ListSteps(ctx context.Context, jobID string) ([]Step, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, job_id::text, step_key, position, status, required,
		       COALESCE(gate_kind, ''), input_snapshot, output_summary, blockers,
		       next_actions, resume_state, started_at, finished_at
		FROM job_steps WHERE job_id = $1 ORDER BY position`, jobID)
	if err != nil {
		return nil, fmt.Errorf("list job steps: %w", err)
	}
	defer rows.Close()
	steps := make([]Step, 0)
	for rows.Next() {
		var step Step
		var startedAt, finishedAt pgtype.Timestamptz
		if err := rows.Scan(
			&step.ID, &step.JobID, &step.Key, &step.Position, &step.Status, &step.Required,
			&step.GateKind, &step.InputSnapshot, &step.OutputSummary, &step.Blockers,
			&step.NextActions, &step.ResumeState, &startedAt, &finishedAt,
		); err != nil {
			return nil, fmt.Errorf("scan job step: %w", err)
		}
		step.StartedAt = timePointer(startedAt)
		step.FinishedAt = timePointer(finishedAt)
		steps = append(steps, step)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate job steps: %w", err)
	}
	return steps, nil
}

func (s *Store) ListEvents(ctx context.Context, jobID string, after int64, limit int) ([]Event, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, job_id::text, COALESCE(job_step_id::text, ''), COALESCE(attempt_id::text, ''),
		       sequence, event_type, actor_type, COALESCE(actor_id, ''), payload,
		       COALESCE(previous_hash, ''), event_hash, created_at
		FROM job_events
		WHERE job_id = $1 AND sequence > $2
		ORDER BY sequence LIMIT $3`, jobID, after, limit)
	if err != nil {
		return nil, fmt.Errorf("list job events: %w", err)
	}
	defer rows.Close()
	events := make([]Event, 0)
	for rows.Next() {
		var event Event
		if err := rows.Scan(
			&event.ID, &event.JobID, &event.StepID, &event.AttemptID, &event.Sequence,
			&event.Type, &event.ActorType, &event.ActorID, &event.Payload,
			&event.PreviousHash, &event.Hash, &event.CreatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan job event: %w", err)
		}
		events = append(events, event)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate job events: %w", err)
	}
	return events, nil
}

func (s *Store) RegisterArtifact(ctx context.Context, input RegisterArtifactInput) (Artifact, error) {
	if input.JobID == "" || input.StepID == "" || input.AttemptID == "" || input.Kind == "" || input.StoragePath == "" || input.Filename == "" || input.SHA256 == "" {
		return Artifact{}, fmt.Errorf("artifact job, step, attempt, kind, path, filename, and sha256 are required")
	}
	if input.SizeBytes < 0 {
		return Artifact{}, fmt.Errorf("artifact size must not be negative")
	}
	if input.Retention <= 0 {
		input.Retention = 30 * 24 * time.Hour
	}
	if len(input.Metadata) == 0 {
		input.Metadata = json.RawMessage(`{}`)
	}
	metadata, err := canonicalJSON(input.Metadata)
	if err != nil {
		return Artifact{}, fmt.Errorf("canonicalize artifact metadata: %w", err)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Artifact{}, fmt.Errorf("begin register artifact transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var lockedJobID string
	if err := tx.QueryRow(ctx, "SELECT id::text FROM jobs WHERE id = $1 FOR UPDATE", input.JobID).Scan(&lockedJobID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Artifact{}, ErrNotFound
		}
		return Artifact{}, fmt.Errorf("lock artifact job: %w", err)
	}
	var artifact Artifact
	err = tx.QueryRow(ctx, `
		INSERT INTO artifacts(
			job_id, job_step_id, attempt_id, kind, storage_backend, storage_path,
			filename, mime_type, size_bytes, sha256, metadata, expires_at
		)
		VALUES ($1, $2, $3, $4, 'local', $5, $6, NULLIF($7, ''), $8, $9, $10, now() + $11::interval)
		RETURNING id::text, created_at, expires_at`,
		input.JobID, input.StepID, input.AttemptID, input.Kind, input.StoragePath,
		input.Filename, input.MIMEType, input.SizeBytes, input.SHA256, metadata,
		intervalLiteral(input.Retention),
	).Scan(&artifact.ID, &artifact.CreatedAt, &artifact.ExpiresAt)
	if err != nil {
		return Artifact{}, fmt.Errorf("insert artifact: %w", err)
	}
	artifact.JobID = input.JobID
	artifact.StepID = input.StepID
	artifact.AttemptID = input.AttemptID
	artifact.Kind = input.Kind
	artifact.StorageBackend = "local"
	artifact.StoragePath = input.StoragePath
	artifact.Filename = input.Filename
	artifact.MIMEType = input.MIMEType
	artifact.SizeBytes = input.SizeBytes
	artifact.SHA256 = input.SHA256
	artifact.Metadata = metadata
	if _, err := appendEvent(ctx, tx, input.JobID, input.StepID, input.AttemptID, "artifact.created", input.Actor, map[string]any{
		"artifact_id": artifact.ID, "kind": input.Kind, "filename": input.Filename,
		"size_bytes": input.SizeBytes, "sha256": input.SHA256,
	}); err != nil {
		return Artifact{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Artifact{}, fmt.Errorf("commit register artifact transaction: %w", err)
	}
	return artifact, nil
}

func (s *Store) ListArtifacts(ctx context.Context, jobID string) ([]Artifact, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, job_id::text, COALESCE(job_step_id::text, ''), COALESCE(attempt_id::text, ''),
		       kind, storage_backend, storage_path, filename, COALESCE(mime_type, ''),
		       size_bytes, sha256, metadata, expires_at, created_at
		FROM artifacts WHERE job_id = $1 ORDER BY created_at, id`, jobID)
	if err != nil {
		return nil, fmt.Errorf("list artifacts: %w", err)
	}
	defer rows.Close()
	artifacts := make([]Artifact, 0)
	for rows.Next() {
		var artifact Artifact
		if err := rows.Scan(
			&artifact.ID, &artifact.JobID, &artifact.StepID, &artifact.AttemptID,
			&artifact.Kind, &artifact.StorageBackend, &artifact.StoragePath, &artifact.Filename,
			&artifact.MIMEType, &artifact.SizeBytes, &artifact.SHA256, &artifact.Metadata,
			&artifact.ExpiresAt, &artifact.CreatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan artifact: %w", err)
		}
		artifacts = append(artifacts, artifact)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate artifacts: %w", err)
	}
	return artifacts, nil
}

func (s *Store) ClaimNextJob(ctx context.Context, owner string, lease time.Duration, actor Actor) (Job, error) {
	if owner == "" {
		return Job{}, fmt.Errorf("lease owner is required")
	}
	if lease <= 0 {
		lease = 30 * time.Second
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Job{}, fmt.Errorf("begin claim transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var id, previousLeaseOwner string
	var previousStatus JobStatus
	err = tx.QueryRow(ctx, `
		SELECT id::text, status, COALESCE(lease_owner, '')
		FROM jobs
		WHERE status = 'queued'
		   OR (status = 'running' AND lease_expires_at < now())
		ORDER BY created_at
		FOR UPDATE SKIP LOCKED
		LIMIT 1`).Scan(&id, &previousStatus, &previousLeaseOwner)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, ErrNotFound
	}
	if err != nil {
		return Job{}, fmt.Errorf("select claimable job: %w", err)
	}
	if previousStatus == JobRunning {
		var stepID, stepKey, attemptID string
		recoveryErr := tx.QueryRow(ctx, `
			SELECT js.id::text, js.step_key, sa.id::text
			FROM job_steps js
			JOIN step_attempts sa ON sa.job_step_id = js.id AND sa.status = 'running'
			WHERE js.job_id = $1 AND js.status = 'running'
			ORDER BY sa.started_at DESC LIMIT 1
			FOR UPDATE OF js, sa`, id).Scan(&stepID, &stepKey, &attemptID)
		if recoveryErr != nil && !errors.Is(recoveryErr, pgx.ErrNoRows) {
			return Job{}, fmt.Errorf("lock expired step attempt: %w", recoveryErr)
		}
		if recoveryErr == nil {
			if _, err := tx.Exec(ctx, `
				UPDATE step_attempts
				SET status = 'failed', error_code = 'worker_lease_expired',
				    error_message = 'worker lease expired before the attempt completed', finished_at = now()
				WHERE id = $1`, attemptID); err != nil {
				return Job{}, fmt.Errorf("close expired step attempt: %w", err)
			}
			if _, err := tx.Exec(ctx, `
				UPDATE job_steps SET status = 'ready', started_at = NULL, finished_at = NULL, updated_at = now()
				WHERE id = $1`, stepID); err != nil {
				return Job{}, fmt.Errorf("make expired step retryable: %w", err)
			}
			if _, err := appendEvent(ctx, tx, id, stepID, attemptID, "step.lease_recovered", actor, map[string]any{
				"step_key": stepKey, "previous_lease_owner": previousLeaseOwner,
			}); err != nil {
				return Job{}, err
			}
		}
	}
	if _, err := tx.Exec(ctx, `
		UPDATE jobs
		SET status = 'running', lease_owner = $2, lease_expires_at = now() + $3::interval,
		    heartbeat_at = now(), started_at = COALESCE(started_at, now()), updated_at = now()
		WHERE id = $1`, id, owner, intervalLiteral(lease)); err != nil {
		return Job{}, fmt.Errorf("claim job: %w", err)
	}
	eventType := "job.claimed"
	if previousStatus == JobRunning {
		eventType = "job.lease_recovered"
	}
	if _, err := appendEvent(ctx, tx, id, "", "", eventType, actor, map[string]any{"lease_owner": owner}); err != nil {
		return Job{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Job{}, fmt.Errorf("commit claim transaction: %w", err)
	}
	return s.GetJob(ctx, id)
}

func (s *Store) Heartbeat(ctx context.Context, jobID, owner string, lease time.Duration) error {
	result, err := s.pool.Exec(ctx, `
		UPDATE jobs
		SET heartbeat_at = now(), lease_expires_at = now() + $3::interval, updated_at = now()
		WHERE id = $1 AND status = 'running' AND lease_owner = $2`, jobID, owner, intervalLiteral(lease))
	if err != nil {
		return fmt.Errorf("heartbeat job: %w", err)
	}
	if result.RowsAffected() != 1 {
		return ErrConflict
	}
	return nil
}

func (s *Store) StartCurrentStep(ctx context.Context, jobID, owner string, actor Actor) (Step, Attempt, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Step{}, Attempt{}, fmt.Errorf("begin start step transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var jobStatus JobStatus
	var stepKey, leaseOwner string
	var jobInput, jobResumeState, jobConfigSnapshot json.RawMessage
	err = tx.QueryRow(ctx, `
		SELECT status, COALESCE(current_step_key, ''), COALESCE(lease_owner, ''),
		       input, resume_state, config_snapshot
		FROM jobs WHERE id = $1 FOR UPDATE`, jobID).Scan(
		&jobStatus, &stepKey, &leaseOwner, &jobInput, &jobResumeState, &jobConfigSnapshot,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Step{}, Attempt{}, ErrNotFound
	}
	if err != nil {
		return Step{}, Attempt{}, fmt.Errorf("lock job for step start: %w", err)
	}
	if jobStatus != JobRunning || leaseOwner != owner || stepKey == "" {
		return Step{}, Attempt{}, fmt.Errorf("%w: job is not leased to this worker", ErrConflict)
	}
	step, err := scanStep(tx.QueryRow(ctx, stepSelect+" WHERE job_id = $1 AND step_key = $2 FOR UPDATE", jobID, stepKey))
	if err != nil {
		return Step{}, Attempt{}, err
	}
	if step.Status != StepReady {
		return Step{}, Attempt{}, fmt.Errorf("%w: step %s is %s, want ready", ErrConflict, step.Key, step.Status)
	}
	inputSnapshot, err := buildStepInputSnapshot(
		ctx, tx, jobID, step.Position, jobInput, jobResumeState, jobConfigSnapshot,
	)
	if err != nil {
		return Step{}, Attempt{}, err
	}
	step.InputSnapshot = inputSnapshot
	var attemptNumber int
	if err := tx.QueryRow(ctx, "SELECT COALESCE(max(attempt), 0) + 1 FROM step_attempts WHERE job_step_id = $1", step.ID).Scan(&attemptNumber); err != nil {
		return Step{}, Attempt{}, fmt.Errorf("allocate step attempt: %w", err)
	}
	var attempt Attempt
	err = tx.QueryRow(ctx, `
		INSERT INTO step_attempts(job_step_id, attempt, status, input_snapshot)
		VALUES ($1, $2, 'running', $3)
		RETURNING id::text, started_at`, step.ID, attemptNumber, inputSnapshot).Scan(&attempt.ID, &attempt.StartedAt)
	if err != nil {
		return Step{}, Attempt{}, fmt.Errorf("insert step attempt: %w", err)
	}
	attempt.StepID = step.ID
	attempt.Number = attemptNumber
	attempt.Status = StepRunning
	if _, err := tx.Exec(ctx, `
		UPDATE job_steps SET status = 'running', input_snapshot = $2,
		       started_at = COALESCE(started_at, now()), updated_at = now()
		WHERE id = $1`, step.ID, inputSnapshot); err != nil {
		return Step{}, Attempt{}, fmt.Errorf("mark step running: %w", err)
	}
	if _, err := appendEvent(ctx, tx, jobID, step.ID, attempt.ID, "step.started", actor, map[string]any{
		"step_key": step.Key, "attempt": attempt.Number, "input_sha256": sha256Hex(inputSnapshot),
	}); err != nil {
		return Step{}, Attempt{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Step{}, Attempt{}, fmt.Errorf("commit start step transaction: %w", err)
	}
	step.Status = StepRunning
	if step.StartedAt == nil {
		startedAt := attempt.StartedAt
		step.StartedAt = &startedAt
	}
	return step, attempt, nil
}

func (s *Store) CompleteStep(
	ctx context.Context,
	jobID, owner, attemptID string,
	output json.RawMessage,
	actor Actor,
) (Job, error) {
	if len(output) == 0 {
		output = json.RawMessage(`{}`)
	}
	canonicalOutput, err := canonicalJSON(output)
	if err != nil {
		return Job{}, fmt.Errorf("canonicalize step output: %w", err)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Job{}, fmt.Errorf("begin complete step transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var jobStatus JobStatus
	var mode ExecutionMode
	var leaseOwner, stopAfter string
	err = tx.QueryRow(ctx, `
		SELECT status, execution_mode, COALESCE(lease_owner, ''), COALESCE(stop_after_step, '')
		FROM jobs WHERE id = $1 FOR UPDATE`, jobID).Scan(&jobStatus, &mode, &leaseOwner, &stopAfter)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, ErrNotFound
	}
	if err != nil {
		return Job{}, fmt.Errorf("lock job for step completion: %w", err)
	}
	if jobStatus != JobRunning || leaseOwner != owner {
		return Job{}, fmt.Errorf("%w: job is not leased to this worker", ErrConflict)
	}
	var stepID, stepKey string
	var position int
	var attemptStatus StepStatus
	err = tx.QueryRow(ctx, `
		SELECT js.id::text, js.step_key, js.position, sa.status
		FROM step_attempts sa
		JOIN job_steps js ON js.id = sa.job_step_id
		WHERE sa.id = $1 AND js.job_id = $2
		FOR UPDATE OF js, sa`, attemptID, jobID).Scan(&stepID, &stepKey, &position, &attemptStatus)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, ErrNotFound
	}
	if err != nil {
		return Job{}, fmt.Errorf("lock step attempt: %w", err)
	}
	if attemptStatus != StepRunning {
		return Job{}, fmt.Errorf("%w: attempt is %s, want running", ErrConflict, attemptStatus)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE step_attempts SET status = 'complete', output_summary = $2, finished_at = now()
		WHERE id = $1`, attemptID, canonicalOutput); err != nil {
		return Job{}, fmt.Errorf("complete step attempt: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE job_steps SET status = 'complete', output_summary = $2, blockers = '[]',
		       next_actions = '[]', finished_at = now(), updated_at = now()
		WHERE id = $1`, stepID, canonicalOutput); err != nil {
		return Job{}, fmt.Errorf("complete job step: %w", err)
	}
	if _, err := appendEvent(ctx, tx, jobID, stepID, attemptID, "step.completed", actor, map[string]any{
		"step_key": stepKey, "output_sha256": sha256Hex(canonicalOutput),
	}); err != nil {
		return Job{}, err
	}

	var nextStepID, nextStepKey string
	err = tx.QueryRow(ctx, `
		SELECT id::text, step_key FROM job_steps
		WHERE job_id = $1 AND position > $2 AND status = 'pending'
		ORDER BY position LIMIT 1 FOR UPDATE`, jobID, position).Scan(&nextStepID, &nextStepKey)
	if errors.Is(err, pgx.ErrNoRows) {
		if _, err := tx.Exec(ctx, `
			UPDATE jobs SET status = 'complete', current_step_key = NULL, summary = $2,
			       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
			       finished_at = now(), updated_at = now()
			WHERE id = $1`, jobID, canonicalOutput); err != nil {
			return Job{}, fmt.Errorf("complete job: %w", err)
		}
		if _, err := appendEvent(ctx, tx, jobID, "", "", "job.completed", actor, map[string]any{"final_step": stepKey}); err != nil {
			return Job{}, err
		}
	} else if err != nil {
		return Job{}, fmt.Errorf("select next job step: %w", err)
	} else {
		if _, err := tx.Exec(ctx, "UPDATE job_steps SET status = 'ready', updated_at = now() WHERE id = $1", nextStepID); err != nil {
			return Job{}, fmt.Errorf("mark next step ready: %w", err)
		}
		nextJobStatus := JobQueued
		if mode == ExecutionStep || stopAfter == stepKey {
			nextJobStatus = JobPaused
		}
		if _, err := tx.Exec(ctx, `
			UPDATE jobs SET status = $2, current_step_key = $3,
			       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL, updated_at = now()
			WHERE id = $1`, jobID, nextJobStatus, nextStepKey); err != nil {
			return Job{}, fmt.Errorf("advance job: %w", err)
		}
		if nextJobStatus == JobPaused {
			if _, err := appendEvent(ctx, tx, jobID, "", "", "job.paused", actor, map[string]any{
				"reason": "step_boundary", "completed_step": stepKey, "next_step": nextStepKey,
			}); err != nil {
				return Job{}, err
			}
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return Job{}, fmt.Errorf("commit complete step transaction: %w", err)
	}
	return s.GetJob(ctx, jobID)
}

func (s *Store) BlockStep(
	ctx context.Context,
	jobID, owner, attemptID string,
	blockers, nextActions, resumeState json.RawMessage,
	actor Actor,
) (Job, error) {
	return s.stopStep(ctx, jobID, owner, attemptID, StepBlocked, blockers, nextActions, resumeState, "step.blocked", actor)
}

func (s *Store) FailStep(
	ctx context.Context,
	jobID, owner, attemptID string,
	blockers, nextActions json.RawMessage,
	actor Actor,
) (Job, error) {
	return s.stopStep(ctx, jobID, owner, attemptID, StepFailed, blockers, nextActions, json.RawMessage(`{}`), "step.failed", actor)
}

func (s *Store) stopStep(
	ctx context.Context,
	jobID, owner, attemptID string,
	stepStatus StepStatus,
	blockers, nextActions, resumeState json.RawMessage,
	eventType string,
	actor Actor,
) (Job, error) {
	values := []*json.RawMessage{&blockers, &nextActions, &resumeState}
	defaults := []json.RawMessage{json.RawMessage(`[]`), json.RawMessage(`[]`), json.RawMessage(`{}`)}
	for index := range values {
		if len(*values[index]) == 0 {
			*values[index] = defaults[index]
		}
		canonical, err := canonicalJSON(*values[index])
		if err != nil {
			return Job{}, fmt.Errorf("canonicalize stopped step data: %w", err)
		}
		*values[index] = canonical
	}
	jobStatus := JobBlocked
	if stepStatus == StepFailed {
		jobStatus = JobFailed
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Job{}, fmt.Errorf("begin stop step transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var stepID, stepKey, leaseOwner string
	var currentJobStatus JobStatus
	var attemptStatus StepStatus
	err = tx.QueryRow(ctx, `
		SELECT js.id::text, js.step_key, COALESCE(j.lease_owner, ''), j.status, sa.status
		FROM step_attempts sa
		JOIN job_steps js ON js.id = sa.job_step_id
		JOIN jobs j ON j.id = js.job_id
		WHERE sa.id = $1 AND j.id = $2
		FOR UPDATE OF j, js, sa`, attemptID, jobID).Scan(&stepID, &stepKey, &leaseOwner, &currentJobStatus, &attemptStatus)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, ErrNotFound
	}
	if err != nil {
		return Job{}, fmt.Errorf("lock stopped step: %w", err)
	}
	if currentJobStatus != JobRunning || leaseOwner != owner || attemptStatus != StepRunning {
		return Job{}, fmt.Errorf("%w: job attempt is not running for this worker", ErrConflict)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE step_attempts SET status = $2, error_message = $3::text, finished_at = now()
		WHERE id = $1`, attemptID, stepStatus, string(blockers)); err != nil {
		return Job{}, fmt.Errorf("stop step attempt: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE job_steps SET status = $2, blockers = $3, next_actions = $4,
		       resume_state = $5, finished_at = now(), updated_at = now()
		WHERE id = $1`, stepID, stepStatus, blockers, nextActions, resumeState); err != nil {
		return Job{}, fmt.Errorf("stop job step: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE jobs SET status = $2, blockers = $3, next_actions = $4, resume_state = $5,
		       lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL, updated_at = now()
		WHERE id = $1`, jobID, jobStatus, blockers, nextActions, resumeState); err != nil {
		return Job{}, fmt.Errorf("stop job: %w", err)
	}
	if _, err := appendEvent(ctx, tx, jobID, stepID, attemptID, eventType, actor, map[string]any{
		"step_key": stepKey, "blockers": json.RawMessage(blockers), "next_actions": json.RawMessage(nextActions),
	}); err != nil {
		return Job{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Job{}, fmt.Errorf("commit stop step transaction: %w", err)
	}
	return s.GetJob(ctx, jobID)
}

func (s *Store) PauseJob(ctx context.Context, jobID string, actor Actor) (Job, error) {
	return s.transitionJob(ctx, jobID, actor, []JobStatus{JobQueued, JobRunning, JobBlocked, JobFailed}, JobPaused, "job.paused", nil)
}

func (s *Store) ResumeJob(ctx context.Context, jobID string, resumeState json.RawMessage, actor Actor) (Job, error) {
	if len(resumeState) == 0 {
		resumeState = json.RawMessage(`{}`)
	}
	if !json.Valid(resumeState) {
		return Job{}, fmt.Errorf("resume state must be valid JSON")
	}
	return s.transitionJob(ctx, jobID, actor, []JobStatus{JobPaused, JobBlocked, JobFailed}, JobQueued, "job.resumed", resumeState)
}

func (s *Store) CancelJob(ctx context.Context, jobID string, actor Actor) (Job, error) {
	return s.transitionJob(ctx, jobID, actor, []JobStatus{JobDraft, JobQueued, JobRunning, JobPaused, JobBlocked, JobFailed}, JobCancelled, "job.cancelled", nil)
}

func (s *Store) transitionJob(
	ctx context.Context,
	jobID string,
	actor Actor,
	allowed []JobStatus,
	target JobStatus,
	eventType string,
	resumeState json.RawMessage,
) (Job, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Job{}, fmt.Errorf("begin job transition: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var current JobStatus
	var currentStepKey string
	if err := tx.QueryRow(ctx, "SELECT status, COALESCE(current_step_key, '') FROM jobs WHERE id = $1 FOR UPDATE", jobID).Scan(&current, &currentStepKey); errors.Is(err, pgx.ErrNoRows) {
		return Job{}, ErrNotFound
	} else if err != nil {
		return Job{}, fmt.Errorf("lock job: %w", err)
	}
	if !containsStatus(allowed, current) {
		return Job{}, fmt.Errorf("%w: cannot transition job from %s to %s", ErrConflict, current, target)
	}
	var interruptedStepID, interruptedAttemptID string
	if current == JobRunning && (target == JobPaused || target == JobCancelled) {
		interruptErr := tx.QueryRow(ctx, `
			SELECT js.id::text, sa.id::text
			FROM job_steps js
			JOIN step_attempts sa ON sa.job_step_id = js.id AND sa.status = 'running'
			WHERE js.job_id = $1 AND js.step_key = $2 AND js.status = 'running'
			ORDER BY sa.started_at DESC LIMIT 1
			FOR UPDATE OF js, sa`, jobID, currentStepKey).Scan(&interruptedStepID, &interruptedAttemptID)
		if interruptErr != nil && !errors.Is(interruptErr, pgx.ErrNoRows) {
			return Job{}, fmt.Errorf("lock interrupted step attempt: %w", interruptErr)
		}
		if interruptErr == nil {
			attemptStatus := StepPaused
			stepStatus := StepPaused
			stepEvent := "step.paused"
			if target == JobCancelled {
				attemptStatus = StepCancelled
				stepStatus = StepCancelled
				stepEvent = "step.cancelled"
			}
			if _, err := tx.Exec(ctx, `
				UPDATE step_attempts SET status = $2, finished_at = now() WHERE id = $1`,
				interruptedAttemptID, attemptStatus); err != nil {
				return Job{}, fmt.Errorf("close interrupted step attempt: %w", err)
			}
			if _, err := tx.Exec(ctx, `
				UPDATE job_steps SET status = $2, finished_at = now(), updated_at = now() WHERE id = $1`,
				interruptedStepID, stepStatus); err != nil {
				return Job{}, fmt.Errorf("stop interrupted job step: %w", err)
			}
			if _, err := appendEvent(ctx, tx, jobID, interruptedStepID, interruptedAttemptID, stepEvent, actor, map[string]any{
				"step_key": currentStepKey, "from": StepRunning, "to": stepStatus,
			}); err != nil {
				return Job{}, err
			}
		}
	}
	finished := target == JobCancelled
	if resumeState != nil {
		_, err = tx.Exec(ctx, `
			UPDATE jobs
			SET status = $2, resume_state = resume_state || $3::jsonb,
			    blockers = '[]', next_actions = '[]', lease_owner = NULL,
			    lease_expires_at = NULL, updated_at = now()
			WHERE id = $1`, jobID, target, resumeState)
	} else {
		_, err = tx.Exec(ctx, `
			UPDATE jobs
			SET status = $2, lease_owner = NULL, lease_expires_at = NULL,
			    heartbeat_at = NULL,
			    finished_at = CASE WHEN $3 THEN now() ELSE finished_at END, updated_at = now()
			WHERE id = $1`, jobID, target, finished)
	}
	if err != nil {
		return Job{}, fmt.Errorf("update job transition: %w", err)
	}
	if target == JobQueued {
		if _, err := tx.Exec(ctx, `
			UPDATE job_steps SET status = 'ready', blockers = '[]', next_actions = '[]',
			       finished_at = NULL, updated_at = now()
			WHERE job_id = $1 AND step_key = (SELECT current_step_key FROM jobs WHERE id = $1)
			  AND status IN ('paused', 'blocked', 'failed')`, jobID); err != nil {
			return Job{}, fmt.Errorf("make current step resumable: %w", err)
		}
	}
	if target == JobCancelled {
		if _, err := tx.Exec(ctx, `
			UPDATE job_steps SET status = 'cancelled', finished_at = now(), updated_at = now()
			WHERE job_id = $1 AND status NOT IN ('complete', 'skipped', 'cancelled')`, jobID); err != nil {
			return Job{}, fmt.Errorf("cancel job steps: %w", err)
		}
	}
	if _, err := appendEvent(ctx, tx, jobID, "", "", eventType, actor, map[string]any{"from": current, "to": target}); err != nil {
		return Job{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Job{}, fmt.Errorf("commit job transition: %w", err)
	}
	return s.GetJob(ctx, jobID)
}

func buildStepInputSnapshot(
	ctx context.Context,
	tx pgx.Tx,
	jobID string,
	position int,
	jobInput, resumeState, configSnapshot json.RawMessage,
) (json.RawMessage, error) {
	rows, err := tx.Query(ctx, `
		SELECT step_key, output_summary FROM job_steps
		WHERE job_id = $1 AND position < $2 AND status IN ('complete', 'skipped')
		ORDER BY position`, jobID, position)
	if err != nil {
		return nil, fmt.Errorf("load prior step outputs: %w", err)
	}
	defer rows.Close()
	previousSteps := make(map[string]json.RawMessage)
	for rows.Next() {
		var key string
		var output json.RawMessage
		if err := rows.Scan(&key, &output); err != nil {
			return nil, fmt.Errorf("scan prior step output: %w", err)
		}
		previousSteps[key] = output
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate prior step outputs: %w", err)
	}
	snapshot, err := json.Marshal(map[string]any{
		"job_input": jobInput, "resume_state": resumeState,
		"config_snapshot": configSnapshot, "previous_steps": previousSteps,
	})
	if err != nil {
		return nil, fmt.Errorf("serialize step input snapshot: %w", err)
	}
	canonical, err := canonicalJSON(snapshot)
	if err != nil {
		return nil, fmt.Errorf("canonicalize step input snapshot: %w", err)
	}
	return canonical, nil
}

const jobSelect = `
	SELECT id::text, kind, status, execution_mode, COALESCE(current_step_key, ''),
	       input, blockers, next_actions, resume_state, summary,
	       created_at, updated_at, started_at, finished_at
	FROM jobs`

const stepSelect = `
	SELECT id::text, job_id::text, step_key, position, status, required,
	       COALESCE(gate_kind, ''), input_snapshot, output_summary, blockers,
	       next_actions, resume_state, started_at, finished_at
	FROM job_steps`

func scanJob(row pgx.Row) (Job, error) {
	var job Job
	var startedAt, finishedAt pgtype.Timestamptz
	err := row.Scan(
		&job.ID, &job.Kind, &job.Status, &job.ExecutionMode, &job.CurrentStep,
		&job.Input, &job.Blockers, &job.NextActions, &job.ResumeState, &job.Summary,
		&job.CreatedAt, &job.UpdatedAt, &startedAt, &finishedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Job{}, ErrNotFound
	}
	if err != nil {
		return Job{}, fmt.Errorf("scan job: %w", err)
	}
	job.StartedAt = timePointer(startedAt)
	job.FinishedAt = timePointer(finishedAt)
	return job, nil
}

func scanStep(row pgx.Row) (Step, error) {
	var step Step
	var startedAt, finishedAt pgtype.Timestamptz
	err := row.Scan(
		&step.ID, &step.JobID, &step.Key, &step.Position, &step.Status, &step.Required,
		&step.GateKind, &step.InputSnapshot, &step.OutputSummary, &step.Blockers,
		&step.NextActions, &step.ResumeState, &startedAt, &finishedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Step{}, ErrNotFound
	}
	if err != nil {
		return Step{}, fmt.Errorf("scan job step: %w", err)
	}
	step.StartedAt = timePointer(startedAt)
	step.FinishedAt = timePointer(finishedAt)
	return step, nil
}

func appendEvent(
	ctx context.Context,
	tx pgx.Tx,
	jobID, stepID, attemptID, eventType string,
	actor Actor,
	payload any,
) (Event, error) {
	payloadJSON, err := json.Marshal(payload)
	if err != nil {
		return Event{}, fmt.Errorf("marshal job event payload: %w", err)
	}
	var sequence int64
	var previousHash sql.NullString
	err = tx.QueryRow(ctx, `
		SELECT sequence, event_hash FROM job_events
		WHERE job_id = $1 ORDER BY sequence DESC LIMIT 1`, jobID).Scan(&sequence, &previousHash)
	if errors.Is(err, pgx.ErrNoRows) {
		sequence = 0
		previousHash = sql.NullString{}
	} else if err != nil {
		return Event{}, fmt.Errorf("read previous job event: %w", err)
	}
	sequence++
	createdAt := time.Now().UTC().Truncate(time.Microsecond)
	eventHash := calculateEventHash(previousHash.String, jobID, sequence, eventType, actor.Type, actor.ID, createdAt, payloadJSON)
	var event Event
	err = tx.QueryRow(ctx, `
		INSERT INTO job_events(
			job_id, job_step_id, attempt_id, sequence, event_type,
			actor_type, actor_id, payload, previous_hash, event_hash, created_at
		)
		VALUES ($1, NULLIF($2, '')::uuid, NULLIF($3, '')::uuid, $4, $5, $6, NULLIF($7, ''), $8, NULLIF($9, ''), $10, $11)
		RETURNING id::text`,
		jobID, stepID, attemptID, sequence, eventType, actor.Type, actor.ID,
		payloadJSON, previousHash.String, eventHash, createdAt,
	).Scan(&event.ID)
	if err != nil {
		return Event{}, fmt.Errorf("append job event: %w", err)
	}
	event.JobID = jobID
	event.StepID = stepID
	event.AttemptID = attemptID
	event.Sequence = sequence
	event.Type = eventType
	event.ActorType = actor.Type
	event.ActorID = actor.ID
	event.Payload = payloadJSON
	event.PreviousHash = previousHash.String
	event.Hash = eventHash
	event.CreatedAt = createdAt
	return event, nil
}

func containsStatus(allowed []JobStatus, value JobStatus) bool {
	for _, status := range allowed {
		if status == value {
			return true
		}
	}
	return false
}

func timePointer(value pgtype.Timestamptz) *time.Time {
	if !value.Valid {
		return nil
	}
	t := value.Time
	return &t
}

func intervalLiteral(duration time.Duration) string {
	if duration <= 0 {
		duration = 30 * time.Second
	}
	return fmt.Sprintf("%f seconds", duration.Seconds())
}

func VerifyEventChain(events []Event) error {
	previousHash := ""
	var previousSequence int64
	for index, event := range events {
		if index > 0 && event.Sequence != previousSequence+1 {
			return fmt.Errorf("event sequence gap: previous=%d current=%d", previousSequence, event.Sequence)
		}
		if event.PreviousHash != previousHash {
			return fmt.Errorf("event %d previous hash mismatch", event.Sequence)
		}
		canonicalPayload, err := canonicalJSON(event.Payload)
		if err != nil {
			return fmt.Errorf("canonicalize event %d payload: %w", event.Sequence, err)
		}
		expected := calculateEventHash(
			event.PreviousHash, event.JobID, event.Sequence, event.Type,
			event.ActorType, event.ActorID, event.CreatedAt.UTC(), canonicalPayload,
		)
		if event.Hash != expected {
			return fmt.Errorf("event %d hash mismatch", event.Sequence)
		}
		previousSequence = event.Sequence
		previousHash = event.Hash
	}
	return nil
}

func calculateEventHash(
	previousHash, jobID string,
	sequence int64,
	eventType, actorType, actorID string,
	createdAt time.Time,
	payload []byte,
) string {
	hashInput := strings.Join([]string{
		previousHash, jobID, fmt.Sprintf("%d", sequence), eventType,
		actorType, actorID, createdAt.Format(time.RFC3339Nano), string(payload),
	}, "\x00")
	return sha256Hex([]byte(hashInput))
}

func canonicalJSON(value []byte) ([]byte, error) {
	var decoded any
	if err := json.Unmarshal(value, &decoded); err != nil {
		return nil, err
	}
	return json.Marshal(decoded)
}

func sha256Hex(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}
