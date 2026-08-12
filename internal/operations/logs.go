package operations

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

var ErrInvalid = errors.New("operations input is invalid")

type LogEntry struct {
	ID            int64           `json:"id"`
	OccurredAt    time.Time       `json:"occurred_at"`
	Level         string          `json:"level"`
	Component     string          `json:"component"`
	Message       string          `json:"message"`
	RequestID     string          `json:"request_id,omitempty"`
	TraceID       string          `json:"trace_id,omitempty"`
	JobID         string          `json:"job_id,omitempty"`
	StepKey       string          `json:"step_key,omitempty"`
	AttemptID     string          `json:"attempt_id,omitempty"`
	Method        string          `json:"method,omitempty"`
	Route         string          `json:"route,omitempty"`
	StatusCode    int             `json:"status_code,omitempty"`
	DurationMS    int64           `json:"duration_ms,omitempty"`
	ResponseBytes int64           `json:"response_bytes,omitempty"`
	ErrorCode     string          `json:"error_code,omitempty"`
	Action        string          `json:"action,omitempty"`
	ErrorDetail   string          `json:"error_detail,omitempty"`
	ActorType     string          `json:"actor_type,omitempty"`
	ActorID       string          `json:"actor_id,omitempty"`
	Attributes    json.RawMessage `json:"attributes,omitempty"`
}

type LogFilter struct {
	From, To                  *time.Time
	Levels                    []string
	Component, Keyword        string
	ErrorCode, RequestID      string
	TraceID, JobID, AttemptID string
	StatusCode                int
	BeforeOccurredAt          *time.Time
	BeforeID                  int64
	AfterID                   int64
	Limit                     int
}

type LogPage struct {
	Logs    []LogEntry
	HasMore bool
}

type LogAuditContext struct {
	ID           string          `json:"id"`
	ActorType    string          `json:"actor_type"`
	ActorID      string          `json:"actor_id,omitempty"`
	Action       string          `json:"action"`
	ResourceType string          `json:"resource_type"`
	ResourceID   string          `json:"resource_id,omitempty"`
	TraceID      string          `json:"trace_id,omitempty"`
	Payload      json.RawMessage `json:"payload"`
	CreatedAt    time.Time       `json:"created_at"`
}

type LogContext struct {
	Log            LogEntry          `json:"log"`
	CorrelatedLogs []LogEntry        `json:"correlated_logs"`
	AuditEvents    []LogAuditContext `json:"audit_events"`
}

type LogWriter interface {
	InsertLog(context.Context, LogEntry) (int64, error)
}

type Store struct{ pool *pgxpool.Pool }

