package schedules

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Store struct{ pool *pgxpool.Pool }

func NewStore(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

func (s *Store) Create(ctx context.Context, input CreateInput, now time.Time) (Schedule, error) {
	input, next, err := normalizeCreate(input, now)
	if err != nil {
		return Schedule{}, err
	}
	config, _ := json.Marshal(input.Config)
	var nextRun any = next
	if !input.Enabled {
		nextRun = nil
	}
	var createdBy any
	if input.CreatedBy != "" {
		if _, err := uuid.Parse(input.CreatedBy); err != nil {
			return Schedule{}, fmt.Errorf("%w: created_by must be a UUID", ErrInvalid)
		}
		createdBy = input.CreatedBy
	}
	var id string
	err = s.pool.QueryRow(ctx, `
		INSERT INTO schedules(name, kind, cron_expression, timezone, enabled, config, next_run_at, created_by)
		VALUES ($1, 'daily_candidates', $2, $3, $4, $5, $6, $7)
		RETURNING id::text`, input.Name, input.CronExpression, input.Timezone, input.Enabled, config, nextRun, createdBy).Scan(&id)
	if err != nil {
		var postgresError *pgconn.PgError
		if errors.As(err, &postgresError) && postgresError.Code == "23505" {
			return Schedule{}, ErrConflict
		}
		return Schedule{}, fmt.Errorf("create daily candidate schedule: %w", err)
	}
	return s.Get(ctx, id)
}

func (s *Store) Get(ctx context.Context, id string) (Schedule, error) {
	return scanSchedule(s.pool.QueryRow(ctx, scheduleSelect+" WHERE id = $1 AND kind = 'daily_candidates'", id))
}

func (s *Store) List(ctx context.Context, limit int) ([]Schedule, error) {
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	rows, err := s.pool.Query(ctx, scheduleSelect+" WHERE kind = 'daily_candidates' ORDER BY name, id LIMIT $1", limit)
	if err != nil {
		return nil, fmt.Errorf("list daily candidate schedules: %w", err)
	}
	defer rows.Close()
	result := make([]Schedule, 0, limit)
	for rows.Next() {
		schedule, err := scanSchedule(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, schedule)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate daily candidate schedules: %w", err)
	}
	return result, nil
}

func (s *Store) Update(ctx context.Context, id string, input UpdateInput, now time.Time) (Schedule, error) {
	current, err := s.Get(ctx, id)
	if err != nil {
		return Schedule{}, err
	}
	cronExpression, timezone, enabled, config := current.CronExpression, current.Timezone, current.Enabled, current.Config
	if input.CronExpression != nil {
		cronExpression = *input.CronExpression
	}
	if input.Timezone != nil {
		timezone = *input.Timezone
	}
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	if input.Config != nil {
		config = *input.Config
	}
	normalized, next, err := normalizeCreate(CreateInput{
		Name: current.Name, CronExpression: cronExpression, Timezone: timezone, Enabled: enabled, Config: config,
	}, now)
	if err != nil {
		return Schedule{}, err
	}
	configBody, _ := json.Marshal(normalized.Config)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Schedule{}, fmt.Errorf("begin schedule update: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	result, err := tx.Exec(ctx, `
		UPDATE schedules SET cron_expression = $2, timezone = $3, enabled = $4, config = $5,
		       next_run_at = CASE WHEN $4 THEN $6::timestamptz ELSE NULL END, updated_at = now()
		WHERE id = $1 AND kind = 'daily_candidates'`, id, normalized.CronExpression, normalized.Timezone, normalized.Enabled, configBody, next)
	if err != nil {
		return Schedule{}, fmt.Errorf("update daily candidate schedule: %w", err)
	}
	if result.RowsAffected() != 1 {
		return Schedule{}, ErrNotFound
	}
	if !normalized.Enabled {
		if _, err := tx.Exec(ctx, `
			UPDATE schedule_runs SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
			WHERE schedule_id = $1 AND status IN ('queued', 'failed')`, id); err != nil {
			return Schedule{}, fmt.Errorf("cancel pending schedule runs: %w", err)
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return Schedule{}, fmt.Errorf("commit schedule update: %w", err)
	}
	return s.Get(ctx, id)
}

const scheduleSelect = `
	SELECT id::text, name, kind, cron_expression, timezone, enabled, config,
	       next_run_at, last_run_at, created_at, updated_at
	FROM schedules`

type rowScanner interface{ Scan(...any) error }

func scanSchedule(row rowScanner) (Schedule, error) {
	var schedule Schedule
	var config []byte
	err := row.Scan(
		&schedule.ID, &schedule.Name, &schedule.Kind, &schedule.CronExpression, &schedule.Timezone,
		&schedule.Enabled, &config, &schedule.NextRunAt, &schedule.LastRunAt, &schedule.CreatedAt, &schedule.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Schedule{}, ErrNotFound
	}
	if err != nil {
		return Schedule{}, fmt.Errorf("scan daily candidate schedule: %w", err)
	}
	if err := json.Unmarshal(config, &schedule.Config); err != nil {
		return Schedule{}, fmt.Errorf("decode daily candidate schedule config: %w", err)
	}
	return schedule, nil
}

var siteCodePattern = regexp.MustCompile(`^[A-Z0-9][A-Z0-9_-]{0,31}$`)

func normalizeCreate(input CreateInput, now time.Time) (CreateInput, time.Time, error) {
	input.Name = strings.TrimSpace(input.Name)
	input.CronExpression = strings.TrimSpace(input.CronExpression)
	input.Timezone = strings.TrimSpace(input.Timezone)
	input.Config.Source = strings.ToUpper(strings.TrimSpace(input.Config.Source))
	input.Config.Target = strings.ToUpper(strings.TrimSpace(input.Config.Target))
	if input.CronExpression == "" {
		input.CronExpression = "0 9 * * *"
	}
	if input.Timezone == "" {
		input.Timezone = "Asia/Shanghai"
	}
	if input.Config.TargetCount == 0 {
		input.Config.TargetCount = 10
	}
	if input.Config.ScanLimit == 0 {
		input.Config.ScanLimit = max(20, input.Config.TargetCount*3)
	}
	if input.Config.Page == 0 {
		input.Config.Page = 1
	}
	if input.Name == "" || len(input.Name) > 100 || strings.ContainsAny(input.Name, "\r\n\x00") {
		return input, time.Time{}, fmt.Errorf("%w: schedule name is required and must not exceed 100 characters", ErrInvalid)
	}
	if !siteCodePattern.MatchString(input.Config.Source) || !siteCodePattern.MatchString(input.Config.Target) || input.Config.Source == input.Config.Target {
		return input, time.Time{}, fmt.Errorf("%w: different valid source and target site codes are required", ErrInvalid)
	}
	if input.Config.TargetCount < 1 || input.Config.TargetCount > 25 || input.Config.ScanLimit < input.Config.TargetCount || input.Config.ScanLimit > 100 {
		return input, time.Time{}, fmt.Errorf("%w: target_count must be 1..25 and scan_limit must be target_count..100", ErrInvalid)
	}
	if input.Config.Page < 1 || input.Config.Page > 1000 {
		return input, time.Time{}, fmt.Errorf("%w: page must be between 1 and 1000", ErrInvalid)
	}
	next, err := NextDailyRun(input.CronExpression, input.Timezone, now)
	if err != nil {
		return input, time.Time{}, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	return input, next, nil
}
