package candidates

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Store struct {
	pool *pgxpool.Pool
}

func NewStore(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

func (s *Store) Upsert(ctx context.Context, input UpsertInput) (Item, error) {
	input.SourceSite = strings.ToUpper(strings.TrimSpace(input.SourceSite))
	input.TargetSite = strings.ToUpper(strings.TrimSpace(input.TargetSite))
	input.SourceTorrentID = strings.TrimSpace(input.SourceTorrentID)
	if input.SourceSite == "" || input.TargetSite == "" || input.SourceTorrentID == "" {
		return Item{}, errors.New("candidate source site, target site, and source torrent id are required")
	}
	if input.Status == "" {
		input.Status = StatusCandidate
	}
	if !validStatus(input.Status) {
		return Item{}, fmt.Errorf("invalid candidate status %q", input.Status)
	}
	if input.RecommendationDate.IsZero() {
		return Item{}, errors.New("candidate recommendation date is required")
	}
	if input.ExpiresAt.IsZero() || !input.ExpiresAt.After(time.Now().Add(-time.Minute)) {
		return Item{}, errors.New("candidate expiry must be in the future")
	}
	payload, err := canonicalJSON(input.Payload)
	if err != nil {
		return Item{}, fmt.Errorf("canonicalize candidate payload: %w", err)
	}

	row := s.pool.QueryRow(ctx, `
		WITH source_site AS (SELECT id FROM sites WHERE code = $1),
		     target_site AS (SELECT id FROM sites WHERE code = $2)
		INSERT INTO candidate_items(
			schedule_id, discovery_job_id, source_site_id, target_site_id, source_torrent_id,
			recommendation_date, rank, score, payload, status, expires_at
		)
		SELECT NULLIF($3, '')::uuid, NULLIF($4, '')::uuid, source_site.id, target_site.id, $5,
		       $6::date, $7, $8, $9, $10, $11
		FROM source_site CROSS JOIN target_site
		ON CONFLICT (recommendation_date, source_site_id, target_site_id, source_torrent_id)
		DO UPDATE SET schedule_id = COALESCE(EXCLUDED.schedule_id, candidate_items.schedule_id),
		              discovery_job_id = EXCLUDED.discovery_job_id,
		              rank = EXCLUDED.rank, score = EXCLUDED.score,
		              payload = EXCLUDED.payload,
		              status = CASE WHEN candidate_items.status = 'submitted' THEN candidate_items.status ELSE EXCLUDED.status END,
		              expires_at = EXCLUDED.expires_at, updated_at = now()
		RETURNING id::text`,
		input.SourceSite, input.TargetSite, input.ScheduleID, input.DiscoveryJobID, input.SourceTorrentID,
		input.RecommendationDate, input.Rank, input.Score, payload, input.Status, input.ExpiresAt,
	)
	var id string
	if err := row.Scan(&id); errors.Is(err, pgx.ErrNoRows) {
		return Item{}, fmt.Errorf("candidate source or target site is not configured")
	} else if err != nil {
		return Item{}, fmt.Errorf("upsert candidate item: %w", err)
	}
	return s.Get(ctx, id)
}

func (s *Store) Get(ctx context.Context, id string) (Item, error) {
	item, err := scanItem(s.pool.QueryRow(ctx, candidateSelect+" WHERE ci.id = $1", id))
	if errors.Is(err, pgx.ErrNoRows) {
		return Item{}, ErrNotFound
	}
	return item, err
}

func (s *Store) MarkSubmitted(ctx context.Context, id, jobID string) (Item, error) {
	result, err := s.pool.Exec(ctx, `
		UPDATE candidate_items
		SET status = 'submitted', submitted_job_id = $2, submitted_at = COALESCE(submitted_at, now()), updated_at = now()
		WHERE id = $1 AND expires_at > now()
		  AND (status = 'candidate' OR (status = 'submitted' AND submitted_job_id = $2))`, id, jobID)
	if err != nil {
		return Item{}, fmt.Errorf("mark candidate submitted: %w", err)
	}
	if result.RowsAffected() != 1 {
		return Item{}, ErrNotSubmittable
	}
	return s.Get(ctx, id)
}

func (s *Store) List(ctx context.Context, filter ListFilter) ([]Item, error) {
	if filter.Limit <= 0 || filter.Limit > 100 {
		filter.Limit = 10
	}
	query := candidateSelect + " WHERE true"
	arguments := make([]any, 0, 5)
	if source := strings.ToUpper(strings.TrimSpace(filter.SourceSite)); source != "" {
		arguments = append(arguments, source)
		query += fmt.Sprintf(" AND source.code = $%d", len(arguments))
	}
	if target := strings.ToUpper(strings.TrimSpace(filter.TargetSite)); target != "" {
		arguments = append(arguments, target)
		query += fmt.Sprintf(" AND target.code = $%d", len(arguments))
	}
	if filter.RecommendationDate != nil {
		arguments = append(arguments, *filter.RecommendationDate)
		query += fmt.Sprintf(" AND ci.recommendation_date = $%d::date", len(arguments))
	}
	if filter.Status != "" {
		if !validStatus(filter.Status) {
			return nil, fmt.Errorf("invalid candidate status %q", filter.Status)
		}
		switch filter.Status {
		case StatusCandidate:
			query += " AND ci.status = 'candidate' AND ci.expires_at > now()"
		case StatusExpired:
			query += " AND (ci.status = 'expired' OR (ci.status = 'candidate' AND ci.expires_at <= now()))"
		default:
			arguments = append(arguments, filter.Status)
			query += fmt.Sprintf(" AND ci.status = $%d", len(arguments))
		}
	}
	arguments = append(arguments, filter.Limit)
	query += fmt.Sprintf(" ORDER BY ci.recommendation_date DESC, ci.rank NULLS LAST, ci.score DESC, ci.id LIMIT $%d", len(arguments))
	rows, err := s.pool.Query(ctx, query, arguments...)
	if err != nil {
		return nil, fmt.Errorf("list candidate items: %w", err)
	}
	defer rows.Close()
	items := make([]Item, 0, filter.Limit)
	for rows.Next() {
		item, err := scanItem(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate candidate items: %w", err)
	}
	return items, nil
}

const candidateSelect = `
	SELECT ci.id::text, COALESCE(ci.schedule_id::text, ''), COALESCE(ci.discovery_job_id::text, ''), COALESCE(ci.submitted_job_id::text, ''), source.code, target.code,
	       ci.source_torrent_id, ci.recommendation_date, ci.rank, ci.score, ci.payload,
	       CASE WHEN ci.status = 'candidate' AND ci.expires_at <= now() THEN 'expired' ELSE ci.status END,
	       ci.discovered_at, ci.expires_at, ci.updated_at, ci.submitted_at
	FROM candidate_items ci
	JOIN sites source ON source.id = ci.source_site_id
	JOIN sites target ON target.id = ci.target_site_id`

type rowScanner interface{ Scan(...any) error }

func scanItem(row rowScanner) (Item, error) {
	var item Item
	if err := row.Scan(
		&item.ID, &item.ScheduleID, &item.DiscoveryJobID, &item.SubmittedJobID, &item.SourceSite, &item.TargetSite,
		&item.SourceTorrentID, &item.RecommendationDate, &item.Rank, &item.Score,
		&item.Payload, &item.Status, &item.DiscoveredAt, &item.ExpiresAt, &item.UpdatedAt, &item.SubmittedAt,
	); err != nil {
		return Item{}, err
	}
	return item, nil
}

func validStatus(status Status) bool {
	switch status {
	case StatusCandidate, StatusBlocked, StatusSubmitted, StatusExpired:
		return true
	default:
		return false
	}
}

func canonicalJSON(raw json.RawMessage) (json.RawMessage, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		raw = json.RawMessage(`{}`)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return nil, errors.New("candidate payload must contain one JSON value")
	}
	body, err := json.Marshal(value)
	return json.RawMessage(body), err
}
