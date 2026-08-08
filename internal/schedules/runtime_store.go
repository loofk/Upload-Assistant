package schedules

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

func (s *Store) EnqueueDue(ctx context.Context, now time.Time, limit int) (int, error) {
	if limit <= 0 || limit > 100 {
		limit = 25
	}
	enqueued := 0
	for enqueued < limit {
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			return enqueued, fmt.Errorf("begin schedule enqueue transaction: %w", err)
		}
		var scheduleID, expression, timezone string
		var scheduledFor time.Time
		err = tx.QueryRow(ctx, `
			SELECT id::text, cron_expression, timezone, next_run_at
			FROM schedules
			WHERE kind = 'daily_candidates' AND enabled AND next_run_at <= $1
			ORDER BY next_run_at, id
			FOR UPDATE SKIP LOCKED LIMIT 1`, now).Scan(&scheduleID, &expression, &timezone, &scheduledFor)
		if errors.Is(err, pgx.ErrNoRows) {
			_ = tx.Rollback(ctx)
			break
		}
		if err != nil {
			_ = tx.Rollback(ctx)
			return enqueued, fmt.Errorf("select due schedule: %w", err)
		}
		next, err := NextDailyRun(expression, timezone, now)
		if err != nil {
			_ = tx.Rollback(ctx)
			return enqueued, fmt.Errorf("calculate next run for schedule %s: %w", scheduleID, err)
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO schedule_runs(schedule_id, scheduled_for, status, next_attempt_at)
			VALUES ($1, $2, 'queued', $3)
			ON CONFLICT (schedule_id, scheduled_for) DO NOTHING`, scheduleID, scheduledFor, now); err != nil {
			_ = tx.Rollback(ctx)
			return enqueued, fmt.Errorf("enqueue schedule run: %w", err)
		}
		if _, err := tx.Exec(ctx, `
			UPDATE schedules SET next_run_at = $2, last_run_at = $3, updated_at = now() WHERE id = $1`,
			scheduleID, next, now); err != nil {
			_ = tx.Rollback(ctx)
			return enqueued, fmt.Errorf("advance schedule: %w", err)
		}
		if err := tx.Commit(ctx); err != nil {
			return enqueued, fmt.Errorf("commit schedule enqueue: %w", err)
		}
		enqueued++
	}
	return enqueued, nil
}

func (s *Store) ClaimRun(ctx context.Context, owner string, now time.Time, lease time.Duration) (Run, error) {
	owner = strings.TrimSpace(owner)
	if owner == "" || lease <= 0 {
		return Run{}, fmt.Errorf("schedule runner owner and positive lease are required")
	}
	row := s.pool.QueryRow(ctx, `
		WITH picked AS (
			SELECT sr.id
			FROM schedule_runs sr
			JOIN schedules schedule ON schedule.id = sr.schedule_id
			WHERE schedule.enabled AND schedule.kind = 'daily_candidates'
			  AND ((sr.status IN ('queued', 'failed') AND sr.next_attempt_at <= $1)
			       OR (sr.status = 'running' AND sr.lease_expires_at <= $1))
			ORDER BY sr.scheduled_for, sr.id
			FOR UPDATE OF sr SKIP LOCKED LIMIT 1
		)
		UPDATE schedule_runs sr
		SET status = 'running', attempts = attempts + 1, lease_owner = $2,
		    lease_expires_at = $1 + make_interval(secs => $3), updated_at = now()
		FROM picked, schedules schedule
		WHERE sr.id = picked.id AND schedule.id = sr.schedule_id
		RETURNING sr.id::text, sr.schedule_id::text, schedule.name, sr.scheduled_for, sr.status,
		          COALESCE(sr.job_id::text, ''), sr.attempts, sr.next_attempt_at, sr.lease_expires_at,
		          COALESCE(sr.last_error, ''), schedule.cron_expression,
		          schedule.timezone, schedule.config, sr.created_at, sr.updated_at`,
		now, owner, lease.Seconds())
	run, err := scanRun(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return Run{}, ErrNoRun
	}
	return run, err
}

func (s *Store) CompleteRun(ctx context.Context, runID, owner, jobID string) error {
	result, err := s.pool.Exec(ctx, `
		UPDATE schedule_runs SET status = 'created', job_id = $3, lease_owner = NULL,
		       lease_expires_at = NULL, last_error = NULL, updated_at = now()
		WHERE id = $1 AND status = 'running' AND lease_owner = $2`, runID, owner, jobID)
	if err != nil {
		return fmt.Errorf("complete schedule run: %w", err)
	}
	if result.RowsAffected() != 1 {
		return ErrConflict
	}
	return nil
}

func (s *Store) FailRun(ctx context.Context, runID, owner string, failure error, now time.Time, retryAfter time.Duration) error {
	if retryAfter < time.Minute {
		retryAfter = time.Minute
	}
	if retryAfter > 30*time.Minute {
		retryAfter = 30 * time.Minute
	}
	message := "scheduled daily candidate job creation failed"
	_ = failure // Raw external/database errors are intentionally not persisted.
	result, err := s.pool.Exec(ctx, `
		UPDATE schedule_runs SET status = 'failed', next_attempt_at = $3, lease_owner = NULL,
		       lease_expires_at = NULL, last_error = $4, updated_at = now()
		WHERE id = $1 AND status = 'running' AND lease_owner = $2`, runID, owner, now.Add(retryAfter), message)
	if err != nil {
		return fmt.Errorf("fail schedule run: %w", err)
	}
	if result.RowsAffected() != 1 {
		return ErrConflict
	}
	return nil
}

func scanRun(row rowScanner) (Run, error) {
	var run Run
	var config []byte
	if err := row.Scan(
		&run.ID, &run.ScheduleID, &run.ScheduleName, &run.ScheduledFor, &run.Status,
		&run.JobID, &run.Attempts, &run.NextAttemptAt, &run.LeaseExpiresAt, &run.LastError,
		&run.CronExpression, &run.Timezone, &config, &run.CreatedAt, &run.UpdatedAt,
	); err != nil {
		return Run{}, err
	}
	if err := json.Unmarshal(config, &run.Config); err != nil {
		return Run{}, fmt.Errorf("decode schedule run config: %w", err)
	}
	return run, nil
}

func (s *Store) ListRuns(ctx context.Context, scheduleID string, limit int) ([]Run, error) {
	if _, err := s.Get(ctx, scheduleID); err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 100 {
		limit = 25
	}
	rows, err := s.pool.Query(ctx, `
		SELECT sr.id::text, sr.schedule_id::text, schedule.name, sr.scheduled_for, sr.status,
		       COALESCE(sr.job_id::text, ''), sr.attempts, sr.next_attempt_at, sr.lease_expires_at,
		       COALESCE(sr.last_error, ''), schedule.cron_expression, schedule.timezone, schedule.config,
		       sr.created_at, sr.updated_at
		FROM schedule_runs sr JOIN schedules schedule ON schedule.id = sr.schedule_id
		WHERE sr.schedule_id = $1 ORDER BY sr.scheduled_for DESC, sr.id DESC LIMIT $2`, scheduleID, limit)
	if err != nil {
		return nil, fmt.Errorf("list schedule runs: %w", err)
	}
	defer rows.Close()
	result := make([]Run, 0, limit)
	for rows.Next() {
		run, err := scanRun(rows)
		if err != nil {
			return nil, fmt.Errorf("scan schedule run: %w", err)
		}
		result = append(result, run)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate schedule runs: %w", err)
	}
	return result, nil
}

func (s *Store) PublishTerminalNotifications(ctx context.Context, limit int) (int, error) {
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	rows, err := s.pool.Query(ctx, `
		SELECT sr.id::text, sr.schedule_id::text, schedule.name, sr.job_id::text,
		       job.status, job.blockers, job.next_actions, job.summary
		FROM schedule_runs sr
		JOIN schedules schedule ON schedule.id = sr.schedule_id
		JOIN jobs job ON job.id = sr.job_id
		LEFT JOIN notifications notification ON notification.schedule_run_id = sr.id
		WHERE sr.status = 'created' AND job.status IN ('complete', 'blocked', 'failed')
		  AND (notification.id IS NULL OR job.updated_at > notification.sent_at)
		ORDER BY sr.updated_at, sr.id LIMIT $1`, limit)
	if err != nil {
		return 0, fmt.Errorf("list terminal schedule notifications: %w", err)
	}
	type terminal struct {
		runID, scheduleID, scheduleName, jobID, status string
		blockers, nextActions, summary                 json.RawMessage
	}
	terminals := make([]terminal, 0, limit)
	for rows.Next() {
		var value terminal
		if err := rows.Scan(&value.runID, &value.scheduleID, &value.scheduleName, &value.jobID, &value.status, &value.blockers, &value.nextActions, &value.summary); err != nil {
			rows.Close()
			return 0, fmt.Errorf("scan terminal schedule notification: %w", err)
		}
		terminals = append(terminals, value)
	}
	rows.Close()
	published := 0
	for _, value := range terminals {
		payload, _ := json.Marshal(map[string]any{
			"schema_version": 1, "kind": "upload-assistant.daily-candidate-notification.v1",
			"schedule_id": value.scheduleID, "schedule_name": value.scheduleName,
			"schedule_run_id": value.runID, "job_id": value.jobID, "job_status": value.status,
			"blockers": value.blockers, "next_actions": value.nextActions, "summary": value.summary,
			"candidate_list": map[string]any{"method": "GET", "path": "/api/v2/candidates/daily"},
			"job_summary":    map[string]any{"method": "GET", "path": "/api/v2/jobs/" + value.jobID + "/summary"},
			"safety":         map[string]any{"submits_candidates": false, "uploads_torrents": false, "requires_user_approval": true},
		})
		result, err := s.pool.Exec(ctx, `
			INSERT INTO notifications(schedule_run_id, job_id, channel, status, payload, attempts, scheduled_at, sent_at)
			VALUES ($1, $2, 'in_app', 'sent', $3, 1, now(), now())
			ON CONFLICT (schedule_run_id) WHERE schedule_run_id IS NOT NULL
			DO UPDATE SET job_id = EXCLUDED.job_id, status = 'sent', payload = EXCLUDED.payload,
			              attempts = notifications.attempts + 1, last_error = NULL, sent_at = now()`, value.runID, value.jobID, payload)
		if err != nil {
			return published, fmt.Errorf("publish in-app schedule notification: %w", err)
		}
		published += int(result.RowsAffected())
	}
	return published, nil
}

func (s *Store) ListNotifications(ctx context.Context, limit int) ([]Notification, error) {
	if limit <= 0 || limit > 100 {
		limit = 25
	}
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, COALESCE(schedule_run_id::text, ''), COALESCE(job_id::text, ''),
		       channel, status, payload, attempts, COALESCE(last_error, ''), scheduled_at, sent_at, created_at
		FROM notifications WHERE channel = 'in_app'
		ORDER BY created_at DESC, id DESC LIMIT $1`, limit)
	if err != nil {
		return nil, fmt.Errorf("list in-app notifications: %w", err)
	}
	defer rows.Close()
	result := make([]Notification, 0, limit)
	for rows.Next() {
		var notification Notification
		if err := rows.Scan(
			&notification.ID, &notification.ScheduleRunID, &notification.JobID, &notification.Channel,
			&notification.Status, &notification.Payload, &notification.Attempts, &notification.LastError,
			&notification.ScheduledAt, &notification.SentAt, &notification.CreatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan in-app notification: %w", err)
		}
		result = append(result, notification)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate in-app notifications: %w", err)
	}
	return result, nil
}
