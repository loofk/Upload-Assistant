package operations

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

var ErrNotFound = errors.New("operations resource not found")
var ErrConflict = errors.New("operations state conflict")

type Incident struct {
	ID              string          `json:"id"`
	Status          string          `json:"status"`
	Severity        string          `json:"severity"`
	Kind            string          `json:"kind"`
	Fingerprint     string          `json:"fingerprint"`
	Title           string          `json:"title"`
	Summary         string          `json:"summary"`
	OccurrenceCount int64           `json:"occurrence_count"`
	FirstOccurredAt time.Time       `json:"first_occurred_at"`
	LastOccurredAt  time.Time       `json:"last_occurred_at"`
	JobID           string          `json:"job_id,omitempty"`
	TraceID         string          `json:"trace_id,omitempty"`
	Evidence        json.RawMessage `json:"evidence"`
	AcknowledgedBy  string          `json:"acknowledged_by,omitempty"`
	AcknowledgedAt  *time.Time      `json:"acknowledged_at,omitempty"`
	ResolvedBy      string          `json:"resolved_by,omitempty"`
	ResolvedAt      *time.Time      `json:"resolved_at,omitempty"`
	CreatedAt       time.Time       `json:"created_at"`
	UpdatedAt       time.Time       `json:"updated_at"`
}

type IncidentFilter struct {
	Status, Severity, Kind, JobID string
	Before                        *time.Time
	BeforeID                      string
	Limit                         int
}

type IncidentPage struct {
	Incidents []Incident
	HasMore   bool
}