func NewStore(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

func (s *Store) InsertLog(ctx context.Context, entry LogEntry) (int64, error) {
	if s == nil || s.pool == nil {
		return 0, errors.New("operational log store is unavailable")
	}
	entry = sanitizeLog(entry)
	if err := validateLog(entry); err != nil {
		return 0, err
	}
	var id int64
	err := s.pool.QueryRow(ctx, `
		INSERT INTO operational_logs(occurred_at, level, component, message, request_id, trace_id,
			job_id, step_key, attempt_id, method, route, status_code, duration_ms, response_bytes,
			error_code, action, error_detail, actor_type, actor_id, attributes)
		VALUES ($1,$2,$3,$4,NULLIF($5,''),NULLIF($6,'')::uuid,NULLIF($7,'')::uuid,NULLIF($8,''),
			NULLIF($9,'')::uuid,NULLIF($10,''),NULLIF($11,''),NULLIF($12,0),$13,$14,NULLIF($15,''),
			NULLIF($16,''),NULLIF($17,''),NULLIF($18,''),NULLIF($19,''),$20)
		RETURNING id`, entry.OccurredAt, entry.Level, entry.Component, entry.Message, entry.RequestID,
		entry.TraceID, entry.JobID, entry.StepKey, entry.AttemptID, entry.Method, entry.Route,
		entry.StatusCode, entry.DurationMS, entry.ResponseBytes, entry.ErrorCode, entry.Action,
		entry.ErrorDetail, entry.ActorType, entry.ActorID, entry.Attributes).Scan(&id)
	if err != nil {
		return 0, fmt.Errorf("insert operational log: %w", err)
	}
	return id, nil
}

func sanitizeLog(entry LogEntry) LogEntry {
	if entry.OccurredAt.IsZero() {
		entry.OccurredAt = time.Now().UTC()
	}
	entry.Level = strings.ToLower(strings.TrimSpace(entry.Level))
	entry.Component = strings.TrimSpace(entry.Component)
	entry.Message, _ = Redact(entry.Message).(string)
	var attributes any = map[string]any{}
	if len(entry.Attributes) > 0 {
		_ = json.Unmarshal(entry.Attributes, &attributes)
	}
	redacted := Redact(attributes)
	entry.Attributes, _ = json.Marshal(redacted)
	if values, ok := redacted.(map[string]any); ok {
		if entry.Action == "" {
			entry.Action = firstLogAttribute(values, "action", "operation")
		}
		if entry.ErrorDetail == "" {
			entry.ErrorDetail = firstLogAttribute(values, "error_detail")
		}
	}
	entry.Action = boundedLogText(entry.Action, 255)
	entry.ErrorDetail = boundedLogText(entry.ErrorDetail, 4000)
	return entry
}

func firstLogAttribute(values map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := values[key].(string); ok && strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func boundedLogText(value string, limit int) string {
	value = strings.TrimSpace(strings.ToValidUTF8(value, "�"))
	runes := []rune(value)
	if len(runes) > limit {
		value = string(runes[:limit])
	}
	redacted, _ := Redact(value).(string)
	return redacted
}

func validateLog(entry LogEntry) error {
	if entry.Level != "debug" && entry.Level != "info" && entry.Level != "warn" && entry.Level != "error" {
		return fmt.Errorf("%w: unsupported log level", ErrInvalid)
	}
	if entry.Component == "" || len(entry.Component) > 100 || entry.Message == "" || len(entry.Message) > 4000 {
		return fmt.Errorf("%w: component and message are required", ErrInvalid)
	}
	return nil
}

func (s *Store) ListLogs(ctx context.Context, filter LogFilter) (LogPage, error) {
	return s.listLogs(ctx, filter, true)
}

// ListLogSummaries deliberately omits the potentially large attributes JSON.
// The console and SSE stream use this path; details are fetched by log ID.
func (s *Store) ListLogSummaries(ctx context.Context, filter LogFilter) (LogPage, error) {
	return s.listLogs(ctx, filter, false)
}

func (s *Store) listLogs(ctx context.Context, filter LogFilter, includeAttributes bool) (LogPage, error) {
	if filter.Limit == 0 {
		filter.Limit = 100
	}
	if filter.Limit < 1 || filter.Limit > 10000 || filter.BeforeID < 0 || filter.AfterID < 0 || (filter.StatusCode != 0 && (filter.StatusCode < 100 || filter.StatusCode > 599)) {
		return LogPage{}, fmt.Errorf("%w: invalid log cursor or limit", ErrInvalid)
	}
	if filter.BeforeOccurredAt != nil && filter.BeforeID == 0 {
		return LogPage{}, fmt.Errorf("%w: log time cursor requires an id", ErrInvalid)
	}
	if filter.From != nil && filter.To != nil && filter.From.After(*filter.To) {
		return LogPage{}, fmt.Errorf("%w: log start time must not follow end time", ErrInvalid)
	}
	for _, level := range filter.Levels {
		if level != "debug" && level != "info" && level != "warn" && level != "error" {
			return LogPage{}, fmt.Errorf("%w: unsupported log level", ErrInvalid)
		}
	}
	query := `SELECT id, occurred_at, level, component, message, COALESCE(request_id,''), trace_id,
		job_id, COALESCE(step_key,''), attempt_id, COALESCE(method,''), COALESCE(route,''),
		COALESCE(status_code,0), COALESCE(duration_ms,0), COALESCE(response_bytes,0),
		COALESCE(error_code,''), COALESCE(action,''), COALESCE(error_detail,''),
		COALESCE(actor_type,''), COALESCE(actor_id,'')`
	if includeAttributes {
		query += `, attributes`
	}
	query += ` FROM operational_logs WHERE true`
	args := make([]any, 0, 20)
	add := func(clause string, value any) {
		args = append(args, value)
		query += fmt.Sprintf(clause, len(args))
	}
	if filter.From != nil {
		add(" AND occurred_at >= $%d", *filter.From)
	}
	if filter.To != nil {
		add(" AND occurred_at <= $%d", *filter.To)
	}
	if len(filter.Levels) > 0 {
		add(" AND level = ANY($%d)", filter.Levels)
	}
	if value := strings.TrimSpace(filter.Component); value != "" {
		add(" AND component = $%d", value)
	}
	if value := strings.TrimSpace(filter.Keyword); value != "" {
		add(" AND search_text ILIKE '%%' || lower($%[1]d) || '%%'", value)
	}
	if filter.StatusCode > 0 {
		add(" AND status_code = $%d", filter.StatusCode)
	}
	for _, item := range []struct {
		clause string
		value  string
	}{
		{" AND error_code = $%d", filter.ErrorCode}, {" AND request_id = $%d", filter.RequestID},
		{" AND trace_id = $%d::uuid", filter.TraceID}, {" AND job_id = $%d::uuid", filter.JobID},
		{" AND attempt_id = $%d::uuid", filter.AttemptID},
	} {
		if value := strings.TrimSpace(item.value); value != "" {
			add(item.clause, value)
		}
	}
	if filter.BeforeOccurredAt != nil {
		args = append(args, *filter.BeforeOccurredAt, filter.BeforeID)
		query += fmt.Sprintf(" AND (occurred_at, id) < ($%d, $%d)", len(args)-1, len(args))
	} else if filter.BeforeID > 0 {
		add(" AND id < $%d", filter.BeforeID)
	}
	if filter.AfterID > 0 {
		add(" AND id > $%d", filter.AfterID)
	}
	args = append(args, filter.Limit+1)
	if filter.AfterID > 0 {
		query += fmt.Sprintf(" ORDER BY id ASC LIMIT $%d", len(args))
	} else {
		query += fmt.Sprintf(" ORDER BY id DESC LIMIT $%d", len(args))
	}
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return LogPage{}, fmt.Errorf("list operational logs: %w", err)
	}
	defer rows.Close()
	logs := make([]LogEntry, 0, filter.Limit+1)
	for rows.Next() {
		entry, err := scanLogRow(rows, includeAttributes)
		if err != nil {
			return LogPage{}, err
		}
		logs = append(logs, entry)
	}
	if err := rows.Err(); err != nil {
		return LogPage{}, fmt.Errorf("iterate operational logs: %w", err)
	}
	hasMore := len(logs) > filter.Limit
	if hasMore {
		logs = logs[:filter.Limit]
	}
	return LogPage{Logs: logs, HasMore: hasMore}, nil
}

func (s *Store) GetLog(ctx context.Context, id int64) (LogEntry, error) {
	if id < 1 {
		return LogEntry{}, fmt.Errorf("%w: log_id must be positive", ErrInvalid)
	}
	entry, err := scanLog(s.pool.QueryRow(ctx, `SELECT id, occurred_at, level, component, message, COALESCE(request_id,''), trace_id,
		job_id, COALESCE(step_key,''), attempt_id, COALESCE(method,''), COALESCE(route,''),
		COALESCE(status_code,0), COALESCE(duration_ms,0), COALESCE(response_bytes,0),
		COALESCE(error_code,''), COALESCE(action,''), COALESCE(error_detail,''),
		COALESCE(actor_type,''), COALESCE(actor_id,''), attributes
		FROM operational_logs WHERE id=$1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return LogEntry{}, ErrNotFound
	}
	return entry, err
}

// GetLogContext returns the selected log plus bounded records sharing its
// trace. This keeps the list endpoint compact while preserving the concrete
// action, external call, and audit context needed for diagnosis.
func (s *Store) GetLogContext(ctx context.Context, id int64) (LogContext, error) {
	entry, err := s.GetLog(ctx, id)
	if err != nil {
		return LogContext{}, err
	}
	result := LogContext{Log: entry, CorrelatedLogs: []LogEntry{}, AuditEvents: []LogAuditContext{}}
	if entry.TraceID == "" {
		return result, nil
	}
	page, err := s.ListLogs(ctx, LogFilter{TraceID: entry.TraceID, Limit: 100})
	if err != nil {
		return LogContext{}, err
	}
	result.CorrelatedLogs = page.Logs
	rows, err := s.pool.Query(ctx, `SELECT id::text,actor_type,COALESCE(actor_id,''),action,resource_type,
		COALESCE(resource_id,''),COALESCE(trace_id::text,''),payload,created_at
		FROM audit_events WHERE trace_id=$1::uuid ORDER BY created_at,id LIMIT 100`, entry.TraceID)
	if err != nil {
		return LogContext{}, err
	}
	defer rows.Close()
	for rows.Next() {
		var item LogAuditContext
		if err = rows.Scan(&item.ID, &item.ActorType, &item.ActorID, &item.Action, &item.ResourceType,
			&item.ResourceID, &item.TraceID, &item.Payload, &item.CreatedAt); err != nil {
			return LogContext{}, err
		}
		item.Payload, _ = json.Marshal(Redact(item.Payload))
		result.AuditEvents = append(result.AuditEvents, item)
	}
	return result, rows.Err()
}

type rowScanner interface{ Scan(...any) error }

func scanLog(row rowScanner) (LogEntry, error) {
	return scanLogRow(row, true)
}

func scanLogRow(row rowScanner, includeAttributes bool) (LogEntry, error) {
	var entry LogEntry
	var traceID, jobID, attemptID pgtype.UUID
	targets := []any{&entry.ID, &entry.OccurredAt, &entry.Level, &entry.Component, &entry.Message,
		&entry.RequestID, &traceID, &jobID, &entry.StepKey, &attemptID, &entry.Method, &entry.Route,
		&entry.StatusCode, &entry.DurationMS, &entry.ResponseBytes, &entry.ErrorCode, &entry.Action,
		&entry.ErrorDetail, &entry.ActorType, &entry.ActorID}
	if includeAttributes {
		targets = append(targets, &entry.Attributes)
	}
	err := row.Scan(targets...)
	if err != nil {
		return LogEntry{}, fmt.Errorf("scan operational log: %w", err)
	}
	entry.TraceID = uuidText(traceID)
	entry.JobID = uuidText(jobID)
	entry.AttemptID = uuidText(attemptID)
	return entry, nil
}

func uuidText(value pgtype.UUID) string {
	if !value.Valid {
		return ""
	}
	return uuid.UUID(value.Bytes).String()
}

func (s *Store) PurgeExpired(ctx context.Context, retentionDays, diagnosticDays int) (int64, error) {
	command, err := s.pool.Exec(ctx, `DELETE FROM operational_logs WHERE occurred_at < now() - make_interval(days => $1)`, retentionDays)
	if err != nil {
		return 0, fmt.Errorf("purge operational logs: %w", err)
	}
	if _, err := s.pool.Exec(ctx, `DELETE FROM diagnostics WHERE created_at < now() - make_interval(days => $1)`, diagnosticDays); err != nil {
		return command.RowsAffected(), fmt.Errorf("purge diagnostics: %w", err)
	}
	return command.RowsAffected(), nil
}

// AsyncLogSink keeps database failures and slow writes off request/worker hot
// paths. Queue overflow is counted and emitted as an aggregate stdout record.
type AsyncLogSink struct {
	writer  LogWriter
	stdout  *slog.Logger
	queue   chan LogEntry
	dropped atomic.Uint64
	total   atomic.Uint64
	done    chan struct{}
	once    sync.Once
}

func NewAsyncLogSink(writer LogWriter, stdout *slog.Logger, capacity int) *AsyncLogSink {
	if capacity < 1 {
		capacity = 1024
	}
	return &AsyncLogSink{writer: writer, stdout: stdout, queue: make(chan LogEntry, capacity), done: make(chan struct{})}
}

func (s *AsyncLogSink) Enqueue(entry LogEntry) {
	entry = sanitizeLog(entry)
	select {
	case s.queue <- entry:
	default:
		s.dropped.Add(1)
		s.total.Add(1)
	}
}

func (s *AsyncLogSink) Dropped() uint64 { return s.total.Load() }

func (s *AsyncLogSink) recordDrop(count uint64) {
	if count == 0 {
		return
	}
	s.dropped.Add(count)
	s.total.Add(count)
}

func (s *AsyncLogSink) Run(ctx context.Context) {
	defer close(s.done)
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case entry := <-s.queue:
			writeCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			_, err := s.writer.InsertLog(writeCtx, entry)
			cancel()
			if err != nil {
				s.recordDrop(1)
			}
		case <-ticker.C:
			if count := s.dropped.Swap(0); count > 0 && s.stdout != nil {
				s.stdout.Warn("operational log sink dropped records", "component", "log_sink", "error_code", "log_sink.dropped", "dropped", count)
			}
		case <-ctx.Done():
			deadline := time.NewTimer(3 * time.Second)
			defer deadline.Stop()
			for {
				select {
				case entry := <-s.queue:
					writeCtx, cancel := context.WithTimeout(context.Background(), time.Second)
					_, _ = s.writer.InsertLog(writeCtx, entry)
					cancel()
				case <-deadline.C:
					s.recordDrop(uint64(len(s.queue)))
					return
				default:
					return
				}
			}
		}
	}
}

func (s *AsyncLogSink) Wait() { <-s.done }

var _ = pgx.ErrNoRows
