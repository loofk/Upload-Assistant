package auditlog

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

var ErrInvalid = errors.New("audit log filter is invalid")

type Event struct {
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

type Filter struct {
	ActorType       string
	Action          string
	ResourceType    string
	ResourceID      string
	BeforeCreatedAt *time.Time
	BeforeID        string
	Limit           int
}

type Page struct {
	Events  []Event
	HasMore bool
}

type Store struct{ pool *pgxpool.Pool }

func NewStore(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

func (s *Store) List(ctx context.Context, filter Filter) (Page, error) {
	if s == nil || s.pool == nil {
		return Page{}, errors.New("audit log store is unavailable")
	}
	filter.ActorType = strings.TrimSpace(filter.ActorType)
	filter.Action = strings.TrimSpace(filter.Action)
	filter.ResourceType = strings.TrimSpace(filter.ResourceType)
	filter.ResourceID = strings.TrimSpace(filter.ResourceID)
	if err := validateFilter(filter); err != nil {
		return Page{}, err
	}
	if filter.Limit == 0 {
		filter.Limit = 50
	}
	query := `
		SELECT id::text, actor_type, COALESCE(actor_id, ''), action, resource_type,
		       COALESCE(resource_id, ''), trace_id, payload, created_at
		FROM audit_events WHERE true`
	arguments := make([]any, 0, 8)
	add := func(clause string, value any) {
		arguments = append(arguments, value)
		query += fmt.Sprintf(clause, len(arguments))
	}
	if filter.ActorType != "" {
		add(" AND actor_type = $%d", filter.ActorType)
	}
	if filter.Action != "" {
		add(" AND action = $%d", filter.Action)
	}
	if filter.ResourceType != "" {
		add(" AND resource_type = $%d", filter.ResourceType)
	}
	if filter.ResourceID != "" {
		add(" AND resource_id = $%d", filter.ResourceID)
	}
	if filter.BeforeCreatedAt != nil {
		arguments = append(arguments, *filter.BeforeCreatedAt, filter.BeforeID)
		query += fmt.Sprintf(" AND (created_at, id) < ($%d, $%d::uuid)", len(arguments)-1, len(arguments))
	}
	arguments = append(arguments, filter.Limit+1)
	query += fmt.Sprintf(" ORDER BY created_at DESC, id DESC LIMIT $%d", len(arguments))
	rows, err := s.pool.Query(ctx, query, arguments...)
	if err != nil {
		return Page{}, fmt.Errorf("list audit events: %w", err)
	}
	defer rows.Close()
	events := make([]Event, 0, filter.Limit+1)
	for rows.Next() {
		var event Event
		var traceID pgtype.UUID
		if err := rows.Scan(
			&event.ID, &event.ActorType, &event.ActorID, &event.Action, &event.ResourceType,
			&event.ResourceID, &traceID, &event.Payload, &event.CreatedAt,
		); err != nil {
			return Page{}, fmt.Errorf("scan audit event: %w", err)
		}
		if traceID.Valid {
			event.TraceID = formatUUID(traceID.Bytes)
		}
		events = append(events, event)
	}
	if err := rows.Err(); err != nil {
		return Page{}, fmt.Errorf("iterate audit events: %w", err)
	}
	hasMore := len(events) > filter.Limit
	if hasMore {
		events = events[:filter.Limit]
	}
	return Page{Events: events, HasMore: hasMore}, nil
}

func validateFilter(filter Filter) error {
	if filter.Limit < 0 || filter.Limit > 100 {
		return fmt.Errorf("%w: limit must be between 1 and 100", ErrInvalid)
	}
	for name, value := range map[string]string{
		"actor_type": filter.ActorType, "action": filter.Action, "resource_type": filter.ResourceType,
	} {
		if len(value) > 100 || !safeName(value) {
			return fmt.Errorf("%w: %s is invalid", ErrInvalid, name)
		}
	}
	if len(filter.ResourceID) > 200 || strings.ContainsAny(filter.ResourceID, "\r\n\x00") {
		return fmt.Errorf("%w: resource_id is invalid", ErrInvalid)
	}
	if (filter.BeforeCreatedAt == nil) != (filter.BeforeID == "") {
		return fmt.Errorf("%w: cursor requires created_at and id", ErrInvalid)
	}
	return nil
}

func safeName(value string) bool {
	for _, character := range value {
		if (character < 'a' || character > 'z') && (character < 'A' || character > 'Z') &&
			(character < '0' || character > '9') && character != '_' && character != '-' && character != '.' {
			return false
		}
	}
	return true
}

func formatUUID(value [16]byte) string {
	return uuid.UUID(value).String()
}