func (s *Store) ListIncidents(ctx context.Context, filter IncidentFilter) (IncidentPage, error) {
	if filter.Limit == 0 {
		filter.Limit = 50
	}
	if filter.Limit < 1 || filter.Limit > 200 {
		return IncidentPage{}, fmt.Errorf("%w: invalid incident limit", ErrInvalid)
	}
	query := incidentSelect + " WHERE true"
	args := make([]any, 0, 8)
	add := func(clause string, value any) { args = append(args, value); query += fmt.Sprintf(clause, len(args)) }
	for _, item := range []struct{ clause, value string }{
		{" AND status = $%d", filter.Status}, {" AND severity = $%d", filter.Severity},
		{" AND kind = $%d", filter.Kind}, {" AND job_id = $%d::uuid", filter.JobID},
	} {
		if value := strings.TrimSpace(item.value); value != "" {
			add(item.clause, value)
		}
	}
	if filter.Before != nil {
		args = append(args, *filter.Before, filter.BeforeID)
		query += fmt.Sprintf(" AND (last_occurred_at,id) < ($%d,$%d::uuid)", len(args)-1, len(args))
	}
	args = append(args, filter.Limit+1)
	query += fmt.Sprintf(" ORDER BY last_occurred_at DESC,id DESC LIMIT $%d", len(args))
	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return IncidentPage{}, fmt.Errorf("list incidents: %w", err)
	}
	defer rows.Close()
	items := make([]Incident, 0, filter.Limit+1)
	for rows.Next() {
		item, err := scanIncident(rows)
		if err != nil {
			return IncidentPage{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return IncidentPage{}, err
	}
	hasMore := len(items) > filter.Limit
	if hasMore {
		items = items[:filter.Limit]
	}
	return IncidentPage{Incidents: items, HasMore: hasMore}, nil
}

const incidentSelect = `SELECT id::text,status,severity,kind,fingerprint,title,summary,occurrence_count,
	first_occurred_at,last_occurred_at,job_id,trace_id,evidence,COALESCE(acknowledged_by::text,''),
	acknowledged_at,COALESCE(resolved_by::text,''),resolved_at,created_at,updated_at FROM incidents`

func scanIncident(row rowScanner) (Incident, error) {
	var item Incident
	var jobID, traceID pgtype.UUID
	err := row.Scan(&item.ID, &item.Status, &item.Severity, &item.Kind, &item.Fingerprint,
		&item.Title, &item.Summary, &item.OccurrenceCount, &item.FirstOccurredAt, &item.LastOccurredAt,
		&jobID, &traceID, &item.Evidence, &item.AcknowledgedBy, &item.AcknowledgedAt,
		&item.ResolvedBy, &item.ResolvedAt, &item.CreatedAt, &item.UpdatedAt)
	if err != nil {
		return Incident{}, fmt.Errorf("scan incident: %w", err)
	}
	item.JobID, item.TraceID = uuidText(jobID), uuidText(traceID)
	return item, nil
}

func (s *Store) GetIncident(ctx context.Context, id string) (Incident, error) {
	item, err := scanIncident(s.pool.QueryRow(ctx, incidentSelect+" WHERE id=$1", id))
	if errors.Is(err, pgx.ErrNoRows) {
		return Incident{}, ErrNotFound
	}
	return item, err
}

type IncidentInput struct {
	Severity, Kind, Fingerprint, Title, Summary, JobID, TraceID string
	Evidence                                                    map[string]any
}

func (s *Store) UpsertIncident(ctx context.Context, input IncidentInput) (Incident, error) {
	if input.Fingerprint == "" || input.Kind == "" || input.Title == "" {
		return Incident{}, fmt.Errorf("%w: incident fingerprint, kind, and title are required", ErrInvalid)
	}
	if input.Severity == "" {
		input.Severity = "warning"
	}
	evidence, _ := json.Marshal(Redact(input.Evidence))
	row := s.pool.QueryRow(ctx, `INSERT INTO incidents(severity,kind,fingerprint,title,summary,job_id,trace_id,evidence)
		VALUES($1,$2,$3,$4,$5,NULLIF($6,'')::uuid,NULLIF($7,'')::uuid,$8)
		ON CONFLICT(fingerprint) DO UPDATE SET status='open',severity=EXCLUDED.severity,title=EXCLUDED.title,
		summary=EXCLUDED.summary,occurrence_count=incidents.occurrence_count+1,last_occurred_at=now(),
		job_id=COALESCE(EXCLUDED.job_id,incidents.job_id),trace_id=COALESCE(EXCLUDED.trace_id,incidents.trace_id),
		evidence=EXCLUDED.evidence,updated_at=now(),resolved_by=NULL,resolved_at=NULL
		RETURNING id::text,status,severity,kind,fingerprint,title,summary,occurrence_count,first_occurred_at,
		last_occurred_at,job_id,trace_id,evidence,COALESCE(acknowledged_by::text,''),acknowledged_at,COALESCE(resolved_by::text,''),
		resolved_at,created_at,updated_at`, input.Severity, input.Kind, input.Fingerprint, input.Title,
		input.Summary, input.JobID, input.TraceID, evidence)
	return scanIncident(row)
}

func (s *Store) SetIncidentStatus(ctx context.Context, id, status string, principal security.Principal, traceID string) (Incident, error) {
	if status != "acknowledged" && status != "resolved" {
		return Incident{}, fmt.Errorf("%w: invalid incident transition", ErrInvalid)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Incident{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	clause := "status='acknowledged',acknowledged_by=$2,acknowledged_at=now(),updated_at=now()"
	if status == "resolved" {
		clause = "status='resolved',resolved_by=$2,resolved_at=now(),updated_at=now()"
	}
	row := tx.QueryRow(ctx, "WITH incidents AS (UPDATE incidents SET "+clause+" WHERE id=$1 AND status <> 'resolved' RETURNING *) "+incidentSelect, id, principal.UserID)
	item, scanErr := scanIncident(row)
	if errors.Is(scanErr, pgx.ErrNoRows) {
		if existing, getErr := s.GetIncident(ctx, id); getErr == nil && existing.Status == status {
			return existing, tx.Commit(ctx)
		}
		return Incident{}, ErrConflict
	}
	if scanErr != nil {
		return Incident{}, scanErr
	}
	payload, _ := json.Marshal(map[string]any{"status": status, "occurrence_count": item.OccurrenceCount})
	if _, err := tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload)
		VALUES('user',$1,$2,'incident',$3,NULLIF($4,'')::uuid,$5)`, principal.UserID, "incident."+status, id, traceID, payload); err != nil {
		return Incident{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Incident{}, err
	}
	return item, nil
}
